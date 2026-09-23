"""PL-DA-D8 runner: real-agent utility observation, base-only vs deep-analysis arms.

Discipline (frozen):
- both arms run the identical fixture commit, prompt, system prompt,
  allowed-tool surface and CLI model; the only difference is whether the
  derived analysis layer has been materialized before the run;
- no prompt text mentions AST/CFG/analysis; the optional capability is
  discoverable only through the query tool's own --help;
- the runner never injects analysis hints and never routes by task.
"""
from __future__ import annotations

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
for _path in (str(REPO_ROOT), str(SRC_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import importlib  # noqa: E402

_harness_models = importlib.import_module("experiments.retrieval-v1.harness.models")
_harness_adapter = importlib.import_module("experiments.retrieval-v1.harness.agent_adapter")
agent_adapter = _harness_adapter
models = _harness_models

FIXTURE_SRC = D8_ROOT / "fixture-src"
TASKS_FILE = D8_ROOT / "d8_tasks.json"
TIMEOUT_S = 240.0

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
ALLOWED_TOOLS = ("Read,Grep,Glob,"
                 "Bash(python -m provenlattice explain-symbol *),"
                 "Bash(provenlattice explain-symbol *)")


def _cli(args: list[str], cwd: Path) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "provenlattice", *args, "--json"],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**__import__("os").environ, "PYTHONPATH": str(SRC_ROOT)},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = {"raw_stdout": completed.stdout[-2000:], "returncode": completed.returncode}
    if completed.returncode != 0:
        raise RuntimeError(f"provenlattice {args[0]} failed: {completed.stderr[-500:]}")
    return payload


def prepare_arms(batch_dir: Path) -> dict[str, Path]:
    repos = {}
    for arm in ("B", "D"):
        repo = batch_dir / "repos" / arm
        shutil.copytree(FIXTURE_SRC, repo)
        _cli(["index", str(repo)], cwd=repo)
        if arm == "D":
            analysis = _cli(["analyze", str(repo), "--scope", "pipeline",
                             "--features", "ast,cfg"], cwd=repo)
            assert analysis["status"] == "complete", analysis
        repos[arm] = repo
    return repos


