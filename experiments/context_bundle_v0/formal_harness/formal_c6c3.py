"""D9-C6-C3 R2 independent blind review infrastructure.

Mirrors the frozen R1 process with role=R2 and a fully separate namespace.
The R2 reviewer never sees R1 verdicts, reasons, distribution, or artifacts.
After both verdict sets are frozen, ``--agreement`` performs the mechanical
per-blinded-id agreement statistic only (no R3, no unsealing, no analysis).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from formal_harness import FormalDataError, _atomic_json, digest

from formal_c6c1 import LEAK_TOKENS
from formal_c6c2 import (PACKET_SET_SHA256, REASON_ENUM, REVIEW_QUESTION,
                         RUBRIC_VERSION, SEALED_MAPPING_SHA256,
                         VERDICT_IDENTITY_TOKENS, load_packet_hashes,
                         verify_packets_unchanged)

CONTRACT_SCHEMA = "d9-c6-c3-r2-contract-v1"
REVIEW_SCHEMA = "d9-c6-c3-r2-review-v1"
FREEZE_SCHEMA = "d9-c6-c3-r2-freeze-v1"
R1_CONTRACT_SHA256 = "671e5f51290a5a6825df56b73390aab8e4f8c8e98b338eedc0578a0865444890"
R1_VERDICT_SET_SHA256 = "9c79c810594c187da0e24073d5ab118166769a05f418246c35fa5c61f522cd8e"

REVIEWER_CONFIG = {
    "reviewer_role": "R2",
    "reviewer_model": "glm-5.3-flash",
    "isolation": ("one fresh LLM reviewer context per packet; the reviewer receives only "
                  "the frozen rubric and the packet file path and may not read any other file; "
                  "no R1 verdict, reason, distribution, or artifact is exposed"),
    "orchestration": ("verdict files are written by the reviewer itself and ingested verbatim; "
                      "the orchestrator validates structure and hashes but does not edit verdict content"),
    "packet_binding": ("verdicts bind to packet content via packet_sha256 from "
                       "packet-set-freeze.json"),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _r1_digests_unchanged(output: Path) -> None:
    contract = json.loads((output / "review" / "r1" / "r1-review-contract.json").read_text(encoding="utf-8"))
    if digest(contract) != R1_CONTRACT_SHA256:
        raise FormalDataError("R1 contract changed", {"schema": "d9-c6-c3-gate-v1"})
    vfreeze = json.loads((output / "review" / "r1" / "r1-verdict-set-freeze.json").read_text(encoding="utf-8"))
    if vfreeze["verdict_set_sha256"] != R1_VERDICT_SET_SHA256:
        raise FormalDataError("R1 verdict set changed", {"schema": "d9-c6-c3-gate-v1"})
    hashes = {}
    for path in (output / "review" / "r1" / "verdicts").glob("blind-*.json"):
        hashes[path.stem] = digest(json.loads(path.read_text(encoding="utf-8")))
    if digest(hashes) != R1_VERDICT_SET_SHA256 or set(hashes) != set(vfreeze["verdict_hashes"]):
        raise FormalDataError("R1 verdict files changed", {"schema": "d9-c6-c3-gate-v1"})


def validate_verdict(raw: dict, bid: str, packet_hashes: dict[str, str]) -> dict:
    record = {"schema": REVIEW_SCHEMA, "blinded_review_id": bid,
              "reviewer_role": "R2", "verdict": raw.get("verdict"),
              "reasons": raw.get("reasons"), "evidence_note": raw.get("evidence_note"),
              "packet_sha256": packet_hashes.get(bid), "rubric": RUBRIC_VERSION,
              "reviewed_at": raw.get("reviewed_at") or now_iso()}
    if record["verdict"] not in REASON_ENUM:
        raise FormalDataError("invalid R2 verdict", {
            "schema": "d9-c6-c3-gate-v1", "gate": "verdict_enum",
            "blinded_review_id": bid, "verdict": record["verdict"]})
    if not isinstance(record["reasons"], list) or not record["reasons"] or \
            not set(record["reasons"]) <= set(REASON_ENUM[record["verdict"]]):
        raise FormalDataError("invalid R2 reason enum", {
            "schema": "d9-c6-c3-gate-v1", "gate": "reason_enum",
            "blinded_review_id": bid, "reasons": record["reasons"]})
    if not isinstance(record["evidence_note"], str) or len(record["evidence_note"].strip()) < 10:
        raise FormalDataError("R2 evidence note missing", {
            "schema": "d9-c6-c3-gate-v1", "gate": "evidence_note", "blinded_review_id": bid})
    if record["packet_sha256"] is None:
        raise FormalDataError("verdict does not bind to a frozen packet", {
            "schema": "d9-c6-c3-gate-v1", "gate": "packet_binding", "blinded_review_id": bid})
    content_blob = json.dumps({"verdict": record["verdict"], "reasons": record["reasons"],
                               "evidence_note": record["evidence_note"]}, sort_keys=True).lower()
    for category, tokens in LEAK_TOKENS.items():
        for token in tokens:
            if token in content_blob:
                raise FormalDataError("R2 verdict leakage", {
                    "schema": "d9-c6-c3-gate-v1", "gate": "leak_scan",
                    "blinded_review_id": bid, "category": category, "token": token})
    for token in VERDICT_IDENTITY_TOKENS:
        if token in content_blob:
            raise FormalDataError("R2 verdict identity leakage", {
                "schema": "d9-c6-c3-gate-v1", "gate": "leak_scan",
                "blinded_review_id": bid, "token": token})
    return record


def freeze_contract(output: Path) -> None:
    path = output / "review" / "r2" / "r2-review-contract.json"
    if path.exists():
        raise FormalDataError("R2 review contract already exists", {
            "schema": "d9-c6-c3-gate-v1", "gate": "contract_fresh", "path": str(path)})
    _r1_digests_unchanged(output)
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
                "r1_isolation": {"r1_contract_sha256": R1_CONTRACT_SHA256,
                                 "r1_verdict_set_sha256": R1_VERDICT_SET_SHA256,
                                 "note": "R2 receives no R1 verdict, reason, distribution, or artifact"},
                "reviewer_config": REVIEWER_CONFIG,
                "created_at": now_iso()}
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(path, contract)
    print(json.dumps({"mode": "R2_CONTRACT_FROZEN", "contract_sha256": digest(contract),
                      "packet_set_sha256": PACKET_SET_SHA256, "path": str(path)}))


def ingest_verdicts(output: Path, verdicts_path: Path) -> None:
    verdicts_root = output / "review" / "r2" / "verdicts"
    if verdicts_root.exists():
        raise FormalDataError("R2 verdict namespace already exists", {
            "schema": "d9-c6-c3-gate-v1", "gate": "verdicts_fresh", "path": str(verdicts_root)})
    _r1_digests_unchanged(output)
    packet_hashes = load_packet_hashes(output)
    verify_packets_unchanged(output, packet_hashes)
    raw_lines = [line for line in verdicts_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    parsed = []
    for index, line in enumerate(raw_lines):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FormalDataError("verdict line is not JSON", {
                "schema": "d9-c6-c3-gate-v1", "gate": "parse", "line": index + 1, "error": str(exc)})
        bid = raw.get("blinded_review_id")
        if bid not in packet_hashes:
            raise FormalDataError("verdict references unknown blinded id", {
                "schema": "d9-c6-c3-gate-v1", "gate": "blinded_id", "line": index + 1, "bid": bid})
        parsed.append(validate_verdict(raw, bid, packet_hashes))
    bids = [r["blinded_review_id"] for r in parsed]
    if len(parsed) != 36 or len(set(bids)) != 36 or set(bids) != set(packet_hashes):
        raise FormalDataError("verdict set is not exactly the frozen packet set", {
            "schema": "d9-c6-c3-gate-v1", "gate": "coverage", "verdicts": len(parsed),
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
              "reviewer_role": "R2", "rubric_version": RUBRIC_VERSION,
              "contract": str(output / "review" / "r2" / "r2-review-contract.json"),
              "packet_set_sha256": PACKET_SET_SHA256,
              "sealed_mapping_sha256": SEALED_MAPPING_SHA256,
              "sealed_mapping_consumed": False,
              "r1_isolation": {"r1_verdict_set_sha256": R1_VERDICT_SET_SHA256},
              "reviewer_config": REVIEWER_CONFIG,
              "verdict_count": len(verdict_hashes), "unique_blinded_ids": len(verdict_hashes),
              "verdict_hashes": verdict_hashes,
              "verdict_set_sha256": digest(verdict_hashes),
              "verdict_distribution": {v: sum(1 for r in verdicts if r["verdict"] == v)
                                       for v in ("YES", "NO")},
              "reviewer_invocation_attempts_total": len(parsed),
              "frozen_at": now_iso()}
    _atomic_json(output / "review" / "r2" / "r2-verdict-set-freeze.json", freeze)
    print(json.dumps({"mode": "R2_VERDICTS_FROZEN", "verdicts": len(verdict_hashes),
                      "verdict_set_sha256": freeze["verdict_set_sha256"],
                      "verdict_distribution": freeze["verdict_distribution"],
                      "agent_invocation_count": 0, "formal_reruns": 0,
                      "r3_adjudications": 0, "sealed_mapping_consumed": False}))


def agreement(output: Path) -> None:
    """Mechanical R1/R2 per-blinded-id agreement statistic only."""
    r1 = {p.stem: json.loads(p.read_text(encoding="utf-8"))["verdict"]
          for p in (output / "review" / "r1" / "verdicts").glob("blind-*.json")}
    r2 = {p.stem: json.loads(p.read_text(encoding="utf-8"))["verdict"]
          for p in (output / "review" / "r2" / "verdicts").glob("blind-*.json")}
    if set(r1) != set(r2) or len(r1) != 36:
        raise FormalDataError("verdict sets do not cover the same packet set", {
            "schema": "d9-c6-c3-gate-v1", "gate": "agreement_inputs"})
    agree = sorted(b for b in r1 if r1[b] == r2[b])
    disagree = sorted(b for b in r1 if r1[b] != r2[b])
    result = {"schema": "d9-c6-c3-agreement-v1", "agreement_count": len(agree),
              "disagreement_count": len(disagree), "agreement_blinded_ids": agree,
              "disagreement_blinded_ids": disagree,
              "state": "READY_FOR_R3" if disagree else "R3_NOT_REQUIRED / READY_FOR_FINAL_UNSEAL",
              "note": "mechanical statistic only; no R3 executed, mapping still sealed"}
    path = output / "review" / "agreement-statistic.json"
    if path.exists():
        raise FormalDataError("agreement statistic already exists", {
            "schema": "d9-c6-c3-gate-v1", "path": str(path)})
    _atomic_json(path, result)
    print(json.dumps(result))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D9-C6-C3 R2 independent blind review")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--freeze-contract", action="store_true")
    parser.add_argument("--ingest-verdicts", type=Path)
    parser.add_argument("--agreement", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.freeze_contract:
        freeze_contract(args.output)
    elif args.ingest_verdicts:
        ingest_verdicts(args.output, args.ingest_verdicts)
    elif args.agreement:
        agreement(args.output)
    else:
        raise FormalDataError("one mode is required", {"schema": "d9-c6-c3-gate-v1"})


if __name__ == "__main__":
    try:
        main()
    except FormalDataError as exc:
        print(json.dumps({"state": "D9_C6_C3_FAILED", "message": str(exc),
                          "diagnostic": exc.diagnostic}), file=sys.stderr)
        raise SystemExit(2)
