"""Sparse CFG materializer (I1-B) over the derived-analysis store.

Ported from PR #1's stage-3 walker and re-shaped for main's architecture, the
same way the I1-A events analyzer was:

* On demand only -- ``analyze --features sparse_cfg``; the default index never
  runs the walker. File x analyzer cached in AnalysisStore, fail-closed
  freshness with a dependency attestation (``dependencies`` =
  ``{"events:cpp": EVENTS_CONTRACT}``): a vocabulary/matcher upgrade
  invalidates every sparse CFG built under the previous one.
* The walker consumes the SAME hits the event layer consumed
  (``EventExtractor.extract_hits``), so an operation cannot be an event while
  being invisible to the CFG -- one matcher, two consumers, no drift.
* Owner binding and stable identity are shared with the events analyzer
  (analysis_semantic_common): vocabulary events get the same
  ``identity.event_id`` here that the events:cpp analyzer computes, and the
  walker's structural points (CHECK/BRANCH/DEREFERENCE) and access events
  (READ/WRITE) get ordinals from the walker's own source-order counters --
  the two families cannot collide because the structural types never come
  from the vocabulary.
* Published rows: one analysis_nodes row per event/point (kind = event type,
  payload carries the owner binding, span, matched facts and the point's
  annotations), one analysis_edges row per CONTROL_REACHES edge
  (``identity.semantic_edge_id``, flag set as discriminator, synthetic exit
  targets resolved as ``identity.cfg_exit_id``).
* Language scope: the C/C++ family (cpp.toml extensions); the C or C++
  grammar profile is chosen per suffix. Known precision boundaries (goto
  targets, macro invocations, single-line short-circuit collapse) travel with
  the PR walker unchanged and are surfaced as diagnostics, never as resolved
  claims.
"""

from __future__ import annotations

import time
from pathlib import Path

from .analysis import (
    ANALYZER_VERSIONS,
    AnalysisEdge,
    AnalysisNode,
    AnalysisStore,
    PayloadTooLargeError,
    analyzer_contract,
    analyzer_id,
)
from .analysis_ast import normalize_scope, scoped_sources
from .models import PointKey
from .analysis_c_events import EVENTS_CONTRACT, EVENTS_SUFFIXES, VOCABULARY
from .analysis_semantic_common import (
    _LineIndex,
    owner_index,
    owner_symbol_id,
    tree_diagnostics,
)
from .identity import cfg_exit_id, event_id, path_id, semantic_edge_id
from .parsing.cpp import CppTreeSitterParser
from .parsing.declarations import DeclarationIndex
from .parsing.events import EventExtractor
from .semantics.vocabulary import vocabulary_for_path
from .sparsecfg import (
    CONTROL_REACHES,
    CPP_PROFILE,
    C_PROFILE,
    SparseCfgBuilder,
)
from .storage import SQLiteStorage

ANALYZER = analyzer_id("sparse_cfg", "cpp")
SCFG_VERSION = ANALYZER_VERSIONS[ANALYZER]
SCFG_CONTRACT = analyzer_contract("sparse_cfg", "cpp", SCFG_VERSION)
CAPABILITY_LEVEL = "level0+structural+access"

_C_SUFFIXES = frozenset({".c", ".h"})


