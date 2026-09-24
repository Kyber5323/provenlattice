from __future__ import annotations

import re
from bisect import bisect_right
from pathlib import PurePosixPath
from typing import Any

from ..models import ParsedFile, ParsedImport, ParsedReference, ParsedSymbol
from ..semantics.vocabulary import Vocabulary
from .events import EventExtractor, OwnerIndex, OwnerSpan


def module_name(relative_path: str) -> str:
    parts = list(PurePosixPath(relative_path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class PythonExtractor:
    """Normalize Python Tree-sitter CST nodes into parser facts."""

    def __init__(self, source: bytes, relative_path: str, vocabulary: Vocabulary | None = None) -> None:
        self.source = source
        self.line_starts = [0, *(index + 1 for index, value in enumerate(source) if value == 10)]
        self.module = module_name(relative_path)
        self.scope: list[tuple[str, str]] = []
        self.symbols: list[ParsedSymbol] = []
        self.references: list[ParsedReference] = []
        self.imports: list[ParsedImport] = []
        self.import_aliases: dict[str, tuple[str, str | None]] = {}
        self.vocabulary = vocabulary
        self.owner_spans: list[OwnerSpan] = []
        self.skipped_events = 0

    def text(self, node: Any | None) -> str:
        if node is None:
            return ""
        return self.source[node.start_byte : node.end_byte].decode("utf-8")

    def qualified(self, name: str | None = None) -> str:
        return ".".join(part for part in [self.module, *(item[0] for item in self.scope), name] if part)

    def current_symbol(self) -> str:
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
        events = []
        if self.vocabulary is not None:
            extractor = EventExtractor(
                self.source,
                self.vocabulary,
                OwnerIndex(self.owner_spans),
                lambda offset, end: bisect_right(self.line_starts, offset),
            )
            events = extractor.extract(root)
            self.skipped_events = extractor.skipped_outside_owner
        return ParsedFile(
            self.symbols, self.references, self.imports,
            events=events, unowned_events=self.skipped_events,
        )

    def _walk(self, node: Any) -> None:
        if node.type in {"function_definition", "async_function_definition"}:
            self._function(node)
            return
        if node.type == "class_definition":
            self._class(node)
            return
        if node.type in {"import_statement", "import_from_statement"}:
            self._import(node)
            return
        if node.type == "call":
            self._call(node)
            return
        if node.type == "type_alias_statement":
            self._type_alias(node)
            return
        if node.type == "assignment" and node.child_by_field_name("type") is not None:
            self._annotated_type(node)
        if node.type == "identifier":
            self._reference(node)
        for child in node.named_children:
            self._walk(child)

    def _function(self, node: Any) -> None:
        name = self.text(node.child_by_field_name("name"))
        parameters = self.text(node.child_by_field_name("parameters"))
        return_type = self.text(node.child_by_field_name("return_type"))
        signature = parameters + (f" -> {return_type}" if return_type else "")
        kind = "Method" if self.scope and self.scope[-1][1] in {"Class", "Interface"} else "Function"
        qualified = self.qualified(name)
        self.symbols.append(
            ParsedSymbol(
                kind, name, qualified, self.line(node), self.line(node, end=True),
                signature, not name.startswith("_"),
                {"async": any(child.type == "async" for child in node.children)},
            )
        )
        self.owner_spans.append(OwnerSpan(node.start_byte, node.end_byte, kind, qualified, signature))
        self.scope.append((name, kind))
        body = node.child_by_field_name("body")
        if body:
            self._walk(body)
        self.scope.pop()

    def _class(self, node: Any) -> None:
        name = self.text(node.child_by_field_name("name"))
        superclass_node = node.child_by_field_name("superclasses")
        superclass_text = self.text(superclass_node)
        bases = [item.strip() for item in superclass_text.strip("()").split(",") if item.strip()]
        kind = "Interface" if any(base.rsplit(".", 1)[-1] in {"Protocol", "ABC"} for base in bases) else "Class"
        qualified = self.qualified(name)
        self.symbols.append(
            ParsedSymbol(
                kind, name, qualified, self.line(node), self.line(node, end=True),
                superclass_text or "()", not name.startswith("_"), {"bases": bases},
            )
        )
        for base in bases:
            if base not in {"object", "ABC", "Protocol"}:
                self.references.append(
                    ParsedReference(
                        qualified, "IMPLEMENTS", base,
                        start_line=self.line(node), end_line=self.line(node, end=True),
                    )
                )
        self.scope.append((name, kind))
        body = node.child_by_field_name("body")
        if body:
            self._walk(body)
        self.scope.pop()

    def _import(self, node: Any) -> None:
        statement = " ".join(self.text(node).replace("(", " ").replace(")", " ").split())
        source = self.current_symbol() or self.module
        if statement.startswith("from "):
            match = re.match(r"from\s+(\S+)\s+import\s+(.+)", statement)
            if not match:
                return
            module, names = match.groups()
            for item in names.split(","):
                parts = item.strip().split(" as ", 1)
                target, alias = parts[0].strip(), parts[1].strip() if len(parts) > 1 else None
                local = alias or target
                self.import_aliases[local] = (module, target)
                self.imports.append(
                    ParsedImport(
                        source, target, module, alias,
                        {"start_line": self.line(node), "end_line": self.line(node, end=True)},
                    )
                )
        elif statement.startswith("import "):
            for item in statement[7:].split(","):
                parts = item.strip().split(" as ", 1)
                module, alias = parts[0].strip(), parts[1].strip() if len(parts) > 1 else None
                local = alias or module.split(".")[0]
                self.import_aliases[local] = (module, None)
                self.imports.append(
                    ParsedImport(
                        source, module, module, alias,
                        {"start_line": self.line(node), "end_line": self.line(node, end=True)},
                    )
                )

    def _call(self, node: Any) -> None:
        target = self.text(node.child_by_field_name("function"))
        source = self.current_symbol()
        if source and target:
            root, _, suffix = target.partition(".")
            imported = self.import_aliases.get(root)
            module = imported[0] if imported else None
            imported_name = imported[1] if imported else None
            if imported_name:
                target = imported_name + (f".{suffix}" if suffix else "")
            elif imported and suffix:
                target = suffix
            function = node.child_by_field_name("function")
            self.references.append(
                ParsedReference(
                    source, "CALLS", target, module,
                    start_line=self.line(function),
                    end_line=self.line(function, end=True),
                )
            )
        for child in node.named_children:
            self._walk(child)

    def _type_alias(self, node: Any) -> None:
        name_node = node.child_by_field_name("left")
        name = self.text(name_node)
        if not name:
            return
        self.symbols.append(
            ParsedSymbol(
                "Type", name, self.qualified(name), self.line(node),
                self.line(node, end=True), self.text(node), not name.startswith("_"), {},
            )
        )

    def _annotated_type(self, node: Any) -> None:
        if self.scope:
            return
        name = self.text(node.child_by_field_name("left"))
        annotation = self.text(node.child_by_field_name("type"))
        if name and name.isidentifier():
            self.symbols.append(
                ParsedSymbol(
                    "Type", name, self.qualified(name), self.line(node),
                    self.line(node, end=True), annotation, not name.startswith("_"), {},
                )
            )

    def _reference(self, node: Any) -> None:
        if not self.current_symbol() or node.parent is None:
            return
        parent = node.parent
        excluded = {
            "parameters", "typed_parameter", "default_parameter", "typed_default_parameter",
            "import_statement", "import_from_statement", "aliased_import",
        }
        if parent.type in excluded:
            return
        if self.same_node(parent.child_by_field_name("name"), node) or (
            parent.type == "call" and self.same_node(parent.child_by_field_name("function"), node)
        ):
            return
        target = self.text(node)
        if target:
            self.references.append(
                ParsedReference(
                    self.current_symbol(), "REFERENCES", target,
                    start_line=self.line(node), end_line=self.line(node, end=True),
                )
            )
