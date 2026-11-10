"""S5 lock scenario candidates (Parent #28): lock_pairing / lock_discipline.

Consumes S3's lock facts -- LOCK/UNLOCK vocabulary events carrying
``lock_identity``, ``subject_exact``, ``acquire_kind`` and ``raii`` -- and emits
Candidate rules for the two lock scenarios. The facts are reused as-is: lock
identity, acquire kind and RAII are never re-derived here, and the events layer,
``defect.py``'s ``lock_order``, the default index and CLI/MCP are untouched.

Every candidate is an evidence-resolution state, never a verified finding. The
vocabulary layer records lock calls but no control flow, so pairing and ordering
are provably-lossy approximations:

* the lock scenarios stay ``ambiguous`` -- a decisive pairing/order cannot be
  proven without control flow, so ``missing_evidence`` always names
  ``control_flow``;
* an empty ``lock_identity`` is ``unresolved`` (cannot be attributed) and never
  merged with a different lock by display name;
* a try/timed acquire may fail and leave the lock unheld, so it is never
  promoted to a held lock in pairing or self-deadlock reasoning;
* a RAII acquisition is implicitly released at scope exit, so it never shows up
  as an unpaired/leaked acquisition;
* a ``wait`` acquire is a blocking wait (condition/semaphore), not a mutex hold,
  so it is excluded from acquire/release pairing and only participates in the
  ``blocking_inside_lock`` rule.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping

_LOCK = "LOCK"
_UNLOCK = "UNLOCK"

_ACQUIRE_BLOCKING = "blocking"
_ACQUIRE_TRY = "try"
_ACQUIRE_TIMED = "timed"
_ACQUIRE_WAIT = "wait"
_MAY_FAIL_ACQUIRES = frozenset({_ACQUIRE_TRY, _ACQUIRE_TIMED})

_SCENARIO_PAIRING = "lock_pairing"
_SCENARIO_DISCIPLINE = "lock_discipline"

_RULE_UNPAIRED_ACQUIRE = "unpaired_acquire"
_RULE_UNPAIRED_RELEASE = "unpaired_release"
_RULE_SELF_DEADLOCK = "self_deadlock"
_RULE_ORDER_INVERSION = "lock_order_inversion"
_RULE_BLOCKING_IN_LOCK = "blocking_inside_lock"

_MISSING_CONTROL_FLOW = "control_flow"
_MISSING_IDENTITY = "lock_identity_inexact"
_MISSING_TRY_TIMED_OUTCOME = "try_timed_outcome"


def _owner(event: Mapping[str, Any]) -> str:
    return event.get("owner_qualified_name") or event.get("owner_symbol_id") or ""


def _identity(event: Mapping[str, Any]) -> str:
    return event.get("lock_identity") or ""


def _byte(event: Mapping[str, Any]) -> int:
    return int(event.get("start_byte") or 0)


def _line_span(event: Mapping[str, Any]) -> list[int]:
    return [int(event.get("start_line") or 0), int(event.get("end_line") or 0)]


def _acquire_kind(event: Mapping[str, Any]) -> str:
    return event.get("acquire_kind") or _ACQUIRE_BLOCKING


def _lock_fact(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event.get("event_id"),
        "event_type": event.get("event_type"),
        "matched_name": event.get("matched_name"),
        "lock_identity": _identity(event),
        "acquire_kind": _acquire_kind(event),
        "raii": bool(event.get("raii")),
        "start_line": int(event.get("start_line") or 0),
        "end_line": int(event.get("end_line") or 0),
    }


def _base_candidate(
    scenario_id: str, rule: str, identity: str, exact: bool, unresolved: bool,
    owner: str, locks: list[Mapping[str, Any]], missing: list[str],
) -> dict[str, Any]:
    return {
        "scenario_id": scenario_id,
        "rule": rule,
        "subject": identity or "",
        "identity": identity or None,
        "exact": exact,
        "unresolved": unresolved,
        "owner": owner,
        "owners": [owner] if owner else [],
        "source_spans": [_line_span(lock) for lock in locks],
        "related_locks": [_lock_fact(lock) for lock in locks],
        "evidence_references": [lock.get("event_id") for lock in locks],
        "missing_evidence": list(dict.fromkeys(missing)),
        "candidate_state": "unresolved" if unresolved else "ambiguous",
    }


def _pairing_missing(identity: str, exact: bool) -> list[str]:
    missing = [_MISSING_CONTROL_FLOW]
    if not exact or not identity:
        missing.append(_MISSING_IDENTITY)
    return missing


def _build_pairing(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """lock_pairing: unpaired acquires and releases per (owner, lock identity)."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        if event.get("event_type") not in (_LOCK, _UNLOCK):
            continue
        if event.get("event_type") == _LOCK:
            # A wait is a blocking wait, not a mutex acquire; a RAII lock is
            # released implicitly at scope exit. Neither is a releasable hold.
            if _acquire_kind(event) == _ACQUIRE_WAIT or event.get("raii"):
                continue
        identity = _identity(event)
        key = (_owner(event), identity)
        group = groups.setdefault(key, {
            "owner": _owner(event), "identity": identity,
            "exact": True, "locks": [], "unlocks": [],
        })
        if event.get("event_type") == _LOCK:
            group["locks"].append(event)
        else:
            group["unlocks"].append(event)
        group["exact"] = group["exact"] and bool(event.get("subject_exact"))

    candidates: list[dict[str, Any]] = []
    for (owner, identity) in sorted(groups):
        group = groups[(owner, identity)]
        blocking = [l for l in group["locks"]
                    if _acquire_kind(l) == _ACQUIRE_BLOCKING]
        may_fail = [l for l in group["locks"]
                    if _acquire_kind(l) in _MAY_FAIL_ACQUIRES]
        n_blocking = len(blocking)
        n_may_fail = len(may_fail)
        n_unlock = len(group["unlocks"])

        # A deterministic acquire left without a release: a possibly-leaked lock.
        unpaired_acquire = n_blocking - n_unlock
        if unpaired_acquire > 0:
            unpaired = sorted(blocking, key=_byte)[-unpaired_acquire:]
            candidates.append(_base_candidate(
                _SCENARIO_PAIRING, _RULE_UNPAIRED_ACQUIRE, identity,
                group["exact"], not identity, group["owner"], unpaired,
                _pairing_missing(identity, group["exact"]),
            ))

        # A release beyond every possible acquire (deterministic + may-fail): a
        # release of a lock that was never provably held.
        surplus_release = n_unlock - n_blocking - n_may_fail
        if surplus_release > 0:
            unpaired = sorted(group["unlocks"], key=_byte)[:surplus_release]
            candidates.append(_base_candidate(
                _SCENARIO_PAIRING, _RULE_UNPAIRED_RELEASE, identity,
                group["exact"], not identity, group["owner"], unpaired,
                _pairing_missing(identity, group["exact"]),
            ))

    return sorted(candidates, key=lambda c: (c["owner"], c["rule"], c["subject"]))


