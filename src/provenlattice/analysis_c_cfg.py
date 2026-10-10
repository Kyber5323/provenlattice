"""C function-local CFG V1 materializer over the normalized C AST boundary."""

from __future__ import annotations

import hashlib
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
from .analysis_ast import ast_node_id, normalize_scope, scoped_sources
from .analysis_c_ast import C_AST_CONTRACT, is_c_ast_source
from .analysis_c_frontend import CFrontend, NormalizedCNode
from .analysis_cfg import _FunctionCFG
from .identity import path_id
from .parser import parser_for_language
from .storage import SQLiteStorage

ANALYZER = analyzer_id("cfg", "c")
C_CFG_VERSION = ANALYZER_VERSIONS[ANALYZER]
C_CFG_CONTRACT = analyzer_contract("cfg", "c", C_CFG_VERSION)
AST_ANALYZER = "ast:c"

CASE_PARSER_KINDS = frozenset({"case_statement"})
STATEMENT_KINDS = frozenset({
    "BLOCK", "EXPRESSION", "RETURN", "LABEL", "LOCAL", "DECLARATION", "UNKNOWN",
})


def _index_tree(root: NormalizedCNode) -> dict:
    """Map every normalized node span to the node and its A1 structural path."""
    index: dict[tuple[int, int], tuple[NormalizedCNode, str]] = {}
    stack = [(root, root.kind)]
    while stack:
        node, path = stack.pop()
        index[(node.start_byte, node.end_byte)] = (node, path)
        for child in node.children:
            stack.append((child, f"{path}/{child.kind}[{child.child_order}]"))
    return index


