"""Lexical declaration index over one parsed file (stage 4.5).

Stage 4.5 gives every event subject a *declaration identity*: which object a
name refers to, where it lives and how long it lives. The graph has no
declaration table -- `nodes` holds functions, types and namespaces, and a local
variable has never been a symbol -- so the facts have to be read from the tree
at parse time, by the same parser that produced the events.

What the index answers, for one use of one name at one byte offset:

- **storage**: `local`, `parameter`, `function_static`, `field`, `file_static`,
  `global`, or nothing at all (a macro, an enum constant, a name declared in a
  header this file does not include). The last case is a real answer and not a
  failure: a cross-file global is the most important shared state a race query
  can see, and dropping it because this parser does not follow `#include` would
  be the one mistake that loses the finding.
- **type_token**: the declared type's last identifier, template arguments
  stripped. A token, not a type: it is enough to tell `std::mutex` from
  `std::weak_ptr`, which is what the LOCK receiver classification needs, and
  not enough to resolve anything.
- **scope_token**: the class a field belongs to, or the file a static lives in.
  Empty for a global -- a file-scope declaration without `static` has external
  linkage, so splitting it by file would invent two objects where the program
  has one.

Resolution is **position sensitive**: within a scope the last declaration
before the use wins, so `void f(int n) { { int n; use(n); } use(n); }` reads
two different objects. Scopes are searched innermost outwards -- blocks, then
the function (whose frame holds the parameters), then enclosing classes, then
the file -- which is also the order in which C++ shadows.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any

from ..models import (
    STORAGE_FIELD, STORAGE_FILE_STATIC, STORAGE_FUNCTION_STATIC, STORAGE_GLOBAL,
    STORAGE_LOCAL, STORAGE_PARAMETER,
)

_WHITESPACE = re.compile(r"\s+")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")

# The declarators a name can hide behind. `init_declarator` is `int x = 1`,
# `function_declarator` is the most-vexing-parse variable `guard(m)`, and the
# rest wrap a pointer, reference or array around the identifier they bind.
_NAME_BEARING_DECLARATORS = frozenset({
    "init_declarator", "function_declarator", "pointer_declarator",
    "reference_declarator", "array_declarator", "parenthesized_declarator",
})
_LEAF_NAMES = frozenset({"identifier", "field_identifier", "type_identifier", "operator_name"})

# A node that declares names. `field_declaration` and `parameter_declaration`
# are declarations in the same sense as `declaration`; only their scope and
# storage differ.
_DECLARATION_NODES = frozenset({"declaration", "field_declaration", "parameter_declaration"})

# Nodes that open a lexical scope. `namespace_definition` is deliberately
# absent: a namespace does not create a distinct object, only a distinct
# spelling, and every query joins on identity rather than on spelling.
_SCOPE_NODES = frozenset({
    "compound_statement", "function_definition",
    "class_specifier", "struct_specifier", "union_specifier",
})
_FUNCTION_NODES = frozenset({"function_definition"})
_CLASS_NODES = frozenset({"class_specifier", "struct_specifier", "union_specifier"})

# The file scope, and the sentinel end of every scope that has no byte limit.
_FILE_SCOPE_END = 1 << 62


@dataclass(frozen=True, slots=True)
class Declaration:
    """One declared name, as the index found it."""

    name: str
    storage: str
    type_token: str
    scope_token: str
    start_byte: int
    # The first argument of the initializing call, verbatim, when the
    # declaration carries one (`std::unique_lock<std::mutex> lk(m)` records
    # `m`). The index does not decide that the declared type is a guard and
    # the argument a mutex -- that is a vocabulary judgment, and this layer
    # stays free of it. What is recorded is the pure syntax fact that the two
    # names are bound by the same declaration, which is what a lock query
    # needs to join a guard to the mutex it holds (5C-2's 2.6).
    init_arg0: str = ""


@dataclass(slots=True)
class _Scope:
    """One lexical scope, with the declarations made directly in it."""

    start_byte: int
    end_byte: int
    parent: int
    kind: str
    class_name: str = ""
    names: dict[str, list[Declaration]] = field(default_factory=dict)


def type_token(text: str) -> str:
    """The last identifier of a type spelling, template arguments removed.

    `std::lock_guard<std::mutex>` is `lock_guard`, `HNSW *` is `HNSW`, and
    `const char *` is `char`. Deliberately shallow: the token exists so a
    receiver can be classified as a mutex or not, and anything deeper would be
    a type system this stage does not have.
    """
    text = _WHITESPACE.sub(" ", text).strip()
    # Strip balanced `<...>` groups from the right, repeatedly: `A<B<C>>`.
    while text.endswith(">"):
        depth = 0
        for index in range(len(text) - 1, -1, -1):
            character = text[index]
            if character == ">":
                depth += 1
            elif character == "<":
                depth -= 1
                if depth == 0:
                    text = text[:index].strip()
                    break
        else:  # pragma: no cover - a stray `>` cannot come from a parser
            break
    identifiers = _IDENTIFIER.findall(text)
    return identifiers[-1] if identifiers else ""


class DeclarationIndex:
    """Every declaration in one file, resolved by position.

    Built once per file from the same tree the symbol extractor walked. The
    index never decides what an event means; it answers where a name comes
    from, and answers `None` -- visibly, and counted -- when the file does not
    say.
    """

    def __init__(self, source: bytes, relative_path: str) -> None:
        self.source = source
        self.relative_path = relative_path
        self._scopes: list[_Scope] = [_Scope(0, _FILE_SCOPE_END, -1, "file")]
        self._starts: list[int] = [0]
        self._stack: list[int] = [0]
        # Diagnostics, read by tests and by the stage's coverage narrative: a
        # miss means "no declaration in this file", which is a fact, while a
        # surprising number of misses means the walk is wrong.
        self.declarations = 0
        self.misses = 0
        # `type * QUALIFIER name` is a shape tree-sitter cannot parse; see
        # `_recover_qualified_parameters`. Keyed by the declaration node's start
        # byte, which is unique within a file for the node kinds involved.
        self._renamed: dict[int, str] = {}
        self.recovered_qualifiers = 0

    @classmethod
    def build(cls, source: bytes, relative_path: str, root: Any) -> DeclarationIndex:
        index = cls(source, relative_path)
        index._walk(root)
        index._sort()
        return index

    # ------------------------------------------------------------------
    # construction

    def _walk(self, node: Any) -> None:
        kind = node.type
        opened = False
        if kind in _SCOPE_NODES and (kind not in _CLASS_NODES or node.child_by_field_name("body") is not None):
            # `struct Foo x;` names a type and opens nothing; a class with a
            # body is a scope whose field declarations resolve from inside its
            # methods.
            self._open(node, kind)
            opened = True
        if kind in _DECLARATION_NODES:
            self._declare(node)
        elif kind == "parameter_list":
            # Before the children are visited, because `_declare` on the
            # parameter reads the correction this records.
            self._recover_qualified_parameters(node)
        for child in node.named_children:
            self._walk(child)
        if opened:
            self._stack.pop()

    def _recover_qualified_parameters(self, node: Any) -> None:
        """Read the real name out of `type * QUALIFIER name`.

        Neither grammar can parse a macro sitting between the `*` and the name,
        which is how C spells a restrict qualifier portably. Both recover the
        same way: the macro becomes the declarator and the real name is pushed
        into an `ERROR` node beside it, so `float * GGML_RESTRICT s` declares a
        parameter named `GGML_RESTRICT` and loses `s`.

        Reading that pair back is not a guess about C -- it is the grammar's own
        recovery shape, and the two halves are checked together: a pointer
        declarator whose own declarator is a bare identifier, immediately
        followed by an error node holding exactly one identifier. Both halves
        are required, so a genuine parse error elsewhere cannot be mistaken for
        a rename.

        The cost of missing it is not just the dropped parameter: the name that
        survives is the macro, so the graph would carry a parameter named after
        a qualifier and the real one would fall through to the spelling
        fallback -- which is the join the race query is built on.
        """
        children = node.children
        for position, child in enumerate(children[:-1]):
            if child.type != "parameter_declaration":
                continue
            following = children[position + 1]
            if following.type != "ERROR" or following.named_child_count != 1:
                continue
            name = following.named_children[0]
            if name.type != "identifier":
                continue
            declarator = child.child_by_field_name("declarator")
            if declarator is None or declarator.type != "pointer_declarator":
                continue
            inner = declarator.child_by_field_name("declarator")
            if inner is None or inner.type != "identifier":
                continue
            self._renamed[child.start_byte] = self._text(name)
            self.recovered_qualifiers += 1

    def _open(self, node: Any, kind: str) -> None:
        class_name = ""
        if kind in _CLASS_NODES:
            class_name = self._text(node.child_by_field_name("name"))
        self._scopes.append(
            _Scope(node.start_byte, node.end_byte, self._stack[-1], kind, class_name)
        )
        self._stack.append(len(self._scopes) - 1)

    def _declare(self, node: Any) -> None:
        declarators = list(node.children_by_field_name("declarator"))
        if not declarators:
            # `struct Foo;` or a prototype's unnamed parameter: a type, not a
            # name. Nothing to index.
            return
        storage = self._storage(node)
        scope_token = self._scope_token(storage)
        token = type_token(self._text(node.child_by_field_name("type")))
        frame = self._scopes[self._stack[-1]]
        for declarator in declarators:
            # A recovered qualifier rename applies to the whole declaration;
            # only `parameter_declaration` is ever renamed, and it carries a
            # single declarator.
            name = self._renamed.get(node.start_byte) or self._declarator_name(declarator)
            if not name:
                continue
            frame.names.setdefault(name, []).append(
                Declaration(
                    name=name, storage=storage, type_token=token,
                    scope_token=scope_token, start_byte=node.start_byte,
                    init_arg0=self._init_arg0(declarator),
                )
            )
            self.declarations += 1

    def _init_arg0(self, declarator: Any) -> str:
        """The initializing call's first argument, verbatim, or "".

        `Type lk(m);` is the shape this exists for, and it parses two ways. As
        an `init_declarator` carrying a `call_expression` value -- `s =
        malloc(n)` records `n` the same way, because this layer records the
        syntax fact and leaves the judgment to the vocabulary's consumers. Or
        as the most-vexing-parse: the whole statement reads as a function
        declaration, and the guard's constructor argument shows up as a
        single *typeless* parameter (`m`, no `int` in front of it). A typed
        parameter is a real prototype and records nothing.
        """
        value = declarator.child_by_field_name("value")
        if value is not None and value.type == "call_expression":
            arguments = value.child_by_field_name("arguments")
            first = arguments.named_children[0] if arguments is not None and arguments.named_children else None
            if first is None:
                return ""
            return " ".join(self._text(first).split())
        if declarator.type == "function_declarator":
            parameters = declarator.child_by_field_name("parameters")
            if parameters is not None and parameters.type == "parameter_list":
                vexing = [
                    child for child in parameters.named_children
                    if child.type == "parameter_declaration"
                    # The argument reads as the parameter's *type* here: `m`
                    # parses as a `type_identifier`, while a real prototype's
                    # parameter is a primitive (`int`) or names its own
                    # declarator. Both of those are genuine declarations and
                    # record nothing.
                    and child.child_by_field_name("type") is not None
                    and child.child_by_field_name("type").type == "type_identifier"
                    and child.child_by_field_name("declarator") is None
                ]
                if len(vexing) == 1:
                    return " ".join(self._text(vexing[0].child_by_field_name("type")).split())
        return ""

    def _storage(self, node: Any) -> str:
        if node.type == "parameter_declaration":
            return STORAGE_PARAMETER
        if node.type == "field_declaration":
            return STORAGE_FIELD
        is_static = self._has_static(node)
        if self._innermost_function_frame() >= 0:
            return STORAGE_FUNCTION_STATIC if is_static else STORAGE_LOCAL
        return STORAGE_FILE_STATIC if is_static else STORAGE_GLOBAL

    def _scope_token(self, storage: str) -> str:
        if storage == STORAGE_FIELD:
            for frame_index in reversed(self._stack):
                name = self._scopes[frame_index].class_name
                if name:
                    return name
            return ""
        if storage == STORAGE_FILE_STATIC:
            return self.relative_path
        # A global keeps no path: a file-scope declaration without `static` has
        # external linkage, and two files that mention the same one are naming
        # the same object.
        return ""

    def _innermost_function_frame(self) -> int:
        for frame_index in reversed(self._stack):
            if self._scopes[frame_index].kind in _FUNCTION_NODES:
                return frame_index
        return -1

    def _has_static(self, node: Any) -> bool:
        return any(
            child.type == "storage_class_specifier" and self._text(child) == "static"
            for child in node.children
        )

    def _declarator_name(self, node: Any) -> str:
        while node is not None:
            if node.type in _LEAF_NAMES:
                return self._text(node)
            if node.type == "qualified_identifier":
                # `int Ns::x;` -- the last part is the name.
                return self._text(node.child_by_field_name("name") or node)
            if node.type in _NAME_BEARING_DECLARATORS:
                inner = node.child_by_field_name("declarator")
                if inner is None:
                    # tree-sitter-cpp leaves a `reference_declarator`'s name
                    # unfielded: `Ctx & ctx` is `&` then `ctx` with no
                    # `declarator` field, where `Ctx * p` carries one. Reading
                    # the field alone drops every C++ reference parameter, and
                    # a reference is the ordinary way to pass a context object
                    # -- so the drop lands on exactly the names a race query
                    # reads, and they fall through to the spelling fallback.
                    # The name is the last named child in that shape; if it is
                    # not a name, the next turn of this loop returns "".
                    inner = node.named_children[-1] if node.named_children else None
                node = inner
                continue
            return ""
        return ""

    def _text(self, node: Any | None) -> str:
        if node is None:
            return ""
        return self.source[node.start_byte:node.end_byte].decode("utf-8", "replace")

    # ------------------------------------------------------------------
    # lookup

    def _sort(self) -> None:
        """Order the scopes for the innermost-first lookup, once, after the walk."""
        order = sorted(
            range(len(self._scopes)),
            key=lambda item: (self._scopes[item].start_byte, -self._scopes[item].end_byte),
        )
        self._scopes = [self._scopes[item] for item in order]
        remap = {old: new for new, old in enumerate(order)}
        for frame in self._scopes:
            frame.parent = remap[frame.parent] if frame.parent >= 0 else -1
        self._starts = [frame.start_byte for frame in self._scopes]

    def resolve(self, use_byte: int, name: str) -> Declaration | None:
        """The declaration `name` refers to at `use_byte`, or None.

        Innermost scope outwards, and inside a scope the last declaration that
        starts at or before the use. A miss is counted rather than raised: a
        name with no declaration in this file is a fact about the file.
        """
        if not name:
            return None
        frame_index = self._innermost_frame(use_byte)
        while frame_index >= 0:
            frame = self._scopes[frame_index]
            found = self._last_before(frame.names.get(name), use_byte)
            if found is not None:
                return found
            frame_index = frame.parent
        self.misses += 1
        return None

    def resolve_member(self, use_byte: int, class_name: str, name: str) -> Declaration | None:
        """The field `name` of `class_name`, seen from a use outside the class.

        An out-of-line method definition (`void S::touch()`) sits outside every
        scope `S` opens, so `resolve()` cannot see `m_` from its body: the
        field and the use share a file but no frame. The class frame is still
        in the index, though, and its spelling of the field is the same token
        an in-class access records (`field:S:m_`), which is the whole point --
        the two spellings of one object join instead of splitting into a field
        identity and an `unknown` one.

        When several classes in one file spell alike, scope order decides, the
        same last-wins rule `resolve()` applies within a frame. Only fields
        answer: a bare name that names a member function is a call, and the
        call machinery resolves it. A miss is *not* counted here -- `resolve()`
        has already counted it by the time this fallback runs.
        """
        if not class_name or not name:
            return None
        for frame in reversed(self._scopes):
            if frame.class_name != class_name:
                continue
            found = self._last_before(frame.names.get(name), use_byte)
            if found is not None and found.storage == STORAGE_FIELD:
                return found
        return None

    @staticmethod
    def _last_before(entries: list[Declaration] | None, use_byte: int) -> Declaration | None:
        if not entries:
            return None
        # Declarations are appended in walk order, which is source order within
        # one scope, so scanning back finds the last one that starts before the
        # use. The list is tiny for all but the largest functions.
        for declaration in reversed(entries):
            if declaration.start_byte <= use_byte:
                return declaration
        return None

    def enclosing_class(self, use_byte: int) -> str:
        """The innermost class or struct the offset sits in, or ""."""
        frame_index = self._innermost_frame(use_byte)
        while frame_index >= 0:
            frame = self._scopes[frame_index]
            if frame.class_name:
                return frame.class_name
            frame_index = frame.parent
        return ""

    def _innermost_frame(self, use_byte: int) -> int:
        """The smallest scope containing the offset.

        The same lookup as `parsing.events.OwnerIndex.owner_of`: scopes sorted
        by open position, walk back from the last one that opens at or before
        the offset, because a containing scope always opens before the scope it
        contains.
        """
        index = bisect_right(self._starts, use_byte) - 1
        while index >= 0:
            if self._scopes[index].end_byte >= use_byte:
                return index
            index -= 1
        return 0
