"""Optional normalized C AST materializer over the derived-analysis store."""

from __future__ import annotations

import hashlib
import json
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
from .analysis_c_frontend import CFrontend, NormalizedCNode, ScenarioPragma
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

# FE-8 (frozen, fe8_formal_feature_contract.md): scenario annotation facts are
# materialised additively inside `ast:c` as synthetic leaf nodes under the FILE
# root. `SCENARIO_ANNOTATION_KIND` is their node kind; the frozen syntax kind
# (only one exists in v1) and the frozen deterministic-id seed/template (§3).
SCENARIO_ANNOTATION_KIND = "SCENARIO_ANNOTATION"
SCENARIO_SYNTAX_KIND = "pragma_region"


def scenario_annotation_id(
    repo: str,
    relative_path: str,
    syntax_kind: str,
    scenario_id: str,
    start_byte: int,
    end_byte: int,
) -> str:
    """Deterministic annotation id (FE-8 §3).

    Seed fields (frozen, template order): ``(repo, relative_path, syntax_kind,
    scenario_id, start_byte, end_byte)``. ``analyzer_version`` is deliberately
    excluded -- identity is content-derived and must not churn when the
    analyzer improves. The fields are joined with ``\\x1f`` and SHA-256 hashed
    (matching ``identity._stable``) so a bare ``:`` in a Linux filename cannot
    make the id ambiguous; the ``scenario:`` prefix isolates this namespace
    from candidate-layer built-in ``scenario_id`` (FE-8 §2.4).
    """
    canonical = "\x1f".join((
        str(repo), relative_path, syntax_kind, scenario_id,
        str(start_byte), str(end_byte),
    ))
    return "scenario:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
            nodes = _materialize(
                store, file_id, repo_id, source.relative_path, source_bytes,
                frontend.root, frontend.scenario_pragmas,
            )
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
        "SELECT id, kind, name, start_line, end_line, metadata FROM nodes WHERE file_id=?",
        (file_id,),
    ):
        if row["start_line"] is not None:
            definition = None
            if row["metadata"]:
                try:
                    definition = json.loads(row["metadata"]).get("definition")
                except ValueError:
                    definition = None
            spans.setdefault((row["start_line"], row["end_line"]), []).append(
                {"id": row["id"], "kind": row["kind"], "name": row["name"],
                 "definition": definition}
            )
    return spans


def _resolve_alignment(candidates: list[dict], expected: frozenset, node: NormalizedCNode):
    """Pick the base symbol a normalized node aligns to, or None.

    Line-span alignment assumed base symbols never shared a (start_line,
    end_line) span. That breaks when a function's span collapses onto one
    physical line together with another Function-kind symbol -- the base graph
    records every C declaration as a Function/Method-kind symbol
    (``definition: false``), so a single-line ``for (int i = 0; ...)`` puts the
    for-initializer symbol on exactly the same span as the enclosing function
    (I0R1 root cause). Disambiguate in two strictly-tightening steps and stay
    unmatched (conservative: a wrong owner is worse than no CFG) when both
    fail:

    1. same ``name`` as the normalized node;
    2. same definition kind -- a ``function_definition`` node aligns to a
       ``definition: true`` symbol, a ``declaration`` node to ``definition:
       false``.
    """
    candidates = [item for item in candidates if item["kind"] in expected]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1 and node.name:
        named = [item for item in candidates if item["name"] == node.name]
        if len(named) == 1:
            return named[0]
        if named:
            candidates = named
    if len(candidates) > 1 and node.parser_kind in {"function_definition", "declaration"}:
        # Definition-kind preference is only meaningful for FUNCTION-aligned
        # nodes; TYPE_DECL alignment keeps its previous ambiguous => unmatched
        # behaviour.
        want = node.parser_kind == "function_definition"
        defined = [item for item in candidates if item["definition"] is want]
        if len(defined) == 1:
            return defined[0]
    return None


def _materialize(
    store: AnalysisStore, file_id: str, repo_id: str, relative_path: str,
    source: bytes, root: NormalizedCNode, scenario_pragmas: tuple[ScenarioPragma, ...],
) -> list[AnalysisNode]:
    spans = _symbol_spans(store, file_id)
    nodes: list[AnalysisNode] = []
    file_root_id: str | None = None
    function_spans: list[tuple[int, int, str | None]] = []
    stack = [(root, None, root.kind)]
    while stack:
        node, parent_id, path = stack.pop()
        node_id = ast_node_id(file_id, path)
        if parent_id is None:
            file_root_id = node_id
        owner_symbol_id, alignment = None, "unmatched"
        expected = ALIGNABLE.get((node.kind, node.parser_kind))
        if expected is not None:
            key = (node.start_point[0] + 1, node.end_point[0] + 1)
            chosen = _resolve_alignment(spans.get(key, []), expected, node)
            if chosen is not None:
                owner_symbol_id, alignment = chosen["id"], "resolved"
        if node.kind == "FUNCTION" and node.parser_kind == "function_definition":
            function_spans.append((node.start_byte, node.end_byte, owner_symbol_id))
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
            **({"part_role": node.part_role} if node.part_role else {}),
        }))
        for child in reversed(node.children):
            child_path = f"{path}/{child.kind}[{child.child_order}]"
            stack.append((child, node_id, child_path))

    if file_root_id is not None:
        for annotation in _pair_scenario_annotations(
            scenario_pragmas, source, repo_id, relative_path,
            function_spans, file_root_id, root.kind,
        ):
            nodes.append(AnalysisNode(
                annotation["annotation_id"], SCENARIO_ANNOTATION_KIND, annotation,
            ))
    return nodes


