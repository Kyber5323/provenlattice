"""D9-C1 frozen-run audit: distinguish discovery, request, and answer use.

Run only after an unchanged D8 runner batch.  This script never invokes an
agent or mutates product code; it reads run telemetry, uses D8's deterministic
reattribution for truncated envelopes, and writes an observation artifact.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from d8_reattribute import reattribute


SYMBOL_RE = re.compile(r"explain-symbol\s+([A-Za-z0-9_.\-]+)")


def _discovery(events: list[dict], requested: bool) -> str:
    commands = [str(event.get("query") or "") for event in events]
    if any("explain-symbol" in command and "--help" in command for command in commands):
        return "yes_by_help"
    return "yes_by_request" if requested else "no"


def audit(batch_dir: Path) -> dict:
    summary = reattribute(batch_dir)
    rows = []
    counts = {key: 0 for key in (
        "UTILITY_OBSERVED", "ADOPTION_GAP", "NO_INCREMENTAL_VALUE",
        "CAPABILITY_GAP", "DISCOVERY_OBSERVED",
    )}
    for summary_row in summary["rows"]:
        task_id = summary_row["task_id"]
        record_path = batch_dir / "runs" / f"{task_id}-D" / "run.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        replayed = record.get("analysis_requests_replayed") or []
        requested = bool(replayed)
        discovered = _discovery(record.get("events") or [], requested)
        symbols = []
        for request in record.get("analysis_requests") or []:
            match = SYMBOL_RE.search(str(request.get("command") or ""))
            if match and match.group(1) not in symbols:
                symbols.append(match.group(1))
        verdict = (summary_row.get("verdict_detail") or {}).get("verdict", "INFRA_EXCLUDED")
        if verdict in counts:
            counts[verdict] += 1
        if discovered != "no":
            counts["DISCOVERY_OBSERVED"] += 1
        evaluation = record.get("evaluation") or {}
        rows.append({
            "case": task_id,
            "run_id": record["run_id"],
            "task_success": bool(evaluation.get("success")),
            "analysis_discovered": discovered,
            "analysis_requested": requested,
            "features": sorted({item.get("feature") for item in replayed}),
            "selected_surface": "explain-symbol" if requested else None,
            "selected_symbol": symbols,
            "analysis_used_in_answer": bool(record.get("analysis_used")
                                            and evaluation.get("incremental_fact_in_answer")),
            "incremental_fact_used": bool(evaluation.get("incremental_fact_in_answer")),
            "verdict": verdict,
            "backend_model": record.get("backend_model"),
            "actual_model": record.get("actual_model"),
        })
    artifact = {
        "experiment": "D9-C1 Frozen Agent Adoption Re-observation",
        "batch_dir": str(batch_dir),
        "rows": rows,
        "counts": counts,
        "method": {
            "discovery": "--help invocation, or an analysis request without recorded help",
            "requested": "recorded explain-symbol --analysis call",
            "used_in_answer": "available replayed analysis plus D8 incremental fact marker",
        },
    }
    (batch_dir / "d9-c1-observation.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    return artifact


if __name__ == "__main__":
    target = Path(sys.argv[1])
    print(json.dumps(audit(target), ensure_ascii=False, indent=2))
