"""T01 candidate ``trace_evidence`` chain assembler (R9, additive).

Pure assembly only: it reuses fields that already exist on a concurrency
candidate (``related_accesses`` / ``related_locks`` / ``evidence_references`` /
``missing_evidence`` / ``candidate_state``) plus the candidate's own
``evidence_id``, and never adds analysis, new facts or a decision. A step that
would require B/C facts (lockset, points-to, MAY_CALL, control flow) is emitted
as a ``limitation`` step, never as a proven ``fact``.

Nothing here changes the default index, ``race_condition`` / ``lock_order``
default behavior, the CLI/MCP envelope or the Candidate/Evidence field contract.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

STEP_ACCESS = "access"
STEP_LOCK = "lock"
STEP_FLOW = "flow"
STEP_CALL = "call"
STEP_LIMITATION = "limitation"

GROUND_FACT = "fact"
GROUND_LIMITATION = "limitation"

_STEP_TYPES = frozenset(
    {STEP_ACCESS, STEP_LOCK, STEP_FLOW, STEP_CALL, STEP_LIMITATION}
)


def _step(
    step: int,
    step_type: str,
    from_loc: Mapping[str, Any] | None,
    to_loc: Mapping[str, Any] | None,
    label: str,
    refs: Sequence[str],
    ground: str,
) -> dict[str, Any]:
    return {
        "step": step,
        "step_type": step_type,
        "from_loc": dict(from_loc) if from_loc else None,
        "to_loc": dict(to_loc) if to_loc else None,
        "label": label,
        "refs": [ref for ref in refs if ref],
        "ground": ground,
    }


def _file(event_id: Any, event_paths: Mapping[str, str] | None) -> str | None:
    return event_paths.get(event_id) if event_paths and event_id else None


def _access_step(
    step: int, item: Mapping[str, Any], subject: str, storage: str, exact: Any,
    event_paths: Mapping[str, str] | None,
) -> dict[str, Any]:
    event_id = item.get("event_id")
    event_type = item.get("event_type") or ""
    line = int(item.get("start_line") or 0)
    return _step(
        step, STEP_ACCESS,
        {"file": _file(event_id, event_paths), "line": line},
        None,
        f"{event_type} fact on {subject or '(unresolved)'} "
        f"(storage={storage}, exact={exact})",
        [event_id or ""],
        GROUND_FACT,
    )


def _lock_step(
    step: int, item: Mapping[str, Any], event_paths: Mapping[str, str] | None,
) -> dict[str, Any]:
    event_id = item.get("event_id")
    event_type = item.get("event_type") or ""
    identity = item.get("lock_identity") or item.get("matched_name") or ""
    acquire_kind = item.get("acquire_kind") or ""
    raii = bool(item.get("raii"))
    line = int(item.get("start_line") or 0)
    return _step(
        step, STEP_LOCK,
        {"file": _file(event_id, event_paths), "line": line},
        None,
        f"{event_type} fact on {identity or '(unresolved lock)'} "
        f"(acquire={acquire_kind}, raii={raii})",
        [event_id or ""],
        GROUND_FACT,
    )


def _lock_identity_step(step: int, name: str) -> dict[str, Any]:
    return _step(
        step, STEP_LOCK, None, None,
        f"lock identity {name}", [], GROUND_FACT,
    )


def _limitation_step(step: int, name: str) -> dict[str, Any]:
    return _step(
        step, STEP_LIMITATION, None, None,
        f"{name} not proven", [], GROUND_LIMITATION,
    )


def _access_sort_key(item: Mapping[str, Any]) -> tuple:
    return (
        int(item.get("start_line") or 0),
        int(item.get("end_line") or 0),
        str(item.get("event_id") or ""),
    )


def _lock_sort_key(item: Any) -> tuple:
    if isinstance(item, Mapping):
        identity = item.get("lock_identity") or item.get("matched_name") or ""
        return (0, identity, int(item.get("start_line") or 0),
                str(item.get("event_id") or ""))
    return (1, str(item), 0, "")


def assemble_trace_chain(
    candidate: Mapping[str, Any],
    evidence_id: str,
    event_paths: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Assemble an ordered ``trace_evidence`` chain for one candidate.

    The chain opens with the candidate's access facts (if any), then its lock
    facts, and always closes on one ``limitation`` step per ``missing_evidence``
    name (lexicographic order, per the R9 contract). ``refs`` only ever cite the
    candidate's own ``evidence_references`` / ``related_accesses`` /
    ``related_locks`` event ids and the candidate's own ``evidence_id``.

    ``evidence_id`` is the candidate's ``E-DEFECT-*`` id and is cited on the
    first fact step so the chain traces back to exactly one candidate through to
    the events it cites.
    """
    subject = str(candidate.get("subject") or "")
    storage = str(candidate.get("storage") or "")
    exact = candidate.get("exact")

    steps: list[dict[str, Any]] = []
    num = 1

    accesses = sorted(
        (item for item in (candidate.get("related_accesses") or [])
         if isinstance(item, Mapping)),
        key=_access_sort_key,
    )
    for item in accesses:
        steps.append(_access_step(num, item, subject, storage, exact, event_paths))
        num += 1

    locks = sorted(candidate.get("related_locks") or [], key=_lock_sort_key)
    for item in locks:
        if isinstance(item, Mapping):
            steps.append(_lock_step(num, item, event_paths))
        else:
            steps.append(_lock_identity_step(num, str(item)))
        num += 1

    missing = sorted(
        str(name) for name in (candidate.get("missing_evidence") or [])
        if str(name)
    )
    for name in missing:
        steps.append(_limitation_step(num, name))
        num += 1

    if not steps:
        steps.append(_limitation_step(1, "no_evidence"))

    if steps and evidence_id:
        steps[0]["refs"] = [*steps[0]["refs"], evidence_id]

    return steps


__all__ = ["assemble_trace_chain", "STEP_ACCESS", "STEP_LOCK", "STEP_FLOW",
           "STEP_CALL", "STEP_LIMITATION", "GROUND_FACT", "GROUND_LIMITATION"]