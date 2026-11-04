"""Query-time scenario relation producer (FE8-F2, Issue #52).

Reads the facts two analyzers already materialised -- ``scenario_annotations[]``
from ``ast:c`` (FE8-F1, #51) and the READ/WRITE access events from
``sparse_cfg:cpp`` -- and derives, fail-closed, the two frozen arrays in
``fe8_formal_feature_contract.md``:

* ``scenario_access_relations[]`` (``§2.2``): ``CONTAINS_ACCESS`` with
  ``join_kind=byte_range_exact``. A relation is built only when every ``§4``
  condition holds; a missing condition produces no relation, never a
  line-distance / function-name / text-similarity fallback.
* ``function_scenario_groups[]`` (``§2.3``): aggregated from explicit
  relations only, keyed ``(scenario_id, function_symbol_id, annotation_id)``.

Pure query-time reader (the ``defect_query`` precedent): it never creates,
rebuilds or refreshes analysis data, never writes a physical edge, and never
changes the SQL schema. Freshness is fail-closed: the ``ast:c`` and
``sparse_cfg:cpp`` caches must both be current (``§4`` condition 6 -- source
hash, analyzer version and dependency contract) for every C/C++ source/header
the base graph holds; a stale or missing upstream answers an empty result with
an explicit ``status``/``reason`` rather than partial relations.

Default OFF (``§7``): nothing here is registered as an analyzer or capability,
and nothing runs during ``index``; the function is only invoked by callers that
ask for FE-8 relation derivation explicitly.

Sort order (``§6.2``): relations sort by
``relative_path -> start_byte -> end_byte -> scenario_id -> access_event_id``
where ``start_byte``/``end_byte`` are the *annotation* region span (the access
is a single point inside it). The three keys ``relative_path``/``start_byte``/
``end_byte`` are denormalised from the annotation onto each relation so the
frozen sort and ``§4`` containment contract are independently verifiable;
they are not new semantic fields beyond ``§2.2``. Groups sort by
``(scenario_id, function_symbol_id, annotation_id)`` and carry access ids
stable-sorted by ``access_event_id``.
"""

from __future__ import annotations

import json
from pathlib import Path

from .analysis import (
    ANALYZER_VERSIONS,
    AnalysisStore,
    dependency_staleness,
)
from .analysis_c_ast import C_EXTENSIONS, SCENARIO_ANNOTATION_KIND
from .analysis_query import AnalysisRequestError
from .semantics.events import ACCESS_EVENTS
from .storage import SQLiteStorage

CONTAINS_ACCESS = "CONTAINS_ACCESS"
BYTE_RANGE_EXACT = "byte_range_exact"

AST_ANALYZER = "ast:c"
SPARSE_CFG_ANALYZER = "sparse_cfg:cpp"

_FUNCTION_OWNER_UNRESOLVED = "function_owner_unresolved"


def _normalize(path: str) -> str:
    return str(path).replace("\\", "/")


def _read_annotations(store: AnalysisStore) -> list[dict]:
    """The FE8-F1 ``scenario_annotations[]`` rows from ``ast:c``."""
    annotations: list[dict] = []
    for row in store.storage.rows(
        "SELECT analysis_node_id, kind, payload, file_id FROM analysis_nodes "
        "WHERE feature=? ORDER BY analysis_node_id",
        (AST_ANALYZER,),
    ):
        if row["kind"] != SCENARIO_ANNOTATION_KIND:
            continue
        payload = json.loads(row["payload"])
        annotations.append({
            "annotation_id": payload["annotation_id"],
            "scenario_id": payload["scenario_id"],
            "relative_path": _normalize(payload["relative_path"]),
            "start_byte": payload["start_byte"],
            "end_byte": payload["end_byte"],
            "function_symbol_id": payload.get("function_symbol_id"),
            "parse_status": payload["parse_status"],
        })
    annotations.sort(key=lambda ann: (
        ann["relative_path"], ann["start_byte"], ann["end_byte"],
        ann["scenario_id"],
    ))
    return annotations