def run_cell(task: dict, arm: str, repo: Path, batch_dir: Path) -> dict:
    run_id = f"D8-{task['task_id']}-{arm}-{uuid.uuid4().hex[:6]}"
    run_dir = batch_dir / "runs" / f"{task['task_id']}-{arm}"
    run_dir.mkdir(parents=True, exist_ok=True)
    request = models.RunRequest(
        run_id=run_id, task_id=task["task_id"], repo_path=str(repo),
        repo_commit="d8-fixture-v1", arm=arm, prompt=task["prompt"],
        allowed_tools=ALLOWED_TOOLS.split(","),
        environment={"PYTHONPATH": str(SRC_ROOT)}, timeout=TIMEOUT_S,
    )
    command = ["claude", "-p", "--verbose", "--output-format", "stream-json",
               "--append-system-prompt", NEUTRAL_SYSTEM_PROMPT,
               "--allowedTools", ALLOWED_TOOLS]
    adapter = agent_adapter.CommandAgentAdapter(command)
    result = adapter.run(request)
    (run_dir / "agent-output.txt").write_text(result.output or "", encoding="utf-8")
    events = [event.to_dict() for event in result.events]
    (run_dir / "events.ndjson").write_text(
        "\n".join(json.dumps(event, ensure_ascii=False, sort_keys=True)
                  for event in events), encoding="utf-8")
    analysis_requests = []
    for event in result.events:
        if event.operation != "explain-symbol":
            continue
        command_text = str(event.query or "")
        if "--analysis" not in command_text:
            continue
        feature = "cfg" if "--analysis cfg" in command_text else (
            "ast" if "--analysis ast" in command_text else "unknown")
        envelope = (event.raw or {}).get("envelope") or {}
        sections = envelope.get("analysis") or {}
        section = sections.get(feature) or {}
        analysis_requests.append({
            "command": command_text, "feature": feature,
            "status": section.get("status"),
            "returned_nodes": section.get("returned_nodes"),
            "total_nodes": section.get("total_nodes"),
        })
    record = {
        "run_id": run_id, "task_id": task["task_id"], "arm": arm,
        "exit_reason": result.exit_reason, "error": result.error,
        "actual_model": result.actual_model, "backend_model": result.backend_model,
        "tool_calls_total": len(result.events),
        "pl_tool_calls": sum(1 for e in result.events
                             if str(e.query or "").find("provenlattice") >= 0),
        "read_calls": sum(1 for e in result.events if e.operation == "read"),
        "search_calls": sum(1 for e in result.events if e.operation in {"grep", "glob"}),
        "analysis_requests": analysis_requests,
        "analysis_used": any(r.get("status") == "available"
                             for r in analysis_requests),
        "tokens": _sum_tokens(result.events),
        "events": events,
        "output": result.output or "",
    }
    (run_dir / "run.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def _sum_tokens(events) -> dict:
    totals: dict[str, int] = {}
    for event in events:
        for key, value in event.tokens.items():
            totals[key] = totals.get(key, 0) + int(value)
    return totals


def evaluate(task: dict, record: dict) -> dict:
    text = (record.get("output") or "").lower()
    facts = []
    for fact in task["oracle"]["facts"]:
        hit = any(term.lower() in text for term in fact["any_of"])
        facts.append({"id": fact["id"], "hit": hit})
    success = all(fact["hit"] for fact in facts) and record["exit_reason"] == "completed"
    incremental = any(marker.lower() in text
                      for marker in task["incremental_markers"])
    return {"facts": facts, "success": success, "incremental_fact_in_answer": incremental}


def attribute(task: dict, record: dict, evaluation: dict) -> dict:
    if record["exit_reason"] not in {"completed"}:
        return {"verdict": "INFRA_EXCLUDED", "detail": record["exit_reason"]}
    used = record["analysis_used"]
    statuses = [r.get("status") for r in record["analysis_requests"]]
    if not used:
        return {"verdict": "ADOPTION_GAP",
                "detail": "analysis available but never requested"}
    if evaluation["success"] and evaluation["incremental_fact_in_answer"]:
        return {"verdict": "UTILITY_OBSERVED",
                "detail": "analysis used + correct answer + incremental fact cited"}
    if evaluation["success"]:
        return {"verdict": "NO_INCREMENTAL_VALUE",
                "detail": "analysis used but answer rests on base-reachable facts"}
    if all(status == "available" for status in statuses):
        return {"verdict": "CAPABILITY_GAP",
                "detail": "analysis used and available, yet answer incorrect/incomplete"}
    return {"verdict": "NO_INCREMENTAL_VALUE",
            "detail": f"analysis used but not usable (statuses={statuses})"}


def main() -> int:
    spec = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    batch_dir = D8_ROOT / "results" / f"D8-{timestamp}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    repos = prepare_arms(batch_dir)
    rows = []
    for task in spec["tasks"]:
        row = {"task_id": task["task_id"]}
        for arm in ("B", "D"):
            record = run_cell(task, arm, repos[arm], batch_dir)
            evaluation = evaluate(task, record)
            record["evaluation"] = evaluation
            if arm == "B":
                row["B_success"] = evaluation["success"]
            else:
                row["D_success"] = evaluation["success"]
                row["AST/CFG used"] = record["analysis_used"]
                row["Incremental fact"] = evaluation["incremental_fact_in_answer"]
                row["verdict_detail"] = attribute(task, record, evaluation)
            row[f"{arm}_run_id"] = record["run_id"]
            row[f"{arm}_tokens"] = record["tokens"]
            time.sleep(2)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))
    counts: dict[str, int] = {}
    for row in rows:
        verdict = row["verdict_detail"]["verdict"]
        counts[verdict] = counts.get(verdict, 0) + 1
    summary = {
        "experiment": spec["experiment"], "batch_dir": str(batch_dir),
        "rows": rows, "verdict_counts": counts,
    }
    (batch_dir / "batch-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(counts, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
