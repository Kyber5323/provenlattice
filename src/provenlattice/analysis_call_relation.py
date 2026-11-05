"""Query-time MAY_CALL derivation (F2, Issue #45 / SUB-09 R6).

Reads the READ/WRITE access facts ``sparse_cfg:cpp`` already materialises and
derives, fail-closed, the finite ``MAY_CALL`` target set the frozen G0
call-relation contract (``sub09_g0_call_relation_contract.md`` §4) and the V4
FE-6 split proposal (``sub09_fe6_implementation_split_proposal.md`` §1.2 / §4)
specify:

* the canonical slot is ``(subject_decl, subject_member)``, scoped to one file;
* F1's bounded indirect field-callee READ is stamped ``is_indirect=true``;
* F1's WRITE of that same slot carries ``write_source`` = the right-hand root
  identifier (``g_timePara.procFunc = timer_fn`` -> ``timer_fn``);
* the two join on the canonical slot; the distinct ``write_source`` values are
  resolved against the base ``nodes`` table (``kind in {Function, Method}`` and
  the same ``file_id``), deduplicated by qualified name, then mapped through the
  frozen rule::

      count == 0  -> MAY_CALL=[]  resolution="unknown"
      count == 1  -> MAY_CALL=[t] resolution="unique_same_file_assignment"
      count >= 2  -> MAY_CALL=[]  resolution="ambiguous_multiple_assignments"

No source-order / "last assignment" inference (``p.fn = a; p.fn = b`` stays
``{}``); an indirect call never upgrades to a definite ``CALLS``, and the
``resolver.py`` ``indirect_call_shape`` fail-closed invariant is preserved
untouched. An unresolved identity (no ``subject_decl``) and a non-function
``write_source`` both fall to ``count == 0``.

Pure query-time reader (the ``scenario_relations`` / ``defect_query``
precedent): it never creates, rebuilds or refreshes analysis data, never writes
a physical edge, and never changes the SQL schema. Default OFF: nothing here is
registered as an analyzer or capability and nothing runs during ``index``.
Freshness is delegated to ``query_analysis``; a stale or missing ``sparse_cfg``
answers that honest status instead of partial facts.
"""

from __future__ import annotations

from pathlib import Path

from .analysis_query import query_analysis
from .storage import SQLiteStorage

SPARSE_CFG_ANALYZER = "sparse_cfg:cpp"
FUNCTION_KINDS = frozenset({"Function", "Method"})

RESOLUTION_UNKNOWN = "unknown"
RESOLUTION_UNIQUE = "unique_same_file_assignment"
RESOLUTION_AMBIGUOUS = "ambiguous_multiple_assignments"


def _annotations(event) -> dict:
    return event.get("annotations") or {}


def _resolve_targets(
    storage: SQLiteStorage, file_id: str | None, write_sources: set[str],
) -> list[dict]:
    """Resolve distinct ``write_source`` names to same-file Function/Method nodes.

    The base ``nodes`` table is the only authority (NIT #2): a name is a valid
    MAY_CALL target only when a ``Function``/``Method`` node with that short
    name and the same ``file_id`` exists. Prototype + definition of one function
    collapse at graph-build time via ``symbol_id``, and this query-side guard
    deduplicates by ``qualified_name`` as well, so the same logical function
    never counts twice (NIT #3). A name that names no function contributes
    nothing -- it is not a target and does not raise the count.
    """
    if not write_sources or file_id is None:
        return []
    by_qualified: dict[str, dict] = {}
    for source in sorted(write_sources):
        for row in storage.rows(
            "SELECT id, name, qualified_name FROM nodes "
            "WHERE kind IN ('Function','Method') AND file_id=? AND name=? "
            "ORDER BY id",
            (file_id, source),
        ):
            by_qualified.setdefault(row["qualified_name"], {
                "name": row["name"],
                "qualified_name": row["qualified_name"],
                "node_id": row["id"],
            })
    return [by_qualified[key] for key in sorted(by_qualified)]


