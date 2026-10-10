"""PL-DA D8-RV1: original failure case revalidation (A2, B2) under current capability.

Frozen from D8 (experiments/analysis_layer/d8/d8_tasks.json):
- identical task prompts, fixture repo (d8-fixture-v1), oracle and evaluation;
- D8 historical results (A2/B2 = ADOPTION_GAP) are preserved untouched.

Current product form (single arm, natural use):
- repo prepared with `index` + `analyze --scope pipeline --features ast,cfg`
  (current shipped analysis layer);
- ProvenLattice exposed the normal way: native MCP tool `mcp__provenlattice__query`
  via the transparent bridge (provenlattice_mcp_server.py); no required-use
  instruction, no task routing, no analysis hints; discoverable only through
  the tool itself (--help);
- one run per case: D8-A2 x1, D8-B2 x1.

Verdicts (D8-RV1):
  CASE_SOLVED_NATURALLY | CASE_SOLVED_WITHOUT_PROVENLATTICE |
  ADOPTION_GAP_REMAINS  | CAPABILITY_GAP_SUSPECTED | INFRA_EXCLUDED
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

D8_ROOT = Path(r"D:\ChatGPT\codegraph\provenlattice\experiments\analysis_layer\d8")
REPO_ROOT = D8_ROOT.parents[2]
SRC_ROOT = REPO_ROOT / "src"
MCP_SERVER = REPO_ROOT / "experiments" / "context_bundle_v0" / "formal_harness" / "provenlattice_mcp_server.py"
MCP_CONTRACT = REPO_ROOT / "experiments" / "context_bundle_v0" / "formal_harness" / "d9-c8" / "d9-c8-a-native-tool-exposure-contract.json"
FIXTURE_SRC = D8_ROOT / "fixture-src"
TASKS_FILE = D8_ROOT / "d8_tasks.json"
TIMEOUT_S = 900
VISIBLE_TOOL = "mcp__provenlattice__query"

RV_TASKS = ["D8-A2", "D8-B2"]

NEUTRAL_SYSTEM_PROMPT = (
    "Read-only code-analysis session in a small Python repository. "
    "Never edit, create, or delete files. "
    "Investigate the task question and answer with concrete code evidence "
    "(file, function, code fragment). End your reply with a final line "
    "starting with 'ANSWER:' followed by your concise conclusion."
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def cli(args: list[str], cwd: Path) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "provenlattice", *args, "--json"],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONPATH": str(SRC_ROOT)},
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = {"raw_stdout": completed.stdout[-2000:], "returncode": completed.returncode}
    if completed.returncode != 0:
        raise RuntimeError(f"provenlattice {args[0]} failed: {completed.stderr[-500:]}")
    return payload


def prepare_repo(repo: Path) -> dict:
    shutil.copytree(FIXTURE_SRC, repo)
    idx = cli(["index", str(repo)], cwd=repo)
    ana = cli(["analyze", str(repo), "--scope", "pipeline", "--features", "ast,cfg"], cwd=repo)
    assert ana.get("status") == "complete", ana
    return {"repo_id": idx.get("repo_id"), "analyze_status": ana.get("status")}


def stream_events(stdout: str):
    events, final_result = [], None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        events.append(e)
        if e.get("type") == "result":
            final_result = e
    return events, final_result


def parse_query_traffic(events: list[dict]) -> dict:
    queries, pending = [], {}
    server_entry, in_enum = None, False
    tool_counts: dict[str, int] = {}
    for e in events:
        if e.get("type") == "system":
            servers = [s for s in (e.get("mcp_servers") or []) if s.get("name") == "provenlattice"]
            if servers:
                server_entry = servers[0]
                in_enum = VISIBLE_TOOL in (e.get("tools") or [])
        if e.get("type") == "assistant":
            for c in (e.get("message") or {}).get("content") or []:
                if c.get("type") == "tool_use":
                    name = c.get("name") or ""
                    tool_counts[name] = tool_counts.get(name, 0) + 1
                    if name == VISIBLE_TOOL:
                        entry = {"input": c.get("input"), "result": None, "id": c.get("id")}
                        queries.append(entry)
                        pending[str(c.get("id"))] = entry
        if e.get("type") == "user":
            for c in (e.get("message") or {}).get("content") or []:
                if c.get("type") == "tool_result" and str(c.get("tool_use_id")) in pending:
                    entry = pending.pop(str(c.get("tool_use_id")))
                    t = c.get("content")
                    entry["result"] = t if isinstance(t, str) else json.dumps(t, ensure_ascii=False)
    return {"server_entry": server_entry, "tool_enumerated": in_enum,
            "queries": queries, "tool_counts": tool_counts}


def classify_queries(traffic: dict) -> dict:
    help_calls, tc_calls, analysis_calls, other_calls = [], [], [], []
    for q in traffic["queries"]:
        cmd = str((q.get("input") or {}).get("command", "")).strip()
        args = [str(a) for a in ((q.get("input") or {}).get("args") or [])]
        if cmd in ("--help", "-h") or cmd == "":
            help_calls.append({"command": cmd, "args": args})
            continue
        if cmd == "task-context":
            tc_calls.append({"command": cmd, "args": args, "result": q.get("result")})
        elif cmd in ("analysis", "explain-symbol", "explain_symbol"):
            analysis_calls.append({"command": cmd, "args": args, "result": q.get("result")})
        else:
            other_calls.append({"command": cmd, "args": args, "result": q.get("result")})

    def inner_text(result_text: str | None) -> str:
        if not result_text:
            return ""
        try:
            arr = json.loads(result_text)
            if isinstance(arr, list):
                return "".join(x.get("text", "") for x in arr
                               if isinstance(x, dict) and x.get("type") == "text")
        except Exception:
            pass
        return result_text

    facts_returned = []
    tc_states = []
    for call in tc_calls:
        try:
            payload = json.loads(inner_text(call["result"]))
        except Exception:
            payload = None
        if isinstance(payload, dict) and payload.get("version") == "task-context-v0":
            fids = [i["fact_id"] for k in ("primary_targets", "source_spans", "relations",
                                           "candidates", "facts")
                    for i in (payload.get(k) or [])
                    if isinstance(i, dict) and isinstance(i.get("fact_id"), str)]
            tc_states.append({"status": payload.get("status"),
                              "facts_extracted": len(fids),
                              "categories_present": [k for k in
                                                     ("primary_targets", "source_spans",
                                                      "relations", "candidates", "facts",
                                                      "expansion_handles")
                                                     if payload.get(k)],
                              "anchor": payload.get("anchor")})
            if payload.get("status") == "available":
                facts_returned.extend(fids)
    analysis_results = []
    for call in analysis_calls:
        try:
            payload = json.loads(inner_text(call["result"]))
        except Exception:
            payload = None
        if isinstance(payload, dict) and payload.get("feature"):
            analysis_results.append({"command": call["command"], "args": call["args"],
                                     "feature": payload.get("feature"),
                                     "status": payload.get("status"),
                                     "nodes": len(payload.get("nodes") or []),
                                     "edges": len(payload.get("edges") or [])})
        else:
            analysis_results.append({"command": call["command"], "args": call["args"],
                                     "status": "unparsed"})
    return {"help_calls": help_calls, "task_context_calls": tc_calls,
            "analysis_calls": analysis_calls, "other_calls": other_calls,
            "task_context_states": tc_states,
            "facts_returned": sorted(set(facts_returned)),
            "analysis_results": analysis_results}


def evaluate(task: dict, output: str, completed: bool) -> dict:
    text = (output or "").lower()
    facts = []
    for fact in task["oracle"]["facts"]:
        hit = any(term.lower() in text for term in fact["any_of"])
        facts.append({"id": fact["id"], "hit": hit})
    success = all(f["hit"] for f in facts) and completed
    incremental = any(marker.lower() in text for marker in task["incremental_markers"])
    return {"facts": facts, "success": success, "incremental_fact_in_answer": incremental}


def facts_used(answer: str, obs: dict) -> dict:
    tokens = list(obs["facts_returned"])
    for ar in obs["analysis_results"]:
        for key in ("analysis_node_id", "analysis_edge_id"):
            pass
    text = answer or ""
    cited = [t for t in tokens if t in text]
    struct_hits = sorted({w for w in ("cnode:", "cedge:", "branch_merge", "loop_exit",
                                      "true edge", "false edge", "condition_true",
                                      "condition_false") if w.lower() in text.lower()})
    return {"fact_ids_cited_in_answer": cited,
            "structural_tokens_in_answer": struct_hits,
            "used": bool(cited or struct_hits)}


def verdict_rv1(solved: bool, completed: bool, obs: dict, cls: dict, usage: dict) -> dict:
    if not completed:
        return {"verdict": "INFRA_EXCLUDED", "detail": f"exit_reason!=completed"}
    facts_obtained = bool(cls["facts_returned"]) or any(
        ar.get("status") == "available" for ar in cls["analysis_results"])
    pl_invoked = bool(obs["queries"])
    if solved and facts_obtained and usage["used"]:
        return {"verdict": "CASE_SOLVED_NATURALLY",
                "detail": "solved with ProvenLattice facts/analysis cited in the answer"}
    if solved and facts_obtained and not usage["used"]:
        return {"verdict": "CASE_SOLVED_WITHOUT_PROVENLATTICE",
                "detail": ("solved correctly; PL facts were returned but the answer does not "
                           "cite them — solve did not depend on the new analysis capability")}
    if solved and not facts_obtained:
        return {"verdict": "CASE_SOLVED_WITHOUT_PROVENLATTICE",
                "detail": ("solved correctly via ordinary Read/Grep/Bash"
                           + (f" (PL tool invoked x{len(obs['queries'])}, no facts consumed)"
                              if pl_invoked else " (PL tool never invoked)"))}
    if not solved and facts_obtained:
        return {"verdict": "CAPABILITY_GAP_SUSPECTED",
                "detail": "PL facts/analysis genuinely obtained yet answer incorrect/incomplete"}
    return {"verdict": "ADOPTION_GAP_REMAINS",
            "detail": ("PL available but never produced facts; case unsolved"
                       + (f" (PL tool invoked x{len(obs['queries'])}, no facts obtained)"
                          if pl_invoked else " (PL tool never invoked)"))}


def run_case(task: dict, batch_dir: Path) -> dict:
    task_id = task["task_id"]
    run_id = f"D8-RV1-{task_id}-{uuid.uuid4().hex[:6]}"
    run_dir = batch_dir / "runs" / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    repo = batch_dir / "repos" / task_id
    prep = prepare_repo(repo)

    home, tmp = run_dir / "home", run_dir / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    cfg_dir = run_dir / "mcp"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "mcp-config.json"
    cfg.write_text(json.dumps({"mcpServers": {"provenlattice": {
        "command": sys.executable,
        "args": [str(MCP_SERVER), str(MCP_CONTRACT)],
        "env": {"PYTHONPATH": str(SRC_ROOT)}}}}, indent=2), encoding="utf-8")

    launcher = shutil.which("claude")
    argv = [launcher, "-p", "--output-format", "stream-json", "--verbose",
            "--dangerously-skip-permissions",
            "--disallowed-tools", "WebFetch", "WebSearch", "Edit", "Write", "NotebookEdit",
            "--mcp-config", str(cfg), "--strict-mcp-config"]
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
           "TMP": str(tmp), "TEMP": str(tmp)}

    started = time.time()
    completed = subprocess.run(argv, cwd=str(repo), input=task["prompt"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=TIMEOUT_S, env=env)
    ended = time.time()
    (run_dir / "agent-output.txt").write_text(completed.stdout or "", encoding="utf-8")
    (run_dir / "agent-stderr.txt").write_text(completed.stderr or "", encoding="utf-8")

    events, final_result = stream_events(completed.stdout or "")
    (run_dir / "events.ndjson").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False, sort_keys=True) for e in events),
        encoding="utf-8")
    answer = ""
    if final_result:
        answer = final_result.get("result") or ""
    (run_dir / "final-answer.txt").write_text(answer, encoding="utf-8")

    traffic = parse_query_traffic(events)
    cls = classify_queries(traffic)
    completed_flag = bool(final_result) and completed.returncode == 0
    evaluation = evaluate(task, answer, completed_flag)
    usage = facts_used(answer, cls)
    verdict = verdict_rv1(evaluation["success"], completed_flag, traffic, cls, usage)

    record = {
        "schema": "d8-rv1-run-v1", "run_id": run_id, "task_id": task_id,
        "category": task["category"], "target": task["target"],
        "generated_at": now_iso(),
        "fixture_commit_fingerprint": "d8-fixture-v1",
        "repo_prep": prep, "prompt": task["prompt"],
        "system_prompt": NEUTRAL_SYSTEM_PROMPT,
        "mcp_server": "provenlattice_mcp_server.py (transparent bridge, no overrides)",
        "exit_code": completed.returncode, "completed": completed_flag,
        "wall_seconds": round(ended - started, 1),
        "observed": {
            "pl_tool_visible": bool(traffic["server_entry"])
                               and traffic["server_entry"].get("status") == "connected"
                               and traffic["tool_enumerated"],
            "pl_tool_calls": len(traffic["queries"]),
            "pl_invoked": bool(traffic["queries"]),
            "help_discovery_calls": len(cls["help_calls"]),
            "task_context_calls": len(cls["task_context_calls"]),
            "task_context_states": cls["task_context_states"],
            "facts_returned": cls["facts_returned"],
            "analysis_calls": cls["analysis_calls"],
            "analysis_results": cls["analysis_results"],
            "other_pl_calls": cls["other_calls"],
            "facts_used": usage,
            "tool_counts": traffic["tool_counts"],
        },
        "evaluation": evaluation,
        "verdict": verdict,
    }
    (run_dir / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print(json.dumps({"run_id": run_id, "task_id": task_id,
                      "solved": evaluation["success"],
                      "facts": evaluation["facts"],
                      "pl_tool_calls": record["observed"]["pl_tool_calls"],
                      "tc_states": cls["task_context_states"],
                      "analysis": cls["analysis_results"],
                      "verdict": verdict}, ensure_ascii=False, indent=2))
    return record


def main() -> int:
    spec = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    tasks = {t["task_id"]: t for t in spec["tasks"]}
    missing = [t for t in RV_TASKS if t not in tasks]
    if missing:
        raise SystemExit(f"missing frozen tasks: {missing}")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    batch_dir = D8_ROOT / "results" / f"D8-RV1-{stamp}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for task_id in RV_TASKS:
        records.append(run_case(tasks[task_id], batch_dir))
    summary = {
        "schema": "d8-rv1-batch-summary-v1",
        "experiment": "D8-RV1 Original Failure Case Revalidation (CURRENT_VERSION_REVALIDATION)",
        "batch_dir": str(batch_dir),
        "generated_at": now_iso(),
        "historical_baseline": {
            "D8-A2": "ADOPTION_GAP", "D8-B2": "ADOPTION_GAP",
            "source": "docs/design-analysis-layer-d8-report.md + results/D8-20260922T091934Z (preserved, untouched)"},
        "runs": [{k: r[k] for k in ("run_id", "task_id", "verdict", "evaluation",
                                    "observed", "wall_seconds")}
                 for r in records],
    }
    (batch_dir / "revalidation-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"batch": str(batch_dir),
                      "verdicts": {r["task_id"]: r["verdict"]["verdict"] for r in records}},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
