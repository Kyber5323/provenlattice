"""D9-C6-C2 R1 blind review infrastructure.

The reviewer receives only the frozen rubric and one blinded packet file in a
fresh isolated context; verdicts are captured verbatim.  This module freezes
the review contract, ingests and validates all verdicts against the frozen
packet set (hash recheck per packet), and freezes the verdict set.  It never
reads the sealed mapping, cell records, normalized traces, or execution
metadata, and never modifies packets.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from formal_harness import FormalDataError, _atomic_json, digest

from formal_c6c1 import LEAK_TOKENS, scan_leaks

CONTRACT_SCHEMA = "d9-c6-c2-r1-contract-v1"
REVIEW_SCHEMA = "d9-c6-c2-r1-review-v1"
FREEZE_SCHEMA = "d9-c6-c2-r1-freeze-v1"
RUBRIC_VERSION = "d9-c6-c2-r1-rubric-v1"
PACKET_SET_SHA256 = "826d9fe7bb05f4ecf4696ece9cf70720f140203381098018775be1d93b202b14"
SEALED_MAPPING_SHA256 = "ebd6f6ba67c79b57e0c7c0c7f92f8a2a49a3e2cbaafb11fac8d315a2a03dd3b9"

REVIEW_QUESTION = ("Does the final_patch, taken at face value together with "
                   "final_explanation, implement the change requested by problem_statement?")
REASON_ENUM = {
    "YES": ["IMPLEMENTS_REQUESTED_CHANGE", "IMPLEMENTS_WITH_MINOR_DEVIATION"],
    "NO": ["DOES_NOT_IMPLEMENT_REQUESTED_CHANGE", "NO_SUBSTANTIVE_PATCH",
           "PATCH_TANGENTIAL_TO_REQUEST"],
}
REVIEWER_CONFIG = {
    "reviewer_role": "R1",
    "reviewer_model": "glm-5.3-flash",
    "isolation": ("one fresh LLM reviewer context per packet; the reviewer receives only "
                  "the frozen rubric and the packet file path and may not read any other file"),
    "orchestration": ("verdict files are written by the reviewer itself and ingested verbatim; "
                      "the orchestrator validates structure and hashes but does not edit verdict content"),
    "packet_binding": ("verdicts bind to packet content via packet_sha256 from "
                       "packet-set-freeze.json"),
}
VERDICT_IDENTITY_TOKENS = ["cell_id", "case_id", '"repeat"', '"arm"', "cell-", "::a::", "::b::"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_packet_hashes(output: Path) -> dict[str, str]:
    freeze = json.loads((output / "review" / "packet-set-freeze.json").read_text(encoding="utf-8"))
    if freeze["packet_set_sha256"] != PACKET_SET_SHA256:
        raise FormalDataError("packet set identity mismatch", {
            "schema": "d9-c6-c2-gate-v1", "gate": "packet_set_binding",
            "expected": PACKET_SET_SHA256, "found": freeze["packet_set_sha256"]})
    return freeze["packet_hashes"]


def verify_packets_unchanged(output: Path, packet_hashes: dict[str, str]) -> None:
    packets_root = output / "review-packets"
    for bid, expected in packet_hashes.items():
        path = packets_root / f"{bid}.json"
        packet = json.loads(path.read_text(encoding="utf-8"))
        if digest(packet) != expected:
            raise FormalDataError("packet content changed after freeze", {
                "schema": "d9-c6-c2-gate-v1", "gate": "packet_immutable",
                "blinded_review_id": bid})


def freeze_contract(output: Path) -> None:
    path = output / "review" / "r1" / "r1-review-contract.json"
    if path.exists():
        raise FormalDataError("R1 review contract already exists", {
            "schema": "d9-c6-c2-gate-v1", "gate": "contract_fresh", "path": str(path)})
    packet_hashes = load_packet_hashes(output)
    verify_packets_unchanged(output, packet_hashes)
    contract = {"schema": CONTRACT_SCHEMA, "experiment_id": "D9-C5-FORMAL",
                "packet_schema": "d9-c5-blinded-packet-v1",
                "packet_set_sha256": PACKET_SET_SHA256,
                "sealed_mapping_sha256": SEALED_MAPPING_SHA256,
                "rubric_version": RUBRIC_VERSION,
                "review_question": REVIEW_QUESTION,
                "verdict_rule": {
                    "YES": "the patch, as written, plausibly implements the requested change; "
                           "minor deviations in style, helper scope, or test coverage are acceptable",
                    "NO": "the patch does not implement the request, is only tangentially related, "
                          "or contains no substantive change toward the request"},
                "reason_enum": REASON_ENUM,
                "review_record_schema": REVIEW_SCHEMA,
                "reviewer_config": REVIEWER_CONFIG,
                "created_at": now_iso()}
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(path, contract)
    print(json.dumps({"mode": "R1_CONTRACT_FROZEN", "contract_sha256": digest(contract),
                      "packet_set_sha256": PACKET_SET_SHA256, "path": str(path)}))


def validate_verdict(raw: dict, bid: str, packet_hashes: dict[str, str]) -> dict:
    record = {"schema": REVIEW_SCHEMA, "blinded_review_id": bid,
              "reviewer_role": "R1", "verdict": raw.get("verdict"),
              "reasons": raw.get("reasons"), "evidence_note": raw.get("evidence_note"),
              "packet_sha256": packet_hashes.get(bid), "rubric": RUBRIC_VERSION,
              "reviewed_at": raw.get("reviewed_at") or now_iso()}
    if record["verdict"] not in REASON_ENUM:
        raise FormalDataError("invalid R1 verdict", {
            "schema": "d9-c6-c2-gate-v1", "gate": "verdict_enum",
            "blinded_review_id": bid, "verdict": record["verdict"]})
    if not isinstance(record["reasons"], list) or not record["reasons"] or \
            not set(record["reasons"]) <= set(REASON_ENUM[record["verdict"]]):
        raise FormalDataError("invalid R1 reason enum", {
            "schema": "d9-c6-c2-gate-v1", "gate": "reason_enum",
            "blinded_review_id": bid, "reasons": record["reasons"]})
    if not isinstance(record["evidence_note"], str) or len(record["evidence_note"].strip()) < 10:
        raise FormalDataError("R1 evidence note missing", {
            "schema": "d9-c6-c2-gate-v1", "gate": "evidence_note", "blinded_review_id": bid})
    if record["packet_sha256"] is None:
        raise FormalDataError("verdict does not bind to a frozen packet", {
            "schema": "d9-c6-c2-gate-v1", "gate": "packet_binding", "blinded_review_id": bid})
    content_blob = json.dumps({"verdict": record["verdict"], "reasons": record["reasons"],
                               "evidence_note": record["evidence_note"]}, sort_keys=True).lower()
    for category, tokens in LEAK_TOKENS.items():
        for token in tokens:
            if token in content_blob:
                raise FormalDataError("R1 verdict leakage", {
                    "schema": "d9-c6-c2-gate-v1", "gate": "leak_scan",
                    "blinded_review_id": bid, "category": category, "token": token})
    for token in VERDICT_IDENTITY_TOKENS:
        if token in content_blob:
            raise FormalDataError("R1 verdict identity leakage", {
                "schema": "d9-c6-c2-gate-v1", "gate": "leak_scan",
                "blinded_review_id": bid, "token": token})
    return record


def ingest_verdicts(output: Path, verdicts_path: Path) -> None:
    verdicts_root = output / "review" / "r1" / "verdicts"
    if verdicts_root.exists():
        raise FormalDataError("R1 verdict namespace already exists", {
            "schema": "d9-c6-c2-gate-v1", "gate": "verdicts_fresh", "path": str(verdicts_root)})
    packet_hashes = load_packet_hashes(output)
    verify_packets_unchanged(output, packet_hashes)
    raw_lines = [line for line in verdicts_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    parsed = []
    for index, line in enumerate(raw_lines):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FormalDataError("verdict line is not JSON", {
                "schema": "d9-c6-c2-gate-v1", "gate": "parse", "line": index + 1, "error": str(exc)})
        bid = raw.get("blinded_review_id")
        if bid not in packet_hashes:
            raise FormalDataError("verdict references unknown blinded id", {
                "schema": "d9-c6-c2-gate-v1", "gate": "blinded_id", "line": index + 1, "bid": bid})
        parsed.append(validate_verdict(raw, bid, packet_hashes))
    bids = [r["blinded_review_id"] for r in parsed]
    if len(parsed) != 36 or len(set(bids)) != 36 or set(bids) != set(packet_hashes):
        raise FormalDataError("verdict set is not exactly the frozen packet set", {
            "schema": "d9-c6-c2-gate-v1", "gate": "coverage", "verdicts": len(parsed),
            "unique": len(set(bids)), "missing": sorted(set(packet_hashes) - set(bids)),
            "extra": sorted(set(bids) - set(packet_hashes))})
    verdicts_root.mkdir(parents=True, exist_ok=False)
    verdict_hashes = {}
    for record in sorted(parsed, key=lambda r: r["blinded_review_id"]):
        path = verdicts_root / f"{record['blinded_review_id']}.json"
        _atomic_json(path, record)
        verdict_hashes[record["blinded_review_id"]] = digest(record)
    verdicts = [json.loads((verdicts_root / f"{b}.json").read_text(encoding="utf-8")) for b in verdict_hashes]
    freeze = {"schema": FREEZE_SCHEMA, "experiment_id": "D9-C5-FORMAL",
              "reviewer_role": "R1", "rubric_version": RUBRIC_VERSION,
              "contract": str(output / "review" / "r1" / "r1-review-contract.json"),
              "packet_set_sha256": PACKET_SET_SHA256,
              "sealed_mapping_sha256": SEALED_MAPPING_SHA256,
              "sealed_mapping_consumed": False,
              "reviewer_config": REVIEWER_CONFIG,
              "verdict_count": len(verdict_hashes), "unique_blinded_ids": len(verdict_hashes),
              "verdict_hashes": verdict_hashes,
              "verdict_set_sha256": digest(verdict_hashes),
              "verdict_distribution": {v: sum(1 for r in verdicts if r["verdict"] == v)
                                       for v in ("YES", "NO")},
              "reviewer_invocation_attempts_total": len(parsed),
              "frozen_at": now_iso()}
    _atomic_json(output / "review" / "r1" / "r1-verdict-set-freeze.json", freeze)
    print(json.dumps({"mode": "R1_VERDICTS_FROZEN", "verdicts": len(verdict_hashes),
                      "verdict_set_sha256": freeze["verdict_set_sha256"],
                      "verdict_distribution": freeze["verdict_distribution"],
                      "agent_invocation_count": 0, "formal_reruns": 0,
                      "r2_reviews": 0, "r3_adjudications": 0,
                      "sealed_mapping_consumed": False}))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D9-C6-C2 R1 blind review")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--freeze-contract", action="store_true")
    parser.add_argument("--ingest-verdicts", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.freeze_contract:
        freeze_contract(args.output)
    elif args.ingest_verdicts:
        ingest_verdicts(args.output, args.ingest_verdicts)
    else:
        raise FormalDataError("one mode is required", {"schema": "d9-c6-c2-gate-v1"})


if __name__ == "__main__":
    try:
        main()
    except FormalDataError as exc:
        print(json.dumps({"state": "D9_C6_C2_FAILED", "message": str(exc),
                          "diagnostic": exc.diagnostic}), file=sys.stderr)
        raise SystemExit(2)