def _owner_sequences(events: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    """One method's lock/unlock events, in source order."""
    by_owner: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        if event.get("event_type") in (_LOCK, _UNLOCK):
            by_owner[_owner(event)].append(event)
    for owner in by_owner:
        by_owner[owner].sort(key=_byte)
    return dict(by_owner)


def _build_discipline(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """lock_discipline: self-deadlock, cross-method order inversion, blocking-in-lock."""
    by_owner = _owner_sequences(events)

    candidates: list[dict[str, Any]] = []
    seen_self_deadlock: set[tuple[str, str, int]] = set()
    seen_blocking_in_lock: set[tuple[str, str, int]] = set()
    owner_lock_order: dict[str, list[str]] = {}

    for owner in sorted(by_owner):
        held: set[str] = set()
        order: list[str] = []
        for event in by_owner[owner]:
            event_type = event.get("event_type")
            kind = _acquire_kind(event)
            identity = _identity(event)

            if event_type == _LOCK and event.get("raii"):
                # Held until an invisible scope exit; never in the held set.
                continue

            if event_type == _LOCK and kind == _ACQUIRE_BLOCKING and identity:
                if identity in held:
                    key = (owner, identity, _byte(event))
                    if key not in seen_self_deadlock:
                        seen_self_deadlock.add(key)
                        candidates.append(_base_candidate(
                            _SCENARIO_DISCIPLINE, _RULE_SELF_DEADLOCK,
                            identity, bool(event.get("subject_exact")), False,
                            owner, [event], [_MISSING_CONTROL_FLOW],
                        ))
                else:
                    held.add(identity)
                    if not order or order[-1] != identity:
                        order.append(identity)
                continue

            if event_type == _LOCK and kind == _ACQUIRE_WAIT:
                if held and identity:
                    key = (owner, identity, _byte(event))
                    if key not in seen_blocking_in_lock:
                        seen_blocking_in_lock.add(key)
                        candidates.append(_base_candidate(
                            _SCENARIO_DISCIPLINE, _RULE_BLOCKING_IN_LOCK,
                            identity, bool(event.get("subject_exact")), False,
                            owner, [event], [_MISSING_CONTROL_FLOW],
                        ))
                continue

            if event_type == _LOCK and kind in _MAY_FAIL_ACQUIRES:
                # May fail; never a held lock. Not treated as a hold.
                continue

            if event_type == _UNLOCK:
                if identity in held:
                    held.discard(identity)

        owner_lock_order[owner] = order

    # Cross-method lock-order inversion: X before Y in one method, Y before X
    # in another (blocking acquisitions only).
    orderings: dict[tuple[str, str], set[str]] = defaultdict(set)
    for owner, order in owner_lock_order.items():
        for i, x in enumerate(order):
            for y in order[i + 1:]:
                if x and y and x != y:
                    orderings[(x, y)].add(owner)

    reported: set[tuple[str, str]] = set()
    for (x, y) in sorted(orderings):
        if (y, x) not in orderings:
            continue
        pair = tuple(sorted((x, y)))
        if pair in reported:
            continue
        reported.add(pair)
        owners = sorted(orderings[(x, y)] | orderings[(y, x)])
        candidates.append({
            "scenario_id": _SCENARIO_DISCIPLINE,
            "rule": _RULE_ORDER_INVERSION,
            "subject": f"{pair[0]} <-> {pair[1]}",
            "identity": None,
            "exact": True,
            "unresolved": False,
            "owner": owners[0],
            "owners": owners,
            "order_pair": list(pair),
            "source_spans": [],
            "related_locks": list(pair),
            "evidence_references": [],
            "missing_evidence": [_MISSING_CONTROL_FLOW],
            "candidate_state": "ambiguous",
        })

    return sorted(candidates, key=lambda c: (c["rule"], c["subject"], c["owner"]))


def build_lock_scenarios(events: Iterable[Mapping[str, Any]]) -> dict:
    """Build lock_pairing and lock_discipline candidates from S3 events.

    ``events`` is the ``env["events"]`` list of an ``events`` query (S3's level-0
    vocabulary events). The function is pure and deterministic: the same events
    yield the same bundle, with keys and lists in stable order.

    Returns ``{"candidates": [...], "coverage": {...}}`` where every candidate
    carries the unified scenario fields (``scenario_id``, ``source_spans``,
    ``related_locks``, ``candidate_state``, ``evidence_references``,
    ``missing_evidence``) and the lock identity, exactness and owner.
    """
    events = list(events)
    candidates = _build_pairing(events) + _build_discipline(events)
    return {
        "candidates": candidates,
        "coverage": {
            "scenarios": {
                _SCENARIO_PAIRING: sum(
                    1 for c in candidates if c["scenario_id"] == _SCENARIO_PAIRING),
                _SCENARIO_DISCIPLINE: sum(
                    1 for c in candidates if c["scenario_id"] == _SCENARIO_DISCIPLINE),
            },
        },
    }


# ---------------------------------------------------------------------------
# SUB-04 (R4): per-access lockset + lock identity relations
#
# The access layer's READ/WRITE points are deliberately edgeless (stage 5A
# keeps them off the CONTROL_REACHES chain), so a per-access held-lockset is a
# projection the lock scenarios compute here, never a graph change: the
# must-hold set (meet = intersection) is propagated over the *threaded* points
# (LOCK/UNLOCK/CALL/CHECK/...) and each access point is attributed to the
# threaded point its source position falls under. This keeps the result
# candidate-grade always -- never a verified "definitely held" -- and never
# invents a points-to or underlying relation (Frozen G0 contract §3).

_READ = "READ"
_WRITE = "WRITE"
_ACCESS_EVENT_TYPES = frozenset({_READ, _WRITE})

# A CALL that is attributable to one of these sibling vocabulary events is a
# known call; any other CALL is an unknown function call whose effect on locks
# held across it is unprovable (cross-function limitation).
_KNOWN_CALL_KINDS = frozenset({
    _LOCK, _UNLOCK, "ATOMIC", "THREAD_SPAWN", "THREAD_JOIN", "ALLOC", "RELEASE",
})

_CONTROL_REACHES = "CONTROL_REACHES"
_CFG_EXIT_PREFIX = "cfg-exit:"
_ENTRY = "\x00entry"
_EXIT = "\x00exit"


def _owner_symbol(event: Mapping[str, Any]) -> str:
    return event.get("owner_symbol_id") or ""


def _owner_label(event: Mapping[str, Any]) -> str:
    return event.get("owner_qualified_name") or event.get("owner_symbol_id") or ""


def _lock_fact_of(event: Mapping[str, Any]) -> dict[str, Any]:
    scope_end = event.get("raii_scope_end")
    return {
        "lock_identity": event.get("lock_identity") or "",
        "subject_exact": bool(event.get("subject_exact")),
        "acquire_kind": event.get("acquire_kind") or _ACQUIRE_BLOCKING,
        "raii": bool(event.get("raii")),
        # Issue #69: the guard's lexical scope end byte, present only on RAII
        # acquisitions; None elsewhere so the field never reads as a scope.
        "raii_scope_end": int(scope_end) if scope_end is not None else None,
    }


def _threaded_dataflow(
    threaded: Mapping[str, Mapping[str, Any]],
    edges: Iterable[Mapping[str, Any]],
    lock_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, frozenset[str]], dict[str, list[str]]]:
    """Must-hold lockset at every threaded point (meet = intersection).

    Returns ``(held_in, succ)``. ``held_in[n]`` is the set of lock identities
    provably held on **every** path reaching ``n`` (empty at the synthetic
    entry), with ``_ENTRY`` and ``_EXIT`` materialised as pseudo nodes so a
    loop header that is also the method entry still meets the empty entry set.
    ``succ`` is the intra-method successor map (including the ``_EXIT`` sink)
    reused later for the unlock-omission walk.

    This is a greatest-fixed-point must analysis: non-entry nodes start at TOP
    (the function's full lock-identity universe) and shrink monotonically via
    meet = intersection, so a loop back-edge's first-round value does not
    under-approximate a held lock to empty. Starting every node at the empty
    set would turn the meet over ``{entry-path, back-edge}`` into ``{}`` on the
    first pass and never recover.
    """
    ids = set(threaded)
    pred: dict[str, list[str]] = {n: [] for n in ids}
    succ: dict[str, list[str]] = {n: [] for n in ids}
    pred[_ENTRY] = []
    pred[_EXIT] = []
    succ[_ENTRY] = []
    succ[_EXIT] = []
    for edge in edges:
        src = edge.get("src_id")
        dst = edge.get("dst_id")
        if src not in ids:
            continue
        if dst is None or str(dst).startswith(_CFG_EXIT_PREFIX):
            dst = _EXIT
        elif dst not in ids:
            continue
        succ[src].append(dst)
        pred[dst].append(src)

    # Synthetic entry: the first point in source order plus every point with no
    # incoming edge (a body fragmented by returns keeps the entry explicit).
    entries: set[str] = {nid for nid in ids if not pred[nid]}
    if ids:
        first = min(ids, key=lambda nid: (int(threaded[nid].get("start_line") or 0), nid))
        entries.add(first)
    for nid in entries:
        succ[_ENTRY].append(nid)
        pred[nid].append(_ENTRY)

    def transfer(nid: str, held: frozenset[str]) -> frozenset[str]:
        if nid in (_ENTRY, _EXIT):
            return held
        node = threaded[nid]
        event_type = node.get("event_type")
        fact = lock_by_id.get(nid)
        if event_type == _LOCK:
            # Only a provably-identified blocking acquisition is a held lock.
            # try/timed may fail, RAII is released at scope exit, a `wait` is a
            # condition wait, and an inexact identity names a root rather than
            # one distinguishable lock -- none of them count as held.
            if (
                fact is not None
                and fact["acquire_kind"] == _ACQUIRE_BLOCKING
                and not fact["raii"]
                and fact["subject_exact"]
                and fact["lock_identity"]
            ):
                return held | {fact["lock_identity"]}
            return held
        if event_type == _UNLOCK:
            if fact is not None and fact["lock_identity"]:
                return held - {fact["lock_identity"]}
            return held
        return held

    # Greatest fixed point: non-entry nodes start at the function's full lock
    # universe (TOP) and shrink via meet = intersection. The synthetic entry
    # holds nothing. An empty predecessor meet is vacuous TOP (universe), which
    # only ever applies to a node without any reaching path.
    universe = frozenset({
        (lock_by_id.get(nid) or {}).get("lock_identity")
        for nid in ids
        if threaded[nid].get("event_type") in (_LOCK, _UNLOCK)
        and (lock_by_id.get(nid) or {}).get("lock_identity")
    })
    held_in: dict[str, frozenset[str]] = {
        n: universe for n in list(ids) + [_EXIT]
    }
    held_out: dict[str, frozenset[str]] = {
        n: universe for n in list(ids) + [_EXIT]
    }
    held_in[_ENTRY] = frozenset()
    held_out[_ENTRY] = frozenset()
    order = sorted(list(ids) + [_ENTRY, _EXIT])
    for _ in range(1000):
        changed = False
        for nid in order:
            if nid == _ENTRY:
                continue
            preds = pred[nid]
            new_in = held_out[preds[0]] if preds else universe
            for p in preds[1:]:
                new_in = new_in & held_out[p]
            new_out = transfer(nid, new_in)
            if new_out != held_out[nid] or new_in != held_in[nid]:
                held_out[nid] = new_out
                held_in[nid] = new_in
                changed = True
        if not changed:
            break
    return held_in, succ


def _flow_successor(
    access: Mapping[str, Any], threaded: Mapping[str, Mapping[str, Any]]
) -> str | None:
    """The threaded point an access point flows into, or None for the exit.

    An access inside a threaded statement's span is an argument/sub-expression
    of that statement, so it "flows into" the statement itself. An access on
    its own edgeless statement flows into the next threaded point in source
    order (or the method exit when there is none).
    """
    lo = int(access.get("start_line") or 0)
    hi = int(access.get("end_line") or lo)
    for nid in sorted(threaded):
        node = threaded[nid]
        s = int(node.get("start_line") or 0)
        e = int(node.get("end_line") or s)
        if s <= lo and hi <= e:
            return nid
    for nid in sorted(
        threaded,
        key=lambda key: (int(threaded[key].get("start_line") or 0), key),
    ):
        if int(threaded[nid].get("start_line") or 0) > hi:
            return nid
    return None


def _omits_unlock_on_path(
    successor: str | None,
    lock: str,
    succ: Mapping[str, list[str]],
    threaded: Mapping[str, Mapping[str, Any]],
    lock_by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Whether a path from the access to the exit never unlocks ``lock``.

    The path starts at the access's flow successor: when that successor is the
    release itself the lock is provably released on the only path, so there is
    no omission; when the successor is the exit the lock is never released.
    """
    if successor is None:
        return True
    if successor == _EXIT:
        return True
    blocked = {
        nid
        for nid, node in threaded.items()
        if node.get("event_type") == _UNLOCK
        and (lock_by_id.get(nid) or {}).get("lock_identity") == lock
    }
    if successor in blocked:
        return False
    stack = [successor]
    seen = {successor}
    while stack:
        current = stack.pop()
        for nxt in succ.get(current, ()):
            if nxt == _EXIT:
                return True
            if nxt in blocked or nxt in seen:
                continue
            seen.add(nxt)
            stack.append(nxt)
    return False


def _access_record(
    owner_symbol: str, owner: str, access: Mapping[str, Any],
    held: frozenset[str], limitations: list[str],
) -> dict[str, Any]:
    annotations = access.get("annotations") or {}
    return {
        "access_event_id": access.get("event_id"),
        "event_type": access.get("event_type"),
        "owner": owner,
        "owner_symbol_id": owner_symbol,
        "subject": annotations.get("subject", ""),
        "subject_decl": annotations.get("subject_decl", ""),
        "start_line": int(access.get("start_line") or 0),
        "end_line": int(access.get("end_line") or 0),
        "held_locks": sorted(held),
        "limitation": list(limitations),
        "candidate_state": "ambiguous",
    }


def _subject_key(record: Mapping[str, Any]) -> str:
    decl = record.get("subject_decl") or ""
    subject = record.get("subject") or ""
    if not decl and not subject:
        return ""
    return decl or f"unresolved\x1f{subject}"


def _relation_sort_key(relation: Mapping[str, Any]) -> tuple:
    return (
        relation.get("relation", ""),
        relation.get("subject") or "",
        tuple(sorted(relation.get("locks", []))),
        tuple(relation.get("access_event_ids", ())),
        relation.get("access_event_id") or "",
        relation.get("lock") or "",
        relation.get("owner") or "",
    )


def _raii_scopes(
    threaded: Mapping[str, Mapping[str, Any]],
    lock_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[list[tuple[str, int, int]], frozenset[str]]:
    """RAII guards active in one method, and the identities they release.

    Returns ``(scopes, identities)``. ``scopes`` is a list of
    ``(lock_identity, declaration_start_byte, scope_end_byte)`` for every
    provably-identified RAII acquisition in the method, sorted by start byte.
    ``identities`` is the set of those lock identities, so the caller can tell
    a RAII guard -- released implicitly at its scope end -- from a blocking
    lock that needs an explicit unlock.

    A RAII acquisition is deterministic and lexical: it cannot be conditionally
    held and it is released on every exit path, so its hold interval is read
    straight off the lock fact (``raii_scope_end``, Issue #69) rather than
    propagated over the CFG. Only an exact, non-empty identity participates; an
    inexact root or an unrecovered scope end stays out, exactly like the
    blocking path's identity gate.
    """
    scopes: list[tuple[str, int, int]] = []
    for nid, ev in threaded.items():
        if ev.get("event_type") != _LOCK:
            continue
        fact = lock_by_id.get(nid)
        if not fact or not fact.get("raii"):
            continue
        if not fact["subject_exact"] or not fact["lock_identity"]:
            continue
        scope_end = fact.get("raii_scope_end")
        if not scope_end:
            continue
        scopes.append((
            fact["lock_identity"],
            int(ev.get("start_byte") or 0),
            int(scope_end),
        ))
    scopes.sort(key=lambda item: (item[1], item[2], item[0]))
    return scopes, frozenset(identity for identity, _, _ in scopes)


def build_access_lockset(
    sparse_events: Iterable[Mapping[str, Any]],
    edges: Iterable[Mapping[str, Any]] = (),
    lock_facts: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Project a per-access held-lockset and lock identity relations (SUB-04/R4).

    ``sparse_events`` and ``edges`` are a `sparse_cfg` query's ``events`` and
    ``edges`` lists; ``lock_facts`` is an `events` query's ``events`` list (the
    S3 lock facts: ``lock_identity``/``subject_exact``/``acquire_kind``/
    ``raii``). LOCK/UNLOCK ids are shared across the two analyzers, so the lock
    facts join onto the sparse CFG by event id.

    The function is pure and deterministic. It never modifies the graph, never
    derives a new points-to or underlying relation, and never reports a locked
    access as "definite" -- every record is ``candidate_state: "ambiguous"``.
    """
    sparse = list(sparse_events)
    edges = list(edges)
    lock_by_id: dict[str, dict[str, Any]] = {
        event.get("event_id"): _lock_fact_of(event)
        for event in lock_facts
        if event.get("event_type") in (_LOCK, _UNLOCK) and event.get("event_id")
    }

    nodes_by_owner: dict[str, dict[str, Mapping[str, Any]]] = {}
    owner_labels: dict[str, str] = {}
    for event in sparse:
        owner = _owner_symbol(event)
        if not owner:
            continue
        nodes_by_owner.setdefault(owner, {})[event.get("event_id")] = event
        owner_labels.setdefault(owner, _owner_label(event))

    edges_by_owner: dict[str, list[Mapping[str, Any]]] = {}
    for edge in edges:
        if edge.get("kind") != _CONTROL_REACHES:
            continue
        owner = (edge.get("payload") or {}).get("owner_symbol_id") or ""
        if owner:
            edges_by_owner.setdefault(owner, []).append(edge)

    access_records: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []

    for owner in sorted(nodes_by_owner):
        node_map = nodes_by_owner[owner]
        threaded = {
            eid: ev for eid, ev in node_map.items()
            if ev.get("event_type") not in _ACCESS_EVENT_TYPES
        }
        accesses = {
            eid: ev for eid, ev in node_map.items()
            if ev.get("event_type") in _ACCESS_EVENT_TYPES
        }
        owner_label = owner_labels.get(owner, owner)
        if not threaded:
            # No control-flow points: nothing to propagate on, so every access
            # is provably unprotected.
            for eid, ev in sorted(
                accesses.items(),
                key=lambda pair: (int(pair[1].get("start_line") or 0), pair[0]),
            ):
                access_records.append(
                    _access_record(owner, owner_label, ev, frozenset(), []))
            continue

        held_in, succ = _threaded_dataflow(
            threaded, edges_by_owner.get(owner, ()), lock_by_id)
        # RAII guards are lexical, not dataflow: their hold interval is read
        # straight off the lock fact rather than propagated over the CFG.
        raii_scopes, raii_identities = _raii_scopes(threaded, lock_by_id)

        # Method-level, conservative limitations.
        limitations: list[str] = []
        known_call_bytes = {
            int(ev.get("start_byte") or 0)
            for ev in threaded.values()
            if ev.get("event_type") in _KNOWN_CALL_KINDS
        }
        for ev in threaded.values():
            if (
                ev.get("event_type") == "CALL"
                and int(ev.get("start_byte") or 0) not in known_call_bytes
            ):
                limitations.append("cross_function_call")
                break
        for eid, ev in threaded.items():
            if ev.get("event_type") in (_LOCK, _UNLOCK):
                fact = lock_by_id.get(eid)
                if fact is not None and (not fact["subject_exact"] or not fact["lock_identity"]):
                    limitations.append("lock_identity_inexact")
                    break

        for eid, ev in sorted(
            accesses.items(),
            key=lambda pair: (int(pair[1].get("start_byte") or 0), pair[0]),
        ):
            successor = _flow_successor(ev, threaded)
            held = held_in[successor] if successor is not None else held_in[_EXIT]
            # A RAII guard held over this access's byte position protects it:
            # the lock is held from its declaration to its scope end (Issue #69),
            # so an access inside that interval is protected and one after it is
            # not -- which is what stops a guard from reaching past an inner
            # scope's closing brace to method exit.
            start_byte = int(ev.get("start_byte") or 0)
            held = held | frozenset(
                identity
                for identity, decl_start, scope_end in raii_scopes
                if decl_start <= start_byte < scope_end
            )
            access_records.append(
                _access_record(owner, owner_label, ev, held, limitations))
            for lock in sorted(held):
                relations.append({
                    "relation": "PROTECTS",
                    "lock": lock,
                    "access_event_id": eid,
                    "owner": owner_label,
                    "owner_symbol_id": owner,
                })
                # A RAII lock releases implicitly at its scope end, so "no
                # explicit unlock on the path" is not a leak and must not
                # surface as UNLOCK_OMITTED_ON_PATH.
                if lock in raii_identities:
                    continue
                if _omits_unlock_on_path(successor, lock, succ, threaded, lock_by_id):
                    relations.append({
                        "relation": "UNLOCK_OMITTED_ON_PATH",
                        "lock": lock,
                        "access_event_id": eid,
                        "owner": owner_label,
                        "owner_symbol_id": owner,
                        "candidate_state": "ambiguous",
                    })

    # SAME_LOCK / DIFFERENT_LOCK over access pairs sharing one subject identity.
    by_subject: dict[str, list[dict[str, Any]]] = {}
    for record in access_records:
        key = _subject_key(record)
        if key:
            by_subject.setdefault(key, []).append(record)
    for key in sorted(by_subject):
        group = sorted(
            by_subject[key],
            key=lambda rec: (rec["start_line"], rec["access_event_id"] or ""),
        )
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                left, right = group[i], group[j]
                common = sorted(set(left["held_locks"]) & set(right["held_locks"]))
                pair = [left["access_event_id"], right["access_event_id"]]
                owners = sorted({left["owner"], right["owner"]})
                if common:
                    relations.append({
                        "relation": "SAME_LOCK",
                        "subject": key,
                        "subject_decl": left["subject_decl"] or right["subject_decl"],
                        "access_event_ids": pair,
                        "owners": owners,
                        "locks": common,
                    })
                else:
                    relations.append({
                        "relation": "DIFFERENT_LOCK",
                        "subject": key,
                        "subject_decl": left["subject_decl"] or right["subject_decl"],
                        "access_event_ids": pair,
                        "owners": owners,
                        "unprotected": not left["held_locks"] or not right["held_locks"],
                    })

    access_records.sort(key=lambda rec: (
        rec["owner_symbol_id"], rec["start_line"], rec["access_event_id"] or "",
    ))
    relations.sort(key=_relation_sort_key)

    relation_order = ("PROTECTS", "SAME_LOCK", "DIFFERENT_LOCK", "UNLOCK_OMITTED_ON_PATH")
    return {
        "access_locksets": access_records,
        "relations": relations,
        "coverage": {
            "accesses": len(access_records),
            "protected_accesses": sum(1 for rec in access_records if rec["held_locks"]),
            "unprotected_accesses": sum(1 for rec in access_records if not rec["held_locks"]),
            "relations": {
                rel: sum(1 for relation in relations if relation["relation"] == rel)
                for rel in relation_order
            },
        },
    }


__all__ = ["build_lock_scenarios", "build_access_lockset"]