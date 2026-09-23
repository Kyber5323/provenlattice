"""D9-C1-T3: deterministic real-Agent ProvenLattice round-trip qualification."""
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
SYMBOL = "pipeline.promote.promote"
TIMEOUT_S = 360.0
RUNS_REQUIRED = 3
ALLOWED_TOOLS = "Bash(python -m provenlattice explain-symbol *)"
SYSTEM_PROMPT = (
    "This is a deterministic read-only transport qualification. You must run exactly "
    f"one command: `python -m provenlattice explain-symbol {SYMBOL} --json`. "
    "Run it directly from the repository root with no cd, pipe, redirect, or wrapper. "
    "Do not run --help, tests, any other ProvenLattice command, or any other tool. "
    "After its result is returned, reply with exactly the symbol name, its file path, "
    "and one fact from the returned result. Never modify files."
)
PROMPT = (
    f"Use ProvenLattice explain-symbol exactly once on `{SYMBOL}`. After the result "
    "returns, report only the symbol name, file path, and one returned fact."
)


def _index(repo: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "provenlattice", "index", str(repo), "--json"],
        cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**__import__("os").environ, "PYTHONPATH": str(SRC_ROOT)},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError(f"fixture index failed: {result.stderr[-800:]}")


def _launcher() -> tuple[str, str]:
    path = shutil.which("claude.cmd")
    if not path:
        raise RuntimeError("claude.cmd is not present on PATH")
    result = subprocess.run([path, "--version"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"claude launcher preflight failed: {result.stderr[-800:]}")
    return path, result.stdout.strip()


def _qualify(result) -> tuple[bool, str, object | None]:
    pl_events = [event for event in result.events if event.operation == "explain-symbol"]
    if "connectionrefused" in (result.error or "").replace(" ", "").casefold():
        return False, "CONNECTION_REFUSED", None
    if result.exit_reason != "completed":
        return False, "AGENT_TRANSPORT_FAILURE", None
    if len(pl_events) != 1:
        return False, "PL_INVOCATION_COUNT_NOT_ONE", pl_events[0] if pl_events else None
    event = pl_events[0]
    command = str(event.query or "")
    if SYMBOL not in command or "--help" in command:
        return False, "WRONG_PL_REQUEST", event
    if (event.result_size or 0) <= 0:
        return False, "TOOL_RESULT_NOT_RETURNED", event
    if not (result.output or "").strip():
        return False, "AGENT_DID_NOT_CONTINUE_AFTER_RESULT", event
    return True, "PASS", event


def main() -> int:
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    batch_dir = D8_ROOT / "results" / f"D9-C1-T3-{timestamp}"
    batch_dir.mkdir(parents=True, exist_ok=False)
    launcher, launcher_version = _launcher()
    command = [launcher, "-p", "--verbose", "--output-format", "stream-json",
               "--append-system-prompt", SYSTEM_PROMPT, "--allowedTools", ALLOWED_TOOLS]
    rows = []
    for sequence in range(1, RUNS_REQUIRED + 1):
        run_id = f"D9-C1-T3-{sequence}-{uuid.uuid4().hex[:8]}"
        run_dir = batch_dir / "runs" / f"run-{sequence}"
        repo = run_dir / "repo"
        run_dir.mkdir(parents=True)
        shutil.copytree(FIXTURE_SRC, repo)
        _index(repo)
        request = models.RunRequest(run_id=run_id, task_id="D9-C1-T3", repo_path=str(repo),
            repo_commit="d8-fixture-v1", arm="transport-probe", prompt=PROMPT,
            allowed_tools=ALLOWED_TOOLS.split(","), environment={"PYTHONPATH": str(SRC_ROOT)},
            timeout=TIMEOUT_S)
        result = agent_adapter.CommandAgentAdapter(command).run(request)
        passed, status, event = _qualify(result)
        events = [item.to_dict() for item in result.events]
        (run_dir / "events.ndjson").write_text(
            "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in events),
            encoding="utf-8")
        (run_dir / "agent-output.txt").write_text(result.output or "", encoding="utf-8")
        row = {
            "sequence": sequence, "run_id": run_id, "symbol": SYMBOL, "passed": passed,
            "gate_status": status, "actual_model": result.actual_model,
            "backend_model": result.backend_model, "exit_reason": result.exit_reason,
            "transport_error": result.error, "pl_invocation_count": len([item for item in result.events if item.operation == "explain-symbol"]),
            "actual_command": event.query if event else None,
            "tool_request_issued": event is not None,
            "tool_result_chars": event.result_size if event else 0,
            "tool_result_returned": bool(event and (event.result_size or 0) > 0),
            "agent_continued_after_result": bool(event and (event.result_size or 0) > 0 and (result.output or "").strip()),
        }
        (run_dir / "run.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        rows.append(row)
        print(json.dumps({key: row[key] for key in ("sequence", "run_id", "gate_status", "passed")}, ensure_ascii=False))
        if not passed:
            break
    final_status = "TRANSPORT_QUALIFIED" if len(rows) == RUNS_REQUIRED and all(row["passed"] for row in rows) else "HOLD"
    (batch_dir / "probe-summary.json").write_text(json.dumps({
        "task": "D9-C1-T3 Deterministic PL Round-Trip Qualification",
        "launcher": launcher, "launcher_version": launcher_version,
        "runs_required": RUNS_REQUIRED, "runs_attempted": len(rows),
        "final_status": final_status, "rows": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"batch_dir": str(batch_dir), "final_status": final_status}, ensure_ascii=False))
    return 0 if final_status == "TRANSPORT_QUALIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