def _line_of(source: bytes, offset: int) -> int:
    """1-based line number of the byte at ``offset`` (FE-8 §2.1 human-readable)."""
    return source.count(b"\n", 0, offset) + 1


def _enclosing_function(
    start_byte: int, end_byte: int, function_spans: list[tuple[int, int, str | None]]
) -> tuple[int, str | None] | None:
    """The innermost function span containing ``[start_byte, end_byte]``.

    Returns ``(span_size, owner_symbol_id)`` or None when no function contains
    the range (a file-scope region). ``owner_symbol_id`` is None when the
    function aligned to no base symbol.
    """
    best: tuple[int, str | None] | None = None
    for fn_start, fn_end, owner in function_spans:
        if fn_start <= start_byte and end_byte <= fn_end:
            size = fn_end - fn_start
            if best is None or size < best[0]:
                best = (size, owner)
    return best


def _pair_scenario_annotations(
    markers: tuple[ScenarioPragma, ...], source: bytes, repo_id: str,
    relative_path: str, function_spans: list[tuple[int, int, str | None]],
    file_root_id: str, root_kind: str,
) -> list[dict]:
    """Pair begin/end markers into annotations (FE-8 §4/§5), deterministic.

    A ``begin``/``end`` pair with equal ``scenario_id`` that is properly nested
    on the LIFO stack resolves; every other shape fails closed:

    * a begin with no matching end -> ``unknown`` + ``unclosed_scenario_region``;
    * a marker covered by ERROR/missing -> ``unknown`` + ``annotation_parse_partial``;
    * a mismatched nesting (crossing overlap) -> the crossed regions are
      ``invalid`` + ``crossing_scenario_regions``; a dangling end (orphan) has
      no region and produces no annotation (F2's relation layer reports it).
    """
    rel = relative_path.replace("\\", "/")
    source_size = len(source)
    annotations: list[dict] = []
    stack: list[ScenarioPragma] = []

    def emit(begin: ScenarioPragma, end_marker: ScenarioPragma | None,
             parse_status: str, limitations: list[str]) -> dict:
        start_byte = begin.end_byte
        end_byte = end_marker.start_byte if end_marker is not None else source_size
        if end_byte < start_byte:
            end_byte = start_byte
        # Enclosing function is anchored by where the begin marker sits (its
        # own span), not by the full region span: an unclosed region's end
        # extends to EOF, past the enclosing function's closing brace, and a
        # crossing region has no single enclosing function. The begin pragma
        # itself always sits inside exactly one function (or none, at file
        # scope).
        enclosing = _enclosing_function(begin.start_byte, begin.end_byte, function_spans)
        if enclosing is None:
            function_symbol_id: str | None = None
        elif enclosing[1] is not None:
            function_symbol_id = enclosing[1]
        else:
            function_symbol_id = None
            limitations = limitations + ["function_owner_unresolved"]
        start_line = _line_of(source, start_byte)
        end_line = _line_of(source, end_byte - 1) if end_byte > start_byte else start_line
        annotation_id = scenario_annotation_id(
            repo_id, rel, SCENARIO_SYNTAX_KIND, begin.scenario_id,
            start_byte, end_byte,
        )
        return {
            "annotation_id": annotation_id,
            "scenario_id": begin.scenario_id,
            "syntax_kind": SCENARIO_SYNTAX_KIND,
            "relative_path": rel,
            "start_byte": start_byte,
            "end_byte": end_byte,
            "start_line": start_line,
            "end_line": end_line,
            "function_symbol_id": function_symbol_id,
            "parse_status": parse_status,
            "limitations": limitations,
            "parent_id": file_root_id,
            "language": "c",
        }

    for marker in markers:
        if marker.is_error or marker.is_missing:
            if marker.action == "begin":
                annotations.append(emit(
                    marker, None, "unknown", ["annotation_parse_partial"]))
            # a partial `end` marker has no region of its own; it only prevents
            # the matching begin from resolving (handled by the crossing branch
            # below when the stack does not match cleanly).
            continue
        if marker.action == "begin":
            stack.append(marker)
            continue
        # end marker
        if not stack:
            continue  # orphan end: no region, no annotation
        top = stack[-1]
        if top.scenario_id == marker.scenario_id:
            stack.pop()
            annotations.append(emit(top, marker, "resolved", []))
        else:
            # crossing: pop until a begin matches this end; every begin popped
            # is a crossed (invalid) region.
            index = None
            for i in range(len(stack) - 1, -1, -1):
                if stack[i].scenario_id == marker.scenario_id:
                    index = i
                    break
            if index is None:
                continue  # orphan end with no matching begin
            crossed = stack[index:]
            del stack[index:]
            for begin in crossed:
                annotations.append(emit(
                    begin, None, "invalid", ["crossing_scenario_regions"]))

    for begin in stack:
        annotations.append(emit(begin, None, "unknown", ["unclosed_scenario_region"]))

    # Deterministic order (FE-8 §6.2): relative_path -> start_byte -> end_byte
    # -> scenario_id; relative_path is constant per file.
    annotations.sort(key=lambda ann: (
        ann["start_byte"], ann["end_byte"], ann["scenario_id"],
    ))
    for index, annotation in enumerate(annotations):
        annotation["structural_path"] = (
            f"{root_kind}/{SCENARIO_ANNOTATION_KIND}[{index}]")
    return annotations
