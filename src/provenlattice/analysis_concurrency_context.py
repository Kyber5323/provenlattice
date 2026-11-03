"""concurrency_context (S2): finite, provable thread synchronization facts.

A query-time reader over the events/sparse_cfg rows. It converts the
THREAD_SPAWN/THREAD_JOIN vocabulary events and the READ/WRITE access facts into
a reusable, honest bundle that the S4/S5 scenario rules will consume.

Fail-closed contract:

* a spawn and a join are paired **only** when their thread-handle identities
  are provably the same object and the spawn precedes the join in program
  order. The events layer does not currently stamp a handle on THREAD_JOIN, so
  this capability reports ``thread_handle_identity`` missing evidence instead of
  pairing by owner + source order (which would merge different threads);
* MAY_PARALLEL is always a **candidate** (``status: "ambiguous"``), never a
  resolved claim, and is associated with the specific shared subject (S1's
  stable ``subject_decl`` identity), its access events and thread entry points;
* an unresolved identity (no ``subject_decl``) is marked ``unresolved`` and
  reported as missing evidence, never merged with a different object by display
  name alone.

It does not promise a complete happens-before, a memory model, points-to, or a
thread-pool analysis.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .analysis_query import query_analysis
from .concurrency_lock_scenarios import build_access_lockset
from .models import (
    STORAGE_FIELD,
    STORAGE_FILE_STATIC,
    STORAGE_FUNCTION_STATIC,
    STORAGE_GLOBAL,
    STORAGE_UNKNOWN,
)

# The storage classes whose subject can name genuinely shared, cross-thread
# state. `local` and `parameter` are frame-scoped and cannot be half of a
# cross-method pair, so they are subtracted rather than enumerated.
SHARED_STORAGES = frozenset({
    STORAGE_FIELD, STORAGE_FILE_STATIC, STORAGE_FUNCTION_STATIC,
    STORAGE_GLOBAL, STORAGE_UNKNOWN,
})

_ACCESS_EVENT_TYPES = frozenset({"READ", "WRITE"})


def _annotations(event: Mapping[str, Any]) -> Mapping[str, Any]:
    return event.get("annotations") or {}


def _owner(event: Mapping[str, Any]) -> str:
    return event.get("owner_qualified_name") or event.get("owner_symbol_id") or ""


def _byte(value: Any) -> int:
    return int(value or 0)


def build_concurrency_context(events: Iterable[Mapping[str, Any]]) -> dict:
    """Build the concurrency_context bundle from sparse_cfg events.

    ``events`` is the ``env["events"]`` list of a ``sparse_cfg`` query. The
    function is pure and deterministic: the same events yield the same bundle,
    with keys and lists in stable order.
    """
    events = list(events)
    spawns: list[Mapping[str, Any]] = []
    joins: list[Mapping[str, Any]] = []
    entry_points: set[str] = set()
    for event in events:
        event_type = event.get("event_type")
        if event_type == "THREAD_SPAWN":
            spawns.append(event)
            thread_function = _annotations(event).get("thread_function", "")
            if thread_function:
                entry_points.add(thread_function)
        elif event_type == "THREAD_JOIN":
            joins.append(event)

    spawn_facts = [
        {
            "event_id": event.get("event_id"),
            "owner": _owner(event),
            "handle": _annotations(event).get("subject", ""),
            "handle_decl": _annotations(event).get("subject_decl", ""),
            "thread_function": _annotations(event).get("thread_function", ""),
            "start_byte": _byte(event.get("start_byte")),
            "end_byte": _byte(event.get("end_byte")),
        }
        for event in spawns
    ]
    join_facts = [
        {
            "event_id": event.get("event_id"),
            "owner": _owner(event),
            "handle": _annotations(event).get("subject", ""),
            "handle_decl": _annotations(event).get("subject_decl", ""),
            "start_byte": _byte(event.get("start_byte")),
            "end_byte": _byte(event.get("end_byte")),
        }
        for event in joins
    ]

    missing_evidence: set[str] = set()
    capability_gaps: list[str] = []
    joins_have_handle = any(join["handle_decl"] for join in join_facts)
    if joins and not joins_have_handle:
        missing_evidence.add("thread_handle_identity")
        capability_gaps.append(
            "THREAD_JOIN carries no thread handle in the events layer, so a "
            "spawn and a join cannot be proven to name the same thread; no "
            "spawn->join pair is asserted"
        )

    # Pair a spawn and a join only when their handle identities are the same
    # declared object (handle_decl) in the same method and the spawn precedes
    # the join. Without a shared handle this must not fire -- pairing two
    # different threads by owner+order would fabricate a happens-before edge.
    spawn_join_pairs: list[dict[str, Any]] = []
    for spawn in spawn_facts:
        if not spawn["handle_decl"]:
            missing_evidence.add("thread_handle_identity")
            continue
        if not spawn["thread_function"]:
            missing_evidence.add("spawn_without_thread_function")
        for join in join_facts:
            if (
                join["handle_decl"] == spawn["handle_decl"]
                and join["owner"] == spawn["owner"]
                and spawn["start_byte"] < join["start_byte"]
            ):
                spawn_join_pairs.append({
                    "owner": spawn["owner"],
                    "handle_decl": spawn["handle_decl"],
                    "thread_function": spawn["thread_function"],
                    "spawn": spawn["event_id"],
                    "join": join["event_id"],
                })

    # Shared-object identity uses S1's stable subject_decl. An access whose
    # identity is unresolved (no subject_decl, unknown storage) is kept apart as
    # `unresolved` and never merged with another object by display name alone.
    by_decl: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event_type") not in _ACCESS_EVENT_TYPES:
            continue
        annotations = _annotations(event)
        subject = annotations.get("subject", "")
        decl = annotations.get("subject_decl", "")
        storage = annotations.get("subject_storage") or STORAGE_UNKNOWN
        if not subject and not decl:
            continue
        if storage not in SHARED_STORAGES:
            continue
        key = decl if decl else f"unresolved\x1f{subject}"
        record = by_decl.setdefault(key, {
            "identity": decl or None,
            "subject": subject,
            "storage": storage,
            "exact": annotations.get("subject_exact", "false"),
            "owners": set(),
            "accesses": [],
        })
        record["owners"].add(_owner(event))
        record["accesses"].append({
            "event_id": event.get("event_id"),
            "event_type": event.get("event_type"),
            "owner": _owner(event),
            "start_byte": _byte(event.get("start_byte")),
            "end_byte": _byte(event.get("end_byte")),
            "exact": annotations.get("subject_exact", "false"),
        })

    # A thread entry owner is a method whose name is a spawned thread_function;
    # this is how a MAY_PARALLEL candidate is tied to the specific thread entry.
    entry_owners = entry_points
    may_parallel = []
    for key in sorted(by_decl):
        record = by_decl[key]
        if len(record["owners"]) < 2:
            continue
        if record["identity"] is None:
            missing_evidence.add("unresolved_identity")
        may_parallel.append({
            "identity": record["identity"],
            "subject": record["subject"],
            "storage": record["storage"],
            "exact": record["exact"],
            "unresolved": record["identity"] is None,
            "owners": sorted(record["owners"]),
            "thread_entry_owners": sorted(record["owners"] & entry_owners),
            "accesses": sorted(
                record["accesses"],
                key=lambda access: (access["start_byte"], access["end_byte"], access["event_id"] or ""),
            ),
            "status": "ambiguous",
        })

    return {
        "thread_entry_points": sorted(entry_points),
        "spawns": spawn_facts,
        "joins": join_facts,
        "spawn_join_pairs": spawn_join_pairs,
        "may_parallel": may_parallel,
        "missing_evidence": sorted(missing_evidence),
        "capability_gaps": capability_gaps,
    }


def query_concurrency_context(
    repo, file_path: str | None = None, database=None,
) -> dict:
    """Read one file's sparse_cfg facts and build its concurrency_context.

    Python/Java and unbuilt/stale files fall through to the sparse_cfg query's
    own honest status (``unsupported`` / ``not_built`` / ``stale``) -- this
    capability is C/C++ only and never fabricates a context where its inputs are
    absent.
    """
    env = query_analysis(repo, "sparse_cfg", file_path=file_path, database=database)
    if env["status"] != "available":
        return {"status": env["status"], "reason": env.get("reason")}
    context = build_concurrency_context(env.get("events", []))
    context["status"] = "available"
    return context


def query_access_lockset(
    repo, file_path: str | None = None, database=None,
) -> dict:
    """Read one file's sparse_cfg + events facts and build its access locksets.

    The per-access ``held_locks`` projection joins S3's lock facts (events) onto
    the sparse-cfg control-flow (edges) by shared lock event id. Python/Java and
    unbuilt/stale files fall through to the sparse_cfg query's own honest status
    -- this capability is C/C++ only and never fabricates a lockset where its
    inputs are absent.
    """
    env = query_analysis(repo, "sparse_cfg", file_path=file_path, database=database)
    if env["status"] != "available":
        return {"status": env["status"], "reason": env.get("reason")}
    lock_env = query_analysis(repo, "events", file_path=file_path, database=database)
    if lock_env["status"] != "available":
        return {"status": lock_env["status"], "reason": lock_env.get("reason")}
    bundle = build_access_lockset(
        env.get("events", []),
        env.get("edges", []),
        lock_env.get("events", []),
    )
    bundle["status"] = "available"
    return bundle


__all__ = [
    "build_concurrency_context",
    "query_concurrency_context",
    "query_access_lockset",
]