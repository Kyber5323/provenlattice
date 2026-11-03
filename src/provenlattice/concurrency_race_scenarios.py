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
from .concurrency_lock_scenarios import build_access_lockset
from .defect import RACE_CONFIDENCE
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

# SUB-05 (R5): the default ranking cutoff and the hard ceiling that bounds a
# competing-access tie set. The band values themselves live in ``defect.py``
# (``RACE_CONFIDENCE``) and are consumed, never re-declared here.
DEFAULT_TOP_K = 3
_MAX_COMPETING_ACCESS = 100


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


def _is_write(access: Mapping[str, Any]) -> bool:
    """Whether one access is a write, read off SUB-02's ``is_write`` fact."""
    return access.get("is_write") == "true"


def _same_storage(identity: str | None) -> str:
    """The three-state ``same_storage`` answer for a within-subject pair.

    A pair is built inside one MAY_PARALLEL subject, so its two accesses already
    share the subject's storage identity; the question reduces to whether that
    identity is resolved. A declared identity provably names one storage object
    (``"true"``); an unresolved identity cannot prove sameness (``"unknown"``)
    and is never upgraded. Two different declared identities never share a
    subject, so ``"false"`` is a state the schema keeps but this builder does
    not fabricate.
    """
    return "unknown" if not identity else "true"


def _competing_sort_key(pair: Mapping[str, Any]) -> tuple:
    """Deterministic ranking key: confidence desc, then identity + event ids.

    Confidence is a band lookup (not an estimate), so two runs of the same
    query give the same order; the trailing identity/event-id keys make the
    order total and stable under identical input.
    """
    return (
        -float(pair["confidence"]),
        pair["identity"] or "",
        pair["subject"] or "",
        pair["accesses"][0]["access_event_id"] or "",
        pair["accesses"][1]["access_event_id"] or "",
    )


def _limit_top_k(
    pairs: list[dict[str, Any]], top_k: int | None, hard_cap: int,
) -> list[dict[str, Any]]:
    """Keep the top-``top_k`` pairs, extending across the cutoff tie.

    `top_k` truncates ranking but must not silently drop a real candidate that
    ties with the last kept one, so every pair at the cutoff's confidence is
    retained. `hard_cap` still bounds the result; a tie set larger than the cap
    is clipped and the caller reports ``truncated``.
    """
    if top_k is not None and top_k > 0 and len(pairs) > top_k:
        cutoff = float(pairs[top_k - 1]["confidence"])
        kept: list[dict[str, Any]] = list(pairs[:top_k])
        index = top_k
        while index < len(pairs) and float(pairs[index]["confidence"]) == cutoff:
            kept.append(pairs[index])
            index += 1
        pairs = kept
    if len(pairs) > hard_cap:
        pairs = list(pairs[:hard_cap])
    return pairs


def _build_competing_pairs(
    subject: Mapping[str, Any],
    accesses: list[dict[str, Any]],
    held_by_event: Mapping[str, list[str]],
) -> list[dict[str, Any]]:
    """Access-level competing-access pairs for one MAY_PARALLEL subject.

    Enumerates cross-owner access pairs (a pair inside one method is not a
    MAY_PARALLEL race), drops read/read and same-lock pairs, and returns the
    survivors with the six frozen fields plus a confidence band. The band key is
    the number of sides that provably hold a lock, the same ``RACE_CONFIDENCE``
    lookup ``defect.py``'s race query uses (0 -> 0.5, 1 -> 0.4, 2 -> 0.3).
    """
    ordered = sorted(
        accesses,
        key=lambda access: (
            access["start_line"], access["end_line"], access["event_id"] or "",
        ),
    )
    pairs: list[dict[str, Any]] = []
    for index in range(len(ordered)):
        for other in range(index + 1, len(ordered)):
            left, right = ordered[index], ordered[other]
            if left["owner"] == right["owner"]:
                continue  # same method: not a MAY_PARALLEL pair.
            if not (_is_write(left) or _is_write(right)):
                continue  # read/read never competes.
            held_left = held_by_event.get(left["event_id"], [])
            held_right = held_by_event.get(right["event_id"], [])
            if set(held_left) & set(held_right):
                continue  # a common lock orders the pair.
            pairs.append({
                "subject": subject["subject"],
                "identity": subject["identity"],
                "same_storage": _same_storage(subject["identity"]),
                "at_least_one_write": True,
                "may_execute_concurrently": True,
                "common_lock": False,
                "different_lock": True,
                "unprotected": not held_left or not held_right,
                "confidence": RACE_CONFIDENCE[
                    (1 if held_left else 0) + (1 if held_right else 0)],
                "candidate_state": "ambiguous",
                "accesses": [
                    _pair_side(left, held_left),
                    _pair_side(right, held_right),
                ],
            })
    return pairs


