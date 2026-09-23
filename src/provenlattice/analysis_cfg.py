"""Python intraprocedural CFG V1 materializer over the derived-analysis store."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path, PurePosixPath

from .analysis import (
    ANALYZER_VERSIONS,
    AnalysisEdge,
    AnalysisNode,
    AnalysisStore,
    PayloadTooLargeError,
    analyzer_contract,
    analyzer_id,
)
from .analysis_ast import (
    PYTHON_AST_CONTRACT,
    ast_node_id,
    normalize_scope,
    scoped_sources,
    structural_paths,
)
from .identity import path_id
from .parser import parser_for_language
from .scanner import scan_repository
from .storage import SQLiteStorage

AST_ANALYZER = "ast:python"
ANALYZER = analyzer_id("cfg", "python")
PYTHON_CFG_VERSION = ANALYZER_VERSIONS[ANALYZER]
PYTHON_CFG_CONTRACT = analyzer_contract("cfg", "python", PYTHON_CFG_VERSION)

FUNCTION_KINDS = frozenset({"function_definition", "async_function_definition"})
FORBIDDEN_CONSTRUCTS = frozenset({
    "try_statement", "with_statement", "match_statement", "raise_statement",
    "yield", "yield_from", "async_for_statement", "async_with_statement",
})


class _Unsupported(Exception):
    pass


class _LoopFrame:
    def __init__(self, header_id: str) -> None:
        self.header_id = header_id
        self.breaks: list[str] = []


class _FunctionCFG:
    def __init__(self, file_id: str, owner_symbol_id: str) -> None:
        self.file_id = file_id
        self.owner = owner_symbol_id
        self.nodes: list[AnalysisNode] = []
        self.edges: list[AnalysisEdge] = []
        self.returns: list[str] = []
        self.loop_stack: list[_LoopFrame] = []
        self._edge_ordinals: dict[tuple[str, str, str], int] = {}

    def node(
        self, kind: str, role: str, path: str, *, ast_node: str | None = None,
        start_byte: int | None = None, end_byte: int | None = None,
        metadata: dict | None = None,
    ) -> str:
        canonical = "\x1f".join((self.file_id, self.owner, role, path))
        node_id = f"cnode:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
        self.nodes.append(
            AnalysisNode(node_id, kind, {
                "owner_symbol_id": self.owner,
                "ast_node_id": ast_node,
                "start_byte": start_byte,
                "end_byte": end_byte,
                "structural_role": role,
                "structural_path": path,
                "metadata": metadata or {},
            })
        )
        return node_id

    def edge(self, src_id: str, dst_id: str, kind: str) -> None:
        key = (src_id, dst_id, kind)
        ordinal = self._edge_ordinals.get(key, 0)
        self._edge_ordinals[key] = ordinal + 1
        canonical = "\x1f".join((*key, str(ordinal)))
        edge_id = f"cedge:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
        self.edges.append(
            AnalysisEdge(edge_id, src_id, dst_id, kind, {"owner_symbol_id": self.owner})
        )


def _ast_id_for(builder_file_id: str, paths: dict, node) -> str | None:
    path = paths.get((node.type, node.start_byte, node.end_byte))
    if path is None:
        return None
    return ast_node_id(builder_file_id, path)


def _is_async(node) -> bool:
    return any(child.type == "async" for child in node.children)


def _scan_unsupported(function_node) -> str | None:
    stack = [function_node]
    while stack:
        node = stack.pop()
        if node is not function_node and node.type in FUNCTION_KINDS:
            continue
        if node.type in FORBIDDEN_CONSTRUCTS:
            return node.type
        if node.type in {"for_statement", "while_statement"} and _is_async(node):
            return "async_loop"
        if node.type in {"for_statement", "while_statement"} and any(
            child.type == "else_clause" for child in node.named_children
        ):
            return "loop_else"
        stack.extend(node.named_children)
    return None


def analyze_cfg_scope(repo: str | Path, scope: str, database: str | Path | None = None) -> dict:
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
    skipped_ast_missing: list[str] = []
    skipped_ast_stale: list[str] = []
    skipped_payload_too_large: list[str] = []
    counters = {
        "functions_built": 0, "functions_skipped_unmatched_owner": 0,
        "functions_skipped_unsupported_construct": 0,
        "functions_skipped_ast_node_missing": 0,
    }
    nodes_written = 0
    edges_written = 0
    with SQLiteStorage(database) as storage:
        repository = storage.repository()
        if not repository:
            raise RuntimeError("repository is not indexed; run `provenlattice index` first")
        repo_id = repository["repo_id"]
        store = AnalysisStore(storage)
        scope_file_ids = {
            path_id(repo_id, "File", source.relative_path) for source in scoped
        }
        for source in scoped:
            if source.language != "python":
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
            ast_read = store.read_current_analysis(file_id, AST_ANALYZER)
            if ast_read is None or ast_read["state"]["metadata"].get("contract") != PYTHON_AST_CONTRACT:
                skipped_ast_missing.append(source.relative_path)
                continue
            if not ast_read["fresh"]:
                skipped_ast_stale.append(source.relative_path)
                continue
            state = store.get_analysis_state(file_id, ANALYZER)
            if (
                state is not None
                and state.get("metadata", {}).get("contract") == PYTHON_CFG_CONTRACT
                and state.get("metadata", {}).get("analyzer") == ANALYZER
                and store.is_analysis_fresh(file_id, ANALYZER, base_hash, PYTHON_CFG_VERSION)
            ):
                reused.append(source.relative_path)
                continue
            try:
                nodes, edges, file_counters = _build_file_cfg(
                    store, file_id, source, ast_read, scope_file_ids
                )
                store.replace_analysis_for_file(
                    file_id, ANALYZER, base_hash, PYTHON_CFG_VERSION, nodes, edges,
                    metadata={
                        "analyzer": ANALYZER, "contract": PYTHON_CFG_CONTRACT,
                        "language": "python", "scope": scope_path.as_posix(),
                        "node_count": len(nodes), "edge_count": len(edges),
                        "ast_contract": PYTHON_AST_CONTRACT, **file_counters,
                    },
                )
            except PayloadTooLargeError:
                skipped_payload_too_large.append(source.relative_path)
                continue
            for key in counters:
                counters[key] += file_counters.get(key, 0)
            materialized.append(source.relative_path)
            nodes_written += len(nodes)
            edges_written += len(edges)
    return {
        "repo_id": repo_id,
        "database": str(database),
        "scope": scope_path.as_posix(),
        "analyzer": ANALYZER,
        "contract": PYTHON_CFG_CONTRACT,
        "feature_version": PYTHON_CFG_VERSION,
        "ast_dependency": AST_ANALYZER,
        "scope_source_files": len(scoped),
        "materialized": sorted(materialized),
        "reused": sorted(reused),
        "skipped_unsupported": sorted(skipped_unsupported),
        "skipped_not_in_base": sorted(skipped_not_in_base),
        "skipped_base_out_of_date": sorted(skipped_base_out_of_date),
        "skipped_ast_missing": sorted(skipped_ast_missing),
        "skipped_ast_stale": sorted(skipped_ast_stale),
        "skipped_payload_too_large": sorted(skipped_payload_too_large),
        "functions_built": counters["functions_built"],
        "functions_skipped_unmatched_owner": counters["functions_skipped_unmatched_owner"],
        "functions_skipped_unsupported_construct": counters["functions_skipped_unsupported_construct"],
        "functions_skipped_ast_node_missing": counters["functions_skipped_ast_node_missing"],
        "nodes_written": nodes_written,
        "edges_written": edges_written,
        "analyze_time_ms": (time.perf_counter() - started) * 1000,
    }


def _build_file_cfg(
    store: AnalysisStore, file_id: str, source, ast_read: dict, scope_file_ids: set[str]
) -> tuple[list[AnalysisNode], list[AnalysisEdge], dict]:
    outcome = parser_for_language("python").parse_bytes(
        source.path.read_bytes(), source.relative_path
    )
    root = outcome.tree.root_node
    paths = structural_paths(root)
    function_alignment = {}
    for row in ast_read["nodes"]:
        if row["kind"] in FUNCTION_KINDS:
            payload = row["payload"]
            key = (payload["start_byte"], payload["end_byte"])
            function_alignment[key] = (
                payload["owner_symbol_id"] if payload["alignment"] == "resolved" else None
            )
    ast_ids = {row["analysis_node_id"] for row in ast_read["nodes"]}
    calls_index: dict[tuple[str, int], list[dict]] = {}
    for row in store.storage.rows(
        "SELECT owner_symbol_id, start_line, status, resolved_symbol_id FROM raw_references "
        "WHERE file_id=? AND reference_type='CALLS'", (file_id,)
    ):
        if row["start_line"] is not None:
            calls_index.setdefault((row["owner_symbol_id"], row["start_line"]), []).append(row)
    scope_symbols = {
        row["id"] for row in store.storage.rows(
            "SELECT id FROM nodes WHERE file_id IN (%s)"
            % ",".join("?" for _ in scope_file_ids),
            tuple(sorted(scope_file_ids)),
        )
    }
    nodes: list[AnalysisNode] = []
    edges: list[AnalysisEdge] = []
    counters = {
        "functions_built": 0, "functions_skipped_unmatched_owner": 0,
        "functions_skipped_unsupported_construct": 0,
        "functions_skipped_ast_node_missing": 0,
    }
    ordered = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in FUNCTION_KINDS:
            ordered.append(node)
        stack.extend(node.named_children)
    for function_node in ordered:
        key = (function_node.start_byte, function_node.end_byte)
        if key not in function_alignment:
            counters["functions_skipped_ast_node_missing"] += 1
            continue
        owner = function_alignment[key]
        if owner is None:
            counters["functions_skipped_unmatched_owner"] += 1
            continue
        if _is_async(function_node):
            counters["functions_skipped_unsupported_construct"] += 1
            continue
        unsupported = _scan_unsupported(function_node)
        if unsupported is not None:
            counters["functions_skipped_unsupported_construct"] += 1
            continue
        builder = _CFGBuilder(file_id, owner, paths, ast_ids, calls_index, scope_symbols)
        function_nodes, function_edges = builder.build(function_node)
        nodes.extend(function_nodes)
        edges.extend(function_edges)
        counters["functions_built"] += 1
    return nodes, edges, counters


class _CFGBuilder:
    def __init__(
        self, file_id: str, owner_symbol_id: str, paths: dict, ast_ids: set[str],
        calls_index: dict, scope_symbols: set[str],
    ) -> None:
        self.f = _FunctionCFG(file_id, owner_symbol_id)
        self.paths = paths
        self.ast_ids = ast_ids
        self.calls_index = calls_index
        self.scope_symbols = scope_symbols
        self.exit_id: str | None = None

    def build(self, function_node):
        function_path = self.paths[(function_node.type, function_node.start_byte, function_node.end_byte)]
        entry = self.f.node("entry", "entry", function_path)
        self.exit_id = self.f.node("exit", "exit", function_path)
        first, ends = self.build_block(function_node.child_by_field_name("body"))
        if first is None:
            self.f.edge(entry, self.exit_id, "next")
        else:
            self.f.edge(entry, first, "next")
            for end in ends:
                self.f.edge(end, self.exit_id, "next")
        for return_id in self.f.returns:
            self.f.edge(return_id, self.exit_id, "return")
        return self.f.nodes, self.f.edges

    def _path(self, node) -> str:
        return self.paths[(node.type, node.start_byte, node.end_byte)]

    def _ast_node(self, node) -> str | None:
        candidate = _ast_id_for(self.f.file_id, self.paths, node)
        return candidate if candidate in self.ast_ids else None

    def build_block(self, block_node):
        return self.build_sequence(block_node.named_children)

    def build_sequence(self, statements):
        first = None
        pending: list[str] = []
        for statement in statements:
            entry, ends = self.build_stmt(statement)
            if entry is None:
                continue
            if first is None:
                first = entry
            for end in pending:
                self.f.edge(end, entry, "next")
            pending = ends
        return first, pending

    def _simple(self, kind: str, node) -> str:
        return self.f.node(
            kind, "statement", self._path(node),
            ast_node=self._ast_node(node),
            start_byte=node.start_byte, end_byte=node.end_byte,
        )

    def _callsite(self, statement) -> str:
        call = statement.named_children[0]
        candidates = self.calls_index.get(
            (self.f.owner, call.start_point[0] + 1), [])
        if len(candidates) == 1 and candidates[0]["status"] == "resolved" \
                and candidates[0]["resolved_symbol_id"]:
            resolved = candidates[0]["resolved_symbol_id"]
            target_scope = "internal" if resolved in self.scope_symbols else "external"
        else:
            resolved, target_scope = None, "unknown"
        return self.f.node(
            "callsite", "statement", self._path(call),
            ast_node=self._ast_node(call),
            start_byte=call.start_byte, end_byte=call.end_byte,
            metadata={"resolved_symbol_id": resolved, "target_scope": target_scope},
        )

    def build_stmt(self, statement):
        kind = statement.type
        if kind == "if_statement":
            return self._build_if(statement)
        if kind in {"for_statement", "while_statement"}:
            return self._build_loop(statement)
        if kind == "return_statement":
            node_id = self._simple("return", statement)
            self.f.returns.append(node_id)
            return node_id, []
        if kind == "break_statement":
            node_id = self._simple("break", statement)
            self.f.loop_stack[-1].breaks.append(node_id)
            return node_id, []
        if kind == "continue_statement":
            node_id = self._simple("continue", statement)
            self.f.edge(node_id, self.f.loop_stack[-1].header_id, "continue")
            return node_id, []
        if kind == "expression_statement" and statement.named_children \
                and statement.named_children[0].type == "call":
            node_id = self._callsite(statement)
            return node_id, [node_id]
        node_id = self._simple("statement", statement)
        return node_id, [node_id]

    def _build_if(self, node):
        condition = node.child_by_field_name("condition")
        condition_id = self.f.node(
            "condition", "if_condition", self._path(condition),
            ast_node=self._ast_node(condition),
            start_byte=condition.start_byte, end_byte=condition.end_byte,
        )
        merge = self.f.node("merge", "branch_merge", self._path(node))
        true_entry, true_ends = self.build_block(node.child_by_field_name("consequence"))
        else_entry, else_ends = None, []
        for clause in node.named_children:
            if clause.type == "elif_clause":
                inner = next(
                    child for child in clause.named_children if child.type == "if_statement"
                )
                else_entry, else_ends = self._build_if(inner)
                break
            if clause.type == "else_clause":
                block = next(
                    child for child in clause.named_children if child.type == "block"
                )
                else_entry, else_ends = self.build_block(block)
                break
        self.f.edge(condition_id, true_entry or merge, "true")
        self.f.edge(condition_id, else_entry or merge, "false")
        for end in true_ends:
            self.f.edge(end, merge, "next")
        for end in else_ends:
            self.f.edge(end, merge, "next")
        return condition_id, [merge]

    def _build_loop(self, node):
        header = self.f.node(
            "condition", "loop_header", self._path(node),
            ast_node=self._ast_node(node),
            start_byte=node.start_byte, end_byte=node.end_byte,
        )
        exit_merge = self.f.node("merge", "loop_exit", self._path(node))
        frame = _LoopFrame(header)
        self.f.loop_stack.append(frame)
        body_entry, body_ends = self.build_block(node.child_by_field_name("body"))
        self.f.loop_stack.pop()
        self.f.edge(header, body_entry or exit_merge, "true")
        self.f.edge(header, exit_merge, "false")
        for end in body_ends:
            self.f.edge(end, header, "back")
        for break_id in frame.breaks:
            self.f.edge(break_id, exit_merge, "break")
        return header, [exit_merge]