def analyze_c_scfg_scope(
    repo: str | Path, scope: str, database: str | Path | None = None
) -> dict:
    started = time.perf_counter()
    root = Path(repo).resolve()
    scope_path = normalize_scope(scope)
    if not (root.joinpath(*scope_path.parts)).is_dir():
        raise ValueError(f"scope directory does not exist: {scope}")
    database = Path(database).resolve() if database else root / ".provenlattice" / "codegraph.db"
    scoped = scoped_sources(root, scope_path)
    materialized: list[str] = []
    reused: list[str] = []
    skipped_unsupported: list[str] = []
    skipped_not_in_base: list[str] = []
    skipped_base_out_of_date: list[str] = []
    skipped_no_points: list[str] = []
    skipped_payload_too_large: list[str] = []
    counters = {
        "points_published": 0, "edges_published": 0,
        "events_outside_owner": 0,
        "functions_bound": 0, "functions_unbound": 0,
    }
    nodes_written = 0
    edges_written = 0
    parser = CppTreeSitterParser(language="cpp")
    with SQLiteStorage(database) as storage:
        repository = storage.repository()
        if not repository:
            raise RuntimeError("repository is not indexed; run `provenlattice index` first")
        repo_id = repository["repo_id"]
        store = AnalysisStore(storage)
        for source in scoped:
            if Path(source.relative_path).suffix.lower() not in EVENTS_SUFFIXES:
                skipped_unsupported.append(source.relative_path)
                continue
            file_id = path_id(repo_id, "File", source.relative_path)
            base_hash = store.base_source_hash(file_id)
            if base_hash is None:
                skipped_not_in_base.append(source.relative_path)
                continue
            if base_hash != source.source_hash:
                skipped_base_out_of_date.append(source.relative_path)
                continue
            state = store.get_analysis_state(file_id, ANALYZER)
            if (
                state is not None
                and state.get("metadata", {}).get("contract") == SCFG_CONTRACT
                and state.get("metadata", {}).get("analyzer") == ANALYZER
                and store.is_analysis_fresh(file_id, ANALYZER, base_hash, SCFG_VERSION)
            ):
                reused.append(source.relative_path)
                continue
            source_bytes = source.path.read_bytes()
            outcome = parser.parse_bytes(source_bytes, source.relative_path)
            lines = _LineIndex(source_bytes)
            owners, owner_counters, owner_ids = owner_index(
                store, file_id, outcome.tree.root_node, source_bytes)
            extractor = EventExtractor(source_bytes, VOCABULARY, owners, lines.line_of)
            hits = extractor.extract_hits(outcome.tree.root_node)
            declarations = DeclarationIndex.build(
                source_bytes, source.relative_path, outcome.tree.root_node)
            suffix = Path(source.relative_path).suffix.lower()
            profile = C_PROFILE if suffix in _C_SUFFIXES else CPP_PROFILE
            builder = SparseCfgBuilder(
                source_bytes, owners, hits, lines.line_of, profile,
                declarations=declarations,
                out_param=VOCABULARY.out_param_names("ALLOC"),
            )
            cfg = builder.build(outcome.tree.root_node)
            diagnostics = tree_diagnostics(outcome.tree.root_node)

            # -- publish: vocabulary events + walker points under one identity
            id_by_key: dict = {}
            nodes: list[AnalysisNode] = []
            for hit in hits:
                event = hit.event
                owner = owner_symbol_id(owner_ids, event)
                key = PointKey(event.owner_kind, event.owner_qualified_name,
                               event.owner_signature, event.event_type,
                               event.ordinal)
                node_id = event_id(repo_id, owner, event.event_type, event.ordinal)
                id_by_key[key] = node_id
                payload = {
                    "owner_symbol_id": owner,
                    "file_id": file_id,
                    "owner_kind": event.owner_kind,
                    "owner_qualified_name": event.owner_qualified_name,
                    "owner_signature": event.owner_signature,
                    "ordinal": event.ordinal,
                    "start_byte": hit.start_byte, "end_byte": hit.end_byte,
                    "start_line": event.start_line, "end_line": event.end_line,
                    "matched_name": event.matched_name,
                    "matched_via": event.matched_via,
                    "flags": list(event.flags),
                    "analyzer": ANALYZER, "contract": SCFG_CONTRACT,
                    "language": VOCABULARY.language, "level": CAPABILITY_LEVEL,
                }
                # annotations travel on the node they describe -- vocabulary
                # events included (PR: the annotations dict covers them too,
                # subject facts have nowhere else to travel)
                annotations = cfg.annotations.get(key) or {}
                if annotations:
                    payload["annotations"] = dict(annotations)
                nodes.append(AnalysisNode(node_id, event.event_type, payload))
            for point in cfg.points:
                owner = owner_symbol_id(owner_ids, point)
                key = PointKey(point.owner_kind, point.owner_qualified_name,
                               point.owner_signature, point.event_type,
                               point.ordinal)
                node_id = event_id(repo_id, owner, point.event_type, point.ordinal)
                id_by_key[key] = node_id
                payload = {
                    "owner_symbol_id": owner,
                    "file_id": file_id,
                    "owner_kind": point.owner_kind,
                    "owner_qualified_name": point.owner_qualified_name,
                    "owner_signature": point.owner_signature,
                    "ordinal": point.ordinal,
                    "start_byte": point.start_byte, "end_byte": point.end_byte,
                    "start_line": point.start_line, "end_line": point.end_line,
                    "matched_name": point.matched_name,
                    "matched_via": point.matched_via,
                    "flags": list(point.flags),
                    "analyzer": ANALYZER, "contract": SCFG_CONTRACT,
                    "language": VOCABULARY.language, "level": CAPABILITY_LEVEL,
                }
                # annotations travel on the node they describe (PR: the
                # metadata column takes them without a schema change)
                annotations = cfg.annotations.get(point.key()) or {}
                if annotations:
                    payload["annotations"] = dict(annotations)
                nodes.append(AnalysisNode(node_id, point.event_type, payload))

            # -- publish CONTROL_REACHES edges (exit resolves per source owner)
            edges: list[AnalysisEdge] = []
            for edge in cfg.edges:
                src_id = id_by_key.get(edge.src)
                if src_id is None:
                    raise RuntimeError(
                        f"sparse cfg edge source was never published: {edge.src}")
                src_owner = _owner_of_key(owner_ids, edge.src)
                if edge.dst is None:
                    dst_id = cfg_exit_id(repo_id, src_owner)
                else:
                    dst_id = id_by_key.get(edge.dst)
                    if dst_id is None:
                        raise RuntimeError(
                            f"sparse cfg edge target was never published: {edge.dst}")
                edges.append(AnalysisEdge(
                    semantic_edge_id(src_id, dst_id, CONTROL_REACHES,
                                     "\x1f".join(sorted(edge.flags))),
                    src_id, dst_id, CONTROL_REACHES,
                    {"owner_symbol_id": src_owner,
                     "flags": list(sorted(edge.flags)),
                     "analyzer": ANALYZER, "contract": SCFG_CONTRACT},
                ))

            try:
                store.replace_analysis_for_file(
                    file_id, ANALYZER, base_hash, SCFG_VERSION, nodes, edges,
                    metadata={
                        "analyzer": ANALYZER, "contract": SCFG_CONTRACT,
                        "language": VOCABULARY.language, "level": CAPABILITY_LEVEL,
                        "scope": scope_path.as_posix(),
                        "event_count": len(nodes), "edge_count": len(edges),
                        "events_outside_owner": extractor.skipped_outside_owner,
                        "dependencies": {"events:cpp": EVENTS_CONTRACT},
                        **owner_counters,
                        **diagnostics,
                        "analysis_status": "degraded" if diagnostics["partial"] else "complete",
                    },
                )
            except PayloadTooLargeError:
                skipped_payload_too_large.append(source.relative_path)
                continue
            if not nodes:
                # A file whose methods contain no events and no structural
                # points (rare: every return emits a point) still persists its
                # empty state so the freshness contract answers repeats.
                skipped_no_points.append(source.relative_path)
            materialized.append(source.relative_path)
            counters["points_published"] += len(nodes)
            counters["edges_published"] += len(edges)
            counters["events_outside_owner"] += extractor.skipped_outside_owner
            counters["functions_bound"] += owner_counters["functions_bound"]
            counters["functions_unbound"] += owner_counters["functions_unbound"]
            nodes_written += len(nodes)
            edges_written += len(edges)
    return {
        "repo_id": repo_id,
        "database": str(database),
        "scope": scope_path.as_posix(),
        "analyzer": ANALYZER,
        "contract": SCFG_CONTRACT,
        "feature_version": SCFG_VERSION,
        "level": CAPABILITY_LEVEL,
        "scope_source_files": len(scoped),
        "materialized": sorted(materialized),
        "reused": sorted(reused),
        "skipped_unsupported": sorted(skipped_unsupported),
        "skipped_not_in_base": sorted(skipped_not_in_base),
        "skipped_base_out_of_date": sorted(skipped_base_out_of_date),
        "skipped_no_points": sorted(skipped_no_points),
        "skipped_payload_too_large": sorted(skipped_payload_too_large),
        "points_published": counters["points_published"],
        "edges_published": counters["edges_published"],
        "events_outside_owner": counters["events_outside_owner"],
        "functions_bound": counters["functions_bound"],
        "functions_unbound": counters["functions_unbound"],
        "nodes_written": nodes_written,
        "edges_written": edges_written,
        "analyze_time_ms": (time.perf_counter() - started) * 1000,
    }


def _owner_of_key(owner_ids: dict, key) -> str:
    identity = (key.owner_kind, key.owner_qualified_name, key.owner_signature)
    if identity not in owner_ids or owner_ids[identity] is None:
        raise RuntimeError(f"sparse cfg point owner is unresolvable: {key}")
    return owner_ids[identity]
