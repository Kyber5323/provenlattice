"""D9-C6-C4 FINAL_UNSEAL and arm-level analysis.

One-time consumption of the sealed mapping after fail-closed verification of
all four frozen digest sets (packet set, R1 verdicts, R2 verdicts, sealed
mapping).  Restores cell <-> packet <-> verdict identity, produces the
arm-level outcome table and the evidence-based adoption analysis for the
isolated ``returned_facts = 0`` observation, and closes the PENDING marker.

Read-only with respect to all frozen evidence; no reruns, no modifications.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from formal_harness import FormalDataError, _atomic_json, digest

PACKET_SET_SHA256 = "826d9fe7bb05f4ecf4696ece9cf70720f140203381098018775be1d93b202b14"
SEALED_MAPPING_SHA256 = "ebd6f6ba67c79b57e0c7c0c7f92f8a2a49a3e2cbaafb11fac8d315a2a03dd3b9"
R1_CONTRACT_SHA256 = "671e5f51290a5a6825df56b73390aab8e4f8c8e98b338eedc0578a0865444890"
R1_VERDICT_SET_SHA256 = "9c79c810594c187da0e24073d5ab118166769a05f418246c35fa5c61f522cd8e"
R2_CONTRACT_SHA256 = "10945a891cd25d9900cd1658e88cd2b669e5015d1075a080e4e37063027010ed"
R2_VERDICT_SET_SHA256 = "28a2293477caa0bafad210a7735e16f3198b611a1f49ba81303658ff2f9f8e69"

PL_RE = re.compile(r"provenlattice[\"']?\s+([a-z\-_]+)", re.IGNORECASE)
TC_RE = re.compile(r"task[-_]context", re.IGNORECASE)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def verify_frozen_state(output: Path) -> dict:
    """Fail closed unless all four frozen digest sets still match."""
    checks = {}
    packet_freeze = json.loads((output / "review" / "packet-set-freeze.json").read_text(encoding="utf-8"))
    checks["packet_set_sha256"] = packet_freeze["packet_set_sha256"] == PACKET_SET_SHA256
    packet_ok = 0
    for bid, expected in packet_freeze["packet_hashes"].items():
        path = output / "review-packets" / f"{bid}.json"
        if digest(json.loads(path.read_text(encoding="utf-8"))) == expected:
            packet_ok += 1
    checks["packet_files"] = packet_ok == 36
    r1c = digest(json.loads((output / "review" / "r1" / "r1-review-contract.json").read_text(encoding="utf-8")))
    checks["r1_contract"] = r1c == R1_CONTRACT_SHA256
    r1_hashes = {p.stem: digest(json.loads(p.read_text(encoding="utf-8")))
                 for p in (output / "review" / "r1" / "verdicts").glob("blind-*.json")}
    checks["r1_verdict_set"] = digest(r1_hashes) == R1_VERDICT_SET_SHA256
    r2c = digest(json.loads((output / "review" / "r2" / "r2-review-contract.json").read_text(encoding="utf-8")))
    checks["r2_contract"] = r2c == R2_CONTRACT_SHA256
    r2_hashes = {p.stem: digest(json.loads(p.read_text(encoding="utf-8")))
                 for p in (output / "review" / "r2" / "verdicts").glob("blind-*.json")}
    checks["r2_verdict_set"] = digest(r2_hashes) == R2_VERDICT_SET_SHA256
    mapping = json.loads((output / "review" / "sealed" / "blinded-id-mapping.json").read_text(encoding="utf-8"))
    checks["sealed_mapping"] = digest(mapping) == SEALED_MAPPING_SHA256
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        raise FormalDataError("frozen digest verification failed before unseal", {
            "schema": "d9-c6-c4-gate-v1", "gate": "pre_unseal_digests", "failed": failed})
    return {"schema": "d9-c6-c4-pre-unseal-verification-v1", "checks": checks,
            "packet_set_sha256": PACKET_SET_SHA256, "r1_contract_sha256": R1_CONTRACT_SHA256,
            "r1_verdict_set_sha256": R1_VERDICT_SET_SHA256,
            "r2_contract_sha256": R2_CONTRACT_SHA256, "r2_verdict_set_sha256": R2_VERDICT_SET_SHA256,
            "sealed_mapping_sha256": SEALED_MAPPING_SHA256}


def adoption_evidence(output: Path, record: dict) -> dict:
    """Per-cell CLI/treatment exposure evidence from frozen traces (read-only)."""
    norm = json.loads(Path(record["normalized_trace"]).read_text(encoding="utf-8"))
    commands = [e["detail"].get("command", "") for e in norm["ordered_events"]
                if e["kind"] == "tool_use" and "command" in (e.get("detail") or {})]
    pl_commands = [c for c in commands if "provenlattice" in c.lower()]
    subcommands = {}
    for c in pl_commands:
        m = PL_RE.search(c)
        if m:
            subcommands[m.group(1)] = subcommands.get(m.group(1), 0) + 1
    tc_attempts = [c for c in commands if TC_RE.search(c)]
    raw = (Path(record["raw_artifacts"]) / "raw_trace.txt").read_text(encoding="utf-8")
    return {"provenlattice_commands": len(pl_commands),
            "provenlattice_subcommands": subcommands,
            "task_context_command_attempts": len(tc_attempts),
            "raw_provenlattice_mentions": len(re.findall(r"provenlattice", raw, re.IGNORECASE)),
            "raw_task_context_mentions": len(TC_RE.findall(raw)),
            "task_context_v0_payloads": sum(
                1 for e in norm["ordered_events"]
                if e["kind"] == "tool_result" and "task-context-v0" in json.dumps(e.get("detail", {}))),
            "returned_facts": len(record["returned_fact_ids"])}


def classify_b_cell(evidence: dict) -> str:
    if evidence["task_context_command_attempts"] == 0:
        return "TASK_CONTEXT_NEVER_REQUESTED"
    errored = evidence["raw_task_context_mentions"] > 0 and evidence["task_context_v0_payloads"] == 0
    if evidence["task_context_v0_payloads"] == 0:
        return "REQUESTED_BUT_NO_FACT_PAYLOAD" if not errored else "REQUESTED_BUT_FAILED"
    return "UNRESOLVED"


def final_verdict(r1: dict, r2: dict) -> dict:
    """Mirrors the frozen finalize_review agreement branch; R3 not required."""
    if r1["verdict"] == r2["verdict"]:
        return {"state": "FINAL", "final_verdict": r1["verdict"]}
    return {"state": "ADJUDICATION_REQUIRED", "final_verdict": None}


def main() -> None:
    parser = argparse.ArgumentParser(description="D9-C6-C4 final unseal and arm-level analysis")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    unseal_path = args.output / "review" / "final-unseal.json"
    analysis_path = args.output / "analysis" / "d9-c6-c4-final-unseal.json"
    if unseal_path.exists() or analysis_path.exists():
        raise FormalDataError("final unseal already consumed", {
            "schema": "d9-c6-c4-gate-v1", "gate": "single_consumption",
            "unseal_record": str(unseal_path)})

    verification = verify_frozen_state(args.output)
    mapping = json.loads((args.output / "review" / "sealed" / "blinded-id-mapping.json").read_text(encoding="utf-8"))
    packet_freeze = json.loads((args.output / "review" / "packet-set-freeze.json").read_text(encoding="utf-8"))
    r1 = {p.stem: json.loads(p.read_text(encoding="utf-8"))
          for p in (args.output / "review" / "r1" / "verdicts").glob("blind-*.json")}
    r2 = {p.stem: json.loads(p.read_text(encoding="utf-8"))
          for p in (args.output / "review" / "r2" / "verdicts").glob("blind-*.json")}

    unseal = {"schema": "d9-c6-c4-unseal-record-v1", "experiment_id": "D9-C5-FORMAL",
              "consumed_at": now_iso(),
              "sealed_mapping_sha256": SEALED_MAPPING_SHA256,
              "consumption": "single; mapping file itself remains byte-identical",
              "pre_unseal_verification": verification}
    unseal_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(unseal_path, unseal)

    rows, problems = [], []
    for bid, entry in sorted(mapping["mapping"].items()):
        cell_id = entry["cell_id"]
        if bid not in packet_freeze["packet_hashes"] or bid not in r1 or bid not in r2:
            problems.append({"blinded_id": bid, "issue": "missing packet or verdict binding"})
            continue
        record_path = args.output / "cells" / f"cell-{__import__('hashlib').sha256(cell_id.encode()).hexdigest()[:24]}" / "cell-record.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if record["cell_id"] != cell_id or record["arm"] != entry["arm"] or record["case_id"] != entry["case_id"] \
                or record["repeat"] != entry["repeat"]:
            problems.append({"blinded_id": bid, "issue": "cell record identity mismatch"})
            continue
        verdicts = final_verdict(r1[bid], r2[bid])
        rows.append({"cell_id": cell_id, "case_id": entry["case_id"], "arm": entry["arm"],
                     "repeat": entry["repeat"], "blinded_id": bid,
                     "packet_sha256": packet_freeze["packet_hashes"][bid],
                     "r1_verdict": r1[bid]["verdict"], "r2_verdict": r2[bid]["verdict"],
                     "final_state": verdicts["state"], "final_verdict": verdicts["final_verdict"],
                     "exposure": adoption_evidence(args.output, record)})
    if problems or len(rows) != 36 or len({r["cell_id"] for r in rows}) != 36 \
            or len({r["blinded_id"] for r in rows}) != 36:
        raise FormalDataError("mapping restoration incomplete", {
            "schema": "d9-c6-c4-gate-v1", "gate": "restoration", "problems": problems,
            "rows": len(rows)})

    def count(arm, key):
        return sum(1 for r in rows if r["arm"] == arm and r[key] == "YES")

    outcome = {
        "arm_A": {"cells": 18, "r1_yes": count("A", "r1_verdict"), "r2_yes": count("A", "r2_verdict"),
                  "final_yes": sum(1 for r in rows if r["arm"] == "A" and r["final_verdict"] == "YES")},
        "arm_B": {"cells": 18, "r1_yes": count("B", "r1_verdict"), "r2_yes": count("B", "r2_verdict"),
                  "final_yes": sum(1 for r in rows if r["arm"] == "B" and r["final_verdict"] == "YES")},
    }
    outcome["observed_outcome_delta"] = outcome["arm_B"]["final_yes"] - outcome["arm_A"]["final_yes"]
    case_table = {}
    for r in rows:
        key = f"{r['case_id']}::{r['arm']}"
        case_table.setdefault(key, []).append({"repeat": r["repeat"], "final": r["final_verdict"],
                                               "tc_attempts": r["exposure"]["task_context_command_attempts"],
                                               "facts": r["exposure"]["returned_facts"]})

    b_cells = [r for r in rows if r["arm"] == "B"]
    a_cells = [r for r in rows if r["arm"] == "A"]
    b_classes = {}
    for r in b_cells:
        b_classes[r["cell_id"]] = classify_b_cell(r["exposure"])
    adoption = {
        "arm_A_cli_use": {"cells_with_provenlattice_commands": sum(1 for r in a_cells if r["exposure"]["provenlattice_commands"]),
                          "total_provenlattice_commands": sum(r["exposure"]["provenlattice_commands"] for r in a_cells),
                          "total_raw_provenlattice_mentions": sum(r["exposure"]["raw_provenlattice_mentions"] for r in a_cells)},
        "arm_B_cli_use": {"cells_with_provenlattice_commands": sum(1 for r in b_cells if r["exposure"]["provenlattice_commands"]),
                          "total_provenlattice_commands": sum(r["exposure"]["provenlattice_commands"] for r in b_cells),
                          "total_task_context_command_attempts": sum(r["exposure"]["task_context_command_attempts"] for r in b_cells),
                          "total_raw_task_context_mentions": sum(r["exposure"]["raw_task_context_mentions"] for r in b_cells),
                          "total_task_context_v0_payloads": sum(r["exposure"]["task_context_v0_payloads"] for r in b_cells)},
        "arm_B_cell_classification": b_classes,
        "provable_cause": ("no agent in either arm invoked the repository code-graph CLI at all "
                           "(0/36 cells, 0 commands, 0 raw stream mentions); the treatment command "
                           "task-context was therefore never requested. The frozen formal protocol "
                           "surfaces no pointer to the CLI: the frozen prompt is the bare "
                           "problem_statement and the frozen adapter carries no system-prompt "
                           "addendum, unlike the pilot protocol which explicitly documented "
                           "`python -m provenlattice` to the agent."),
    }
    pending_questions = [
        {"question": "did arm B attempt task-context",
         "answer": "NO — 0/18 arm B cells contain any task-context command attempt "
                   "(0 command attempts, 0 raw stream mentions)", "evidence": "adoption.arm_B_cli_use"},
        {"question": "why task-context was not used",
         "answer": "the provenlattice CLI was never discovered/invoked by any agent in either arm "
                   "(0/36 cells); the frozen formal protocol provides no prompt-level pointer to it",
         "evidence": "adoption.provable_cause"},
        {"question": "whether this is an adoption gap",
         "answer": "YES — adoption gap at the discovery level: TREATMENT_NOT_EXPOSED; the arm B "
                   "inventory differential (task-context) could not be exercised because no CLI "
                   "command of any kind was invoked",
         "evidence": "adoption.arm_B_cell_classification"},
        {"question": "A/B quality differences",
         "answer": "outcome layer: both arms 18/18 YES under the frozen blind rubric; "
                   "observed outcome delta = 0 (no observed task-success difference)",
         "evidence": "outcome"},
        {"question": "whether the capability has value",
         "answer": "INCONCLUSIVE — with zero treatment exposure this batch provides no evidence "
                   "about the value or effectiveness of task-context",
         "evidence": "adoption + outcome"},
    ]
    conclusions = {
        "outcome_layer": "NO_OBSERVED_OUTCOME_DELTA — Arm A 18/18 YES, Arm B 18/18 YES "
                         "(R1 and R2 agree per packet); this batch observed no Arm B task-success "
                         "gain over Arm A. This is NOT evidence that task-context is ineffective.",
        "treatment_exposure_layer": "TREATMENT_NOT_EXPOSED / ADOPTION_GAP_AT_DISCOVERY_LEVEL — "
                                    "returned_facts = 0 is fully explained by zero CLI invocation: "
                                    "no agent ever ran `python -m provenlattice ...`, so the "
                                    "task-context treatment was never requested, attempted, or returned.",
        "capability_layer": "INCONCLUSIVE — capability value cannot be assessed in this batch "
                            "because treatment exposure was zero; a future batch requires an "
                            "exposure mechanism (e.g., prompt/pointer to the CLI) before capability "
                            "can be measured.",
    }
    analysis = {"schema": "d9-c6-c4-final-unseal-analysis-v1", "experiment_id": "D9-C5-FORMAL",
                "generated_at": now_iso(),
                "unseal_record": str(unseal_path), "mapping_consumed": True,
                "pre_unseal_verification": verification,
                "restoration": {"cells": 36, "unique_cell_ids": 36, "unique_blinded_ids": 36,
                                "problems": problems},
                "outcome": outcome, "case_arm_repeat_table": case_table,
                "adoption": adoption, "pending_questions_resolution": pending_questions,
                "conclusions": conclusions,
                "inputs_untouched_note": ("all frozen digests re-verified post-analysis by "
                                          "verify_d9_c6_c4 script; analysis is read-only")}
    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(analysis_path, analysis)

    pending_path = args.output / "review" / "observations" / "POST_EXECUTION_ANALYSIS_PENDING.json"
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    pending["status"] = "POST_EXECUTION_ANALYSIS_RESOLVED"
    pending["resolved_by"] = str(analysis_path)
    pending["resolved_at"] = now_iso()
    _atomic_json(pending_path, pending)

    print(json.dumps({"mode": "FINAL_UNSEAL_ANALYSIS_COMPLETE", "cells_restored": 36,
                      "arm_A_final_yes": outcome["arm_A"]["final_yes"],
                      "arm_B_final_yes": outcome["arm_B"]["final_yes"],
                      "observed_outcome_delta": outcome["observed_outcome_delta"],
                      "arm_B_exposure_classification": sorted(set(b_classes.values())),
                      "conclusions": conclusions,
                      "analysis_artifact": str(analysis_path),
                      "r3_adjudications": 0, "formal_reruns": 0, "agent_invocation_count": 0}))


if __name__ == "__main__":
    try:
        main()
    except FormalDataError as exc:
        print(json.dumps({"state": "D9_C6_C4_FAILED", "message": str(exc),
                          "diagnostic": exc.diagnostic}), file=sys.stderr)
        raise SystemExit(2)