def build_call_relation(
    events: list[dict], file_id: str | None, storage: SQLiteStorage,
) -> dict:
    """Derive per-slot MAY_CALL records from one file's sparse_cfg events.

    Pure and deterministic: the same events + storage yield the same records,
    with the slots sorted by ``(subject_decl, subject_member)`` and each slot's
    evidence lists stable-sorted.
    """
    reads_by_slot: dict[tuple[str, str], list[str]] = {}
    writes_by_slot: dict[tuple[str, str], dict] = {}
    for event in events:
        event_type = event.get("event_type")
        annotations = _annotations(event)
        decl = annotations.get("subject_decl", "")
        member = annotations.get("subject_member", "")
        if event_type == "READ" and annotations.get("is_indirect") == "true":
            reads_by_slot.setdefault((decl, member), []).append(
                event.get("event_id"))
        elif event_type == "WRITE" and annotations.get("write_source"):
            record = writes_by_slot.setdefault(
                (decl, member), {"sources": set(), "event_ids": set()})
            record["sources"].add(annotations["write_source"])
            record["event_ids"].add(event.get("event_id"))

    relations: list[dict] = []
    for (decl, member) in sorted(reads_by_slot):
        read_ids = sorted(reads_by_slot[(decl, member)])
        writes = writes_by_slot.get((decl, member), {"sources": set(), "event_ids": set()})
        write_ids = sorted(writes["event_ids"])
        if not decl:
            # Unresolved identity: never join on the member name alone.
            targets, resolution, may_be_false_positive = [], RESOLUTION_UNKNOWN, True
        else:
            targets = _resolve_targets(storage, file_id, writes["sources"])
            if len(targets) == 0:
                resolution, may_be_false_positive = RESOLUTION_UNKNOWN, True
            elif len(targets) == 1:
                resolution, may_be_false_positive = RESOLUTION_UNIQUE, False
            else:
                resolution, may_be_false_positive = RESOLUTION_AMBIGUOUS, False
        # The frozen rule: a single target is the only case that names a target;
        # zero and two-or-more both answer an empty MAY_CALL list.
        may_call = [target["name"] for target in targets] if len(targets) == 1 else []
        relations.append({
            "subject_decl": decl,
            "subject_member": member,
            "may_call": may_call,
            "resolution": resolution,
            "may_be_false_positive": may_be_false_positive,
            "read_event_ids": read_ids,
            "write_event_ids": write_ids,
        })

    return {
        "call_relations": relations,
        "provenance": {
            "upstream_analyzer": SPARSE_CFG_ANALYZER,
            "read_slot_count": len(reads_by_slot),
            "relation_count": len(relations),
        },
    }


def query_call_relation(
    repo: str | Path, file_path: str, *, database: str | Path | None = None,
) -> dict:
    """Read one file's sparse_cfg facts and derive its MAY_CALL relations.

    C/C++ only, same-file scope. Python/Java and unbuilt/stale/corrupt files
    fall through to the sparse_cfg query's own honest status
    (``unsupported`` / ``not_built`` / ``stale`` / ``corrupt``) -- this reader
    never fabricates a target where its inputs are absent.
    """
    env = query_analysis(repo, "sparse_cfg", file_path=file_path, database=database)
    if env["status"] != "available":
        return {"status": env["status"], "reason": env.get("reason"),
                "call_relations": []}
    file_id = env.get("file_id")
    root = Path(repo).resolve()
    database = Path(database).resolve() if database else root / ".provenlattice" / "codegraph.db"
    with SQLiteStorage(database) as storage:
        result = build_call_relation(env.get("events", []), file_id, storage)
    result["status"] = "available"
    result["reason"] = None
    return result


__all__ = [
    "SPARSE_CFG_ANALYZER",
    "FUNCTION_KINDS",
    "RESOLUTION_UNKNOWN",
    "RESOLUTION_UNIQUE",
    "RESOLUTION_AMBIGUOUS",
    "build_call_relation",
    "query_call_relation",
]
