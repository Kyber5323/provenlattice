from __future__ import annotations

import re
from bisect import bisect_right
from pathlib import PurePosixPath
from typing import Any

from ..models import ParsedFile, ParsedImport, ParsedReference, ParsedSymbol
from ..semantics.vocabulary import Vocabulary
from ..sparsecfg import CPP_PROFILE, SparseCfgBuilder
from .base import ParseOutcome
from .declarations import DeclarationIndex
from .events import EventExtractor, OwnerIndex, OwnerSpan
from .tree_sitter import event_vocabulary

try:  # Keep the Python-only installation usable until C grammars are installed.
    import tree_sitter_c
    import tree_sitter_cpp
    from tree_sitter import Language, Parser
except ImportError:  # pragma: no cover - exercised only in incomplete environments.
    tree_sitter_c = tree_sitter_cpp = None
    Language = Parser = None


CPP_EXTENSIONS = frozenset({".h", ".hh", ".hpp", ".hxx", ".inc", ".c", ".cc", ".cpp", ".cxx", ".c++"})


def module_name(relative_path: str) -> str:
    return ".".join(PurePosixPath(relative_path).with_suffix("").parts)


class CppExtractor:
    """Normalize the conservative, syntax-only C/C++ Tree-sitter facts."""

    def __init__(self, source: bytes, relative_path: str, language: str = "cpp", vocabulary: Vocabulary | None = None) -> None:
        self.source = source
        self.line_starts = [0, *(index + 1 for index, value in enumerate(source) if value == 10)]
        self.relative_path = relative_path
        self.module = module_name(relative_path)
        self.language = language
        self.scope: list[tuple[str, str]] = []
        self.symbols: list[ParsedSymbol] = []
        self.references: list[ParsedReference] = []
        self.imports: list[ParsedImport] = []
        # Syntax-level callable tracking for call-site classification.
        self.callable_typedefs: set[str] = set()
        self.local_callables: set[str] = set()
        self.function_names: set[str] = set()
        self.vocabulary = vocabulary
        # Every symbol that can own events, recorded as it is created so the
        # event layer reads the same identity the symbol layer published.
        self.owner_spans: list[OwnerSpan] = []
        self.skipped_events = 0
        self.demoted_prototypes = 0

    def text(self, node: Any | None) -> str:
        return "" if node is None else self.source[node.start_byte:node.end_byte].decode("utf-8", "replace")

    def qualified(self, name: str | None = None) -> str:
        return ".".join(part for part in (*(item[0] for item in self.scope), name) if part)

    def owner(self) -> str:
        return self.qualified()

    def line(self, node: Any, *, end: bool = False) -> int:
        return bisect_right(self.line_starts, node.end_byte if end else node.start_byte)

    @staticmethod
    def same_node(left: Any | None, right: Any | None) -> bool:
        return (
            left is not None and right is not None
            and left.type == right.type
            and left.start_byte == right.start_byte
            and left.end_byte == right.end_byte
        )

    def extract(self, root: Any) -> ParsedFile:
        self._walk(root)
        for reference in self.references:
            if reference.type != "CALLS" or reference.metadata.get("call_kind") != "direct":
                continue
            references = [
                name for name in reference.metadata.get("argument_identifiers", [])
                if name in self.function_names
            ]
            if references:
                reference.metadata["function_reference_args"] = references[:4]
        events = []
        cfg = None
        # Zero when there is no vocabulary: the declaration index is built from
        # the same hits the events come from, so without them there is nothing
        # to resolve against either.
        recovered_qualifiers = 0
        if self.vocabulary is not None:
            owners = OwnerIndex(self.owner_spans)
            line_of = lambda offset, end: bisect_right(self.line_starts, offset)  # noqa: E731
            extractor = EventExtractor(self.source, self.vocabulary, owners, line_of)
            # One matcher feeds both layers: the events and the CFG edges come
            # from the same hits, so an operation cannot appear in one and be
            # missing from the other.
            hits = extractor.extract_hits(root)
            events = [hit.event for hit in hits]
            self.skipped_events = extractor.skipped_outside_owner
            # The declarations are read from the same tree by a walker of their
            # own, and handed to the CFG builder as a fact rather than looked up
            # by it: the builder consumes facts, it does not produce them.
            declarations = DeclarationIndex.build(self.source, self.relative_path, root)
            recovered_qualifiers = declarations.recovered_qualifiers
            # C and C++ share one profile today: they agree on every grammar
            # fact the walker reads, and the two that differ (condition
            # wrapper, subscript index field) are handled by trying
            # alternatives. When they drift, the parser factory must hand each
            # dialect its own profile.
            cfg = SparseCfgBuilder(
                self.source, owners, hits, line_of, CPP_PROFILE, declarations,
                out_param=self.vocabulary.out_param_names("ALLOC"),
            ).build(root)
        deduplicated: dict[tuple[str, str, str], ParsedSymbol] = {}
        for symbol in self.symbols:
            key = (symbol.kind, symbol.qualified_name, symbol.signature)
            existing = deduplicated.get(key)
            if existing is None:
                deduplicated[key] = symbol
                continue
            declaration_lines = existing.metadata.setdefault("declaration_lines", [])
            declaration_lines.append(existing.start_line)
            if symbol.metadata.get("definition"):
                symbol.metadata["declaration_lines"] = sorted(set(declaration_lines))
                deduplicated[key] = symbol
        return ParsedFile(
            list(deduplicated.values()), self.references, self.imports,
            parser="tree-sitter-cpp", events=events, unowned_events=self.skipped_events,
            demoted_local_prototypes=self.demoted_prototypes, cfg=cfg,
            recovered_qualifier_parameters=recovered_qualifiers,
        )

    def _walk(self, node: Any) -> None:
        kind = node.type
        if kind == "preproc_include":
            self._include(node)
            return
        if kind == "namespace_definition":
            self._namespace(node)
            return
        if kind in {"class_specifier", "struct_specifier"}:
            self._class(node)
            return
        if kind in {"function_definition", "declaration", "field_declaration"} and self._function(node):
            return
        if kind in {"type_definition", "alias_declaration", "using_declaration"}:
            self._type(node)
            return
        if kind == "call_expression":
            self._call(node)
            return
        for child in node.named_children:
            self._walk(child)

    def _namespace(self, node: Any) -> None:
        name = self.text(node.child_by_field_name("name")) or "anonymous"
        qualified = self.qualified(name)
        self.symbols.append(ParsedSymbol("Namespace", name, qualified, self.line(node), self.line(node, end=True),
                                         "namespace", True, {"syntax": "namespace_definition"}))
        self.scope.append((name, "Namespace"))
        body = node.child_by_field_name("body")
        if body:
            for child in body.named_children:
                self._walk(child)
        self.scope.pop()

    def _class(self, node: Any) -> None:
        name = self.text(node.child_by_field_name("name"))
        if not name:
            return
        kind = "Struct" if node.type == "struct_specifier" or any(self.text(child) == "struct" for child in node.children) else "Class"
        bases = self.text(node.child_by_field_name("base_class_clause"))
        self.symbols.append(ParsedSymbol(kind, name, self.qualified(name), self.line(node), self.line(node, end=True),
                                         bases or kind.lower(), True, {"bases": [item.strip() for item in bases.split(",") if item.strip()]}))
        self.scope.append((name, kind))
        body = node.child_by_field_name("body")
        if body:
            for child in body.named_children:
                self._walk(child)
        self.scope.pop()

    def _function(self, node: Any) -> bool:
        declarator = node.child_by_field_name("declarator")
        if declarator is None:
            declarator = next((child for child in node.named_children if "declarator" in child.type), None)
        # A declaration only names a function when its declarator really is a
        # function declarator. A variable's identifier is just as easy to read,
        # so without this check `int x = 1;` becomes a Function symbol named
        # `f.x` -- and returning True here stops the walk, which silently drops
        # every call in the initialiser (`Foo *p = create();` produced no CALLS
        # edge at all).
        if node.type != "function_definition":
            if self._find_function_declarator(declarator) is None:
                return False
            if (
                node.type == "declaration"
                and self._in_function_body()
                and not self._is_function_pointer_declarator(declarator)
            ):
                # Most-vexing-parse. Inside a body, `std::lock_guard<std::mutex>
                # lock(mutex);` *is* a declaration whose declarator is a
                # function declarator -- `lock(mutex)` reads as a prototype --
                # but it declares a variable, and the parenthesised name is a
                # constructor argument, not a parameter list. Promoting it made
                # a one-line phantom Function symbol that then owned the
                # declaration's byte span: the RAII acquisition inside it was
                # attributed to the phantom, so every query that groups by owner
                # could not see the lock at all (`llama.cpp` had 101 of these,
                # named `lock`, `lk`, `guard`). A real prototype inside a body
                # is rare and was itself a one-line phantom, so nothing of value
                # is given up here. The gate above exempts function-pointer
                # declarators: `int (*fp)(int,int);` in a body declares a
                # callable variable (parenthesised declarator), not a
                # constructor argument list, and must still reach
                # `local_callables` so a later `fp(...)` classifies indirect.
                #
                # Counted only here, after the declarator check: the counter is
                # the price of the heuristic, and `int n = 1;` -- a declaration
                # in a body that was never going to become a symbol -- is not
                # part of that price.
                self.demoted_prototypes += 1
                return False
        name = self._declarator_name(declarator)
        if not name:
            return False
        explicit_scope, short_name = self._split_qualified(name)
        qualified = ".".join(part for part in (*(item[0] for item in self.scope), *explicit_scope, short_name) if part)
        kind = "Method" if any(item[1] in {"Class", "Struct"} for item in self.scope) or explicit_scope else "Function"
        signature = self._signature(declarator, short_name)
        self.symbols.append(ParsedSymbol(kind, short_name, qualified, self.line(node), self.line(node, end=True),
                                         signature, not short_name.startswith("_"),
                                         {"definition": node.type == "function_definition"}))
        if kind in {"Function", "Method"}:
            self.function_names.add(short_name)
        if node.type == "function_definition":
            self.local_callables = set()
        elif self._is_function_pointer_declarator(declarator):
            # A function-pointer variable declaration (`int (*fp)(int);` or a
            # callback struct field) declares a callable, not a function.
            self.local_callables.add(short_name)
        if node.type == "function_definition":
            self._collect_parameter_callables(declarator)
        self.owner_spans.append(OwnerSpan(node.start_byte, node.end_byte, kind, qualified, signature))
        self.scope.append((short_name, kind))
        body = node.child_by_field_name("body")
        if body:
            for child in body.named_children:
                self._walk(child)
        self.scope.pop()
        if node.type in {"declaration", "field_declaration"}:
            # Variable initializers can contain calls (`int x = f();`); walk
            # only the initializer values so they are captured, owned by the
            # enclosing function rather than the declared variable, and
            # without re-emitting type symbols.
            for child in node.named_children:
                if child.type == "init_declarator":
                    value = child.child_by_field_name("value")
                    if value is not None:
                        self._walk(value)
        return True

    def _collect_parameter_callables(self, declarator: Any | None) -> None:
        function = self._find_function_declarator(declarator)
        if function is None:
            return
        parameters = function.child_by_field_name("parameters")
        if parameters is None:
            return
        for parameter in parameters.named_children:
            if parameter.type != "parameter_declaration":
                continue
            param_name = self._declarator_name(parameter.child_by_field_name("declarator"))
            if not param_name:
                continue
            type_node = parameter.child_by_field_name("type")
            type_name = self.text(type_node) if type_node is not None else ""
            if type_name in self.callable_typedefs or self._is_function_pointer_declarator(
                    parameter.child_by_field_name("declarator")):
                self.local_callables.add(param_name)

    def _is_function_pointer_declarator(self, declarator: Any | None) -> bool:
        function = self._find_function_declarator(declarator)
        if function is None:
            return False
        stack = [function.child_by_field_name("declarator")]
        while stack:
            current = stack.pop()
            if current is None:
                continue
            if current.type == "parenthesized_declarator":
                return True
            stack.extend(current.named_children)
        return False
    def _in_function_body(self) -> bool:
        """Whether the walk is currently inside a function or method body.

        The *innermost* scope is what decides, not any scope on the stack: a
        class defined inside a function (`void f() { struct S { void g(); }; }`)
        puts `S` on top, and `void g();` there is a real declaration that must
        stay a symbol. Blocks are not scopes in this stack, so
        `if (x) { void g(); }` still reads as being inside `f` -- which is the
        one case this heuristic gives up, a local prototype in C.
        """
        return bool(self.scope) and self.scope[-1][1] in {"Function", "Method"}

    def _type(self, node: Any) -> None:
        name_node = node.child_by_field_name("name") or node.child_by_field_name("declarator")
        name = self._declarator_name(name_node) or self.text(name_node)
        if not name or "(" in name:
            return
        name = name.split("::")[-1]
        if node.type == "type_definition" and self._is_function_pointer_declarator(
                node.child_by_field_name("declarator")):
            self.callable_typedefs.add(name)
        self.symbols.append(ParsedSymbol("Type", name, self.qualified(name), self.line(node), self.line(node, end=True),
                                         self._normalize(self.text(node)), True, {"syntax": node.type}))

    def _include(self, node: Any) -> None:
        path = self.text(node.child_by_field_name("path")) or self.text(node)
        path = path.removeprefix("#include").strip().strip('<>"')
        if path:
            self.imports.append(ParsedImport(self.owner(), path, path,
                                             metadata={"start_line": self.line(node), "end_line": self.line(node, end=True),
                                                      "syntax": "preproc_include"}))

    def _call(self, node: Any) -> None:
        function = node.child_by_field_name("function")
        arguments = self._argument_identifiers(node, function)
        if self.language != "c":
            # A3 classification is a C semantic; other languages keep their
            # legacy shape so their foundation behavior is unchanged.
            metadata = {"syntax": "call_expression", "call_kind": "direct",
                        "callee_shape": "legacy"}
        else:
            call_kind, callee_shape, callee_detail = self._classify_callee(function)
            metadata = {"syntax": "call_expression", "call_kind": call_kind,
                        "callee_shape": callee_shape, **callee_detail}
        if arguments:
            metadata["argument_identifiers"] = arguments
        target = self._normalize(self.text(function)).replace("::", ".")
        if target:
            self.references.append(ParsedReference(self.owner(), "CALLS", target,
                                                   start_line=self.line(node), end_line=self.line(node, end=True),
                                                   metadata=metadata))
        for child in node.named_children:
            if not self.same_node(child, function):
                self._walk(child)

    def _classify_callee(self, function: Any | None) -> tuple[str, str, dict]:
        """Syntax-shape classification; indirect is never name-bound later."""
        if function is None:
            return "indirect", "missing", {}
        if function.type == "identifier":
            name = self.text(function)
            if name and name in self.local_callables:
                return "indirect", "identifier", {"callee_symbol": name}
            return "direct", "identifier", {}
        if function.type in {"qualified_identifier", "qualified_type_identifier",
                             "template_function", "namespace_identifier"}:
            return "direct", "qualified", {}
        if function.type == "field_expression":
            field = self.text(function.child_by_field_name("field"))
            receiver = self.text(function.child_by_field_name("argument"))
            return "indirect", "field_access", {"callee_symbol": receiver, "field": field}
        if function.type == "parenthesized_expression":
            inner = function.named_children[0] if function.named_children else None
            detail = {"callee_expression": self.text(function)}
            if inner is not None and inner.type == "pointer_expression":
                target = inner.named_children[0] if inner.named_children else None
                if target is not None and target.type == "identifier":
                    detail["callee_symbol"] = self.text(target)
            return "indirect", "deref" if inner is not None and inner.type == "pointer_expression" else "complex", detail
        if function.type == "subscript_expression":
            return "indirect", "subscript", {"callee_expression": self.text(function)}
        if function.type == "call_expression":
            return "indirect", "nested_call", {"callee_expression": self.text(function)}
        return "indirect", "complex", {"callee_expression": self.text(function)}

    def _argument_identifiers(self, node: Any, function: Any | None) -> list[str]:
        arguments = node.child_by_field_name("arguments")
        if arguments is None:
            return []
        names = []
        for child in arguments.named_children:
            if child.type == "identifier" and not self.same_node(child, function):
                name = self.text(child)
                if name:
                    names.append(name)
        return names[:8]

    def _declarator_name(self, node: Any | None) -> str:
        if node is None:
            return ""
        if node.type in {"identifier", "field_identifier", "type_identifier", "namespace_identifier", "operator_name"}:
            return self.text(node)
        if node.type in {"qualified_identifier", "qualified_type_identifier", "template_function"}:
            return self._normalize(self.text(node))
        for field in ("declarator", "name", "function"):
            child = node.child_by_field_name(field)
            result = self._declarator_name(child)
            if result:
                return result
        for child in node.named_children:
            result = self._declarator_name(child)
            if result:
                return result
        return ""

    def _split_qualified(self, name: str) -> tuple[list[str], str]:
        parts = [item for item in re.split(r"::|\.", name) if item]
        return parts[:-1], parts[-1] if parts else ""

    def _signature(self, declarator: Any | None, name: str) -> str:
        function = self._find_function_declarator(declarator)
        if function is None:
            return "()"
        parameters = function.child_by_field_name("parameters")
        if parameters is None:
            return self._normalize(self.text(function)) or "()"
        suffix = self.source[parameters.end_byte:function.end_byte].decode("utf-8", "replace")
        return self._normalize(f"{self.text(parameters)} {suffix}")

    def _find_function_declarator(self, node: Any | None) -> Any | None:
        if node is None:
            return None
        if node.type == "function_declarator":
            return node
        for child in node.named_children:
            result = self._find_function_declarator(child)
            if result is not None:
                return result
        return None

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(value.replace("\n", " ").split())


