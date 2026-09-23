"""D9-C1 transport recovery gate: three independent real Agent tool round trips.

This is infrastructure qualification only.  It deliberately reuses the D8 A1
task and frozen tool description; it does not create a bundle or alter any
ProvenLattice product behavior.
"""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path


D8_ROOT = Path(__file__).resolve().parent
REPO_ROOT = D8_ROOT.parents[2]
SRC_ROOT = REPO_ROOT / "src"
for path in (str(REPO_ROOT), str(SRC_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

models = importlib.import_module("experiments.retrieval-v1.harness.models")
agent_adapter = importlib.import_module("experiments.retrieval-v1.harness.agent_adapter")

FIXTURE_SRC = D8_ROOT / "fixture-src"
TASKS_FILE = D8_ROOT / "d8_tasks.json"
# The frozen D8 A1 task can have a >240 s model time-to-first-token on the
# external gateway.  This is transport-gate observation time only; it does
# not change the Agent prompt, tools, model, fixture, or PL behavior.
TIMEOUT_S = 360.0
RUNS_REQUIRED = 3
NEUTRAL_SYSTEM_PROMPT = (
    "Read-only code-analysis session in a small Python repository. "
    "Tools: Read, Grep, Glob, and a repository query CLI: "
    "`python -m provenlattice explain-symbol <QUALIFIED_SYMBOL> --json` "
    "(run it from the repository root; a symbol is qualified like "
    "pkg.module.function; run `python -m provenlattice explain-symbol --help` "
    "to see all options). Never edit, create, or delete files. "
    "Investigate the task question and answer with concrete code evidence "
    "(file, function, code fragment). End your reply with a final line "
    "starting with 'ANSWER:' followed by your concise conclusion."
)
ALLOWED_TOOLS = (
    "Read,Grep,Glob,Bash(python -m provenlattice explain-symbol *),"
    "Bash(provenlattice explain-symbol *)"
)


def _index(repo: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "provenlattice", "index", str(repo), "--json"],
        cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**__import__("os").environ, "PYTHONPATH": str(SRC_ROOT)},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise RuntimeError(f"fixture index failed: {completed.stderr[-800:]}")


def _gateway_identity(result) -> dict:
    # Stream JSON exposes served model and CLI version, but deliberately never
    # records endpoint URLs or any environment values that could be credentials.
    return {
        "transport": "Claude Code CLI stream-json",
        "served_backend_model": result.backend_model,
        "requested_model": result.actual_model,
        "cli_version": result.actual_version,
        "endpoint": "not exposed by Claude stream-json",
    }


def _qualify(result) -> tuple[bool, str, dict | None]:
    pl_events = [
        event for event in result.events
        if event.operation == "explain-symbol"
        and "provenlattice" in str(event.query or "").casefold()
    ]
    if "connectionrefused" in (result.error or "").replace(" ", "").casefold():
        return False, "CONNECTION_REFUSED", None
    if result.exit_reason != "completed":
        return False, "AGENT_TRANSPORT_OR_PRE_TOOL_FAILURE", None
    if not pl_events:
        return False, "NO_REAL_PL_INVOCATION", None
    received = next((event for event in pl_events if (event.result_size or 0) > 0), None)
    if received is None:
        return False, "TOOL_RESULT_NOT_RETURNED_TO_AGENT", pl_events[0]
    if not (result.output or "").strip():
        return False, "AGENT_DID_NOT_CONTINUE_AFTER_TOOL_RESULT", received
    return True, "PASS", received


def _claude_launcher() -> tuple[str, str]:
    """Return a verified npm CMD launcher before opening an Agent session."""
    launcher = shutil.which("claude.cmd")
    if not launcher:
        raise RuntimeError("claude.cmd is not present on PATH")
    check = subprocess.run(
        [launcher, "--version"], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if check.returncode != 0:
        raise RuntimeError(f"claude launcher preflight failed: {check.stderr[-800:]}")
    return launcher, check.stdout.strip()


def main() -> int:
    tasks = json.loads(TASKS_FILE.read_text(encoding="utf-8"))["tasks"]
    task = next(item for item in tasks if item["task_id"] == "D8-A1")
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    batch_dir = D8_ROOT / "results" / f"D9-C1-TRANSPORT-{timestamp}"
    batch_dir.mkdir(parents=True, exist_ok=False)
    launcher, launcher_version = _claude_launcher()
    command = [
        launcher, "-p", "--verbose", "--output-format", "stream-json",
        "--append-system-prompt", NEUTRAL_SYSTEM_PROMPT,
        "--allowedTools", ALLOWED_TOOLS,
    ]
    rows = []
    for sequence in range(1, RUNS_REQUIRED + 1):
        run_id = f"D9-C1-TRANSPORT-{sequence}-{uuid.uuid4().hex[:8]}"
        run_dir = batch_dir / "runs" / f"run-{sequence}"
        repo = run_dir / "repo"
        run_dir.mkdir(parents=True)
        shutil.copytree(FIXTURE_SRC, repo)
        _index(repo)
        request = models.RunRequest(
            run_id=run_id, task_id=task["task_id"], repo_path=str(repo),
            repo_commit="d8-fixture-v1", arm="transport-gate",
            prompt=task["prompt"], allowed_tools=ALLOWED_TOOLS.split(","),
            environment={"PYTHONPATH": str(SRC_ROOT)}, timeout=TIMEOUT_S,
        )
        result = agent_adapter.CommandAgentAdapter(command).run(request)
        passed, gate_status, received_event = _qualify(result)
        events = [event.to_dict() for event in result.events]
        (run_dir / "events.ndjson").write_text(
            "\n".join(json.dumps(event, ensure_ascii=False, sort_keys=True) for event in events),
            encoding="utf-8",
        )
        (run_dir / "agent-output.txt").write_text(result.output or "", encoding="utf-8")
        row = {
            "sequence": sequence, "run_id": run_id, "task_id": task["task_id"],
            "gate_status": gate_status, "passed": passed,
            "actual_model": result.actual_model, "backend_model": result.backend_model,
            "gateway_identity": _gateway_identity(result),
            "exit_reason": result.exit_reason, "error": result.error,
            "tool_calls_total": len(result.events),
            "pl_tool_calls": sum(1 for event in result.events if event.operation == "explain-symbol"),
            "tool_result_receipt": (received_event.to_dict() if received_event else None),
            "agent_continued_after_result": bool((result.output or "").strip()) and received_event is not None,
        }
        (run_dir / "run.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        rows.append(row)
        print(json.dumps({key: row[key] for key in ("sequence", "run_id", "gate_status", "passed")}, ensure_ascii=False))
        if not passed:
            break
    summary = {
        "gate": "D9-C1 Agent Transport Recovery Gate",
        "launcher": launcher, "launcher_version": launcher_version,
        "runs_required": RUNS_REQUIRED, "runs_attempted": len(rows), "rows": rows,
        "final_status": (
            "READY_FOR_CONTEXT_BUNDLE_CONTRACT_FREEZE"
            if len(rows) == RUNS_REQUIRED and all(row["passed"] for row in rows)
            else "BLOCKED_BY_AGENT_TRANSPORT"
        ),
    }
    (batch_dir / "gate-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"batch_dir": str(batch_dir), "final_status": summary["final_status"]}, ensure_ascii=False))
    return 0 if summary["final_status"].startswith("READY") else 1


if __name__ == "__main__":
    raise SystemExit(main())
