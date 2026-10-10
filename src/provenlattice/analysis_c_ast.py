"""Optional normalized C AST materializer over the derived-analysis store."""

from __future__ import annotations

import time
from pathlib import Path

from .analysis import (
    ANALYZER_VERSIONS,
    AnalysisNode,
    AnalysisStore,
    PayloadTooLargeError,
    analyzer_contract,
    analyzer_id,
)
from .analysis_ast import ast_node_id, normalize_scope, scoped_sources
from .analysis_c_frontend import CFrontend, NormalizedCNode
from .identity import path_id
from .storage import SQLiteStorage


ANALYZER = analyzer_id("ast", "c")
C_AST_VERSION = ANALYZER_VERSIONS[ANALYZER]
C_AST_CONTRACT = analyzer_contract("ast", "c", C_AST_VERSION)
C_EXTENSIONS = frozenset({".c", ".h"})
ALIGNABLE = {
    ("FUNCTION", "function_definition"): frozenset({"Function", "Method"}),
    ("FUNCTION", "declaration"): frozenset({"Function", "Method"}),
    ("TYPE_DECL", "struct_specifier"): frozenset({"Struct", "Class"}),
    ("TYPE_DECL", "type_definition"): frozenset({"Type"}),
}


def is_c_ast_source(source) -> bool:
    return Path(source.relative_path).suffix.lower() in C_EXTENSIONS


def analyze_c_ast_scope(
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
    skipped_payload_too_large: list[str] = []
    nodes_written = 0
    with SQLiteStorage(database) as storage:
        repository = storage.repository()
        if not repository:
            raise RuntimeError("repository is not indexed; run `provenlattice index` first")
        repo_id = repository["repo_id"]
        store = AnalysisStore(storage)
        for source in scoped:
            if not is_c_ast_source(source):
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
                and state.get("metadata", {}).get("contract") == C_AST_CONTRACT
                and state.get("metadata", {}).get("analyzer") == ANALYZER
                and store.is_analysis_fresh(file_id, ANALYZER, base_hash, C_AST_VERSION)
            ):
                reused.append(source.relative_path)
                continue
            source_bytes = source.path.read_bytes()
            frontend = CFrontend().parse(source_bytes, source.relative_path)
            nodes = _materialize(store, file_id, frontend.root)
            diagnostics = frontend.diagnostics
            try:
                store.replace_analysis_for_file(
                    file_id, ANALYZER, base_hash, C_AST_VERSION, nodes, [],
                    metadata={
                        "analyzer": ANALYZER,
                        "contract": C_AST_CONTRACT,
                        "language": "c",
                        "frontend": "tree-sitter-c",
                        "scope": scope_path.as_posix(),
                        "node_count": len(nodes),
                        "error_node_count": diagnostics.error_nodes,
                        "missing_node_count": diagnostics.missing_nodes,
                        "parse_coverage": diagnostics.parse_coverage,
                        "analysis_status": "degraded" if diagnostics.partial else "complete",
                        "partial": diagnostics.partial,
                    },
                )
            except PayloadTooLargeError:
                skipped_payload_too_large.append(source.relative_path)
                continue
            materialized.append(source.relative_path)
            nodes_written += len(nodes)
    return {
        "repo_id": repo_id,
        "database": str(database),
        "scope": scope_path.as_posix(),
        "analyzer": ANALYZER,
        "contract": C_AST_CONTRACT,
        "feature_version": C_AST_VERSION,
        "scope_source_files": len(scoped),
        "materialized": sorted(materialized),
        "reused": sorted(reused),
        "skipped_unsupported": sorted(skipped_unsupported),
        "skipped_not_in_base": sorted(skipped_not_in_base),
        "skipped_base_out_of_date": sorted(skipped_base_out_of_date),
        "skipped_payload_too_large": sorted(skipped_payload_too_large),
        "nodes_written": nodes_written,
        "analyze_time_ms": (time.perf_counter() - started) * 1000,
    }


def _symbol_spans(store: AnalysisStore, file_id: str) -> dict:
    spans: dict[tuple[int, int], list[dict]] = {}
    for row in store.storage.rows(
        "SELECT id, kind, start_line, end_line FROM nodes WHERE file_id=?", (file_id,)
    ):
        if row["start_line"] is not None:
            spans.setdefault((row["start_line"], row["end_line"]), []).append(
                {"id": row["id"], "kind": row["kind"]}
            )
    return spans


def _materialize(
    store: AnalysisStore, file_id: str, root: NormalizedCNode
) -> list[AnalysisNode]:
    spans = _symbol_spans(store, file_id)
    nodes: list[AnalysisNode] = []
    stack = [(root, None, root.kind)]
    while stack:
        node, parent_id, path = stack.pop()
        node_id = ast_node_id(file_id, path)
        owner_symbol_id, alignment = None, "unmatched"
        expected = ALIGNABLE.get((node.kind, node.parser_kind))
        if expected is not None:
            key = (node.start_point[0] + 1, node.end_point[0] + 1)
            candidates = [item for item in spans.get(key, []) if item["kind"] in expected]
            if len(candidates) == 1:
                owner_symbol_id, alignment = candidates[0]["id"], "resolved"
        nodes.append(AnalysisNode(node_id, node.kind, {
            "parent_id": parent_id,
            "structural_path": path,
            "language": "c",
            "parser_kind": node.parser_kind,
            "start_byte": node.start_byte,
            "end_byte": node.end_byte,
            "start_row": node.start_point[0],
            "start_col": node.start_point[1],
            "end_row": node.end_point[0],
            "end_col": node.end_point[1],
            "child_order": node.child_order,
            "name": node.name,
            "owner_symbol_id": owner_symbol_id,
            "alignment": alignment,
            "partial": node.is_error or node.is_missing,
        }))
        for child in reversed(node.children):
            child_path = f"{path}/{child.kind}[{child.child_order}]"
            stack.append((child, node_id, child_path))
    return nodes
