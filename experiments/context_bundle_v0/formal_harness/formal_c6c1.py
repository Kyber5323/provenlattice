"""D9-C6-C1 freeze blinded reviewer packets from the frozen formal 36-run.

Packets are derived ONLY from the 36 formal cell records and their frozen
normalized traces plus the canonical bundle prompts.  Blinding reuses the
qualified C5-C primitives (``_scrub`` and ``assert_packet_blind``) and the
frozen ``d9-c5-blinded-packet-v1`` field names; the fact-review-only fields
(``fact_id``, ``returned_fact``, ``relevant_trace``) are omitted because no
returned facts exist and fact counts must not leak.  The sealed
blinded-id-to-cell mapping is stored separately from the packets and never
enters reviewer-visible content.

Fail closed: no packet, mapping, or freeze record is ever overwritten.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from formal_harness import (FormalDataError, _atomic_json, _scrub,
                            assert_packet_blind, digest, load_canonical_bundle)

PACKET_SCHEMA = "d9-c5-blinded-packet-v1"
MAPPING_SCHEMA = "d9-c6-c1-sealed-mapping-v1"
FREEZE_SCHEMA = "d9-c6-c1-packet-freeze-v1"
OBSERVATION_SCHEMA = "d9-c6-c1-analysis-isolation-v1"

LEAK_TOKENS = {
    "execution_environment": ["f90c45b62f06905abb2e2d0ccebe72e7bccbe988e068f6b7badf532c56118e31",
                              "f90c45b6", "desktop-u1amjdf", "sunqinghw", "formal-approved",
                              "pl-c4-pilot", "formal-readiness-r3", "d:\\pl-c4"],
    "runtime": ["cf323c5c6128725f942520a8445a41ab7897213f", "cf323c5c",
                "e385681146f9d70b08466093060b8cc8204c65a8", "e3856811",
                "pl-arm-a", "pl-arm-b", "runtime_sha", "runtime root"],
    "arm": ['"arm"', "::a::", "::b::", "arm a", "arm b"],
    "task_context": ["task-context", "task_context", "context bundle", "context-bundle"],
    "returned_facts": ["returned_fact", "fact_id", '"facts"', "returned_facts"],
    "execution_timing": ["wall_seconds", "duration_ms", "started_at", "ended_at",
                         "attempt_count", "agent_invocation", "tool_use", "tool_counts"],
    "reviewer": ["r1_verdict", "r2_verdict", "reviewer_role", "reviewer identity"],
}


def blinded_id(cell_identifier: str) -> str:
    """Stable blinded identity in the existing harness ``blind-`` form."""
    return "blind-" + digest({"cell": cell_identifier})[:20]


def scan_leaks(packet: dict) -> dict[str, list[str]]:
    blob = json.dumps(packet, sort_keys=True).lower()
    found: dict[str, list[str]] = {}
    for category, tokens in LEAK_TOKENS.items():
        hits = sorted({token for token in tokens if token in blob})
        if hits:
            found[category] = hits
    return found


def build_packet(record: dict, normalized: dict, problem_statement: str) -> dict:
    packet = {"schema": PACKET_SCHEMA,
              "blinded_review_id": blinded_id(record["cell_id"]),
              "problem_statement": _scrub(problem_statement),
              "final_patch": _scrub(normalized.get("final_patch") or ""),
              "final_explanation": _scrub(normalized.get("final_result") or "")}
    assert_packet_blind(packet)
    leaks = scan_leaks(packet)
    if leaks:
        raise FormalDataError("blinded packet leakage scan failed", {
            "schema": "d9-c6-c1-blinding-gate-v1", "blinded_review_id": packet["blinded_review_id"],
            "leaks": leaks})
    if not packet["final_explanation"].strip() or not packet["final_patch"].strip():
        raise FormalDataError("packet content incomplete", {
            "schema": "d9-c6-c1-blinding-gate-v1", "blinded_review_id": packet["blinded_review_id"],
            "final_result_chars": len(packet["final_explanation"]),
            "final_patch_bytes": len(packet["final_patch"])})
    return packet


def load_formal_records(out_root: Path, bundle: dict) -> list[dict]:
    cells_root = out_root / "cells"
    records = []
    for key_dir in sorted(cells_root.iterdir()):
        path = key_dir / "cell-record.json"
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
            normalized_path = Path(record["normalized_trace"])
            if not normalized_path.exists():
                raise FormalDataError("normalized trace missing for formal record", {
                    "schema": "d9-c6-c1-input-gate-v1", "cell_id": record["cell_id"],
                    "normalized_trace": str(normalized_path)})
            record["_normalized"] = json.loads(normalized_path.read_text(encoding="utf-8"))
            records.append(record)
    manifest_cells = {c["cell_id"]: c for c in bundle["manifest"]["cells"]}
    ids = [r["cell_id"] for r in records]
    if len(records) != 36 or len(set(ids)) != 36 or set(ids) != set(manifest_cells):
        raise FormalDataError("formal record set is not the frozen 36", {
            "schema": "d9-c6-c1-input-gate-v1", "records": len(records),
            "unique": len(set(ids)), "manifest_cells": len(manifest_cells)})
    arms = {}
    for r in records:
        cell = manifest_cells[r["cell_id"]]
        if r["arm"] != cell["arm"] or r["case_id"] != cell["case_id"] or r["repeat"] != cell["repeat"]:
            raise FormalDataError("formal record identity drift", {
                "schema": "d9-c6-c1-input-gate-v1", "cell_id": r["cell_id"]})
        arms[r["arm"]] = arms.get(r["arm"], 0) + 1
    if arms != {"A": 18, "B": 18}:
        raise FormalDataError("formal arm counts drifted", {
            "schema": "d9-c6-c1-input-gate-v1", "arms": arms})
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D9-C6-C1 blinded packet freeze")
    parser.add_argument("--formal-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="formal artifact root (cells/, readiness/)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    bundle = load_canonical_bundle(args.formal_root)
    packets_root = args.output / "review-packets"
    sealed_root = args.output / "review" / "sealed"
    observations_root = args.output / "review" / "observations"
    if packets_root.exists() or sealed_root.exists():
        raise FormalDataError("review packet namespace already exists", {
            "schema": "d9-c6-c1-input-gate-v1", "packets_root": str(packets_root),
            "sealed_root": str(sealed_root)})
    records = load_formal_records(args.output, bundle)
    packets: dict[str, dict] = {}
    mapping: dict[str, dict] = {}
    sources: dict[str, dict] = {}
    for record in records:
        cell_id = record["cell_id"]
        packet = build_packet(record, record["_normalized"], bundle["prompts"][record["case_id"]])
        bid = packet["blinded_review_id"]
        if bid in packets:
            raise FormalDataError("blinded id collision", {
                "schema": "d9-c6-c1-blinding-gate-v1", "blinded_review_id": bid})
        packets[bid] = packet
        mapping[bid] = {"cell_id": cell_id, "case_id": record["case_id"],
                        "arm": record["arm"], "repeat": record["repeat"]}
        sources[cell_id] = {"cell_record_sha256": digest(
            {k: v for k, v in record.items() if k != "_normalized"}),
            "normalized_trace_sha256": digest(record["_normalized"]),
            "raw_artifacts": record["raw_artifacts"]}
    if len(packets) != 36:
        raise FormalDataError("packet set incomplete", {
            "schema": "d9-c6-c1-blinding-gate-v1", "packets": len(packets)})

    packets_root.mkdir(parents=True, exist_ok=False)
    packet_hashes = {}
    for bid, packet in sorted(packets.items()):
        path = packets_root / f"{bid}.json"
        _atomic_json(path, packet)
        packet_hashes[bid] = digest(packet)
    mapping_doc = {"schema": MAPPING_SCHEMA, "experiment_id": "D9-C5-FORMAL",
                   "packet_schema": PACKET_SCHEMA,
                   "created_at": datetime.now(timezone.utc).isoformat(),
                   "mapping": mapping}
    sealed_root.mkdir(parents=True, exist_ok=False)
    _atomic_json(sealed_root / "blinded-id-mapping.json", mapping_doc)
    freeze = {"schema": FREEZE_SCHEMA, "experiment_id": "D9-C5-FORMAL",
              "packet_schema": PACKET_SCHEMA,
              "generator": "formal_c6c1.py (C5-C _scrub + assert_packet_blind primitives)",
              "frozen_at": datetime.now(timezone.utc).isoformat(),
              "formal_cells": 36, "review_packets": 36, "unique_blinded_ids": len(packet_hashes),
              "packet_hashes": packet_hashes,
              "packet_set_sha256": digest(packet_hashes),
              "sealed_mapping": str(sealed_root / "blinded-id-mapping.json"),
              "sealed_mapping_sha256": digest(mapping_doc),
              "source_records": sources,
              "blinding_scan": {"packets_scanned": 36, "leak_categories": 0,
                                "note": "assert_packet_blind + category token scan, fail closed"}}
    review_root = args.output / "review"
    _atomic_json(review_root / "packet-set-freeze.json", freeze)
    observations_root.mkdir(parents=True, exist_ok=True)
    _atomic_json(observations_root / "POST_EXECUTION_ANALYSIS_PENDING.json", {
        "schema": OBSERVATION_SCHEMA, "experiment_id": "D9-C5-FORMAL",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "observation": "returned_facts = 0 across 36/36 formal cells (18/18 arm B cells included)",
        "status": "POST_EXECUTION_ANALYSIS_PENDING",
        "deferred_questions": [
            "did arm B attempt task-context",
            "why task-context was not used",
            "whether this is an adoption gap",
            "A/B quality differences",
            "whether the capability has value"],
        "unblocks_after": "blind R1/R2 verdicts frozen"})
    print(json.dumps({"mode": "REVIEW_PACKETS_FROZEN", "review_packets": 36,
                      "unique_blinded_ids": len(packet_hashes),
                      "packet_set_sha256": freeze["packet_set_sha256"],
                      "sealed_mapping_sha256": freeze["sealed_mapping_sha256"],
                      "packets_root": str(packets_root),
                      "sealed_mapping": freeze["sealed_mapping"],
                      "freeze_record": str(review_root / "packet-set-freeze.json"),
                      "agent_invocation_count": 0, "formal_reruns": 0,
                      "r1_reviews": 0, "r2_reviews": 0, "r3_adjudications": 0}))


if __name__ == "__main__":
    try:
        main()
    except FormalDataError as exc:
        print(json.dumps({"state": "D9_C6_C1_FAILED", "message": str(exc),
                          "diagnostic": exc.diagnostic}), file=sys.stderr)
        raise SystemExit(2)
