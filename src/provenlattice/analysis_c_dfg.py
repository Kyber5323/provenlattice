"""Sparse DFG materializer (I1-C) over the derived-analysis store.

Ported from PR #1's stage-5B mint and re-shaped for main's architecture, the
same way the I1-A/I1-B analyzers were:

* On demand only -- ``analyze --features sparse_dfg``; the default index never
  runs the mint. File x analyzer cached in AnalysisStore, fail-closed
  freshness with a dependency attestation over BOTH feeds
  (``dependencies`` = ``{"events:cpp": ..., "sparse_cfg:cpp": ...}``): a
  vocabulary/matcher or walker upgrade invalidates every DFG built under the
  previous one, on the rebuild path AND on both read paths (the I1-B unified
  rule).
* The mint consumes the SAME hits and the SAME walker output the sparse_cfg
  analyzer consumed -- one matcher, one walker, no drift. The minted edges
  reference event ids under the shared identity scheme
  (``identity.event_id`` / ``cfg_exit_id``); the endpoint rows live under the
  sparse_cfg:cpp feature, which the orchestrator materializes as this
  feature's declared dependency.
* Scope honesty (PR-registered approximations, unchanged): method-local only;
  no SSA and no reaching-definitions (a use binds to the newest same-identity
  definition in source order, not to every reaching def); a RELEASE kills
  only the live ALLOC, not a plain WRITE; alias chains extend definitions
  without points-to; fields/arrays participate through their member path in
  the flow identity; cross-function flow is a query-time join in the defect
  layer (I2), not edges. ``CONSUMES`` covers only the allocation handed
  through its own acquiring expression (``f(malloc(n))``) -- the
  ``p = malloc(n); f(p)`` shape belongs to DATA_FLOW_TO instead.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from .analysis import (
    ANALYZER_VERSIONS,
    AnalysisEdge,
    AnalysisStore,
    PayloadTooLargeError,
    analyzer_contract,
    analyzer_id,
)
from .analysis_ast import normalize_scope, scoped_sources
from .analysis_c_events import EVENTS_CONTRACT, EVENTS_SUFFIXES, VOCABULARY
from .analysis_c_scfg import SCFG_CONTRACT
from .analysis_semantic_common import (
    _LineIndex,
    owner_index,
    owner_symbol_id,
    tree_diagnostics,
)
from .identity import event_id, path_id
from .models import PointKey
from .parsing.cpp import CppTreeSitterParser
from .parsing.declarations import DeclarationIndex
from .parsing.events import EventExtractor
from .semantics.events import ACCESS_EVENTS
from .sparsecfg import (
    CPP_PROFILE,
    C_PROFILE,
    SparseCfgBuilder,
)
from .sparsedfg import (
    CONSUMES,
    DATA_FLOW_TO,
    mint_dataflow_edges,
)
from .storage import SQLiteStorage

ANALYZER = analyzer_id("sparse_dfg", "cpp")
DFG_VERSION = ANALYZER_VERSIONS[ANALYZER]
DFG_CONTRACT = analyzer_contract("sparse_dfg", "cpp", DFG_VERSION)
CAPABILITY_LEVEL = "level0+structural+access+dfg"
CAPABILITY_GRADE = "local-only, diagnostic"

_C_SUFFIXES = frozenset({".c", ".h"})


def analyze_c_dfg_scope(
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
    skipped_no_edges: list[str] = []
    skipped_payload_too_large: list[str] = []
    counters = {
        "dfg_edges": 0, "data_flow_to": 0, "consumes": 0,
        "functions_bound": 0, "functions_unbound": 0,
    }
    edges_written = 0
    parser = CppTreeSitterParser(language="cpp")
    with SQLiteStorage(database) as storage:
        repository = storage.repository()
        if not repository:
            raise RuntimeError("repository is not indexed; run `provenlattice index` first")
        repo_id = repository["repo_id"]
        store = AnalysisStore(storage)
        # One transaction for the whole file loop instead of one commit per
        # file; see AnalysisStore.defer_commit for the measurement.
        store.defer_commit()
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
                and state.get("metadata", {}).get("contract") == DFG_CONTRACT
                and state.get("metadata", {}).get("analyzer") == ANALYZER
                and store.is_analysis_fresh(file_id, ANALYZER, base_hash, DFG_VERSION)
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

            # -- the same publication the sparse_cfg analyzer performs: one id
            # per vocabulary event and per walker point, keyed by PointKey
            id_by_key: dict = {}
            for hit in hits:
                event = hit.event
                key = PointKey(event.owner_kind, event.owner_qualified_name,
                               event.owner_signature, event.event_type,
                               event.ordinal)
                id_by_key[key] = event_id(
                    repo_id,
                    _resolve(owner_ids, event.owner_kind, event.owner_qualified_name,
                             event.owner_signature),
                    event.event_type, event.ordinal)
            for point in cfg.points:
                key = PointKey(point.owner_kind, point.owner_qualified_name,
                               point.owner_signature, point.event_type,
                               point.ordinal)
                id_by_key[key] = event_id(
                    repo_id,
                    _resolve(owner_ids, point.owner_kind, point.owner_qualified_name,
                             point.owner_signature),
                    point.event_type, point.ordinal)

            parsed_view = SimpleNamespace(
                events=[hit.event for hit in hits], cfg=cfg)
            semantic_edges = mint_dataflow_edges(
                repo_id, parsed_view, id_by_key, file_id,
                relative_path=source.relative_path, shard_path="")
            # -- evidence contract (I1C-R1): the mint's control_reachable is
            # binary over the CONNECTED subgraph, but the walker publishes
            # access points (READ/WRITE) without control edges
            # (_EDGELESS_EVENT_TYPES) and a partial parse may hide paths. A
            # binary false therefore conflates "no path in the graph" with
            # "the graph cannot answer". Reclassify honestly:
            #   true            a control path exists in the observed graph
            #   false           both endpoints connected, BFS found no path
            #   unknown         an endpoint is an edgeless access point, or
            #                   the parse was partial (paths may be missing)
            event_type_by_id = {
                node_id: key.event_type for key, node_id in id_by_key.items()}
            edges = []
            for edge in semantic_edges:
                payload = dict(edge.metadata)
                if payload.get("control_reachable") == "false":
                    src_type = event_type_by_id.get(edge.src_event_id)
                    dst_type = event_type_by_id.get(edge.dst_event_id)
                    if diagnostics["partial"]:
                        payload["control_reachable"] = "unknown"
                        payload["control_reachable_note"] = (
                            "partial parse: the control graph may be incomplete")
                    elif (src_type in ACCESS_EVENTS or dst_type in ACCESS_EVENTS):
                        payload["control_reachable"] = "unknown"
                        payload["control_reachable_note"] = (
                            "endpoint is an edgeless access point (READ/WRITE "
                            "publish without control edges)")
                edges.append(
                    AnalysisEdge(
                        edge.edge_id, edge.src_event_id, edge.dst_event_id,
                        edge.relation, payload,
                    ))

            try:
                store.replace_analysis_for_file(
                    file_id, ANALYZER, base_hash, DFG_VERSION, [], edges,
                    metadata={
                        "analyzer": ANALYZER, "contract": DFG_CONTRACT,
                        "language": VOCABULARY.language, "level": CAPABILITY_LEVEL,
                        "grade": CAPABILITY_GRADE,
                        "scope": scope_path.as_posix(),
                        "edge_count": len(edges),
                        "data_flow_to": sum(1 for e in edges
                                            if e.kind == DATA_FLOW_TO),
                        "consumes": sum(1 for e in edges if e.kind == CONSUMES),
                        "dependencies": {
                            "events:cpp": EVENTS_CONTRACT,
                            "sparse_cfg:cpp": SCFG_CONTRACT,
                        },
                        **owner_counters,
                        **diagnostics,
                        "analysis_status": "degraded" if diagnostics["partial"] else "complete",
                    },
                )
            except PayloadTooLargeError:
                skipped_payload_too_large.append(source.relative_path)
                continue
            if not edges:
                # Zero data-flow relations is a legitimate outcome (files whose
                # events carry no subject annotations mint nothing); the empty
                # state persists so freshness answers repeats.
                skipped_no_edges.append(source.relative_path)
            materialized.append(source.relative_path)
            counters["dfg_edges"] += len(edges)
            counters["data_flow_to"] += sum(1 for e in edges if e.kind == DATA_FLOW_TO)
            counters["consumes"] += sum(1 for e in edges if e.kind == CONSUMES)
            counters["functions_bound"] += owner_counters["functions_bound"]
            counters["functions_unbound"] += owner_counters["functions_unbound"]
            edges_written += len(edges)
    return {
        "repo_id": repo_id,
        "database": str(database),
        "scope": scope_path.as_posix(),
        "analyzer": ANALYZER,
        "contract": DFG_CONTRACT,
        "feature_version": DFG_VERSION,
        "level": CAPABILITY_LEVEL,
        "grade": CAPABILITY_GRADE,
        "scope_source_files": len(scoped),
        "materialized": sorted(materialized),
        "reused": sorted(reused),
        "skipped_unsupported": sorted(skipped_unsupported),
        "skipped_not_in_base": sorted(skipped_not_in_base),
        "skipped_base_out_of_date": sorted(skipped_base_out_of_date),
        "skipped_no_edges": sorted(skipped_no_edges),
        "skipped_payload_too_large": sorted(skipped_payload_too_large),
        "dfg_edges": counters["dfg_edges"],
        "data_flow_to": counters["data_flow_to"],
        "consumes": counters["consumes"],
        "functions_bound": counters["functions_bound"],
        "functions_unbound": counters["functions_unbound"],
        "edges_written": edges_written,
        "analyze_time_ms": (time.perf_counter() - started) * 1000,
    }


def _resolve(owner_ids: dict, owner_kind: str, owner_qualified_name: str,
             owner_signature: str) -> str:
    event = SimpleNamespace(
        owner_kind=owner_kind,
        owner_qualified_name=owner_qualified_name,
        owner_signature=owner_signature,
    )
    return owner_symbol_id(owner_ids, event)
