"""Tree-sitter C frontend and normalized AST boundary for optional analysis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .parser import parser_for_language


@dataclass(frozen=True, slots=True)
class CParseDiagnostics:
    error_nodes: int
    missing_nodes: int
    parse_coverage: float
    partial: bool


@dataclass(frozen=True, slots=True)
class NormalizedCNode:
    kind: str
    parser_kind: str
    start_byte: int
    end_byte: int
    start_point: tuple[int, int]
    end_point: tuple[int, int]
    child_order: int
    name: str | None
    is_error: bool
    is_missing: bool
    children: tuple["NormalizedCNode", ...]


@dataclass(frozen=True, slots=True)
class CFrontendResult:
    root: NormalizedCNode
    diagnostics: CParseDiagnostics


CONTROL_KINDS = frozenset({
    "if_statement", "switch_statement", "case_statement", "while_statement",
    "do_statement", "for_statement", "break_statement", "continue_statement",
    "goto_statement",
})

NORMALIZED_KINDS = {
    "translation_unit": "FILE",
    "function_definition": "FUNCTION",
    "parameter_declaration": "PARAMETER",
    "compound_statement": "BLOCK",
    "expression_statement": "EXPRESSION",
    "return_statement": "RETURN",
    "labeled_statement": "LABEL",
    "call_expression": "CALL",
    "assignment_expression": "ASSIGNMENT",
    "unary_expression": "OPERATOR",
    "binary_expression": "OPERATOR",
    "update_expression": "OPERATOR",
    "field_expression": "FIELD_ACCESS",
    "subscript_expression": "SUBSCRIPT",
    "cast_expression": "CAST",
    "conditional_expression": "CONDITIONAL",
    "identifier": "IDENTIFIER",
    "field_identifier": "IDENTIFIER",
    "type_identifier": "IDENTIFIER",
    "statement_identifier": "IDENTIFIER",
    "number_literal": "LITERAL",
    "string_literal": "LITERAL",
    "char_literal": "LITERAL",
    "true": "LITERAL",
    "false": "LITERAL",
    "null": "LITERAL",
    "type_definition": "TYPE_DECL",
    "struct_specifier": "TYPE_DECL",
    "union_specifier": "TYPE_DECL",
    "enum_specifier": "TYPE_DECL",
    "field_declaration": "FIELD",
    "declaration": "DECLARATION",
    "init_declarator": "DECLARATION",
    "comment": "COMMENT",
    "ERROR": "UNKNOWN",
}


class CFrontend:
    """Convert Tree-sitter C nodes into a small language-neutral vocabulary."""

    def parse(self, source: bytes, relative_path: str) -> CFrontendResult:
        outcome = parser_for_language("c").parse_bytes(source, relative_path)
        raw_root = outcome.tree.root_node
        diagnostics = self._diagnostics(raw_root, len(source))
        root = self._normalize(raw_root, source, child_order=0, in_function=False)
        return CFrontendResult(root, diagnostics)

    def _normalize(
        self, node: Any, source: bytes, *, child_order: int, in_function: bool
    ) -> NormalizedCNode:
        kind = self._kind(node, in_function=in_function)
        nested_in_function = in_function or node.type == "function_definition"
        raw_children = [
            child for child in node.children
            if child.is_named or child.is_error or child.is_missing
        ]
        children = tuple(
            self._normalize(
                child, source, child_order=index,
                in_function=nested_in_function,
            )
            for index, child in enumerate(raw_children)
        )
        return NormalizedCNode(
            kind=kind,
            parser_kind=node.type,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            start_point=tuple(node.start_point),
            end_point=tuple(node.end_point),
            child_order=child_order,
            name=self._name(node, source, kind),
            is_error=bool(node.is_error),
            is_missing=bool(node.is_missing),
            children=children,
        )

    def _kind(self, node: Any, *, in_function: bool) -> str:
        if node.type in CONTROL_KINDS:
            return "CONTROL_STRUCTURE"
        if node.type.startswith("preproc_"):
            return "PREPROCESSOR"
        if node.type == "declaration":
            if self._is_function_prototype(node):
                return "FUNCTION"
            return "LOCAL" if in_function else "DECLARATION"
        if node.type in {
            "primitive_type", "sized_type_specifier", "type_qualifier",
            "storage_class_specifier",
        }:
            return "TYPE"
        return NORMALIZED_KINDS.get(node.type, "SYNTAX")

    def _is_function_prototype(self, node: Any) -> bool:
        declarator = node.child_by_field_name("declarator")
        function = self._find(declarator, "function_declarator")
        if function is None:
            return False
        inner = function.child_by_field_name("declarator")
        return self._find(inner, "pointer_declarator") is None

    def _name(self, node: Any, source: bytes, kind: str) -> str | None:
        if kind == "FUNCTION":
            return self._declarator_name(node.child_by_field_name("declarator"), source)
        if kind in {"PARAMETER", "LOCAL", "DECLARATION", "FIELD"}:
            return self._declarator_name(node.child_by_field_name("declarator"), source)
        if kind == "TYPE_DECL":
            candidate = node.child_by_field_name("name") or node.child_by_field_name("declarator")
            return self._declarator_name(candidate, source) or self._text(candidate, source)
        if kind == "LABEL":
            return self._text(node.child_by_field_name("label"), source)
        if kind == "CALL":
            return self._compact(self._text(node.child_by_field_name("function"), source))
        if node.type in {"identifier", "field_identifier", "type_identifier", "statement_identifier"}:
            return self._text(node, source)
        return None

    def _declarator_name(self, node: Any | None, source: bytes) -> str | None:
        if node is None:
            return None
        if node.type in {"identifier", "field_identifier", "type_identifier"}:
            return self._text(node, source)
        for field in ("declarator", "name", "function"):
            value = self._declarator_name(node.child_by_field_name(field), source)
            if value:
                return value
        for child in node.named_children:
            value = self._declarator_name(child, source)
            if value:
                return value
        return None

    @staticmethod
    def _find(node: Any | None, parser_kind: str) -> Any | None:
        if node is None:
            return None
        if node.type == parser_kind:
            return node
        for child in node.named_children:
            found = CFrontend._find(child, parser_kind)
            if found is not None:
                return found
        return None

    @staticmethod
    def _text(node: Any | None, source: bytes) -> str | None:
        if node is None:
            return None
        return source[node.start_byte:node.end_byte].decode("utf-8", "replace")

    @staticmethod
    def _compact(value: str | None) -> str | None:
        compact = " ".join((value or "").split())
        return compact or None

    @staticmethod
    def _diagnostics(root: Any, source_size: int) -> CParseDiagnostics:
        error_nodes = 0
        missing_nodes = 0
        error_bytes = 0
        stack = [root]
        while stack:
            node = stack.pop()
            if node.is_error:
                error_nodes += 1
                error_bytes += max(0, node.end_byte - node.start_byte)
            if node.is_missing:
                missing_nodes += 1
            stack.extend(node.children)
        denominator = max(1, source_size)
        coverage = max(0.0, 1.0 - min(error_bytes, denominator) / denominator)
        return CParseDiagnostics(
            error_nodes=error_nodes,
            missing_nodes=missing_nodes,
            parse_coverage=round(coverage, 6),
            partial=bool(error_nodes or missing_nodes),
        )