def _pair_side(
    access: Mapping[str, Any], held_locks: list[str],
) -> dict[str, Any]:
    """One side of a competing-access pair, citing SUB-02/SUB-04 facts as-is."""
    return {
        "access_event_id": access["event_id"],
        "event_type": access["event_type"],
        "access_kind": access["access_kind"],
        "is_write": _is_write(access),
        "owner": access["owner"],
        "start_line": access["start_line"],
        "source_span": [*_span(access)],
        "held_locks": list(held_locks),
    }


def build_race_scenarios(
    events: Iterable[Mapping[str, Any]], context: Mapping[str, Any] | None = None,
    *, edges: Iterable[Mapping[str, Any]] | None = None,
    lock_facts: Iterable[Mapping[str, Any]] | None = None,
    top_k: int | None = DEFAULT_TOP_K,
) -> dict:
    """Build race candidates and competing-access pairs from sparse_cfg events.

    Returns ``{"candidates": [...], "competing_access": [...], "coverage": {...}}``.
    Each candidate carries the unified scenario fields (``scenario_id``,
    ``source_spans``, ``related_accesses``, ``candidate_state``,
    ``evidence_references``, ``missing_evidence``) plus the subject identity and
    owners. The output is deterministic: subjects are sorted by identity and
    scenarios by id.

    When both ``edges`` (the sparse-cfg ``CONTROL_REACHES`` rows) and
    ``lock_facts`` (the events query's lock rows) are supplied, the SUB-04
    per-access ``held_locks`` projection is consumed to emit access-level
    ``competing_access`` pairs (SUB-05/R5): read/read and same-lock pairs are
    excluded, the survivors carry the six frozen fields plus a confidence band,
    and are ranked by that band descending with a tie-preserving ``top_k`` cut.
    Without lock facts no held-lockset can be derived, so ``competing_access``
    stays empty rather than fabricating an "unprotected" answer.
    """
    events = list(events)
    context = context if context is not None else build_concurrency_context(events)

    # SUB-04 held-locksets are consumed only when their inputs are present; the
    # projection is never re-derived here and never guessed into existence.
    have_lockset = edges is not None and lock_facts is not None
    held_by_event: dict[str, list[str]] = {}
    if have_lockset:
        lockset = build_access_lockset(events, edges, lock_facts)
        for record in lockset["access_locksets"]:
            held_by_event[record["access_event_id"]] = list(record["held_locks"])

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
    competing: list[dict[str, Any]] = []
    for subject in context["may_parallel"]:
        key = _identity_key(subject["identity"] or "", subject["subject"])
        accesses = access_by_identity.get(key)
        if not accesses:
            continue
        if key in atomic_subjects:
            continue

        if have_lockset:
            competing.extend(
                _build_competing_pairs(subject, accesses, held_by_event))

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
    competing.sort(key=_competing_sort_key)
    competing_total = len(competing)
    competing = _limit_top_k(competing, top_k, _MAX_COMPETING_ACCESS)
    return {
        "candidates": candidates,
        "competing_access": competing,
        "coverage": {
            "shared_subjects": len(context["may_parallel"]),
            "scenarios": {
                scenario: sum(
                    1 for candidate in candidates
                    if candidate["scenario_id"] == scenario
                )
                for scenario in scenario_order
            },
            "competing_access": {
                "total": competing_total,
                "returned": len(competing),
                "truncated": len(competing) < competing_total,
                "top_k": top_k,
            },
        },
    }


__all__ = ["build_race_scenarios"]