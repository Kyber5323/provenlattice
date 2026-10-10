"""D9-C9-C1 normalization contract v2 for real task-context-v0 payloads.

Fixes the schema mismatch where the frozen C5-C ``normalize_trace`` only read a
top-level ``facts`` array while the real ProvenLattice task-context-v0 runtime
payload carries fact-bearing entities under ``primary_targets`` /
``source_spans`` / ``relations`` / ``candidates``.

Future-run measurement layer only: the frozen ``normalize_trace`` and every
historical artifact remain untouched.  Mapping is schema/category-based — no
case-specific or fact-specific special-casing; no new semantic fact_ids are
generated.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from formal_harness import FormalDataError, normalize_trace

NORMALIZATION_CONTRACT = "d9-c9-c1-normalization-v2"
TASK_CONTEXT_VERSION = "task-context-v0"

# Fact-bearing sections of the real task-context-v0 schema, in deterministic
# extraction order.  ``expansion_handles`` is deliberately excluded: its items
# carry only symbol_id/command (no stable fact_id) and are handles for further
# queries, not returned facts.
SECTION_ORDER = ("primary_targets", "source_spans", "relations", "candidates")
SECTION_FACT_KIND = {
    "primary_targets": "primary_target",
    "source_spans": "source_span",
    "relations": "relation",
    "candidates": "candidate",
}
KNOWN_NON_AVAILABLE_STATES = ("not_built", "not_found")


def _payload_provenance(payload: dict[str, Any]) -> dict[str, Any]:
    return {"anchor": payload.get("anchor"),
            "status": payload.get("status"),
            "repository": payload.get("repository"),
            "base_commit": payload.get("base_commit")}


def extract_task_context_facts_v2(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract returned facts from task-context-v0 payloads per the v2 contract.

    Returns ``(facts, parse_states)``.  Fail-closed: nothing that is not an
    ``available`` task-context-v0 payload with fact-bearing section items ever
    produces a fact; malformed/unparsed results are recorded as explicit
    states, never fabricated into facts.
    """
    facts: list[dict[str, Any]] = []
    states: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}

    def add_fact(fid: str, section: str, item: dict[str, Any],
                 payload: dict[str, Any], sequence: int) -> None:
        if fid in seen:
            if section not in seen[fid]["sections"]:
                seen[fid]["sections"].append(section)
            return
        evidence = item.get("evidence_ids")
        fact = {"fact_id": fid,
                "fact_kind": "legacy_fact" if section == "facts" else SECTION_FACT_KIND[section],
                "source_section": section,
                "sections": [section],
                "evidence_ids": [str(e) for e in evidence] if isinstance(evidence, list) else [],
                "item": deepcopy(item),
                "payload_provenance": _payload_provenance(payload),
                "returned_sequence": sequence}
        seen[fid] = fact
        facts.append(fact)

    for event in events:
        sequence = event.get("sequence")
        if event.get("kind") != "tool_result":
            continue
        detail = event.get("detail") or {}
        payload = detail.get("result")
        if isinstance(payload, str):
            if TASK_CONTEXT_VERSION in payload:
                states.append({"sequence": sequence,
                               "state": "unparsed_text_result_not_ingested",
                               "is_task_context_shaped_text": True})
            continue
        if not isinstance(payload, dict):
            continue
        version = payload.get("version")
        if version is None:
            continue  # not a versioned payload; not a normalization input
        if version != TASK_CONTEXT_VERSION:
            states.append({"sequence": sequence, "version": version,
                           "state": "unknown_version_not_ingested"})
            continue
        if payload.get("control") is True or payload.get("status") == "control_no_facts":
            # D9-C10 control response: explicitly marked, never a treatment fact
            states.append({"sequence": sequence, "version": version,
                           "status": payload.get("status"),
                           "state": "control_no_facts", "facts_extracted": 0})
            continue
        status = payload.get("status")
        if status in KNOWN_NON_AVAILABLE_STATES:
            states.append({"sequence": sequence, "version": version, "status": status,
                           "state": f"status_{status}_no_facts", "facts_extracted": 0})
            continue
        if status != "available":
            states.append({"sequence": sequence, "version": version, "status": status,
                           "state": "status_unavailable_no_facts", "facts_extracted": 0})
            continue
        extracted = 0
        # legacy top-level facts[] first (backward-compatible precedence)
        for item in payload.get("facts") or []:
            if not isinstance(item, dict) or not isinstance(item.get("fact_id"), str) \
                    or not item["fact_id"].strip():
                raise FormalDataError("returned fact lacks stable identity", {
                    "schema": "d9-c9-c1-normalization-v2", "sequence": sequence,
                    "section": "facts"})
            add_fact(item["fact_id"], "facts", item, payload, sequence)
            extracted += 1
        for section in SECTION_ORDER:
            for item in payload.get(section) or []:
                if not isinstance(item, dict):
                    continue
                fid = item.get("fact_id")
                if not isinstance(fid, str) or not fid.strip():
                    continue  # section items without a stable fact_id are not facts
                add_fact(fid, section, item, payload, sequence)
                extracted += 1
        states.append({"sequence": sequence, "version": version, "status": status,
                       "state": "available_facts_extracted", "facts_extracted": extracted})
    return facts, states


def normalize_trace_v2(cell: dict[str, Any], raw_trace_pointer: str, events: list[dict[str, Any]],
                       final_patch: str, final_result: str) -> dict[str, Any]:
    """C5-C normalization with the v2 task-context fact contract.

    The legacy ``normalize_trace`` behavior (ordered events, tool counts, the
    strict legacy facts[] path and its fail-closed identity check) is preserved
    verbatim; ``returned_facts`` is then computed by the v2 extraction so real
    runtime payloads are ingested.  Consumers of the historical shape
    (``fact_id``/``payload``/``returned_sequence``) remain compatible.
    """
    trace = normalize_trace(cell, raw_trace_pointer, events, final_patch, final_result)
    facts, states = extract_task_context_facts_v2(events)
    trace["returned_facts"] = [{"fact_id": f["fact_id"],
                                "payload": deepcopy(f),
                                "returned_sequence": f["returned_sequence"]} for f in facts]
    trace["returned_fact_count"] = len(facts)
    trace["task_context_parse_states"] = states
    trace["normalization_contract"] = NORMALIZATION_CONTRACT
    return trace
