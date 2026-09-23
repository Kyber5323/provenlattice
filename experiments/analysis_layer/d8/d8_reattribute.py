"""D8 re-attribution: replay Agent explain-symbol calls against the D6 API.

The Agent's own Bash pipelines truncate the JSON envelope, so the harness
cannot always parse the analysis status out of the recorded tool result.
The derived store is static after `analyze` (no query mutates it), so
replaying each recorded (symbol, feature) call against the same database
yields the exact status the Agent's call evaluated to. Command text and
truncation behavior are preserved as recorded.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

D8_ROOT = Path(__file__).resolve().parent
REPO_ROOT = D8_ROOT.parents[2]
SRC_ROOT = REPO_ROOT / "src"
for _path in (str(REPO_ROOT), str(SRC_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from provenlattice.analysis_query import query_analysis  # noqa: E402
from provenlattice.query import GraphQuery  # noqa: E402

SYMBOL_RE = re.compile(r"explain-symbol\s+([A-Za-z0-9_.\-]+)")
FEATURE_RE = re.compile(r"--analysis[= ]([A-Za-z_,]+)")


def _replay_symbol_call(name: str, feature: str, database: str) -> dict:
    """Mirror the D7 tool chain: resolve the anchor by name, then query by
    the exact base symbol id (D6 contract)."""
    with GraphQuery(database) as query:
        definition = query.get_definition(name)
        anchor = definition.get("data")
        if not anchor:
            return {"status": "not_available", "reason": "symbol_not_found"}
        return query_analysis(
            str(database).rsplit("\\.provenlattice", 1)[0], feature,
            symbol_id=anchor["id"], database=database,
        )


def reattribute(batch_dir: Path) -> dict:
    spec = json.loads((D8_ROOT / "d8_tasks.json").read_text(encoding="utf-8"))
    tasks = {task["task_id"]: task for task in spec["tasks"]}
    summary_path = batch_dir / "batch-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for row in summary["rows"]:
        task = tasks[row["task_id"]]
        run_path = batch_dir / "runs" / f"{row['task_id']}-D" / "run.json"
        record = json.loads(run_path.read_text(encoding="utf-8"))
        repo_path = str(batch_dir / "repos" / "D")
        replayed = []
        for request in record["analysis_requests"]:
            match = SYMBOL_RE.search(request["command"])
            features = FEATURE_RE.search(request["command"])
            feature_list = (features.group(1).split(",") if features else [])
            for feature in feature_list:
                if match:
                    result = _replay_symbol_call(
                        match.group(1), feature,
                        str(batch_dir / "repos" / "D" / ".provenlattice" / "codegraph.db"),
                    )
                else:
                    result = {"status": "unparsable_command"}
                replayed.append({
                    "feature": feature, "replayed_status": result["status"],
                    "replayed_reason": result.get("reason"),
                    "replayed_nodes": len(result.get("nodes") or []),
                })
        used = any(item["replayed_status"] == "available" for item in replayed)
        record["analysis_requests_replayed"] = replayed
        record["analysis_used"] = used
        evaluation = record.get("evaluation") or _evaluate_offline(task, record)
        verdict = attribute(task, record, evaluation)
        row["AST/CFG used"] = used
        row["verdict_detail"] = verdict
        row["replayed_requests"] = replayed
        counts[verdict["verdict"]] = counts.get(verdict["verdict"], 0) + 1
        run_path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"{row['task_id']:8} used={used} replayed={replayed} "
              f"verdict={verdict['verdict']}")
    summary["verdict_counts"] = counts
    summary["attribution_note"] = (
        "replayed: each recorded explain-symbol --analysis call was re-evaluated "
        "against the same static derived store (D6 API) because the Agent's own "
        "shell pipelines truncated the JSON envelope")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    return summary


def _evaluate_offline(task: dict, record: dict) -> dict:
    from d8_runner import evaluate
    return evaluate(task, record)


def attribute(task: dict, record: dict, evaluation: dict) -> dict:
    if record["exit_reason"] != "completed":
        return {"verdict": "INFRA_EXCLUDED", "detail": record["exit_reason"]}
    used = record["analysis_used"]
    if not used:
        return {"verdict": "ADOPTION_GAP",
                "detail": "analysis available but never requested"}
    if evaluation["success"] and evaluation["incremental_fact_in_answer"]:
        return {"verdict": "UTILITY_OBSERVED",
                "detail": "analysis used + correct answer + incremental fact cited"}
    if evaluation["success"]:
        return {"verdict": "NO_INCREMENTAL_VALUE",
                "detail": "analysis used but answer rests on base-reachable facts"}
    return {"verdict": "CAPABILITY_GAP",
            "detail": "analysis used and available, yet answer incorrect/incomplete"}


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if target is None:
        base = D8_ROOT / "results"
        target = sorted(p for p in base.iterdir()
                        if p.is_dir() and p.name.startswith("D8-2026"))[-1]
    result = reattribute(target)
    print(json.dumps(result["verdict_counts"], ensure_ascii=False))