def _read_access_events(store: AnalysisStore) -> list[dict]:
    """The READ/WRITE access events from ``sparse_cfg:cpp``.

    Only the access family joins (``ACCESS_EVENTS = {READ, WRITE}``); the
    structural walker points (BRANCH/CHECK/DEREFERENCE) are not accesses and
    never produce a relation.
    """
    accesses: list[dict] = []
    for row in store.storage.rows(
        "SELECT analysis_node_id, kind, payload, file_id FROM analysis_nodes "
        "WHERE feature=? ORDER BY analysis_node_id",
        (SPARSE_CFG_ANALYZER,),
    ):
        if row["kind"] not in ACCESS_EVENTS:
            continue
        payload = json.loads(row["payload"])
        accesses.append({
            "access_event_id": row["analysis_node_id"],
            "event_type": row["kind"],
            "owner_symbol_id": payload.get("owner_symbol_id"),
            "start_byte": payload.get("start_byte"),
            "end_byte": payload.get("end_byte"),
            "file_id": row["file_id"],
        })
    return accesses


def _build_relations(
    annotations: list[dict], accesses: list[dict], file_paths: dict[str, str],
) -> list[dict]:
    """Apply ``§4``'s six conditions; condition 6 (freshness) is gated upstream.

    * condition 1 ``parse_status == resolved`` -> skip otherwise (annotation's
      own limitation is the reason, carried on ``scenario_annotations[]``);
    * condition 2 same ``relative_path``;
    * condition 3 access has a definite byte span
      (``access_span_insufficient`` -> no relation);
    * condition 4 access span fully inside the region content span;
    * condition 5 both owners non-null and equal -> record; both null -> no
      constraint; single-side null -> build + ``function_owner_unresolved``;
      both non-null and different (``§4`` X1 region spanning a function
      boundary) -> no relation.
    """
    relations: list[dict] = []
    for ann in annotations:
        if ann["parse_status"] != "resolved":
            continue
        ann_fn = ann["function_symbol_id"]
        for acc in accesses:
            acc_path = file_paths.get(acc["file_id"])
            if acc_path is None or acc_path != ann["relative_path"]:
                continue
            acc_start, acc_end = acc["start_byte"], acc["end_byte"]
            if acc_start is None or acc_end is None:
                continue
            if not (ann["start_byte"] <= acc_start and acc_end <= ann["end_byte"]):
                continue
            acc_fn = acc["owner_symbol_id"]
            limitations: list[str] = []
            if ann_fn is not None and acc_fn is not None:
                if ann_fn != acc_fn:
                    continue
                function_symbol_id = ann_fn
            elif ann_fn is None and acc_fn is None:
                function_symbol_id = None
            else:
                function_symbol_id = None
                limitations = [_FUNCTION_OWNER_UNRESOLVED]
            relations.append({
                "annotation_id": ann["annotation_id"],
                "scenario_id": ann["scenario_id"],
                "access_event_id": acc["access_event_id"],
                "relation": CONTAINS_ACCESS,
                "join_kind": BYTE_RANGE_EXACT,
                "function_symbol_id": function_symbol_id,
                "limitations": limitations,
                # denormalised sort/containment keys (§6.2, §4.4)
                "relative_path": ann["relative_path"],
                "start_byte": ann["start_byte"],
                "end_byte": ann["end_byte"],
            })
    relations.sort(key=lambda rel: (
        rel["relative_path"], rel["start_byte"], rel["end_byte"],
        rel["scenario_id"], rel["access_event_id"],
    ))
    return relations


def _build_groups(relations: list[dict]) -> list[dict]:
    """Aggregate ``function_scenario_groups[]`` from explicit relations (§2.3)."""
    grouped: dict[tuple[str, str | None, str], list[str]] = {}
    for relation in relations:
        key = (relation["scenario_id"], relation["function_symbol_id"],
               relation["annotation_id"])
        grouped.setdefault(key, []).append(relation["access_event_id"])
    groups = [
        {
            "scenario_id": scenario_id,
            "function_symbol_id": function_symbol_id,
            "annotation_id": annotation_id,
            "access_event_ids": sorted(access_ids),
        }
        for (scenario_id, function_symbol_id, annotation_id), access_ids in grouped.items()
    ]
    groups.sort(key=lambda group: (
        group["scenario_id"], group["function_symbol_id"] or "",
        group["annotation_id"],
    ))
    return groups