class _CCfgBuilder:
    """Build one function CFG from the A1 normalized C AST vocabulary."""

    def __init__(
        self, graph: _FunctionCFG, index: dict, ast_ids: set[str],
        calls_index: dict, scope_symbols: set[str], for_index: dict,
    ) -> None:
        self.f = graph
        self.index = index
        self.ast_ids = ast_ids
        self.calls_index = calls_index
        self.scope_symbols = scope_symbols
        self.for_index = for_index
        self.exit_id: str | None = None
        self.labels: dict[str, str] = {}
        self.frames: list[dict] = []
        self.counters = {
            "unresolved_gotos": 0, "unresolved_breaks": 0,
            "unresolved_continues": 0, "error_statements": 0, "stray_cases": 0,
        }

    def build(self, function_node: NormalizedCNode) -> dict:
        function_path = self._path(function_node)
        entry = self.f.node("entry", "entry", function_path)
        self.exit_id = self.f.node("exit", "exit", function_path)
        self._collect_labels(function_node)
        body = next(
            (child for child in function_node.children
             if child.parser_kind == "compound_statement"),
            None,
        )
        first, ends = self.build_seq(body.children) if body is not None else (None, [])
        if first is None:
            self.f.edge(entry, self.exit_id, "next")
        else:
            self.f.edge(entry, first, "next")
            for end in ends:
                self.f.edge(end, self.exit_id, "next")
        for return_id in self.f.returns:
            self.f.edge(return_id, self.exit_id, "return")
        return dict(self.counters)

    def _path(self, node: NormalizedCNode) -> str:
        return self.index[(node.start_byte, node.end_byte)][1]

    def _ast(self, node: NormalizedCNode) -> str | None:
        candidate = ast_node_id(self.f.file_id, self._path(node))
        return candidate if candidate in self.ast_ids else None

    def _linked(self, kind: str, role: str, node: NormalizedCNode, metadata=None) -> str:
        return self.f.node(
            kind, role, self._path(node),
            ast_node=self._ast(node),
            start_byte=node.start_byte, end_byte=node.end_byte,
            metadata=metadata,
        )

    def _preview_node_id(self, role: str, path: str) -> str:
        canonical = "\x1f".join((self.f.file_id, self.f.owner, role, path))
        return f"cnode:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    def _collect_labels(self, function_node: NormalizedCNode) -> None:
        stack = [function_node]
        while stack:
            node = stack.pop()
            if node.kind == "LABEL" and node.name:
                self.labels.setdefault(
                    node.name, self._preview_node_id("label", self._path(node)))
            stack.extend(node.children)

    def build_seq(self, statements) -> tuple[str | None, list[str]]:
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

    def build_stmt(self, statement: NormalizedCNode):
        kind = statement.kind
        parser_kind = statement.parser_kind
        if kind == "BLOCK":
            return self.build_seq(statement.children)
        if kind == "CONTROL_STRUCTURE":
            if parser_kind == "if_statement":
                return self._build_if(statement)
            if parser_kind == "while_statement":
                return self._build_while(statement)
            if parser_kind == "do_statement":
                return self._build_do(statement)
            if parser_kind == "for_statement":
                return self._build_for(statement)
            if parser_kind == "switch_statement":
                return self._build_switch(statement)
            if parser_kind == "break_statement":
                return self._build_break(statement)
            if parser_kind == "continue_statement":
                return self._build_continue(statement)
            if parser_kind == "goto_statement":
                return self._build_goto(statement)
            if parser_kind in CASE_PARSER_KINDS:
                self.counters["stray_cases"] += 1
            return self._simple(statement)
        if kind == "RETURN":
            node_id = self._linked("return", "statement", statement)
            self.f.returns.append(node_id)
            return node_id, []
        if kind == "LABEL":
            return self._build_label(statement)
        if kind == "EXPRESSION":
            inner = statement.children[0] if statement.children else None
            if inner is not None and inner.kind == "CALL":
                node_id = self._callsite(inner)
                return node_id, [node_id]
        if kind == "UNKNOWN":
            self.counters["error_statements"] += 1
        return self._simple(statement)

    def _simple(self, statement: NormalizedCNode):
        node_id = self._linked("statement", "statement", statement)
        return node_id, [node_id]

    def _callsite(self, call: NormalizedCNode) -> str:
        candidates = self.calls_index.get((self.f.owner, call.start_point[0] + 1), [])
        if len(candidates) == 1 and candidates[0]["status"] == "resolved" \
                and candidates[0]["resolved_symbol_id"]:
            resolved = candidates[0]["resolved_symbol_id"]
            target_scope = "internal" if resolved in self.scope_symbols else "external"
        else:
            resolved, target_scope = None, "unknown"
        return self.f.node(
            "callsite", "statement", self._path(call),
            ast_node=self._ast(call),
            start_byte=call.start_byte, end_byte=call.end_byte,
            metadata={"resolved_symbol_id": resolved, "target_scope": target_scope},
        )

    def _condition(
        self, condition: NormalizedCNode | None, role: str, owner: NormalizedCNode
    ) -> str:
        if condition is not None:
            return self._linked("condition", role, condition)
        return self.f.node("condition", role, self._path(owner))

    @staticmethod
    def _split_cond_body(node: NormalizedCNode):
        if node.children and node.children[0].parser_kind == "parenthesized_expression":
            return node.children[0], list(node.children[1:])
        return None, list(node.children)

    @staticmethod
    def _split_body_cond(node: NormalizedCNode):
        if node.children and node.children[-1].parser_kind == "parenthesized_expression":
            return list(node.children[:-1]), node.children[-1]
        return list(node.children), None

    def _build_if(self, node: NormalizedCNode):
        condition, rest = self._split_cond_body(node)
        alternative = None
        if rest and rest[-1].parser_kind == "else_clause":
            alternative = rest.pop()
        condition_id = self._condition(condition, "if_condition", node)
        merge = self.f.node("merge", "branch_merge", self._path(node))
        true_entry, true_ends = self.build_seq(rest)
        else_entry, else_ends = None, []
        if alternative is not None:
            else_entry, else_ends = self.build_seq(alternative.children)
        self.f.edge(condition_id, true_entry or merge, "true")
        self.f.edge(condition_id, else_entry or merge, "false")
        for end in true_ends:
            self.f.edge(end, merge, "next")
        for end in else_ends:
            self.f.edge(end, merge, "next")
        return condition_id, [merge]

    def _build_while(self, node: NormalizedCNode):
        condition, body = self._split_cond_body(node)
        condition_id = self._condition(condition, "loop_header", node)
        exit_merge = self.f.node("merge", "loop_exit", self._path(node))
        frame = {"type": "loop", "continue": condition_id,
                 "break": exit_merge, "breaks": []}
        self.frames.append(frame)
        body_entry, body_ends = self.build_seq(body)
        self.frames.pop()
        self.f.edge(condition_id, body_entry or exit_merge, "true")
        self.f.edge(condition_id, exit_merge, "false")
        for end in body_ends:
            self.f.edge(end, condition_id, "back")
        for break_id in frame["breaks"]:
            self.f.edge(break_id, exit_merge, "break")
        return condition_id, [exit_merge]

    def _build_do(self, node: NormalizedCNode):
        body, condition = self._split_body_cond(node)
        condition_id = self._condition(condition, "loop_header", node)
        exit_merge = self.f.node("merge", "loop_exit", self._path(node))
        frame = {"type": "loop", "continue": condition_id,
                 "break": exit_merge, "breaks": []}
        self.frames.append(frame)
        body_entry, body_ends = self.build_seq(body)
        self.frames.pop()
        if body_entry is None:
            self.f.edge(condition_id, exit_merge, "true")
            self.f.edge(condition_id, exit_merge, "false")
            return condition_id, [exit_merge]
        for end in body_ends:
            self.f.edge(end, condition_id, "back")
        self.f.edge(condition_id, body_entry, "true")
        self.f.edge(condition_id, exit_merge, "false")
        for break_id in frame["breaks"]:
            self.f.edge(break_id, exit_merge, "break")
        return body_entry, [exit_merge]

    def _build_for(self, node: NormalizedCNode):
        raw = self.for_index.get((node.start_byte, node.end_byte))
        normalized_by_span = {
            (child.start_byte, child.end_byte): child for child in node.children
        }
        init_n = cond_n = update_n = body_n = None
        if raw is not None:
            def normalized(field_node):
                if field_node is None:
                    return None
                return normalized_by_span.get(
                    (field_node.start_byte, field_node.end_byte))
            init_n = normalized(raw.child_by_field_name("initializer"))
            cond_n = normalized(raw.child_by_field_name("condition"))
            update_n = normalized(raw.child_by_field_name("update"))
            body_n = normalized(raw.child_by_field_name("body"))
        else:
            condition, rest = self._split_cond_body(node)
            cond_n = condition
            body_n = rest[0] if rest else None
        condition_id = self._condition(cond_n, "loop_header", node)
        exit_merge = self.f.node("merge", "loop_exit", self._path(node))
        init_id = self._linked("statement", "for_init", init_n) if init_n else None
        update_id = self._linked("statement", "for_update", update_n) if update_n else None
        frame = {"type": "loop", "continue": update_id or condition_id,
                 "break": exit_merge, "breaks": []}
        self.frames.append(frame)
        body_entry, body_ends = self.build_seq([body_n]) if body_n else (None, [])
        self.frames.pop()
        if init_id is not None:
            self.f.edge(init_id, condition_id, "next")
        self.f.edge(
            condition_id, body_entry or update_id or condition_id, "true")
        self.f.edge(condition_id, exit_merge, "false")
        for end in body_ends:
            if update_id is not None:
                self.f.edge(end, update_id, "next")
            else:
                self.f.edge(end, condition_id, "back")
        if update_id is not None:
            self.f.edge(update_id, condition_id, "back")
        for break_id in frame["breaks"]:
            self.f.edge(break_id, exit_merge, "break")
        return init_id or condition_id, [exit_merge]

    def _build_switch(self, node: NormalizedCNode):
        condition, rest = self._split_cond_body(node)
        condition_id = self._condition(condition, "switch_condition", node)
        exit_merge = self.f.node("merge", "switch_exit", self._path(node))
        frame = {"type": "switch", "break": exit_merge, "breaks": []}
        self.frames.append(frame)
        segments = []
        for child in rest:
            if child.parser_kind in CASE_PARSER_KINDS:
                segments.extend(self._case_segments(child))
            elif child.kind == "BLOCK":
                for sub in child.children:
                    if sub.parser_kind in CASE_PARSER_KINDS:
                        segments.extend(self._case_segments(sub))
                    else:
                        self.build_stmt(sub)
            else:
                self.build_stmt(child)
        case_infos = []
        for case_node, value, statements in segments:
            is_default = value is None
            case_id = self._linked(
                "case", "switch_default" if is_default else "switch_case",
                case_node, metadata={"default": is_default},
            )
            body_entry, body_ends = self.build_seq(statements)
            if body_entry is None:
                body_ends = [case_id]
            else:
                self.f.edge(case_id, body_entry, "next")
            case_infos.append((case_id, body_ends, is_default))
        self.frames.pop()
        if not case_infos:
            self.f.edge(condition_id, exit_merge, "next")
        else:
            for case_id, _, is_default in case_infos:
                self.f.edge(condition_id, case_id, "default" if is_default else "case")
            for (_, prev_ends, _), (next_id, _, _) in zip(case_infos, case_infos[1:]):
                for end in prev_ends:
                    self.f.edge(end, next_id, "fallthrough")
            for end in case_infos[-1][1]:
                self.f.edge(end, exit_merge, "next")
        for break_id in frame["breaks"]:
            self.f.edge(break_id, exit_merge, "break")
        return condition_id, [exit_merge]

    @staticmethod
    def _is_statement(node: NormalizedCNode) -> bool:
        if node.kind == "CONTROL_STRUCTURE" or node.kind in STATEMENT_KINDS:
            return True
        return False

    def _case_segments(self, case_node: NormalizedCNode):
        children = list(case_node.children)
        value = None
        if children and not self._is_statement(children[0]):
            value = children[0]
            children = children[1:]
        statements = []
        for child in children:
            if child.parser_kind in CASE_PARSER_KINDS:
                return [(case_node, value, statements)] + self._case_segments(child)
            statements.append(child)
        return [(case_node, value, statements)]

    def _build_break(self, node: NormalizedCNode):
        node_id = self._linked("break", "statement", node)
        target = self.frames[-1]["break"] if self.frames else None
        if target is None:
            self.counters["unresolved_breaks"] += 1
            return node_id, []
        self.f.edge(node_id, target, "break")
        return node_id, []

    def _build_continue(self, node: NormalizedCNode):
        node_id = self._linked("continue", "statement", node)
        target = None
        for frame in reversed(self.frames):
            if frame["type"] == "loop":
                target = frame["continue"]
                break
        if target is None:
            self.counters["unresolved_continues"] += 1
            return node_id, []
        self.f.edge(node_id, target, "continue")
        return node_id, []

    def _build_goto(self, node: NormalizedCNode):
        node_id = self._linked("goto", "statement", node)
        label = next((child.name for child in node.children if child.name), None)
        target = self.labels.get(label) if label else None
        if target is None:
            self.counters["unresolved_gotos"] += 1
            return node_id, []
        self.f.edge(node_id, target, "goto")
        return node_id, []

    def _build_label(self, node: NormalizedCNode):
        label_id = self._linked("label", "label", node, metadata={"name": node.name})
        statements = [
            child for child in node.children
            if child.parser_kind != "statement_identifier"
        ]
        first, ends = self.build_seq(statements)
        if first is None:
            return label_id, [label_id]
        self.f.edge(label_id, first, "next")
        return label_id, ends


