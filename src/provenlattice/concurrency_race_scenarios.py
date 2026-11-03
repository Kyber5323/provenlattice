"""S4 race scenario candidates: non_atomic_incdec / bool_flag_race / shared_state_race.

Consumes S1's access facts (READ/WRITE with a stable ``subject_decl`` identity
and the read-modify-write read-half) and S2's ``concurrency_context``
(MAY_PARALLEL candidates over shared subjects) to emit Candidate rules for the
three race scenarios.

Every candidate here is an evidence-resolution state only -- never a verified
finding. MAY_PARALLEL stays ambiguous, missing lock/atomic/identity evidence is
named, and an atomic access stays ``synchronized`` rather than becoming a
non-atomic race.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .analysis_concurrency_context import build_concurrency_context
from .models import (
    STORAGE_FIELD,
    STORAGE_FILE_STATIC,
    STORAGE_FUNCTION_STATIC,
    STORAGE_GLOBAL,
    STORAGE_UNKNOWN,
)

_READ = "READ"
_WRITE = "WRITE"
_ATOMIC = "ATOMIC"
_BOOL_TYPES = frozenset({"bool", "_Bool"})
_SHARED_STORAGES = frozenset({
    STORAGE_FIELD, STORAGE_FILE_STATIC, STORAGE_FUNCTION_STATIC,
    STORAGE_GLOBAL, STORAGE_UNKNOWN,
})


def _annotations(event: Mapping[str, Any]) -> Mapping[str, Any]:
    return event.get("annotations") or {}


def _owner(event: Mapping[str, Any]) -> str:
    return event.get("owner_qualified_name") or event.get("owner_symbol_id") or ""


def _span(access: Mapping[str, Any]) -> tuple[int, int]:
    return (int(access.get("start_line") or 0), int(access.get("end_line") or 0))


def _identity_key(decl: str, subject: str) -> str:
    return decl if decl else f"unresolved\x1f{subject}"


def _rmw_spans(accesses: Iterable[Mapping[str, Any]]) -> list[list[int]]:
    """Source lines of first-class read-modify-write accesses.

    S1 publishes ``access_kind == READ_MODIFY_WRITE`` on the write half of an
    update/compound assignment (`x++`, `x += y`, `--x`), so a read-modify-write
    is a fact read off the access record rather than an approximation from a
    read/write line overlap. A plain `x = x + 1` writes (WRITE) and reads
    (READ) but never publishes a READ_MODIFY_WRITE kind, so it is not an RMW.
    """
    return sorted(
        [*_span(access)]
        for access in accesses
        if access.get("access_kind") == "READ_MODIFY_WRITE"
    )


def build_race_scenarios(
    events: Iterable[Mapping[str, Any]], context: Mapping[str, Any] | None = None,
) -> dict:
    """Build race candidates from sparse_cfg events.

    Returns ``{"candidates": [...], "coverage": {...}}``. Each candidate carries
    the unified scenario fields (``scenario_id``, ``source_spans``,
    ``related_accesses``, ``candidate_state``, ``evidence_references``,
    ``missing_evidence``) plus the subject identity and owners. The output is
    deterministic: subjects are sorted by identity and scenarios by id.
    """
    events = list(events)
    context = context if context is not None else build_concurrency_context(events)

    # Access facts keyed by stable identity, with their real line spans and type.
    access_by_identity: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if event.get("event_type") not in (_READ, _WRITE):
            continue
        annotations = _annotations(event)
        decl = annotations.get("subject_decl", "")
        subject = annotations.get("subject", "")
        storage = annotations.get("subject_storage") or STORAGE_UNKNOWN
        if storage not in _SHARED_STORAGES:
            continue
        if not decl and not subject:
            continue
        access_by_identity.setdefault(_identity_key(decl, subject), []).append({
            "event_id": event.get("event_id"),
            "event_type": event.get("event_type"),
            "access_kind": annotations.get("access_kind", ""),
            "is_write": annotations.get("is_write", "false"),
            "owner": _owner(event),
            "start_line": int(event.get("start_line") or 0),
            "end_line": int(event.get("end_line") or 0),
            "exact": annotations.get("subject_exact", "false"),
            "subject_type": annotations.get("subject_type", ""),
        })

    # Subjects synchronized by an atomic operation are not non-atomic races.
    atomic_subjects: set[str] = set()
    for event in events:
        if event.get("event_type") != _ATOMIC:
            continue
        annotations = _annotations(event)
        key = _identity_key(
            annotations.get("subject_decl", ""), annotations.get("subject", ""))
        if key != "unresolved\x1f":
            atomic_subjects.add(key)

    candidates: list[dict[str, Any]] = []
    for subject in context["may_parallel"]:
        key = _identity_key(subject["identity"] or "", subject["subject"])
        accesses = access_by_identity.get(key)
        if not accesses:
            continue
        if key in atomic_subjects:
            continue

        types = {access["subject_type"] for access in accesses} - {""}
        is_bool = bool(types & _BOOL_TYPES)
        has_read = any(access["event_type"] == _READ for access in accesses)
        has_write = any(access["event_type"] == _WRITE for access in accesses)
        rmw_spans = _rmw_spans(accesses)

        candidate_state = "unresolved" if subject["unresolved"] else "ambiguous"
        missing_evidence = ["may_parallel", "lock_protection"]
        if subject["unresolved"]:
            missing_evidence.insert(0, "identity_unresolved")

        base = {
            "subject": subject["subject"],
            "identity": subject["identity"],
            "storage": subject["storage"],
            "exact": subject["exact"],
            "unresolved": subject["unresolved"],
            "owners": subject["owners"],
            "thread_entry_owners": subject["thread_entry_owners"],
            "source_spans": [[*_span(access)] for access in accesses],
            "related_accesses": accesses,
            "evidence_references": [access["event_id"] for access in accesses],
            "missing_evidence": missing_evidence,
        }

        # General shared-state race: any shared subject with an access.
        candidates.append({
            **base, "scenario_id": "shared_state_race",
            "candidate_state": candidate_state,
        })
        # read-modify-write update: the two sides share a line.
        if rmw_spans:
            candidates.append({
                **base, "scenario_id": "non_atomic_incdec",
                "candidate_state": candidate_state, "rmw_spans": rmw_spans,
            })
        # bool flag toggle: a bool-typed subject that is read and written.
        if is_bool and has_read and has_write:
            candidates.append({
                **base, "scenario_id": "bool_flag_race",
                "candidate_state": candidate_state,
            })

    scenario_order = ("non_atomic_incdec", "bool_flag_race", "shared_state_race")
    return {
        "candidates": candidates,
        "coverage": {
            "shared_subjects": len(context["may_parallel"]),
            "scenarios": {
                scenario: sum(
                    1 for candidate in candidates
                    if candidate["scenario_id"] == scenario
                )
                for scenario in scenario_order
            },
        },
    }


__all__ = ["build_race_scenarios"]