class CppTreeSitterParser:
    language = "cpp"
    extensions = CPP_EXTENSIONS

    def __init__(self, language: str = "cpp") -> None:
        if Language is None or tree_sitter_cpp is None or tree_sitter_c is None:
            raise RuntimeError("C/C++ Tree-sitter grammars are not installed; install tree-sitter-c and tree-sitter-cpp")
        grammar = tree_sitter_c.language() if language == "c" else tree_sitter_cpp.language()
        self.language = language
        self._parser = Parser(Language(grammar))

    def parse(self, path: Any, relative_path: str) -> ParsedFile:
        return self.parse_bytes(path.read_bytes(), relative_path).parsed

    def parse_bytes(self, source: bytes, relative_path: str, old_tree: Any | None = None) -> ParseOutcome:
        tree = self._parser.parse(source) if old_tree is None else self._parser.parse(source, old_tree)
        changed_ranges = []
        if old_tree is not None:
            changed_ranges = [{"start_byte": item.start_byte, "end_byte": item.end_byte,
                               "start_point": tuple(item.start_point), "end_point": tuple(item.end_point)}
                              for item in old_tree.changed_ranges(tree)]
        # The C and C++ vocabularies are one file by design: the same POSIX and
        # C11 calls appear in both, so `language` being "c" still means "cpp".
        parsed = CppExtractor(source, relative_path, self.language, event_vocabulary("cpp")).extract(tree.root_node)
        return ParseOutcome(parsed, tree, changed_ranges)
