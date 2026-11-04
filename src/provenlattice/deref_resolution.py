"""S7-F2: restricted query-time ``DEREF_RESOLVES_TO`` derivation (Issue #48).

SUB-07 step 2 consumes S7-F1's ``thread_argument_decl_scoped`` (produced on
THREAD_SPAWN, contract §1.3) together with the existing ``alias`` /
``alias_source`` facts to resolve a *thread-entry* dereference (``*f``) back to
the storage object whose address was passed to ``pthread_create``.

The derivation is strictly three-state (contract §5.11.1 / §1.5 / §1.6):

* ``must``    — provable identity chain only: ``form == address_of`` AND the
  entry function has a single parameter AND the dereferenced pointer is an
  unconditional, unique frame-local bare copy of that parameter AND there is no
  runtime instance ambiguity (exactly one distinct scoped identity per entry
  function).
* ``may``     — any weaker evidence (address form missing, conditional or
  multi-source copy, multiple distinct spawns, or an unresolvable parameter
  name). Never upgraded to ``must``.
* ``unknown`` — no evidence at all (no spawn for the function, or the passed
  declaration is unresolved/empty).

``same_storage=true`` is emitted only for cross-owner dereference pairs that
BOTH resolve with ``must`` to the same scoped identity (contract §1.6); a
``may``/``unknown`` resolution is never treated as ``false`` and never promotes
to ``true``.

Default OFF (contract §1.8): with ``enable=False`` the function returns an empty
derivation, so the pre-existing envelope (and FIX-05's 0-candidate behavior) is
byte-for-byte unchanged.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

_MUST = "must"
_MAY = "may"
_UNKNOWN = "unknown"


def _annotations(event: Mapping[str, Any]) -> Mapping[str, Any]:
    return event.get("annotations") or {}


def _sole_param_name(signature: str | None) -> str | None:
    """The single parameter name of a function signature, or ``None``.

    A pthread thread entry is always ``void *(*)(void *)``, so a well-formed
    signature has exactly one parameter. ``None`` means "not a single-argument
    entry" and forces a ``may`` downgrade rather than a guessed resolution.
    """
    if not signature:
        return None
    inner = signature.strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    inner = inner.strip()
    if not inner:
        return None
    parts = [part.strip() for part in inner.split(",")]
    if len(parts) != 1:
        return None
    token = parts[0].split()[-1] if parts[0].split() else ""
    return token.lstrip("*") or None


def _resolve_deref(
    event: Mapping[str, Any],
    alias: str,
    spawns: list[Mapping[str, str]],
    writes: list[Mapping[str, str]],
) -> tuple[str, str]:
    """Return ``(confidence, scoped_identity)`` for one DEREFERENCE event."""
    if not spawns:
        return (_UNKNOWN, "")  # not a known thread entry: no chain to resolve.

    scoped_ids = {spawn["scoped"] for spawn in spawns}
    forms = {spawn["form"] for spawn in spawns}
    if len(scoped_ids) != 1:
        return (_MAY, "")  # multiple distinct bindings: runtime ambiguity.
    scoped = next(iter(scoped_ids))
    if not scoped:
        return (_UNKNOWN, "")  # passed declaration is unresolved/undeclared.

    if forms != {"address_of"}:
        return (_MAY, scoped)  # not a provable address pass (e.g. pointer_var).

    # Single-argument entry: the dereferenced pointer's alias must be the
    # function's sole parameter, and that pointer must be an unconditional,
    # unique bare copy of the parameter (contract §1.5 frame-local chain).
    param = _sole_param_name(event.get("owner_signature"))
    if param is None or alias != param:
        return (_MAY, scoped)
    if not writes or {write["write_source"] for write in writes} != {alias}:
        return (_MAY, scoped)  # not a unique bare copy: conditional/multi-source.
    if any(write["unconditional"] != "true" for write in writes):
        return (_MAY, scoped)

    return (_MUST, scoped)


def _side(record: Mapping[str, Any]) -> dict[str, Any]:
    """One side of a same-storage dereference pair."""
    return {
        "access_event_id": record["access_event_id"],
        "event_type": "DEREFERENCE",
        "owner": record["owner"],
        "expression": record["expression"],
        "start_line": record["start_line"],
        "deref_resolves_to": record["deref_resolves_to"],
        "confidence": record["confidence"],
    }


def build_deref_resolution(
    events: Iterable[Mapping[str, Any]], *, enable: bool = False,
) -> dict[str, Any]:
    """Derive query-time ``DEREF_RESOLVES_TO`` and must-only ``same_storage``.

    Args:
        events: sparse_cfg events for a scope (as returned by ``query_analysis``).
        enable: opt-in switch. When false (default) nothing is derived and the
            result is an empty envelope (contract §1.8 default-OFF).
    """
    result: dict[str, Any] = {
        "deref_resolves_to": [],
        "same_storage": [],
        "coverage": {
            "dereferences": 0,
            "resolved_must": 0,
            "resolved_may": 0,
            "resolved_unknown": 0,
            "same_storage_pairs": 0,
        },
    }
    if not enable:
        return result

    events = list(events)
    spawns_by_function: dict[str, list[dict[str, str]]] = {}
    writes_by_decl: dict[str, list[dict[str, str]]] = {}
    for event in events:
        event_type = event.get("event_type")
        annotations = _annotations(event)
        if event_type == "THREAD_SPAWN":
            function = annotations.get("thread_function", "")
            if function:
                spawns_by_function.setdefault(function, []).append({
                    "scoped": annotations.get("thread_argument_decl_scoped", ""),
                    "form": annotations.get("thread_argument_form", ""),
                })
        elif event_type == "WRITE":
            decl = annotations.get("subject_decl", "")
            if decl:
                writes_by_decl.setdefault(decl, []).append({
                    "write_source": annotations.get("write_source", ""),
                    "unconditional": annotations.get("unconditional", ""),
                })

    resolved: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") != "DEREFERENCE":
            continue
        annotations = _annotations(event)
        owner = event.get("owner_qualified_name") or ""
        alias = annotations.get("alias", "") or ""
        decl = annotations.get("subject_decl", "") or ""
        confidence, scoped = _resolve_deref(
            event, alias,
            spawns_by_function.get(owner, []),
            writes_by_decl.get(decl, []),
        )
        resolved.append({
            "access_event_id": event.get("event_id"),
            "owner": owner,
            "expression": annotations.get("subject")
                          or event.get("matched_name") or "",
            "start_line": int(event.get("start_line") or 0),
            "deref_resolves_to": scoped,
            "confidence": confidence,
        })
        result["coverage"][f"resolved_{confidence}"] += 1
        result["coverage"]["dereferences"] += 1

    result["deref_resolves_to"] = resolved

    # same_storage=true only for cross-owner pairs that both MUST-resolve to the
    # same scoped identity (contract §1.6).
    by_target: dict[str, list[dict[str, Any]]] = {}
    for record in resolved:
        if record["confidence"] == _MUST and record["deref_resolves_to"]:
            by_target.setdefault(record["deref_resolves_to"], []).append(record)

    same_storage: list[dict[str, Any]] = []
    for target in sorted(by_target):
        entries = by_target[target]
        for index in range(len(entries)):
            for other in range(index + 1, len(entries)):
                left, right = entries[index], entries[other]
                if left["owner"] == right["owner"]:
                    continue  # same method is not a cross-thread competition.
                same_storage.append({
                    "storage_object_id": target,
                    "same_storage": "true",
                    "deref_resolves_to": target,
                    "accesses": [_side(left), _side(right)],
                })
    result["same_storage"] = same_storage
    result["coverage"]["same_storage_pairs"] = len(same_storage)
    return result


__all__ = ["build_deref_resolution"]