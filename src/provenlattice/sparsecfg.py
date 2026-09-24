"""The sparse control-flow graph over semantic events (stage 3, appendix B.4).

Two halves share this module, and the split is deliberate:

* `SparseCfgBuilder` reads tree-sitter trees and produces one file's CFG as
  plain data (`models.ParsedCfg`). It runs at parse time, where the source
  bytes and the owner spans live, and never learns a repository id -- point
  identity travels as `models.PointKey`, which `graph.py` resolves against the
  events it published.
* `find_paths` and the three pattern functions answer reachability over
  published edges. They are pure functions over plain data, so tests can drive
  them without a database and the query layer stays a thin reader.

Neither half imports tree-sitter, so the module imports cleanly when the C
grammar is not installed.

What the graph compresses: a method's statements collapse to the events the
vocabulary produced plus three structurally-derived points (CHECK / BRANCH /
DEREFERENCE) and one synthetic exit per method. Straight-line code produces no
edges at all beyond the chain between its points -- that is what keeps the
graph sparse at million-line scale.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Collection, Iterable

from .identity import CFG_EXIT_PREFIX
from .models import (
    STORAGE_FIELD, STORAGE_FUNCTION_STATIC, STORAGE_FILE_STATIC, STORAGE_GLOBAL,
    STORAGE_LOCAL, STORAGE_PARAMETER, STORAGE_UNKNOWN,
    PointKey, ParsedCfg, ParsedEdge, ParsedPoint,
)
from .semantics.events import ACCESS_EVENTS

CONTROL_REACHES = "CONTROL_REACHES"

# Edge flags (design doc §6, plus `fallthrough` for switch chains). The sorted
# flag set is the edge id's discriminator, so the exact spelling is frozen:
# renaming a flag changes every id in the database.
BRANCH_TRUE = "branch=true"
BRANCH_FALSE = "branch=false"
EXCEPTION = "exception"
LOOP_BACK = "loop_back"
FALLTHROUGH = "fallthrough"

# Point flag: the condition's top-level operator is a short-circuit, so the
# inner points feed both arms. Marks the `||` blind spot recorded in the stage
# 3 execution record: `if (p != NULL || p->f)` hides its dereference from the
# unchecked_dereferences pattern exactly like the guarded `p && p->f` does.
SHORT_CIRCUIT = "short_circuit"

# Annotation keys that ride on a point's metadata (stage 4). `subject` says
# which name an operation acts on; `subject_from` says how the walker got it,
# because a name recovered from an assignment is a stronger claim than one
# recovered from a member call's receiver -- and the typed queries need that
# difference to keep an unresolved identity from reading as a resolved one.
SUBJECT_FROM_ASSIGNMENT = "assignment"
SUBJECT_FROM_ARGUMENT = "argument"
SUBJECT_FROM_RECEIVER = "receiver"
SUBJECT_FROM_RAII = "raii"
SUBJECT_FROM_RETURN = "return"

# The event types that get a subject recovered for them. Stage 3 covered
# ALLOC/RELEASE/CALL; the typed queries need the lock, atomic, spawn and
# return points too -- `RETURN` because `return p;` is how a method hands a
# resource to its caller, which is the one ownership transfer a sparse CFG can
# see at all.
_ANNOTATED_EVENT_TYPES = frozenset(
    {"ALLOC", "RELEASE", "CALL", "LOCK", "UNLOCK", "ATOMIC", "THREAD_SPAWN", "RETURN"}
)

# The event types that are published but never connected (stage 5A). An access
# is a fact about an expression, not a position control flow can reach: giving
# it an edge would put it on a `find_paths` path, change which paths the
# `max_paths` truncation keeps, and move the stage 5C rulings that were
# measured against the current path set. Enforced in `_emit`, which is the one
# function every edge goes through, and it raises rather than dropping the edge
# silently -- this can only be broken by an edit to the walker, never by source
# code, so it must be loud.
_EDGELESS_EVENT_TYPES = ACCESS_EVENTS

# Which child of a statement is the object being written. Position is the only
# signal there is: no keyword in `x = y` says which side is the write.
WRITE_TARGET_FIELDS: dict[str, str] = {
    "assignment_expression": "left",
    "compound_assignment_expression": "left",
    "update_expression": "argument",
    "init_declarator": "declarator",
}

# A declaration names an object; it does not read one. `int x;` and the `x` in
# `int x = 0;` are the same non-access.
DECLARED_FIELDS: dict[str, str] = {
    "declaration": "declarator",
    "parameter_declaration": "declarator",
    "field_declaration": "declarator",
    "function_definition": "declarator",
}

# Fields whose expression is a name rather than a variable: the callee of a
# call is what is being invoked, not an object being touched.
ACCESS_SKIP_FIELDS: dict[str, str] = {"call_expression": "function"}

# The storage classes an access is emitted for. Stage 5A's rule was: the
# filter is not an optimisation, it is the identity -- `_facts_for` gives a
# `local` or a `parameter` a token scoped to the method, so such an object
# can never be half of a cross-method pair. Stage 5B keeps the identity
# claim and narrows the exception: a `parameter` WRITE is published (only
# the write side -- the read side has no in-frame consumer) because the
# query-time parameter join composes cross-method pairs from it, in the
# caller's terms, without ever minting a cross-method edge. The method-
# scoped token means these writes still cannot pair on their own, and the
# race query's `multi_method_subjects_by_storage` continues to report zero.
# `function_static` is emitted: its token carries no scope at all (the
# declaration index only stamps a class for a field and a path for a
# file_static), so it merges repo-wide by name and is genuinely shared.
# `unknown` is emitted because a cross-file global is the most valuable race
# subject there is and it is exactly what a name with no declaration looks
# like; the noise that comes with it is reported, not hidden.
ACCESS_STORAGES = frozenset({
    STORAGE_FIELD, STORAGE_FILE_STATIC, STORAGE_FUNCTION_STATIC, STORAGE_GLOBAL,
    STORAGE_PARAMETER, STORAGE_UNKNOWN,
})


@dataclass(frozen=True, slots=True)
class LanguageProfile:
    """The grammar facts the CFG walker reads, held as data.

    C and C++ agree on every fact the walker needs today, and the two shapes
    that do differ between the grammars (the node wrapping an if/while/switch
    condition, the subscript index field name) are handled by trying
    alternatives rather than by forking the algorithm. The profile exists so a
    third language joins as another instance, not another walker. Python's
    profile is a registered gap of stage 3, not an oversight.
    """

    name: str
    deref_node_types: frozenset[str] = frozenset(
        {"pointer_expression", "field_expression", "subscript_expression"}
    )
    # `*p` dereferences; `&p` does not.
    deref_unary_operators: frozenset[str] = frozenset({"*"})
    # `p->f` dereferences; `p.f` does not.
    deref_member_operators: frozenset[str] = frozenset({"->"})
    # The tree-sitter `null` rule covers NULL and nullptr in both dialects.
    # `p == 0` is a number_literal and stays unrecognised (registered
    # imprecision, matching the design doc's conservative reading).
    null_node_types: frozenset[str] = frozenset({"null"})
    short_circuit_operators: frozenset[str] = frozenset({"&&", "||"})
    # The expressions that denote an object being touched (stage 5A). One
    # access is one outermost expression: `p->buf[i]` is a subscript_expression
    # whose argument is a field_expression whose argument is an identifier, and
    # it is *one* access, not three -- the walker emits for the outermost and
    # stops. These are grammar facts like `deref_node_types` above, and they
    # live in the profile for the same reason: a third language joins by
    # declaring its own, not by editing the walker.
    access_node_types: frozenset[str] = frozenset(
        {"identifier", "field_expression", "subscript_expression", "pointer_expression"}
    )


C_PROFILE = LanguageProfile(name="c")
CPP_PROFILE = LanguageProfile(name="cpp")

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ControlPath:
    """One acyclic path, as three parallel tuples: event ids, their types, and
    the edge ids between consecutive points."""

    points: tuple[str, ...]
    types: tuple[str, ...]
    edges: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "points": list(self.points),
            "types": list(self.types),
            "edges": list(self.edges),
        }


def _field_of(edge_or_event: Any, name: str) -> Any:
    """Read a field from a `SemanticEdge`/`SemanticEvent` object or its row."""
    if isinstance(edge_or_event, dict):
        return edge_or_event[name]
    return getattr(edge_or_event, name)


def _flags_of(edge: Any) -> frozenset:
    flags = _field_of(edge, "flags")
    if isinstance(flags, str):
        flags = json.loads(flags)
    return frozenset(flags)


def _text_of(source: bytes, node: Any) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _expression_text(source: bytes, node: Any) -> str:
    """Whitespace-free source text, so `p  ->  f` reads as `p->f`."""
    return _WHITESPACE.sub("", _text_of(source, node))


def _operator_of(node: Any, source: bytes) -> str:
    """The operator token of an expression node, whatever the grammar names it.

    The bundled grammars carry an `operator` field on most expression nodes,
    but the fallback keeps the walker honest against grammars that leave the
    token anonymous: scan the node's own children for a operator-shaped token.
    """
    operator = node.child_by_field_name("operator")
    if operator is not None:
        return _text_of(source, operator).strip()
    for child in node.children:
        if not child.is_named and child.type in {
            "*", "&", "->", ".", "!", "&&", "||",
            "==", "!=", "<", ">", "<=", ">=",
        }:
            return _text_of(source, child).strip()
    return ""


def _span(node: Any) -> tuple[int, int] | None:
    """A node's identity as a span, or None. Comparing spans, not Node objects:
    py-tree-sitter hands back a fresh wrapper per accessor call, so `in` on the
    wrappers is whatever its `__eq__` does today."""
    return None if node is None else (node.start_byte, node.end_byte)


def _outermost_access(node: Any, access_types: frozenset[str]) -> Any:
    """The access expression a target denotes, skipping parens and casts.

    `s->buf` is already an access; `(int *)p->buf` reaches the
    `field_expression` through the cast. Returns None when the target names no
    object this stage recognises (a literal, a call, a compound literal).
    """
    if node is None:
        return None
    if node.type in access_types:
        return node
    for child in node.named_children:
        found = _outermost_access(child, access_types)
        if found is not None:
            return found
    return None


def _unwrap_condition(node: Any) -> Any:
    """The expression inside a condition's wrapper node, if there is one.

    C++ wraps if/while/switch conditions in `condition_clause`; C uses
    `parenthesized_expression`; `do` uses a parenthesized expression in both.
    Everything else is already the expression.
    """
    if node is None:
        return None
    if node.type == "condition_clause":
        value = node.child_by_field_name("value")
        if value is not None:
            return value
    if node.type == "parenthesized_expression" and node.named_children:
        return node.named_children[0]
    return node


def _label_flags(case: Any) -> tuple[str, ...]:
    """`default:` hangs off the false edge; every real case is a true edge."""
    if case.child_by_field_name("value") is None:
        return (BRANCH_FALSE,)
    return (BRANCH_TRUE,)


class SparseCfgBuilder:
    """Walk one parsed file and produce its compressed CFGs.

    The builder consumes the SAME hits the event layer consumed --
    `EventExtractor.extract_hits` -- so an operation cannot be an event while
    being invisible to the CFG, or the reverse. It never assigns event
    identity beyond `PointKey`: the repository id does not exist at parse time.
    """

    def __init__(
        self,
        source: bytes,
        owners: Any,
        hits: Iterable[Any],
        line_of: Callable[[int, bool], int],
        profile: LanguageProfile,
        declarations: Any = None,
    ) -> None:
        """`owners` is a parsing.events.OwnerIndex; `line_of(byte, is_end)`.

        `declarations` is a parsing.declarations.DeclarationIndex, or None when
        the language has none (Python today). It is handed in rather than built
        here because this class consumes facts and does not produce them: the
        same reason `owners` is handed in.
        """
        self.source = source
        self.owners = owners
        self.line_of = line_of
        self.profile = profile
        self.declarations = declarations
        # The hits grouped by the span of the node that produced them. Two
        # events share one span when one node matches two event types
        # (`free(p)` is both CALL and RELEASE); they chain in type order.
        self._hits_by_span: dict[tuple[int, int], list[Any]] = {}
        for hit in hits:
            self._hits_by_span.setdefault((hit.start_byte, hit.end_byte), []).append(hit)
        for group in self._hits_by_span.values():
            group.sort(key=lambda hit: (hit.event.event_type, hit.event.ordinal))
        self._points: list[_Point] = []
        # Edges under construction: (src, dst, flags) where dst is a _Point or
        # the _EXIT marker. `None` never appears -- it is dropped at _emit.
        self._edges: list[tuple[_Point, Any, tuple[str, ...]]] = []
        self._annotations: dict[PointKey, dict[str, str]] = {}
        self._seq = 0
        # Per-method state, reset in _build_method.
        self._span: Any = None
        self._has_points = False
        self._assign_target: str | None = None
        self._assign_exact = True
        self._assign_node: Any = None
        # How many conditional constructs the walk is currently inside. This
        # is the walker's *own* control model -- the same dispatch that builds
        # the edges -- so "depth 0" means what the graph means by it: no if,
        # loop, switch or catch encloses the statement being walked. The
        # escape facts (5C-2's 2.11) are keyed on it; a plain assignment that
        # every path of the method executes is the one an escape ruling can
        # trust.
        self._conditional_depth = 0
        # Where each vocabulary hit sits, by the same key its annotations are
        # stored under. The escape post-pass needs positions to tell "a write
        # after the allocation" from "a same-named local earlier in the
        # method"; hit points themselves are transient (the event layer
        # publishes them), so their positions are kept here instead.
        self._hit_positions: dict[PointKey, tuple[int, int]] = {}
        self._breaks: list[_BreakContext] = []
        self._switches: list[_SwitchContext] = []
        self._handlers: list[_HandlerContext] = []
        self._pending_throws: list[tuple[list[_Open], _HandlerContext]] = []

    # ------------------------------------------------------------------
    # entry points

    def build(self, root: Any) -> ParsedCfg:
        spans = {(span.start_byte, span.end_byte): span for span in self.owners.spans}
        nodes_by_span: dict[tuple[int, int], Any] = {}
        self._collect(root, spans, nodes_by_span)
        for span_key in sorted(nodes_by_span):
            self._build_method(spans[span_key], nodes_by_span[span_key])
        self._assign_ordinals()
        return ParsedCfg(
            # The public shape: a ParsedPoint per point, flags in their sorted
            # canonical form. `matched_via` says which of the walker's two
            # products this is: a structural control point, or an access. The
            # vocabulary's own `matched_via` never reaches here -- those points
            # come from `_point_from_hit` and are published by the event layer.
            points=[
                ParsedPoint(
                    event_type=point.event_type,
                    owner_kind=point.owner_kind,
                    owner_qualified_name=point.owner_qualified_name,
                    owner_signature=point.owner_signature,
                    ordinal=point.ordinal,
                    start_line=point.start_line,
                    end_line=point.end_line,
                    matched_name=point.matched_name,
                    matched_via="access" if point.event_type in ACCESS_EVENTS else "structural",
                    flags=tuple(sorted(point.flags)),
                )
                for point in sorted(self._points, key=lambda point: point.key())
            ],
            edges=self._parsed_edges(),
            annotations=self._annotations,
        )

    def _collect(self, node: Any, spans: dict, found: dict) -> None:
        key = (node.start_byte, node.end_byte)
        if key in spans:
            found[key] = node
        for child in node.named_children:
            self._collect(child, spans, found)

    def _build_method(self, span: Any, node: Any) -> None:
        body = node.child_by_field_name("body")
        if body is None:
            # A prototype owns no control flow, and an EXIT after it would
            # claim a body the source does not have.
            return
        self._span = span
        self._has_points = False
        try:
            _, opens = self._linearize(body, [_Open(None, ())])
        finally:
            self._span = None
            self._assign_target = None
            self._assign_exact = True
            self._assign_node = None
            self._conditional_depth = 0
            self._breaks = []
            self._switches = []
            self._handlers = []
            self._pending_throws = []
        if not self._has_points:
            return
        self._annotate_escapes(span)
        self._annotate_aliases(span)
        # Every dangling open falls into the synthetic exit. Returns and throws
        # already emitted their own exit edges inside the walk. The open's own
        # flags ride along: a `while (c)` condition's false arm reaches the exit
        # as `branch=false`, not as a flag-less fall into the void.
        for open_edge in opens:
            if open_edge.src is not None:
                self._emit(open_edge.src, _EXIT, open_edge.flags)

    def _annotate_escapes(self, span: Any) -> None:
        """Link frame-local allocations to the writes that let them escape.

        5C-2's 2.11, the half the WRITE events alone do not carry. `tmp =
        strdup(x)` declares a local, and `g_name = tmp;` copies the pointer
        into storage that outlives the frame; neither fact answers "does this
        allocation leak" on its own -- the lifetime walk sees an allocation
        with no release, and the WRITE is a fact about `g_name`, not about
        `tmp`. The link lands on the allocation, where the lifetime query's
        path-independent rulings read it.

        Only unconditional writes join, and only into storage the access
        layer itself calls shared: a conditional escape is exactly the shape
        where the other path still leaks, and the query would have to stay
        silent about it. A write that does not start after the allocation is
        not an escape of *this* allocation -- a same-named local in an earlier
        turn of a loop is a different object with the same spelling.
        """
        # `self._points` and `self._annotations` accumulate across methods; the
        # escape of one method's local is only ever written by the same
        # method's code.
        owner_key = (span.kind, span.qualified_name, span.signature)
        writes = [
            point for point in self._points
            if point.event_type == "WRITE"
            and (point.owner_kind, point.owner_qualified_name, point.owner_signature)
            == owner_key
            and point.annotations.get("write_source")
            and point.annotations.get("unconditional") == "true"
            and point.annotations.get("subject_storage") in _ESCAPE_TARGET_STORAGES
        ]
        if not writes:
            return
        for key, anns in self._annotations.items():
            if (
                key.event_type != "ALLOC"
                or (key.owner_kind, key.owner_qualified_name, key.owner_signature)
                != owner_key
                or anns.get("subject_storage") not in _FRAME_LOCAL_STORAGES
            ):
                continue
            name = anns.get("subject", "")
            if not name:
                continue
            alloc_start = self._hit_positions.get(key)
            if alloc_start is None:
                continue
            targets: list[str] = []
            line = 0
            for write in writes:
                if write.annotations["write_source"] != name or write.start_byte <= alloc_start[0]:
                    continue
                target = write.annotations.get("subject", "")
                if target and target not in targets:
                    targets.append(target)
                if line == 0 or write.start_line < line:
                    line = write.start_line
            if targets:
                anns["escaped_to"] = ";".join(targets)
                anns["escaped_to_line"] = str(line)

    def _annotate_aliases(self, span: Any) -> None:
        """Stamp the MAY_ALIAS candidates a bare copy creates (stage 5B).

        `q = p` and `q = &p` make the two names possible aliases for the rest
        of the method; `_access_point` records the copy itself as
        `alias_source` on the write. This pass propagates the fact to the
        ends that have events of their own: the write, and every later point
        of either root. The annotation is a candidate, never a fact edge --
        the matrix's MAY_ALIAS stays at candidate metadata, and the DFG's
        chain walk reads `alias_source` to know a def continues another
        name's chain.

        Position discipline, same reasoning as `_annotate_escapes`: an event
        before the copy is not an access through the alias -- a same-named
        local in an earlier turn of a loop is a different object with the
        same spelling. Pairs where one side has no events record on the side
        that does; pairs where neither side has an event publish nothing,
        which is the registered residual, not a hidden one.
        """
        owner_key = (span.kind, span.qualified_name, span.signature)
        copies = [
            point for point in self._points
            if point.event_type == "WRITE"
            and (point.owner_kind, point.owner_qualified_name, point.owner_signature)
            == owner_key
            and point.annotations.get("alias_source")
        ]
        if not copies:
            return
        # Collected by point identity and written back onto the points
        # directly: mid-walk there are no ordinals yet (`_assign_ordinals`
        # runs after the walk), so `point.key()` would collapse every
        # same-type point onto one entry, and the point channel reaches
        # `cfg.annotations` only through that assignment. Hits (ALLOC,
        # RELEASE, CALL) stay untouched -- they publish through their own
        # vocabulary channel and read `alias_source`, not `alias`.
        alias_of: dict[int, set[str]] = {}
        for copy in copies:
            target = copy.annotations.get("subject", "")
            source = copy.annotations["alias_source"]
            start = copy.start_byte
            alias_of.setdefault(id(copy), set()).add(source)
            for point in self._points:
                if (
                    point.start_byte <= start
                    or (point.owner_kind, point.owner_qualified_name, point.owner_signature)
                    != owner_key
                ):
                    continue
                subject = point.annotations.get("subject", "")
                if subject == target:
                    alias_of.setdefault(id(point), set()).add(source)
                elif subject == source:
                    alias_of.setdefault(id(point), set()).add(target)
        for point in self._points:
            names = alias_of.get(id(point))
            if names:
                point.annotations["alias"] = ";".join(sorted(names))

    def _under_conditional_expression(self, node: Any) -> bool:
        """Whether the expression sits inside `&&`/`||` or a lambda body.

        The statement-level conditionals are the walker's own dispatch, which
        counts them in `_conditional_depth`. These two never reach that
        dispatch: a short-circuit's right operand runs only when the left
        allowed it, and a lambda body runs when the closure is called -- which
        is not a property of this method's paths at all.
        """
        while node is not None:
            node = node.parent
            if node is None:
                break
            if node.type == "lambda_expression":
                return True
            if node.type == "binary_expression" and _operator_of(
                node, self.source
            ) in self.profile.short_circuit_operators:
                return True
        return False

    def _assign_ordinals(self) -> None:
        # Same rule as the event layer: source order, reproducible by reading
        # the file. CHECK/BRANCH/DEREFERENCE never come out of the vocabulary,
        # so these counters cannot collide with a hit's ordinals.
        ordered = sorted(self._points, key=lambda point: (point.start_byte, point.end_byte, point.seq))
        counters: dict[tuple[str, str, str, str], int] = {}
        for point in ordered:
            owner_key = (
                point.owner_kind,
                point.owner_qualified_name,
                point.owner_signature,
                point.event_type,
            )
            point.ordinal = counters.get(owner_key, 0)
            counters[owner_key] = point.ordinal + 1
            if point.annotations:
                self._annotations[point.key()] = point.annotations

    def _parsed_edges(self) -> list[ParsedEdge]:
        edges: list[ParsedEdge] = []
        seen: set[ParsedEdge] = set()
        for src, dst, flags in self._edges:
            edge = ParsedEdge(
                src=src.key(),
                dst=None if dst is _EXIT else dst.key(),
                flags=tuple(sorted(flags)),
            )
            if edge not in seen:
                seen.add(edge)
                edges.append(edge)
        # EXIT sorts after any real point for the same source; the order only
        # has to be total and stable, which (key, sentinel, flags) is.
        edges.sort(key=lambda edge: (edge.src, edge.dst if edge.dst is not None else _EXIT_SORT_KEY, edge.flags))
        return edges

    # ------------------------------------------------------------------
    # the walk

    def _linearize(
        self, node: Any, incoming: list[_Open]
    ) -> tuple["_Point | None", list[_Open]]:
        """Thread control through `node`; return (first point, open edges)."""
        if node is None:
            return None, list(incoming)
        span = self.owners.owner_of(node.start_byte, node.end_byte)
        if span is not None and span is not self._span:
            # Inside another symbol's span: that symbol's own walk covers it.
            # Lambda bodies carry no span and stay inlined here, which is the
            # event layer's attribution too.
            return None, list(incoming)
        handler = _CONTROL_HANDLERS.get(node.type)
        if handler is not None or node.type in _CONDITIONAL_STATEMENTS:
            # The whole construct -- heading and arms -- walks one level
            # deeper. The heading counts too, deliberately: a write inside an
            # `if (c)` condition runs on the branch paths, not on every path,
            # and the escape facts would rather miss one than invent one. The
            # extra `_CONDITIONAL_STATEMENTS` are constructs the walker
            # linearizes as plain statements but whose bodies still run only
            # sometimes (a catch clause), so the CFG's fall-through must not
            # read as "always".
            self._conditional_depth += 1
            try:
                if handler is not None:
                    return getattr(self, handler)(node, incoming)
                return self._plain_generic(node, incoming)
            finally:
                self._conditional_depth -= 1
        return self._plain_generic(node, incoming)

    def _plain_generic(self, node: Any, incoming: list[_Open]) -> tuple["_Point | None", list[_Open]]:
        if node.type in {"init_declarator", "assignment_expression"}:
            target_node = node.child_by_field_name("left")
            if target_node is None:
                target_node = node.child_by_field_name("declarator")
            target = self._root_identifier(target_node) if target_node is not None else ""
            saved = self._assign_target
            saved_exact = self._assign_exact
            # The inner assignment of `a = b = f()` wins while it is walked;
            # a failed extraction inherits the enclosing target, if any.
            self._assign_target = target or saved
            # `p = malloc(n)` binds the name `p`; `s->buf = malloc(n)` binds a
            # field of `s`, so the name recovered is the struct, not the
            # resource. The two are different identities and the queries say so.
            self._assign_exact = (
                _names_the_object(target_node) if target else saved_exact
            )
            saved_node = self._assign_node
            self._assign_node = target_node if target else saved_node
            try:
                return self._generic(node, incoming)
            finally:
                self._assign_target = saved
                self._assign_exact = saved_exact
                self._assign_node = saved_node
        return self._generic(node, incoming)

    def _generic(self, node: Any, incoming: list[_Open]) -> tuple["_Point | None", list[_Open]]:
        entry, current = self._children_linearize(node, incoming)
        # Post-order: this node's own points come after everything inside it.
        # One rule yields both "the condition's inner points precede the CHECK"
        # and "a call's arguments precede the CALL".
        return self._thread_own_points(node, entry, current)

    def _children_linearize(
        self, node: Any, incoming: list[_Open]
    ) -> tuple["_Point | None", list[_Open]]:
        entry: _Point | None = None
        current = list(incoming)
        for child in node.named_children:
            child_entry, current = self._linearize(child, current)
            if entry is None:
                entry = child_entry
        return entry, current

    def _connect(self, incoming: list[_Open], point: _Point) -> list[_Open]:
        for open_edge in incoming:
            if open_edge.src is not None:
                self._emit(open_edge.src, point, open_edge.flags)
        return [_Open(point, ())]

    def _emit(self, src: _Point | None, dst: Any, flags: tuple[str, ...]) -> None:
        if src is None or dst is None:
            # A malformed construct (an empty do-while body, say) must not
            # fabricate an edge to nowhere. `_EXIT`, not `None`, is the exit.
            return
        # The edgeless invariant, at the one function every edge goes through.
        # `_thread_own_points` already keeps the access layer off the chain, so
        # this cannot fire from source code -- only from an edit that routes an
        # access point somewhere else. It raises rather than dropping the edge
        # because a silent drop would leave a query reading a graph that is
        # quietly missing reachability, which is worse than a failed index.
        if src.event_type in _EDGELESS_EVENT_TYPES:
            raise RuntimeError(f"{src.event_type} point must not be an edge source")
        if dst is not _EXIT and dst.event_type in _EDGELESS_EVENT_TYPES:
            raise RuntimeError(f"{dst.event_type} point must not be an edge target")
        self._edges.append((src, dst, tuple(sorted(flags))))

    # ------------------------------------------------------------------
    # point construction

    def _own_points(self, node: Any) -> tuple[list[_Point], list[_Point]]:
        """The points this node itself contributes, in two sets.

        `threaded` is the control-flow chain: the vocabulary hits and the
        structural CHECK/BRANCH/DEREFERENCE points, exactly as before stage 5A.
        `event_only` is the access layer: READ/WRITE rows that are published
        and never connected. They are returned together rather than appended
        to `self._points` from two places because the *caller* is what decides
        whether a point is on the chain, and every caller must make the same
        decision -- see `_thread_own_points`.
        """
        threaded = [
            self._point_from_hit(hit, node)
            for hit in self._hits_by_span.get((node.start_byte, node.end_byte), ())
        ]
        structural = self._structural_point(node)
        if structural is not None:
            threaded.append(structural)
        return threaded, self._access_points(node)

    def _thread_own_points(
        self, node: Any, entry: "_Point | None", current: list[_Open]
    ) -> tuple["_Point | None", list[_Open]]:
        """Connect this node's own points onto the chain, and nothing else.

        The one place `_own_points` is consumed, so the access layer's
        exclusion lives in one place. It has to cover the `entry` assignment
        and not just `_connect`: a point returned as `entry` becomes a jump
        target for the enclosing construct, so a READ returned as the entry of
        a `while` condition would collect the loop's back edge, the `continue`
        edges and the `do` statement's true arm -- real edges into an edgeless
        event type, which is the one thing this stage must not do.
        """
        threaded, _event_only = self._own_points(node)
        for point in threaded:
            current = self._connect(current, point)
            if entry is None:
                entry = point
        return entry, current

    def _point_from_hit(self, hit: Any, node: Any) -> _Point:
        event = hit.event
        # No operation carries the variable it acts on in matched_name (`free`
        # is a keyword, not a subject), so the subject is recovered here and
        # rides to `graph.py` in `ParsedCfg.annotations`. Stage 3 annotated
        # ALLOC/RELEASE/CALL; stage 4 widened the set, because a lock-order or
        # race query without lock subjects and return subjects has nothing to
        # pair on.
        annotations = self._annotations_for_hit(event, node)
        if annotations:
            key = PointKey(
                event.owner_kind,
                event.owner_qualified_name,
                event.owner_signature,
                event.event_type,
                event.ordinal,
            )
            self._hit_positions[key] = (hit.start_byte, event.start_line)
            # Whole package, first writer wins. One span can carry two events
            # (`free(p)` is a CALL and a RELEASE), and merging key by key let the
            # second one fill in the gaps of the first -- a record whose subject
            # came from one event and whose declaration identity came from
            # another is a statement about no operation that ever ran. With the
            # two writers agreeing (they read the same node through the same
            # rules) the atomic rule costs nothing and cannot drift.
            self._annotations.setdefault(key, annotations)
        self._has_points = True
        return _Point(
            event_type=event.event_type,
            owner_kind=event.owner_kind,
            owner_qualified_name=event.owner_qualified_name,
            owner_signature=event.owner_signature,
            start_byte=hit.start_byte,
            end_byte=hit.end_byte,
            start_line=event.start_line,
            end_line=event.end_line,
            matched_name=event.matched_name,
            ordinal=event.ordinal,
            seq=0,
        )

    def _annotations_for_hit(self, event: Any, node: Any) -> dict[str, str]:
        """The subject-bearing annotations for one vocabulary hit.

        `raii` is recorded even when the subject is not: a RAII acquisition
        with an unidentifiable target is still known to release in its
        destructor, and that is exactly what keeps the incomplete-cleanup
        query (4.6) from reporting every `lock_guard` in the repository.
        """
        event_type = event.event_type
        if event_type not in _ANNOTATED_EVENT_TYPES:
            return {}
        annotations: dict[str, str] = {}
        if event_type in {"LOCK", "UNLOCK"} and node.type == "declaration":
            annotations["raii"] = "true"
        subject, subject_from, exact = self._subject_for_hit(event_type, node)
        if subject:
            annotations["subject"] = subject
            annotations["subject_from"] = subject_from
            # Whether the name is the whole designation of the object or only
            # the root of a path that reaches it. `g_index_lock` names a lock;
            # `index` in `&index->slot_locks[i]` names the struct that holds
            # one, and two different elements of the array would otherwise look
            # like one lock acquired twice.
            annotations["subject_exact"] = "true" if exact else "false"
            annotations.update(self._subject_facts(self._subject_node(event_type, node), subject))
        elif event_type == "ALLOC" and node.type == "new_expression":
            # An anonymous heap object: it has no name, so there is no name to
            # resolve, but it does have an identity -- the allocation site. That
            # is what "this object" means for a value never bound to a variable,
            # and it is the fact that turns `new X(k)` from *identity unknown*
            # into *ownership unknown*: the two `new`s in a loop are the same
            # site and a different object from the two `new`s in another method.
            # Deterministic and line-free: the owner triple, the event type and
            # the ordinal within it -- the same inputs the event id hashes.
            annotations["subject_site"] = _site_token(event)
        if event_type == "ALLOC":
            # The two 5C-2 hand-off facts (2.4 and 2.12). An allocation that is
            # returned or passed as an argument *in its own acquiring
            # expression* leaves this frame at birth, and the leak walk cannot
            # see either: the RETURN point of `return new X(k)` has no subject
            # to join on, and the call that receives the allocation names its
            # own parameters. The annotation is the join.
            passed_to, returned = self._alloc_handoff(node)
            if passed_to:
                annotations["passed_to"] = passed_to
            if returned:
                annotations["returned"] = "true"
        arguments = _call_arguments(node, self.source)
        if arguments:
            annotations["arguments"] = arguments
        thread_function = self._thread_function(event_type, event.matched_name, node)
        if thread_function:
            annotations["thread_function"] = thread_function
        return annotations

    def _subject_node(self, event_type: str, node: Any) -> Any:
        """The expression the subject name was recovered from.

        `_subject_for_hit` returns the name and not the node, because the name
        is what the query joins on; the declaration index needs the node as
        well, to know *where* the use is. The rules are the same ones
        `_subject_for_hit` applies, in the same order, and they must stay that
        way: a name read from one expression and a declaration looked up at
        another would be two different objects.
        """
        if event_type == "RELEASE":
            return self._release_argument_node(node)
        if event_type in {"LOCK", "UNLOCK"} and node.type == "declaration":
            return self._declaration_argument_node(node)
        if event_type == "RETURN":
            return self._return_value_node(node)
        if event_type == "THREAD_SPAWN" and node.type == "declaration":
            return self._declaration_target_node(node)
        if self._assign_target:
            return self._assign_node
        if event_type == "ALLOC" and node.type == "new_expression":
            return None
        argument = _first_call_argument(node)
        if argument is not None:
            return argument
        if node.type == "call_expression":
            return self._call_receiver_node(node)
        return None

    def _subject_facts(self, node: Any, name: str) -> dict[str, str]:
        """The declaration identity behind a subject name (stage 4.5).

        A name is not an object. `int n` in one method and `int n` in another
        are two variables, `index->slot_locks[i]` and `index->global_lock` are
        two locks, and `g_total` is one variable however many files mention it.
        The stage 4 queries joined on the name, which is what made the race
        query return 557,315 candidates for redis -- every one of them a pair
        of same-named locals in different methods.

        What is recorded is deliberately a *token*, not a resolved object:
        storage class, declared type name, and the member path left over after
        the root identifier. The declaration index answers where the name comes
        from in this file, and answers nothing -- visibly -- when the file does
        not say. An empty `subject_decl` is that answer: the name may be a
        macro, an enum constant, or a global declared in a header this parser
        never sees, and the consumer falls back to the name rather than
        dropping the candidate.
        """
        if not name:
            return {}
        root, member, this_root, exact = self._member_chain(node)
        exact_value = "true" if exact else "false"
        if this_root:
            # `this->m_` is the whole designation: the object is the field, and
            # there is no path left over. The field declaration is looked up for
            # its type, but the identity is the member either way -- a local
            # that happens to share the name cannot be what `this->` means.
            declaration = self._resolve(node, member)
            if declaration is None:
                # Same 2.9 fallback as the bare-root path: `this->` in an
                # out-of-line body still names the class's field, and the
                # class comes from the method's qualified name.
                declaration = self._resolve_member(node, member)
                if declaration is not None and declaration.storage == STORAGE_FIELD:
                    return self._facts_for(declaration, "", exact_value)
            if declaration is None or declaration.storage != STORAGE_FIELD:
                class_name = self._enclosing_class(node)
                token = f"field:{class_name}:{member}" if class_name else ""
                if not token:
                    return {"subject_storage": STORAGE_UNKNOWN, "subject_member": "", "subject_exact": exact_value}
                return {
                    "subject_decl": token, "subject_storage": STORAGE_FIELD,
                    "subject_type": "", "subject_member": "", "subject_exact": exact_value,
                }
            return self._facts_for(declaration, "", exact_value)
        declaration = self._resolve(node, root)
        if declaration is None:
            # Out-of-line member function definitions (`void S::touch()`) sit
            # outside every scope `S` opens, so the plain resolve cannot see
            # the class's fields from their bodies. The method's qualified
            # name carries the class (`S.touch`), and the index can still
            # answer from the class frame -- the same token an in-class access
            # records, so both spellings join instead of splitting into
            # `field:S:m_` and `name:m_`. This is 5C-2's item 2.9, and it is
            # also why the unknown bucket widens: without it every bare member
            # of an out-of-line body lands here as `unknown`.
            declaration = self._resolve_member(node, root)
            if declaration is not None:
                return self._facts_for(declaration, member, exact_value)
            # No declaration in this file. `unknown` is a value, not a failure:
            # a cross-file global is the most important shared state a race
            # query can see, and a name with no declaration is exactly what it
            # looks like.
            return {
                "subject_storage": STORAGE_UNKNOWN,
                "subject_member": member, "subject_exact": exact_value,
            }
        if declaration.storage == STORAGE_FIELD and not member:
            # A bare field name inside a method (`m_.lock()`): the object is
            # the field, so the field *is* the designation.
            return self._facts_for(declaration, "", exact_value)
        return self._facts_for(declaration, member, exact_value)

    @staticmethod
    def _facts_for(declaration: Any, member: str, exact_value: str) -> dict[str, str]:
        if declaration.storage == STORAGE_FIELD:
            token = f"field:{declaration.scope_token}:{declaration.name}"
        elif declaration.scope_token:
            token = f"{declaration.storage}:{declaration.scope_token}:{declaration.name}"
        else:
            token = f"{declaration.storage}:{declaration.name}"
        facts = {
            "subject_decl": token,
            "subject_storage": declaration.storage,
            "subject_type": declaration.type_token,
            "subject_member": member,
            "subject_exact": exact_value,
        }
        # 5C-2's 2.6: when the declaration initializes from a call, the first
        # argument is the pure syntax fact a lock query joins a guard to its
        # mutex with. Recorded only when it exists, so every other subject's
        # facts stay byte-identical.
        init_arg0 = getattr(declaration, "init_arg0", "")
        if init_arg0:
            facts["subject_init_arg0"] = init_arg0
        return facts

    def _resolve(self, node: Any, name: str) -> Any:
        if self.declarations is None or not name:
            return None
        return self.declarations.resolve(node.start_byte, name)

    def _resolve_member(self, node: Any, name: str) -> Any:
        """The field `name` of the class the current method belongs to.

        Only an out-of-line definition needs this: inside the class body the
        plain resolve already walks the class frame. The method's qualified
        name spells the owner path with dots (`S.touch`), so the class is
        everything before the last dot -- a free function has none, and the
        fallback answers nothing for it.
        """
        if self.declarations is None or not name or self._span is None:
            return None
        qualified = self._span.qualified_name or ""
        if "." not in qualified:
            return None
        class_name = qualified.rsplit(".", 1)[0]
        return self.declarations.resolve_member(node.start_byte, class_name, name)

    def _enclosing_class(self, node: Any) -> str:
        if self.declarations is None:
            return ""
        return self.declarations.enclosing_class(node.start_byte)

    def _member_chain(self, node: Any) -> tuple[str, str, bool, bool]:
        """`(root identifier, member path, root is this, exact)` for a subject.

        `index->slot_locks[i]` is the root `index` with the path
        `slot_locks[i]` left over; `this->m_` is the field itself, and `m_`
        is the whole designation. The path is the member names and the
        subscript texts the descent passed, joined with whitespace removed, so
        `slot_locks[0]` and `slot_locks[1]` stay two identities -- collapsing
        them to the bare member name would report an array of locks as one
        lock acquired twice.

        The subscript text is spelled by the grammar and the two grammars
        differ: tree-sitter-cpp's `indices` is a node that carries the
        brackets (`[i]`), tree-sitter-c's `index` is the bare expression
        (`i`). So the same expression gives `slot_locks[i]` in C++ and
        `slot_locksi` in C. That is a spelling difference and not a split:
        an identity is only ever compared against another identity from the
        same repository, and the declaring file's language fixes the spelling
        on both sides of the comparison.

        Exactness is asked of the *whole* expression and not of the root the
        descent stopped on: `&index->slot_locks[i]` is inexact because it left a
        path behind, and the root `index` being a plain identifier says nothing
        about that. Asking the residual would call every member access exact.
        """
        whole = node
        pieces: list[str] = []
        while node is not None:
            kind = node.type
            if kind == "field_expression":
                pieces.insert(0, self._text(node.child_by_field_name("field")))
                node = node.child_by_field_name("argument")
                continue
            if kind == "subscript_expression":
                index_node = node.child_by_field_name("indices") or node.child_by_field_name("index")
                pieces.append(_path_text(self._text(index_node)))
                node = node.child_by_field_name("argument")
                continue
            if kind in {"pointer_expression", "parenthesized_expression"}:
                node = node.child_by_field_name("argument") or (
                    node.named_children[0] if node.named_children else None
                )
                continue
            if kind == "this":
                return "", "".join(pieces), True, True
            break
        root = self._root_identifier(node)
        return root, "".join(pieces), False, _names_the_object(whole)

    def _subject_for_hit(self, event_type: str, node: Any) -> tuple[str, str, bool]:
        """The name an operation acts on, how it was recovered, and whether it
        is the whole designation of the object or only the root of a path.

        The rules are ordered by how much the source actually says: a declared
        target is the thing being bound; a first argument is the thing being
        passed; a receiver is the object a member call is made on, which for
        `g_pool_lock.lock()` is the mutex but for `pool->lock()` is a struct
        that may merely contain one. Downstream keeps that difference, and the
        third element keeps a further one: `&g_index_lock` names the lock,
        `&index->slot_locks[i]` names the struct that holds an array of them.

        An unbound `new` is the one allocation with no name at all, and it is
        excluded from the argument rule rather than served by it: in
        `sink.emplace_back(new X(k))` the first argument `k` is a different
        object from the allocation, and the same for
        `return new X(params);`. Reporting one of them as the subject would
        attribute the candidate to something it is not and call the identity
        resolved; an anonymous heap object has no name, so the candidate is
        `unresolved` and the transfer to whatever consumes it is the missing
        fact (recorded in the stage 4 execution record).
        """
        if event_type == "RELEASE":
            value = self._release_argument_node(node)
            return self._root_identifier(value), SUBJECT_FROM_ARGUMENT, _names_the_object(value)
        if event_type in {"LOCK", "UNLOCK"} and node.type == "declaration":
            value = self._declaration_argument_node(node)
            return self._root_identifier(value), SUBJECT_FROM_RAII, _names_the_object(value)
        if event_type == "RETURN":
            value = self._return_value_node(node)
            return self._root_identifier(value), SUBJECT_FROM_RETURN, _names_the_object(value)
        if event_type == "THREAD_SPAWN" and node.type == "declaration":
            # A declaration binds a name to the handle, so the target is always
            # the whole designation.
            return self._declaration_target(node), SUBJECT_FROM_ASSIGNMENT, True
        if self._assign_target:
            return self._assign_target, SUBJECT_FROM_ASSIGNMENT, self._assign_exact
        if event_type == "ALLOC" and node.type == "new_expression":
            return "", "", False
        argument = _first_call_argument(node)
        if argument is not None:
            return self._root_identifier(argument), SUBJECT_FROM_ARGUMENT, _names_the_object(argument)
        if node.type == "call_expression":
            name, _, exact = self._receiver_subject(node)
            return name, SUBJECT_FROM_RECEIVER, exact
        return "", "", False

    def _receiver_subject(self, node: Any) -> tuple[str, Any, bool]:
        """The object a member call is made on, as a name and as an expression.

        `g_pool_lock.lock()` names the mutex, `p->m.f()` names `p` with `m` left
        as the member path -- the root is what a declaration index can resolve.
        `this->m_.lock()` names the field `m_`, and it is the one case the root
        rule cannot reach: `this` is not a name but the method's own object, so
        the literal token would join every field of every class into a single
        identity. The field name is the designation instead, and the declaration
        index resolves it to the field it is.
        """
        receiver = self._call_receiver_node(node)
        if receiver is None:
            return "", None, False
        root, member, this_root, _ = self._member_chain(receiver)
        if this_root:
            return member, receiver, True
        return root, receiver, _names_the_object(receiver)

    def _return_value_node(self, node: Any) -> Any:
        # `return buffer;` hands the pointer to the caller; `return -1;` and
        # `return 0;` name nothing. Only the first is a transfer, and the
        # difference is the whole reason the return subject is recorded.
        if node.type != "return_statement" or not node.named_children:
            return None
        return node.named_children[0]

    def _call_receiver_node(self, node: Any) -> Any:
        callee = node.child_by_field_name("function")
        if callee is None or callee.type != "field_expression":
            return None
        return callee.child_by_field_name("argument")

    def _declaration_target(self, node: Any) -> str:
        """The name a declaration binds: `std::thread t{fn};` names `t`."""
        return self._root_identifier(self._declaration_target_node(node))

    def _declaration_target_node(self, node: Any) -> Any:
        """The declarator a declaration binds, or None when identity is unclear.

        Both spellings reach here. `t{fn}` is an `init_declarator`; `t(fn)` is a
        `function_declarator` left by the most-vexing-parse, and the name it
        binds sits in the same `declarator` field either way -- the difference
        is entirely in what follows it. The node comes back rather than the name
        because the declaration index resolves a name at a position, and the
        position is the declarator's, not the whole statement's.
        """
        declarator = self._single_declarator(node)
        if declarator is None or declarator.type not in {"init_declarator", "function_declarator"}:
            return None
        return declarator.child_by_field_name("declarator")

    def _declaration_argument_node(self, node: Any) -> Any:
        """The expression a RAII declaration locks or binds, not its variable.

        `lock_guard<std::mutex> guard{g_pool_lock};` locks `g_pool_lock`; the
        name `guard` is a local handle whose identity is worthless to a
        lock-order query.

        Two shapes reach here, because tree-sitter reads the same statement two
        ways depending on what is inside the parentheses:

        - `guard{a}`, `guard(a)`, `guard(p->m)`, `guard(&a)` and `guard(f())`
          parse as an `init_declarator` whose value is an `initializer_list` or
          an `argument_list`, and the argument is that list's first element.
        - `guard(a)` where `a` is a bare name parses as a **function
          declaration** (the most-vexing-parse): a `function_declarator` whose
          parameter list is really a constructor argument list. The argument is
          the parameter's `type` node, because there is nothing else -- the
          parser has already committed to reading `a` as a type name.

        What is deliberately *not* read: a parameter that carries its own
        declarator is a function type, which means the argument was a call
        (`guard(get_mutex())`, `guard(std::move(m))`). The object being locked
        is then whatever that call returns, which this layer cannot name, so the
        subject stays empty and the candidate is capped at `ambiguous` rather
        than attributed to the callee's name.
        """
        declarator = self._single_declarator(node)
        if declarator is None:
            return None
        if declarator.type == "function_declarator":
            return self._mmp_argument_node(declarator)
        if declarator.type != "init_declarator":
            return None
        value = declarator.child_by_field_name("value")
        if value is None:
            return None
        if value.type in {"initializer_list", "argument_list"}:
            value = value.named_children[0] if value.named_children else None
        return value

    def _mmp_argument_node(self, declarator: Any) -> Any:
        """The argument of a most-vexing-parse declaration, or None.

        The first parameter that is not a function type is the constructor's
        first argument: `guard(m)` names `m`, and `lk(m, std::defer_lock)` --
        the canonical way to spell a deferred `unique_lock` -- names `m` as
        well, with `std::defer_lock` reading as a second parameter declaration.
        A parameter that has a declarator of its own is a call, not an object,
        and stops the read.
        """
        parameters = declarator.child_by_field_name("parameters")
        if parameters is None:
            return None
        for parameter in parameters.named_children:
            if parameter.type != "parameter_declaration":
                return None
            if parameter.child_by_field_name("declarator") is not None:
                return None
            return parameter.child_by_field_name("type")
        return None

    def _declaration_argument(self, node: Any) -> str:
        return self._root_identifier(self._declaration_argument_node(node))

    def _single_declarator(self, node: Any) -> Any:
        """The declaration's one declarator, or None when identity is unclear.

        `lock_guard<std::mutex> a{m}, b{n};` acquires twice through two
        handles; reporting one argument for both would be a guess about which
        one a lock point refers to.
        """
        declarators = list(node.children_by_field_name("declarator"))
        if len(declarators) != 1:
            return None
        return declarators[0]

    def _thread_function(self, event_type: str, matched_name: str, node: Any) -> str:
        """The function a spawn starts, when the call shape says where it is."""
        if event_type != "THREAD_SPAWN":
            return ""
        if node.type == "declaration":
            return self._declaration_argument(node)
        if matched_name in _THREAD_QUALIFIED_SPAWNS:
            index = 0
        else:
            index = _THREAD_ENTRY_ARGUMENT.get(matched_name)
        if index is None:
            return ""
        arguments = node.child_by_field_name("arguments")
        named = list(arguments.named_children) if arguments is not None else []
        if index >= len(named):
            return ""
        return self._root_identifier(named[index])

    def _structural_point(self, node: Any) -> _Point | None:
        kind = node.type
        if kind not in self.profile.deref_node_types:
            return None
        if kind == "pointer_expression":
            if _operator_of(node, self.source) not in self.profile.deref_unary_operators:
                return None
        elif kind == "field_expression":
            if _operator_of(node, self.source) not in self.profile.deref_member_operators:
                return None
        return self._deref_point(node)

    def _deref_point(self, node: Any) -> _Point:
        operand = self._deref_operand(node)
        subject = self._root_identifier(operand)
        text = self._text(node)
        annotations = {"expression": text}
        if subject:
            annotations["subject"] = subject
            # The structural points carry subjects too, and they are not a
            # minority: nearly half the race query's subjects arrive through
            # `p[i]` and `if (!p)` rather than through a vocabulary hit. Leaving
            # the identity keys off here would send that half back to the
            # name-based join, and the stage's reduction would be half of what
            # the declaration index actually buys.
            annotations.update(self._subject_facts(operand, subject))
        return self._new_point("DEREFERENCE", node, annotations=annotations, matched_name=text)

    def _deref_operand(self, node: Any) -> Any:
        # The subject of `p[i]` is `p`, the pointer being indexed -- the index
        # expression is not what dereferences. C++ names the index field
        # `indices`, C names it `index`; only the subject matters here.
        return node.child_by_field_name("argument")

    # ------------------------------------------------------------------
    # the access layer (stage 5A)

    def _access_points(self, node: Any) -> list[_Point]:
        """The READ/WRITE accesses this node contributes (stage 5A).

        An access has no keyword. Nothing in `x = y` says which side is the
        write -- only position does -- so this cannot be a vocabulary matcher
        axis, and it lives here where the walker already computes the
        assignment target.

        Three rules; the third is what keeps the count honest:

        * **Write side.** This node is an assignment, a compound assignment, an
          update or an initializer: the object written is the outermost access
          expression inside its target field. `p->buf[i] = 5` writes one
          thing, not three.
        * **Read side.** Each named child that is itself an access expression
          is one read. The rule does not descend past it: `p->buf[i]`'s
          children are the same access spelled more finely, and the walker will
          reach them on its own (their DEREFERENCE points must not change).
        * **Skips.** The write target is not also a read; a declarator names an
          object being declared, not one being read; a callee is what is
          invoked, not a variable.

        Emission is filtered by storage class, and the filter is the identity
        rather than a heuristic -- see `ACCESS_STORAGES`. The subject comes
        from `_subject_facts` on the access *expression*, the same ruler every
        other event's identity is measured with, which is also what keeps the
        member path: `s->buf = malloc(n)` is `s#buf`, not `s`.
        """
        points: list[_Point] = []
        target = None
        target_field = WRITE_TARGET_FIELDS.get(node.type)
        if target_field is not None:
            target = node.child_by_field_name(target_field)
            written = _outermost_access(target, self.profile.access_node_types)
            if written is not None:
                point = self._access_point(written, "WRITE", origin=node)
                if point is not None:
                    points.append(point)
        skip_fields = (DECLARED_FIELDS.get(node.type), ACCESS_SKIP_FIELDS.get(node.type))
        skip = {
            _span(node.child_by_field_name(field))
            for field in skip_fields if field is not None
        }
        skip.discard(None)
        # This node is itself an access expression: its parent emits for it.
        # Emitting here too would count `p->buf[i]` three times over.
        if node.type in self.profile.access_node_types:
            return points
        for child in node.named_children:
            if child.type not in self.profile.access_node_types:
                continue
            if _span(child) in skip or _span(child) == _span(target):
                continue
            point = self._access_point(child, "READ")
            if point is not None:
                points.append(point)
        return points

    def _access_point(self, expr: Any, event_type: str, origin: Any = None) -> _Point | None:
        root, member, this_root, _exact = self._member_chain(expr)
        # `this->m_` names the field directly. Taking only `_root_identifier`
        # here would stop at `this` -- a keyword, not an identifier -- and the
        # whole access would be dropped, so the chain is the gate and the
        # subject is the member when the chain rooted on `this`.
        subject = member if this_root else root
        if not subject:
            return None
        facts = self._subject_facts(expr, subject)
        storage = facts.get("subject_storage")
        if storage not in ACCESS_STORAGES:
            return None
        if storage == STORAGE_PARAMETER and event_type != "WRITE":
            # Stage 5B publishes the write side of a parameter only. The
            # value arrives from the caller, so an in-frame read has no
            # consumer of its own; the query-time parameter join composes
            # the cross-method pairs from these writes in the caller's
            # terms. Registered approximation: a callee that only *reads*
            # through its parameter publishes nothing (see the plan's
            # registration list).
            return None
        text = self._text(expr)
        annotations = {"expression": text}
        annotations["subject"] = subject
        annotations.update(facts)
        if event_type == "WRITE" and origin is not None:
            # The escape half of 5C-2's 2.11: what the write *read*, and
            # whether the statement carrying it runs on every path of the
            # method. A later frame-local allocation whose pointer this write
            # copies into shared storage is then one post-pass away from an
            # escape fact on the allocation itself.
            rhs_field = WRITE_SOURCE_FIELDS.get(origin.type)
            rhs = origin.child_by_field_name(rhs_field) if rhs_field else None
            if rhs is not None:
                source_root = self._root_identifier(rhs)
                if source_root and source_root != subject:
                    annotations["write_source"] = source_root
                    # 5B's alias facts ride the write too, but only for the
                    # bare shapes: `q = p` and `q = &p` make the names
                    # possible aliases of one another, while `q = p->f` reads
                    # a field and aliases nothing. The LHS must be the bare
                    # root as well -- `*p = q` writes *through* `p`, it does
                    # not alias anything.
                    rhs_text = self._text(rhs)
                    if (
                        text == subject
                        and (rhs_text == source_root or rhs_text == f"&{source_root}")
                    ):
                        annotations["alias_source"] = source_root
                # The null half of the DFG's NULL_SOURCE class: a write whose
                # right-hand side is the null literal defines the subject as
                # null. Number literals (`p = 0`) stay unrecognized -- the
                # registered stage-4 approximation, not re-litigated here.
                if rhs.type == "null" or self._text(rhs) in ("NULL", "nullptr"):
                    annotations["null_source"] = "true"
            annotations["unconditional"] = (
                "true" if self._conditional_depth == 0 and not self._under_conditional_expression(expr)
                else "false"
            )
        return self._new_point(event_type, expr, annotations=annotations, matched_name=text)

    def _condition_point(self, condition: Any) -> _Point:
        event_type, subject, polarity, subject_node = self._classify(condition)
        text = self._text(condition)
        annotations: dict[str, str] = {"expression": text}
        flags: tuple[str, ...] = ()
        if (
            condition.type == "binary_expression"
            and _operator_of(condition, self.source) in self.profile.short_circuit_operators
        ):
            flags = (SHORT_CIRCUIT,)
        if subject:
            annotations["subject"] = subject
            annotations.update(self._subject_facts(subject_node, subject))
        if polarity:
            annotations["polarity"] = polarity
        return self._new_point(event_type, condition, flags=flags, annotations=annotations, matched_name=text)

    def _classify(self, condition: Any) -> tuple[str, str, str, Any]:
        """CHECK for validity tests, BRANCH for everything else (v1 rule).

        A validity test is `!p`, or `p == NULL` / `p != NULL` with a `null`
        node on one side. Bare identifiers, comparisons against 0, calls and
        conjunctions are all plain branches -- picking more would need type
        information this stage deliberately does not use.

        The expression the subject was read from comes back with the name: the
        declaration index resolves a name *at a position*, and `!p->buf` and
        `!q->buf` share a name without sharing an object.
        """
        if condition.type == "unary_expression":
            if _operator_of(condition, self.source) == "!":
                argument = condition.child_by_field_name("argument")
                return "CHECK", self._root_identifier(argument), "true_when_null", argument
        if condition.type == "binary_expression":
            operator = _operator_of(condition, self.source)
            if operator in {"==", "!="}:
                left = condition.child_by_field_name("left")
                right = condition.child_by_field_name("right")
                null_side, other = (left, right) if self._is_null(left) else (right, left) if self._is_null(right) else (None, None)
                if null_side is not None:
                    polarity = "true_when_null" if operator == "==" else "true_when_non_null"
                    return "CHECK", self._root_identifier(other), polarity, other
        return "BRANCH", "", "", None

    def _is_null(self, node: Any) -> bool:
        return node is not None and node.type in self.profile.null_node_types

    def _new_point(
        self,
        event_type: str,
        node: Any,
        flags: tuple[str, ...] = (),
        annotations: dict[str, str] | None = None,
        matched_name: str = "",
    ) -> _Point:
        point = _Point(
            event_type=event_type,
            owner_kind=self._span.kind,
            owner_qualified_name=self._span.qualified_name,
            owner_signature=self._span.signature,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            start_line=self.line_of(node.start_byte, False),
            end_line=self.line_of(node.end_byte, True),
            matched_name=matched_name,
            flags=flags,
            annotations=dict(annotations or {}),
            seq=self._seq,
        )
        self._seq += 1
        self._points.append(point)
        self._has_points = True
        return point

    def _text(self, node: Any) -> str:
        return _expression_text(self.source, node)

    # ------------------------------------------------------------------
    # helpers over the current node

    def _root_identifier(self, node: Any) -> str:
        """The leftmost identifier of an expression: `p->buf[i]` names `p`."""
        return _root_identifier(self.source, node)

    def _release_argument_node(self, node: Any) -> Any:
        """The expression a release acts on: `free(p)` names `p`, and
        `delete p->buf` names the field's root, which `_names_the_object`
        reports as the path it is."""
        if node.type == "delete_expression":
            value = node.child_by_field_name("value")
            if value is None and node.named_children:
                value = node.named_children[-1]
            return value
        return _first_call_argument(node)

    def _alloc_handoff(self, node: Any) -> tuple[str, bool]:
        """`(callee, returned)` for an allocation's own acquiring expression.

        The walk goes from the allocation's node to the *nearest* ancestor
        that says where the value goes: a `return_statement` means the caller
        receives it, and a call's `argument_list` means the callee is handed
        it. Nearest wins because the two are not the same hand-over --
        `return push(new X(k))` records `push`, since what happens past `push`
        is `push`'s contract, and this method's own return says nothing about
        the object itself. Anything else (`tmp = strdup(x)` bound to a local,
        an allocation in a loop heading) hands over nobody, and answers
        without a fact.
        """
        node = node.parent
        while node is not None:
            if node.type == "return_statement":
                return "", True
            if node.type == "argument_list":
                call = node.parent
                if call is not None and call.type == "call_expression":
                    callee = call.child_by_field_name("function")
                    if callee is not None:
                        return _expression_text(self.source, callee), False
                return "", False
            node = node.parent
        return "", False

    # ------------------------------------------------------------------
    # control handlers

    def _if_statement(self, node: Any, incoming: list[_Open]) -> tuple[_Point | None, list[_Open]]:
        condition = _unwrap_condition(node.child_by_field_name("condition"))
        if condition is None:
            return self._plain_generic(node, incoming)
        entry, cond_opens = self._linearize(condition, incoming)
        check = self._condition_point(condition)
        self._connect(cond_opens, check)
        _, true_opens = self._linearize(
            node.child_by_field_name("consequence"), [_Open(check, (BRANCH_TRUE,))]
        )
        alternative = node.child_by_field_name("alternative")
        if alternative is not None:
            # An else-if chain is a nested if_statement inside the else_clause;
            # recursing lands in this handler again. No special case needed.
            _, false_opens = self._linearize(alternative, [_Open(check, (BRANCH_FALSE,))])
        else:
            false_opens = [_Open(check, (BRANCH_FALSE,))]
        return entry or check, [*true_opens, *false_opens]

    def _while_statement(self, node: Any, incoming: list[_Open]) -> tuple[_Point | None, list[_Open]]:
        condition = _unwrap_condition(node.child_by_field_name("condition"))
        if condition is None:
            return self._plain_generic(node, incoming)
        entry, cond_opens = self._linearize(condition, incoming)
        check = self._condition_point(condition)
        self._connect(cond_opens, check)
        loop_entry = entry or check
        context = _LoopContext()
        self._breaks.append(context)
        try:
            _, body_opens = self._linearize(
                node.child_by_field_name("body"), [_Open(check, (BRANCH_TRUE,))]
            )
        finally:
            self._breaks.pop()
        # The back edge to the re-tested condition carries only `loop_back`:
        # the `branch=true` fact already lives on the check-to-body edge.
        self._flush_loop(context, loop_entry, body_opens, loop_entry)
        return loop_entry, [*context.breaks, _Open(check, (BRANCH_FALSE,))]

    def _flush_loop(
        self,
        context: _LoopContext,
        continue_target: _Point | None,
        back_opens: list[_Open],
        back_target: _Point | None,
    ) -> None:
        for open_edge in context.continues:
            self._emit(open_edge.src, continue_target, (*open_edge.flags, LOOP_BACK))
        for open_edge in back_opens:
            self._emit(open_edge.src, back_target, (*open_edge.flags, LOOP_BACK))

    def _do_statement(self, node: Any, incoming: list[_Open]) -> tuple[_Point | None, list[_Open]]:
        condition = _unwrap_condition(node.child_by_field_name("condition"))
        if condition is None:
            return self._plain_generic(node, incoming)
        context = _LoopContext()
        self._breaks.append(context)
        try:
            body_entry, body_opens = self._linearize(node.child_by_field_name("body"), incoming)
        finally:
            self._breaks.pop()
        cond_entry, cond_opens = self._linearize(condition, body_opens)
        check = self._condition_point(condition)
        self._connect(cond_opens, check)
        # The true arm IS the back edge here -- there is no separate
        # check-to-body edge, so both facts share the one edge. An empty body
        # loops straight back into the re-tested condition.
        self._emit(check, body_entry or cond_entry or check, (BRANCH_TRUE, LOOP_BACK))
        for open_edge in context.continues:
            self._emit(open_edge.src, cond_entry or check, (LOOP_BACK,))
        return body_entry, [*context.breaks, _Open(check, (BRANCH_FALSE,))]

    def _for_statement(self, node: Any, incoming: list[_Open]) -> tuple[_Point | None, list[_Open]]:
        init = node.child_by_field_name("initializer")
        if init is not None:
            _, incoming = self._linearize(init, incoming)
        condition = _unwrap_condition(node.child_by_field_name("condition"))
        update = node.child_by_field_name("update")
        check: _Point | None = None
        cond_entry: _Point | None = None
        body_incoming = incoming
        if condition is not None:
            cond_entry, cond_opens = self._linearize(condition, incoming)
            check = self._condition_point(condition)
            self._connect(cond_opens, check)
            body_incoming = [_Open(check, (BRANCH_TRUE,))]
        context = _LoopContext()
        self._breaks.append(context)
        try:
            body_entry, body_opens = self._linearize(node.child_by_field_name("body"), body_incoming)
        finally:
            self._breaks.pop()
        # C semantics: `continue` runs the update before re-testing.
        continue_target = cond_entry or check or body_entry
        back_opens = body_opens
        if update is not None:
            upd_entry, upd_opens = self._linearize(update, body_opens)
            continue_target = upd_entry or continue_target
            back_opens = upd_opens
        back_target = cond_entry or check or body_entry
        self._flush_loop(context, continue_target, back_opens, back_target)
        if check is None:
            # `for (;;)` has no condition to fail: only `break` leaves.
            return body_entry, list(context.breaks)
        return cond_entry or body_entry, [*context.breaks, _Open(check, (BRANCH_FALSE,))]

    def _for_range_loop(self, node: Any, incoming: list[_Open]) -> tuple[_Point | None, list[_Open]]:
        right = node.child_by_field_name("right")
        if right is None:
            return self._plain_generic(node, incoming)
        entry, cond_opens = self._linearize(right, incoming)
        check = self._condition_point(right)
        self._connect(cond_opens, check)
        loop_entry = entry or check
        context = _LoopContext()
        self._breaks.append(context)
        try:
            _, body_opens = self._linearize(
                node.child_by_field_name("body"), [_Open(check, (BRANCH_TRUE,))]
            )
        finally:
            self._breaks.pop()
        self._flush_loop(context, loop_entry, body_opens, loop_entry)
        return loop_entry, [*context.breaks, _Open(check, (BRANCH_FALSE,))]

    def _switch_statement(self, node: Any, incoming: list[_Open]) -> tuple[_Point | None, list[_Open]]:
        condition = _unwrap_condition(node.child_by_field_name("condition"))
        if condition is None:
            return self._plain_generic(node, incoming)
        entry, cond_opens = self._linearize(condition, incoming)
        check = self._condition_point(condition)
        self._connect(cond_opens, check)
        context = _SwitchContext(check=check)
        self._breaks.append(context)
        self._switches.append(context)
        pending: list[_Open] = []
        try:
            body = node.child_by_field_name("body")
            for child in body.named_children if body is not None else ():
                if child.type == "case_statement":
                    # Every label is an entry off the same check. The body
                    # threads linearly between labels, so the fallthrough into
                    # the next label is exactly the pending chain, flagged --
                    # keeping whatever arm the open arrived on, then marked.
                    fallthrough = [
                        _Open(open_edge.src, (*open_edge.flags, FALLTHROUGH))
                        for open_edge in pending
                    ]
                    _, pending = self._linearize(
                        child, [*fallthrough, _Open(check, _label_flags(child))]
                    )
                else:
                    _, pending = self._linearize(child, pending)
        finally:
            self._breaks.pop()
            self._switches.pop()
        opens = [*context.breaks, *pending]
        if not context.saw_default:
            opens.append(_Open(check, (BRANCH_FALSE,)))
        return entry or check, opens

    def _case_statement(self, node: Any, incoming: list[_Open]) -> tuple[_Point | None, list[_Open]]:
        switch = self._switches[-1] if self._switches else None
        if switch is None:
            return self._plain_generic(node, incoming)
        value = node.child_by_field_name("value")
        if value is None:
            switch.saw_default = True
        entry: _Point | None = None
        current = list(incoming)
        if value is not None:
            entry, current = self._linearize(value, current)
        for statement in node.named_children:
            if statement is value:
                continue
            if statement.type == "case_statement":
                # `case 1: case 2: ...` -- the nested label is another entry
                # off the same switch check.
                label = _Open(switch.check, _label_flags(statement))
                child_entry, current = self._linearize(statement, [*current, label])
            else:
                child_entry, current = self._linearize(statement, current)
            if entry is None:
                entry = child_entry
        return entry, current

    def _return_statement(self, node: Any, incoming: list[_Open]) -> tuple[_Point | None, list[_Open]]:
        entry, current = self._children_linearize(node, incoming)
        entry, current = self._thread_own_points(node, entry, current)
        # Control leaves the method: the chain ends at the synthetic exit.
        for open_edge in current:
            self._emit(open_edge.src, _EXIT, ())
        return entry, []

    def _throw_statement(self, node: Any, incoming: list[_Open]) -> tuple[_Point | None, list[_Open]]:
        entry, current = self._children_linearize(node, incoming)
        entry, current = self._thread_own_points(node, entry, current)
        for open_edge in current:
            self._emit(open_edge.src, _EXIT, (EXCEPTION,))
        if self._handlers and current:
            # Which catch clauses these edges land on is unknown until the
            # enclosing try has walked them; the flush happens there. A throw
            # binds to the innermost try -- picking among its handlers is the
            # source's business, not this walker's, so every handler gets an
            # edge and no handler is fabricated for `exit()`-style calls.
            self._pending_throws.append((current, self._handlers[-1]))
        return entry, []

    def _break_statement(self, node: Any, incoming: list[_Open]) -> tuple[_Point | None, list[_Open]]:
        # Outside every loop and switch the break is simply dropped -- bad code
        # must not crash the walk.
        if self._breaks:
            self._breaks[-1].breaks.extend(incoming)
        return None, []

    def _continue_statement(self, node: Any, incoming: list[_Open]) -> tuple[_Point | None, list[_Open]]:
        # `continue` binds to loops only; inside a bare switch it is dropped.
        if self._breaks and isinstance(self._breaks[-1], _LoopContext):
            self._breaks[-1].continues.extend(incoming)
        return None, []

    def _try_statement(self, node: Any, incoming: list[_Open]) -> tuple[_Point | None, list[_Open]]:
        context = _HandlerContext()
        self._handlers.append(context)
        try:
            body_entry, opens = self._linearize(node.child_by_field_name("body"), incoming)
        finally:
            self._handlers.pop()
        # Over-approximation: the body's normal completion continues past the
        # try even though a throw may have routed it into a handler instead.
        for child in node.named_children:
            if child.type not in {"catch_clause", "handler", "except_clause"}:
                continue
            # Nothing normal flows into a handler; the exception edges from the
            # body's throws are its only in-edges.
            catch_entry, catch_opens = self._linearize(child, [])
            if catch_entry is not None:
                context.entries.append(catch_entry)
            opens = [*opens, *catch_opens]
        self._flush_throws(context)
        return body_entry, opens

    def _flush_throws(self, context: _HandlerContext) -> None:
        remaining: list[tuple[list[_Open], _HandlerContext]] = []
        for opens, owner in self._pending_throws:
            if owner is not context:
                remaining.append((opens, owner))
                continue
            for open_edge in opens:
                for entry in context.entries:
                    self._emit(open_edge.src, entry, (EXCEPTION,))
        self._pending_throws = remaining

    def _goto_statement(self, node: Any, incoming: list[_Open]) -> tuple[_Point | None, list[_Open]]:
        # The jump target is not modelled; the chain simply ends. The resulting
        # incompleteness is a registered imprecision of stage 3.
        return None, []

    def _conditional_expression(self, node: Any, incoming: list[_Open]) -> tuple[_Point | None, list[_Open]]:
        children = node.named_children
        condition = node.child_by_field_name("condition")
        if condition is None or len(children) < 3:
            return self._plain_generic(node, incoming)
        consequence = node.child_by_field_name("consequence")
        alternative = node.child_by_field_name("alternative")
        if consequence is None:
            consequence = children[1]
        if alternative is None:
            alternative = children[-1]
        entry, cond_opens = self._linearize(condition, incoming)
        check = self._condition_point(condition)
        self._connect(cond_opens, check)
        _, true_opens = self._linearize(consequence, [_Open(check, (BRANCH_TRUE,))])
        _, false_opens = self._linearize(alternative, [_Open(check, (BRANCH_FALSE,))])
        return entry or check, [*true_opens, *false_opens]


# Joins the root identifiers of a call's arguments into one annotation value.
# A C identifier cannot contain it, so the encoding is unambiguous without a
# second storage column, and the annotation channel stays `dict[str, str]`.
ARGUMENT_SEPARATOR = "\x1f"

# `p->buf` descends through `argument`; `*buffer` and `(Foo *)p` too. Declarator
# kinds matter because `char *buffer = malloc(n)` names the target through a
# pointer_declarator, not an identifier.
_DESCEND_FIELDS = {
    "field_expression": "argument",
    "pointer_expression": "argument",
    "subscript_expression": "argument",
    "unary_expression": "argument",
    "update_expression": "argument",
    "cast_expression": "value",
    "pointer_declarator": "declarator",
    "array_declarator": "declarator",
    "reference_declarator": "declarator",
}


def _root_identifier(source: bytes, node: Any) -> str:
    """The leftmost identifier of an expression: `p->buf[i]` names `p`."""
    while node is not None:
        if node.type in {"identifier", "type_identifier"}:
            # `type_identifier` is a leaf name too, and it is what a
            # most-vexing-parse leaves behind where an expression would
            # otherwise be: `guard(m)` spells `m` as a type name.
            return _expression_text(source, node)
        if node.type == "parenthesized_expression":
            node = node.named_children[0] if node.named_children else None
            continue
        field_name = _DESCEND_FIELDS.get(node.type)
        node = node.child_by_field_name(field_name) if field_name else None
    return ""


def _path_text(text: str) -> str:
    """A member path with its whitespace removed, so it is an identity.

    `arr[i + 1]` and `arr[i+1]` are the same object, and an identity token that
    changes when someone reformats an expression is a token that splits one
    object into two candidates.
    """
    return "".join(text.split())


def _site_token(event: Any) -> str:
    """The identity of an allocation that binds no name.

    Same shape as `PointKey`, same reason: it must be computable at parse time,
    before the repository id exists, and must not contain a line number -- a
    blank line inserted at the top of the file may not create a new object.
    """
    return "|".join((
        event.owner_kind,
        event.owner_qualified_name,
        event.owner_signature,
        event.event_type,
        str(event.ordinal),
    ))


def _call_arguments(node: Any, source: bytes) -> str:
    """Every argument's root identifier, in source order, `\\x1f`-joined.

    Stage 4 recorded the *first* argument as the subject, which is right for
    `free(p)` and wrong for `RedisModule_SetKeyMeta(cls, key, p)`: the resource
    is handed over as the third argument, and a query that only ever looks at
    the first one calls that hand-off resolved. The whole list is kept so the
    ownership question ("is this name passed to a callee at all?") can be asked
    without a second parse.

    `\\x1f` because the channel is a `dict[str, str]` of annotations and the
    names in it are C identifiers: a separator that cannot appear inside one
    keeps the decoding unambiguous without a second storage column.
    """
    arguments = node.child_by_field_name("arguments")
    if arguments is None:
        return ""
    names = [_root_identifier(source, item) for item in arguments.named_children]
    names = [name for name in names if name]
    return ARGUMENT_SEPARATOR.join(names)


def _first_call_argument(node: Any) -> Any:
    arguments = node.child_by_field_name("arguments")
    if arguments is not None and arguments.named_children:
        return arguments.named_children[0]
    return None


# Node types the name walk passes through without leaving an object behind:
# `&m`, `*m`, `(m)`, a cast, and the declarators that spell a pointer,
# reference or array around the name they bind. Everything else the walk can
# descend -- `s->m`, `a[i]`, `!p` -- leaves an object behind, so the identifier
# recovered from it names that object and not the one the operation acts on.
_TRANSPARENT_WRAPPERS = frozenset({
    "parenthesized_expression", "pointer_expression", "cast_expression",
    "pointer_declarator", "reference_declarator", "array_declarator",
})


def _names_the_object(node: Any) -> bool:
    """Whether the recovered identifier is the whole designation.

    `&g_index_lock` names the lock; `&index->slot_locks[i]` names the struct
    that holds an array of locks, so two different elements of that array would
    otherwise read as one lock acquired twice. The difference is not in the
    name but in whether anything was left behind on the way down to it -- the
    same descent `_root_identifier` makes, with a note of what it passed.

    A bare `type_identifier` is the one node type that has to be read the other
    way: in `guard(m)` the most-vexing-parse leaves `m` spelled as a *type*,
    and that spelling is a fact about the parse and not about the object. It is
    a single token with nothing left behind on the way down, so it names the
    object exactly.
    """
    if node is not None and node.type == "type_identifier" and not node.named_children:
        return True
    while node is not None and node.type != "identifier":
        if node.type not in _TRANSPARENT_WRAPPERS:
            return False
        if node.type == "parenthesized_expression":
            node = node.named_children[0] if node.named_children else None
            continue
        node = node.child_by_field_name(_DESCEND_FIELDS[node.type])
    return node is not None


# Where a spawn call names the function the new thread runs. Positional
# arguments, not names: `pthread_create(&t, NULL, worker, NULL)` names `worker`
# third. A spawn whose name is missing from this table gets NO thread function
# -- guessing a position would attach a thread to whatever expression happened
# to sit there, and a wrong edge is worse than a missing one.
_THREAD_ENTRY_ARGUMENT = {
    "pthread_create": 2,
    "thrd_create": 1,
    "CreateThread": 2,
    "_beginthread": 1,
    "_beginthreadex": 2,
    "g_thread_new": 1,
    "g_thread_try_new": 1,
}

# The qualified C++ spawns take the callable as the construction argument, so
# their thread function is the declaration's argument rather than a fixed
# position in a call's argument list.
_THREAD_QUALIFIED_SPAWNS = frozenset({"std.thread", "std.jthread", "std.async"})


_EXIT_SORT_KEY = PointKey("", "", "", "", -1)

# Internal marker for the synthetic method exit on an edge under construction.
# Deliberately not None: None means "no target", which a malformed construct
# produces and which must be dropped rather than published.
_EXIT = object()


@dataclass(slots=True)
class _Point:
    """One CFG point under construction; becomes an edge endpoint key."""

    event_type: str
    owner_kind: str
    owner_qualified_name: str
    owner_signature: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    matched_name: str
    seq: int
    # Hits carry their ordinal from the event layer; structural points get one
    # in `_assign_ordinals` (-1 is never observable after `build`).
    ordinal: int = -1
    flags: tuple[str, ...] = ()
    annotations: dict[str, str] = field(default_factory=dict)

    def key(self) -> PointKey:
        return PointKey(
            self.owner_kind,
            self.owner_qualified_name,
            self.owner_signature,
            self.event_type,
            self.ordinal,
        )


@dataclass(frozen=True, slots=True)
class _Open:
    """Control flowing from `src` into whatever is linearized next.

    Flags ride on the open edge, not on the point: it is the edge that leaves a
    check's true arm which must carry `branch=true`. `src is None` is the
    method entry -- consumed by the first point, never published, because
    there is no entry node to hang an edge on.
    """

    src: _Point | None
    flags: tuple[str, ...] = ()


@dataclass(slots=True)
class _BreakContext:
    breaks: list[_Open] = field(default_factory=list)


@dataclass(slots=True)
class _LoopContext(_BreakContext):
    continues: list[_Open] = field(default_factory=list)


@dataclass(slots=True)
class _SwitchContext(_BreakContext):
    check: _Point | None = None
    saw_default: bool = False


@dataclass(slots=True)
class _HandlerContext:
    entries: list[_Point] = field(default_factory=list)


# Handler names, resolved per node type. String names rather than bound
# methods: the table lives before the class body finishes.
_CONTROL_HANDLERS = {
    "if_statement": "_if_statement",
    "switch_statement": "_switch_statement",
    "case_statement": "_case_statement",
    "while_statement": "_while_statement",
    "do_statement": "_do_statement",
    "for_statement": "_for_statement",
    "for_range_loop": "_for_range_loop",
    "return_statement": "_return_statement",
    "throw_statement": "_throw_statement",
    "break_statement": "_break_statement",
    "continue_statement": "_continue_statement",
    "try_statement": "_try_statement",
    "goto_statement": "_goto_statement",
    "conditional_expression": "_conditional_expression",
}

# Constructs whose bodies run only sometimes but which the walker linearizes
# as plain statements, so the handler map does not already answer for them.
# The try body itself is unconditional and is deliberately absent: the escape
# facts would rather under-count one write than invent one.
_CONDITIONAL_STATEMENTS = {"catch_clause", "handler"}

# The storages an escape can land in: the same set the access layer publishes,
# because the question is the same -- does this object outlive the frame? An
# `unknown` target is a name the file does not declare, which can never be a
# local of the method doing the writing.
# Stage 5B put the parameter storage into ACCESS_STORAGES so the write side
# of a parameter could publish, and that must not silently widen this set:
# a parameter slot is frame-local (it dies with the frame), so `q = p`
# writes into one is not an escape -- if the value leaves, the RETURN that
# carries it is the fact that says so, not the write.
_ESCAPE_TARGET_STORAGES = ACCESS_STORAGES - {STORAGE_PARAMETER}

# The allocations whose escape matters for the lifetime query: a frame-local
# is the only acquire whose leak claim an escape write can overturn. A
# function-static or a field does not leak by outliving the frame -- it *is*
# the longer-lived owner.
_FRAME_LOCAL_STORAGES = frozenset({STORAGE_LOCAL, STORAGE_PARAMETER})

# Where the written-to value is read from, per write-shaped node.
WRITE_SOURCE_FIELDS = {
    "assignment_expression": "right",
    "compound_assignment_expression": "right",
    "init_declarator": "value",
}


# ----------------------------------------------------------------------
# reachability over published edges


DEFAULT_ADJACENCY_RELATIONS = frozenset({CONTROL_REACHES})


def build_adjacency(
    edges: Iterable[Any],
    relations: frozenset[str] = DEFAULT_ADJACENCY_RELATIONS,
) -> dict[str, list[tuple[str, str, frozenset[str]]]]:
    """`src -> [(edge_id, dst, flags)]`, neighbours ordered by (dst, edge_id).

    Accepts `SemanticEdge` objects or the rows the storage loader returns --
    both are the same four facts. The ordering here is what makes a walk's
    result independent of the order rows came out of SQLite in.

    The `relations` filter exists because stage 5B started sharing the
    `semantic_edges` table: a DATA_FLOW_TO neighbour is not a control
    predecessor, and every path walk in this module (and the defect layer's)
    reasons about control reachability only. The default keeps the pre-5B
    behaviour bit for bit; a consumer that wants flow edges names them
    explicitly, so no existing caller had to change.
    """
    adjacency: dict[str, list[tuple[str, str, frozenset[str]]]] = {}
    for edge in edges:
        if relations is not None and _field_of(edge, "relation") not in relations:
            continue
        entry = (_field_of(edge, "edge_id"), _field_of(edge, "dst_event_id"), _flags_of(edge))
        adjacency.setdefault(_field_of(edge, "src_event_id"), []).append(entry)
    for neighbours in adjacency.values():
        neighbours.sort(key=lambda item: (item[1], item[0]))
    return adjacency


def _flags_of_point(point: Any) -> tuple[str, ...]:
    flags = _field_of(point, "flags")
    if isinstance(flags, str):
        flags = json.loads(flags) if flags else []
    return tuple(flags)


def materialize_points(events: Iterable[Any], edges: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """One info record per event, plus one synthetic record per exit id.

    The exit has no `semantic_events` row (口径 1: it is an edge endpoint, not
    an event), so paths that end at it need it materialised from the edge rows.

    Three keys are the reachability contract the pattern functions read --
    `event_type`, `subject`, `polarity` -- and they are all stage 3 ever put
    here. The rest are the defect layer's: `subject_from`, `subject_exact` and
    `raii` decide whether an identity is resolved or a name-level proxy,
    `thread_function` is what a spawn edge would point at, and the
    span/owner/file keys are what a candidate cites. Widening this dict is how
    stage 4 reads facts without a second query against the database.

    Stage 4.5 adds the declaration identity: `subject_decl` is the token that
    says *which* object a subject name refers to, `subject_storage` how long it
    lives, `subject_type` what it was declared as, `subject_member` the member
    path left over after the root name, `subject_site` the allocation site of an
    anonymous `new`, and `arguments` every root identifier passed to the call.
    A record without `subject_decl` is an old row or a name the file does not
    declare; the defect layer falls back to the name rather than dropping it.
    """
    points: dict[str, dict[str, Any]] = {}
    for event in events:
        metadata = _field_of(event, "metadata")
        if isinstance(metadata, str):
            metadata = json.loads(metadata) if metadata else {}
        metadata = metadata or {}
        points[_field_of(event, "event_id")] = {
            "event_type": _field_of(event, "event_type"),
            "subject": metadata.get("subject", ""),
            "polarity": metadata.get("polarity", ""),
            "subject_from": metadata.get("subject_from", ""),
            "subject_exact": metadata.get("subject_exact", ""),
            "raii": metadata.get("raii", ""),
            "thread_function": metadata.get("thread_function", ""),
            "subject_decl": metadata.get("subject_decl", ""),
            "subject_storage": metadata.get("subject_storage", ""),
            "subject_type": metadata.get("subject_type", ""),
            "subject_member": metadata.get("subject_member", ""),
            "subject_site": metadata.get("subject_site", ""),
            "arguments": metadata.get("arguments", ""),
            # 5C-2's annotation facts, read off the acquisition point itself:
            # `passed_to`/`returned` say where an allocation goes on its own
            # line (a call argument, the method's return value), `escaped_to`/
            # `escaped_to_line` name the shared storages a plain assignment
            # copies the pointer into, and `subject_init_arg0` is the mutex
            # bound in the same declaration as a guard variable. The escape
            # rulings in the defect layer join on these with no contract
            # dependency, so they must survive materialisation like the
            # declaration-identity keys above them.
            "passed_to": metadata.get("passed_to", ""),
            "returned": metadata.get("returned", ""),
            "escaped_to": metadata.get("escaped_to", ""),
            "escaped_to_line": metadata.get("escaped_to_line", ""),
            "subject_init_arg0": metadata.get("subject_init_arg0", ""),
            # The access layer's own annotation keys (5A/5C-2's 2.11): the
            # expression text, the root name a plain assignment read, and
            # whether the write sits outside every conditional construct.
            # Stage 5B adds the data-flow pair: `alias` is the MAY_ALIAS
            # candidate a bare copy stamped on this point (semicolon-joined
            # opposite roots), and `null_source` marks a write whose
            # right-hand side was the null literal. Both are candidate-
            # grade facts -- neither ever becomes a fact edge by itself.
            "expression": metadata.get("expression", ""),
            "write_source": metadata.get("write_source", ""),
            "unconditional": metadata.get("unconditional", ""),
            "alias": metadata.get("alias", ""),
            "null_source": metadata.get("null_source", ""),
            "matched_name": _field_of(event, "matched_name"),
            "matched_via": _field_of(event, "matched_via"),
            # Ordinal is a per-(owner, event type) source-order counter, so it
            # orders acquisitions without a line number: the lock-order query
            # sorts LOCK points by it, and reformatting cannot move a lock.
            "ordinal": _field_of(event, "ordinal"),
            "flags": _flags_of_point(event),
            "owner_symbol_id": _field_of(event, "owner_symbol_id"),
            "file_id": _field_of(event, "file_id"),
            "start_line": _field_of(event, "start_line"),
            "end_line": _field_of(event, "end_line"),
            "relative_path": metadata.get("relative_path", ""),
        }
    for edge in edges:
        dst = _field_of(edge, "dst_event_id")
        if dst.startswith(CFG_EXIT_PREFIX) and dst not in points:
            points[dst] = {
                "event_type": "EXIT",
                "subject": "",
                "polarity": "",
                "subject_from": "",
                "subject_exact": "",
                "raii": "",
                "thread_function": "",
                # The synthetic exit is not an operation, so it has no identity
                # to declare. The keys are present and empty rather than absent:
                # every reader does `info.get(key, "")`, and a record with a
                # different shape is one `KeyError` away from being the only
                # path nobody tested.
                "subject_decl": "",
                "subject_storage": "",
                "subject_type": "",
                "subject_member": "",
                "subject_site": "",
                "arguments": "",
                "passed_to": "",
                "returned": "",
                "escaped_to": "",
                "escaped_to_line": "",
                "subject_init_arg0": "",
                "expression": "",
                "write_source": "",
                "unconditional": "",
                "alias": "",
                "null_source": "",
                "matched_name": "",
                "matched_via": "",
                "ordinal": 0,
                "flags": (),
                "owner_symbol_id": "",
                "file_id": "",
                "start_line": 0,
                "end_line": 0,
                "relative_path": "",
            }
    return points


def find_paths(
    adjacency: dict[str, list[tuple[str, str, frozenset[str]]]],
    points: dict[str, dict[str, Any]],
    *,
    starts: Iterable[str],
    is_target: Callable[[dict[str, Any]], bool],
    is_blocked: Callable[[dict[str, Any]], bool] | None = None,
    blocked_points: Collection[str] = (),
    blocked_edges: Collection[str] = (),
    require_flags: Collection[str] = (),
    max_hops: int = 32,
    max_paths: int = 8,
) -> tuple[list[ControlPath], bool]:
    """Deterministic breadth-first search for the first `max_paths` acyclic
    paths from any start to any point the predicates accept.

    Determinism contract: starts are visited in sorted order, neighbours in
    (dst, edge_id) order, and the reported paths sort by (length, points). The
    per-path visited set makes cycles terminate without losing the second,
    longer way round a loop -- two genuinely different paths are two facts.
    Returns (paths, truncated), where `truncated` says some work was left
    unexplored because a cap fired.
    """
    blocked_edges = frozenset(blocked_edges)
    require = frozenset(require_flags)
    found: list[ControlPath] = []
    truncated = False
    for start in sorted(starts):
        if len(found) >= max_paths:
            truncated = True
            break
        start_info = points.get(start)
        if (
            start_info is None
            or (is_blocked is not None and is_blocked(start_info))
            or start in blocked_points
        ):
            continue
        queue: deque[tuple[str, tuple[str, ...], tuple[str, ...], frozenset[str]]] = deque()
        queue.append((start, (start,), (), frozenset({start})))
        while queue and len(found) < max_paths:
            current, point_ids, edge_ids, visited = queue.popleft()
            for edge_id, dst, flags in adjacency.get(current, ()):
                if len(found) >= max_paths:
                    break
                if edge_id in blocked_edges or not require <= flags:
                    continue
                if dst in visited or dst in blocked_points:
                    continue
                dst_info = points.get(dst)
                if dst_info is None:
                    continue
                if is_blocked is not None and is_blocked(dst_info):
                    continue
                next_points = point_ids + (dst,)
                next_edges = edge_ids + (edge_id,)
                if is_target(dst_info):
                    found.append(
                        ControlPath(
                            next_points,
                            tuple(points.get(pid, {}).get("event_type", "") for pid in next_points),
                            next_edges,
                        )
                    )
                if len(next_edges) >= max_hops:
                    continue
                queue.append((dst, next_points, next_edges, visited | {dst}))
        if queue and len(found) >= max_paths:
            truncated = True
    found.sort(key=lambda path: (len(path.points), path.points))
    return found, truncated


def _subject_matches(info: dict[str, Any], subject: str) -> bool:
    return bool(subject) and info.get("subject", "") == subject


def unreleased_resources(
    edges: Iterable[Any],
    points: dict[str, dict[str, Any]],
    *,
    acquire: str = "ALLOC",
    release: str = "RELEASE",
    exclude_null_guard: bool = True,
    excluded: Collection[str] = (),
    max_hops: int = 64,
    max_paths: int = 8,
) -> tuple[list[ControlPath], bool]:
    """A point of type `acquire` reaches the exit with no `release` of the same
    subject on the path.

    Parameterised in stage 4 so the same walk answers 4.1 (`ALLOC`/`RELEASE`)
    and the incomplete-cleanup half of 4.6 (`LOCK`/`UNLOCK`). The two differ in
    more than their type names:

    * `exclude_null_guard` is right for allocations and wrong for locks. A
      `true_when_null` guard on an allocation is "the allocation failed, return
      early" -- not a leak. The same guard on a lock is "the lock failed, return
      early" -- which is precisely the error path 4.6 exists to report, so the
      lock query turns the exclusion off.
    * `excluded` removes points as starts and as traversal targets rather than
      deleting them from `points`. Deleting would make `find_paths` skip paths
      that pass *through* the point (`dst_info is None`), silently weakening
      reachability for every other query in the same call.

    A subject-less acquire still produces candidates -- its block set is empty.
    Over-reporting is the intended bias for a candidate generator; the AI judge
    decides.
    """
    adjacency = build_adjacency(edges)
    excluded_set = frozenset(excluded)
    found: list[ControlPath] = []
    truncated = False
    for start in sorted(
        pid
        for pid, info in points.items()
        if info.get("event_type") == acquire and pid not in excluded_set
    ):
        subject = points[start].get("subject", "")
        blocked = {
            pid
            for pid, info in points.items()
            if info.get("event_type") == release and _subject_matches(info, subject)
        } | excluded_set
        blocked_edges: set[str] = set()
        if subject and exclude_null_guard:
            for pid, info in points.items():
                if (
                    info.get("event_type") == "CHECK"
                    and _subject_matches(info, subject)
                    and info.get("polarity") == "true_when_null"
                ):
                    for edge_id, _dst, flags in adjacency.get(pid, ()):
                        if BRANCH_TRUE in flags:
                            blocked_edges.add(edge_id)
        paths, more = find_paths(
            adjacency,
            points,
            starts={start},
            is_target=lambda info: info.get("event_type") == "EXIT",
            blocked_points=blocked,
            blocked_edges=blocked_edges,
            max_hops=max_hops,
            max_paths=max_paths,
        )
        found.extend(paths)
        truncated = truncated or more
    return found, truncated


def leaked_allocations(
    edges: Iterable[Any], points: dict[str, dict[str, Any]], *, max_hops: int = 64, max_paths: int = 8
) -> tuple[list[ControlPath], bool]:
    """Matrix 4.1 verbatim: ALLOC reaches the exit with no RELEASE of the same
    subject on the path. The stage 3 spelling of `unreleased_resources`."""
    return unreleased_resources(edges, points, max_hops=max_hops, max_paths=max_paths)


def unchecked_dereferences(
    edges: Iterable[Any], points: dict[str, dict[str, Any]], *, max_hops: int = 64, max_paths: int = 8
) -> tuple[list[ControlPath], bool]:
    """Matrix 3.1: a dereference reachable from the null side of a check on the
    same subject.

    `true_when_null` seeds from `branch=true` (`!p` true means p is null),
    `true_when_non_null` from `branch=false`. The `||` blind spot applies: a
    check whose condition is a short-circuit feeds both arms, and this pattern
    does not expand them -- the `short_circuit` flag makes those checkers
    queryable for stage 4.
    """
    adjacency = build_adjacency(edges)
    found: list[ControlPath] = []
    truncated = False
    checks = sorted(
        pid
        for pid, info in points.items()
        if info.get("event_type") == "CHECK"
        and bool(info.get("subject"))
        and info.get("polarity") in {"true_when_null", "true_when_non_null"}
    )
    for check_id in checks:
        info = points[check_id]
        wanted = BRANCH_TRUE if info["polarity"] == "true_when_null" else BRANCH_FALSE
        seeds = [dst for _edge_id, dst, flags in adjacency.get(check_id, ()) if wanted in flags]
        if not seeds:
            continue
        subject = info["subject"]
        paths, more = find_paths(
            adjacency,
            points,
            starts=seeds,
            is_target=lambda target_info, subject=subject: (
                target_info.get("event_type") == "DEREFERENCE" and _subject_matches(target_info, subject)
            ),
            max_hops=max_hops,
            max_paths=max_paths,
        )
        found.extend(paths)
        truncated = truncated or more
    return found, truncated


def use_after_release(
    edges: Iterable[Any], points: dict[str, dict[str, Any]], *, max_hops: int = 64, max_paths: int = 8
) -> tuple[list[ControlPath], bool]:
    """Matrix 4.3's first half: a dereference of a subject after its release.

    USE is DEREFERENCE-only in stage 3 (口径 2): passing the pointer to a
    callee is a registered gap. Aliases are invisible (`q = p; free(p);
    sink(q)` reports nothing, `free(p); p = NULL` guards nothing) -- that is
    the "confirmation slides to B" half of matrix 4.3, awaiting the DFG.
    """
    adjacency = build_adjacency(edges)
    found: list[ControlPath] = []
    truncated = False
    releases = sorted(
        pid for pid, info in points.items() if info.get("event_type") == "RELEASE" and bool(info.get("subject"))
    )
    for release_id in releases:
        subject = points[release_id]["subject"]
        paths, more = find_paths(
            adjacency,
            points,
            starts={release_id},
            is_target=lambda target_info, subject=subject: (
                target_info.get("event_type") == "DEREFERENCE" and _subject_matches(target_info, subject)
            ),
            max_hops=max_hops,
            max_paths=max_paths,
        )
        found.extend(paths)
        truncated = truncated or more
    return found, truncated