def _upstream_state(store: AnalysisStore) -> tuple[list[dict], list[str]]:
    """Freshness of ``ast:c`` and ``sparse_cfg:cpp`` over every C source/header.

    Coverage is part of freshness (the ``defect_query`` rule): a base C/C++
    file with no current state row means the derived graph does not cover the
    tree, and the query must fail closed instead of answering over a partial
    graph. A repository with no C/C++ files is trivially covered.
    """
    stale: list[dict] = []
    missing: list[str] = []
    for analyzer in (AST_ANALYZER, SPARSE_CFG_ANALYZER):
        rows = store.storage.rows(
            "SELECT s.file_id, s.status, s.source_hash, s.feature_version, "
            "s.metadata, f.path FROM analysis_state s "
            "JOIN files f ON f.file_id=s.file_id WHERE s.feature=?",
            (analyzer,),
        )
        seen_files: set[str] = set()
        for row in rows:
            seen_files.add(row["path"])
            state = dict(row)
            state["metadata"] = json.loads(row["metadata"] or "{}")
            reasons: list[str] = []
            if state["status"] != "current":
                reasons.append("status_not_current")
            base = store.storage.row(
                "SELECT source_hash FROM file_state WHERE file_id=?", (row["file_id"],)
            )
            if base is None:
                reasons.append("file_missing_from_base")
            elif base["source_hash"] != state["source_hash"]:
                reasons.append("source_hash_changed")
            if int(state["feature_version"]) != ANALYZER_VERSIONS[analyzer]:
                reasons.append("feature_version_changed")
            dependency_reason = dependency_staleness(state["metadata"])
            if dependency_reason is not None:
                reasons.append(dependency_reason)
            if reasons:
                stale.append({"file": row["path"], "feature": analyzer,
                              "reason": "+".join(reasons)})
        expected = {
            row["path"]
            for row in store.storage.rows("SELECT path FROM files")
            if Path(row["path"]).suffix.lower() in C_EXTENSIONS
        }
        if expected:
            uncovered = sorted(expected - seen_files)
            if uncovered:
                stale.append({"feature": analyzer, "reason": "files_not_covered",
                              "files": uncovered[:20], "count": len(uncovered)})
            if not rows:
                missing.append(analyzer)
    return stale, missing


def derive_scenario_relations(
    repo: str | Path, *, database: str | Path | None = None,
) -> dict:
    """Derive ``scenario_access_relations[]`` + ``function_scenario_groups[]``.

    Pure reader: no table is created, rebuilt or refreshed. The upstream
    chain (``ast:c`` from FE8-F1 and ``sparse_cfg:cpp`` access events) must
    already be materialised and fresh -- ``index`` + ``analyze --features
    ast,c`` + ``analyze --features sparse_cfg`` does that. A stale or missing
    upstream fails the whole query closed rather than yielding relations over
    partial facts.
    """
    root = Path(repo).resolve()
    database = Path(database).resolve() if database else root / ".provenlattice" / "codegraph.db"
    with SQLiteStorage(database) as storage:
        repository = storage.repository()
        if repository is None:
            raise AnalysisRequestError(
                "base_graph_required", "run `provenlattice index` before scenario queries")
        store = AnalysisStore(storage)
        stale, missing = _upstream_state(store)
        if missing:
            return {
                "status": "not_built",
                "reason": "upstream_not_built",
                "missing_analyzers": missing,
                "scenario_access_relations": [],
                "function_scenario_groups": [],
            }
        if stale:
            return {
                "status": "stale",
                "reason": "upstream_analysis_stale",
                "stale": stale[:20],
                "scenario_access_relations": [],
                "function_scenario_groups": [],
            }
        file_paths = {
            row["file_id"]: _normalize(row["path"])
            for row in store.storage.rows("SELECT file_id, path FROM files")
        }
        annotations = _read_annotations(store)
        accesses = _read_access_events(store)
        relations = _build_relations(annotations, accesses, file_paths)
        groups = _build_groups(relations)
        return {
            "status": "available",
            "reason": None,
            "scenario_access_relations": relations,
            "function_scenario_groups": groups,
            "provenance": {
                "upstream_analyzers": [AST_ANALYZER, SPARSE_CFG_ANALYZER],
                "ast_c_version": ANALYZER_VERSIONS[AST_ANALYZER],
                "sparse_cfg_version": ANALYZER_VERSIONS[SPARSE_CFG_ANALYZER],
                "annotation_count": len(annotations),
                "access_event_count": len(accesses),
                "relation_count": len(relations),
                "group_count": len(groups),
            },
        }


__all__ = [
    "CONTAINS_ACCESS", "BYTE_RANGE_EXACT", "AST_ANALYZER", "SPARSE_CFG_ANALYZER",
    "derive_scenario_relations",
]