def analyze_c_cfg_scope(repo: str | Path, scope: str, database: str | Path | None = None) -> dict:
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
    skipped_no_functions: list[str] = []
    skipped_payload_too_large: list[str] = []
    counters = {
        "functions_built": 0, "functions_degraded": 0,
        "functions_skipped_unmatched_owner": 0,
        "functions_skipped_ast_node_missing": 0,
    }
    diagnostics_totals = {
        "unresolved_gotos": 0, "unresolved_breaks": 0, "unresolved_continues": 0,
        "error_statements": 0, "stray_cases": 0,
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
        scope_symbols = set()
        if scope_file_ids:
            placeholders = ",".join("?" for _ in scope_file_ids)
            scope_symbols = {
                row["id"] for row in store.storage.rows(
                    "SELECT id FROM nodes WHERE file_id IN (%s)" % placeholders,
                    tuple(sorted(scope_file_ids)),
                )
            }
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
            ast_read = store.read_current_analysis(file_id, AST_ANALYZER)
            if ast_read is None or ast_read["state"]["metadata"].get("contract") != C_AST_CONTRACT:
                skipped_ast_missing.append(source.relative_path)
                continue
            if not ast_read["fresh"]:
                skipped_ast_stale.append(source.relative_path)
                continue
            state = store.get_analysis_state(file_id, ANALYZER)
            if (
                state is not None
                and state.get("metadata", {}).get("contract") == C_CFG_CONTRACT
                and state.get("metadata", {}).get("analyzer") == ANALYZER
                and store.is_analysis_fresh(file_id, ANALYZER, base_hash, C_CFG_VERSION)
            ):
                reused.append(source.relative_path)
                continue
            try:
                nodes, edges, file_counters, file_diagnostics, parse_diagnostics = \
                    _build_file_cfg(store, file_id, source, ast_read, scope_symbols)
                if file_counters["functions_built"] == 0:
                    # Nothing to persist: an empty graph would fail the
                    # entry/exit integrity contract. Accounted as a skip.
                    skipped_no_functions.append(source.relative_path)
                    continue
                degraded = (
                    any(file_diagnostics.values()) or parse_diagnostics["partial"]
                )
                store.replace_analysis_for_file(
                    file_id, ANALYZER, base_hash, C_CFG_VERSION, nodes, edges,
                    metadata={
                        "analyzer": ANALYZER, "contract": C_CFG_CONTRACT,
                        "language": "c", "frontend": "tree-sitter-c",
                        "scope": scope_path.as_posix(),
                        "node_count": len(nodes), "edge_count": len(edges),
                        "ast_contract": C_AST_CONTRACT,
                        **file_counters, **file_diagnostics,
                        **parse_diagnostics,
                        "analysis_status": "degraded" if degraded else "complete",
                    },
                )
            except PayloadTooLargeError:
                skipped_payload_too_large.append(source.relative_path)
                continue
            for key in counters:
                counters[key] += file_counters.get(key, 0)
            for key in diagnostics_totals:
                diagnostics_totals[key] += file_diagnostics.get(key, 0)
            materialized.append(source.relative_path)
            nodes_written += len(nodes)
            edges_written += len(edges)
    return {
        "repo_id": repo_id,
        "database": str(database),
        "scope": scope_path.as_posix(),
        "analyzer": ANALYZER,
        "contract": C_CFG_CONTRACT,
        "feature_version": C_CFG_VERSION,
        "ast_dependency": AST_ANALYZER,
        "scope_source_files": len(scoped),
        "materialized": sorted(materialized),
        "reused": sorted(reused),
        "skipped_unsupported": sorted(skipped_unsupported),
        "skipped_not_in_base": sorted(skipped_not_in_base),
        "skipped_base_out_of_date": sorted(skipped_base_out_of_date),
        "skipped_ast_missing": sorted(skipped_ast_missing),
        "skipped_ast_stale": sorted(skipped_ast_stale),
        "skipped_no_functions_built": sorted(skipped_no_functions),
        "skipped_payload_too_large": sorted(skipped_payload_too_large),
        "functions_built": counters["functions_built"],
        "functions_degraded": counters["functions_degraded"],
        "functions_skipped_unmatched_owner": counters["functions_skipped_unmatched_owner"],
        "functions_skipped_ast_node_missing": counters["functions_skipped_ast_node_missing"],
        "unresolved_gotos": diagnostics_totals["unresolved_gotos"],
        "unresolved_breaks": diagnostics_totals["unresolved_breaks"],
        "unresolved_continues": diagnostics_totals["unresolved_continues"],
        "error_statements": diagnostics_totals["error_statements"],
        "stray_cases": diagnostics_totals["stray_cases"],
        "nodes_written": nodes_written,
        "edges_written": edges_written,
        "analyze_time_ms": (time.perf_counter() - started) * 1000,
    }


def _build_file_cfg(store, file_id: str, source, ast_read: dict, scope_symbols: set[str]):
    source_bytes = source.path.read_bytes()
    frontend = CFrontend().parse(source_bytes, source.relative_path)
    raw_outcome = parser_for_language("c").parse_bytes(source_bytes, source.relative_path)
    index = _index_tree(frontend.root)
    for_index: dict[tuple[int, int], object] = {}
    raw_stack = [raw_outcome.tree.root_node]
    while raw_stack:
        raw_node = raw_stack.pop()
        if raw_node.type == "for_statement":
            for_index[(raw_node.start_byte, raw_node.end_byte)] = raw_node
        raw_stack.extend(raw_node.children)
    function_alignment = {}
    for row in ast_read["nodes"]:
        payload = row["payload"]
        if row["kind"] == "FUNCTION" and payload.get("parser_kind") == "function_definition":
            function_alignment[(payload["start_byte"], payload["end_byte"])] = (
                payload["owner_symbol_id"] if payload.get("alignment") == "resolved" else None
            )
    ast_ids = {row["analysis_node_id"] for row in ast_read["nodes"]}
    calls_index: dict[tuple[str, int], list[dict]] = {}
    for row in store.storage.rows(
        "SELECT owner_symbol_id, start_line, status, resolved_symbol_id FROM raw_references "
        "WHERE file_id=? AND reference_type='CALLS'", (file_id,)
    ):
        if row["start_line"] is not None:
            calls_index.setdefault((row["owner_symbol_id"], row["start_line"]), []).append(row)
    nodes: list[AnalysisNode] = []
    edges: list[AnalysisEdge] = []
    counters = {
        "functions_built": 0, "functions_degraded": 0,
        "functions_skipped_unmatched_owner": 0,
        "functions_skipped_ast_node_missing": 0,
    }
    diagnostics = {
        "unresolved_gotos": 0, "unresolved_breaks": 0, "unresolved_continues": 0,
        "error_statements": 0, "stray_cases": 0,
    }
    functions = sorted(
        (node, path) for (node, path) in index.values()
        if node.kind == "FUNCTION" and node.parser_kind == "function_definition"
    )
    for function_node, _ in functions:
        key = (function_node.start_byte, function_node.end_byte)
        if key not in function_alignment:
            counters["functions_skipped_ast_node_missing"] += 1
            continue
        owner = function_alignment[key]
        if owner is None:
            counters["functions_skipped_unmatched_owner"] += 1
            continue
        graph = _FunctionCFG(file_id, owner)
        builder = _CCfgBuilder(graph, index, ast_ids, calls_index, scope_symbols, for_index)
        function_diagnostics = builder.build(function_node)
        nodes.extend(graph.nodes)
        edges.extend(graph.edges)
        counters["functions_built"] += 1
        for key in diagnostics:
            diagnostics[key] += function_diagnostics.get(key, 0)
        if any(function_diagnostics.values()):
            counters["functions_degraded"] += 1
    parse_diagnostics = {
        "error_node_count": frontend.diagnostics.error_nodes,
        "missing_node_count": frontend.diagnostics.missing_nodes,
        "parse_coverage": frontend.diagnostics.parse_coverage,
        "partial": frontend.diagnostics.partial,
    }
    return nodes, edges, counters, diagnostics, parse_diagnostics
