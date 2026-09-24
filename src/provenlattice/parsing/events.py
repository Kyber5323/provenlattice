from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ..models import ParsedEvent
from ..semantics.vocabulary import EventSpec, Vocabulary

# `::`, `->` and `.` all separate the parts of a name, and which one appears is
# a spelling difference rather than a semantic one.
_NAME_SEPARATOR = re.compile(r"::|->|\.")
_WHITESPACE = re.compile(r"\s+")


def _strip_template_arguments(name: str) -> str:
    """Drop a trailing balanced `<...>` group: `foo<int>` becomes `foo`.

    Only a balanced group is removed, so `operator<` and `operator<<` survive
    with their names intact.
    """
    if not name.endswith(">"):
        return name
    depth = 0
    for index in range(len(name) - 1, -1, -1):
        character = name[index]
        if character == ">":
            depth += 1
        elif character == "<":
            depth -= 1
            if depth == 0:
                return name[:index]
    return name


def _canonical(name: str) -> str:
    return _NAME_SEPARATOR.sub(".", name)


def _split_name(name: str) -> list[str]:
    return [part for part in _NAME_SEPARATOR.split(name) if part]


@dataclass(frozen=True, slots=True)
class OwnerSpan:
    """The byte range and identity of one symbol that can own events."""

    start_byte: int
    end_byte: int
    kind: str
    qualified_name: str
    signature: str


@dataclass(frozen=True, slots=True)
class EventHit:
    """One vocabulary hit with the byte range it was found at (stage 3).

    The event layer needs only the `ParsedEvent`; the CFG builder additionally
    needs *where* in the tree each event sits, because edges are position
    facts. Both consume the same matcher through this one record, so a hit
    cannot appear in the event layer while being invisible to the CFG layer --
    two implementations of the matcher would drift, and the failure mode is
    exactly the silent kind: edges that skip an operation the events table has.
    """

    start_byte: int
    end_byte: int
    visit_index: int
    event: ParsedEvent


class OwnerIndex:
    """Innermost-enclosing lookup over the spans the symbol extractor produced.

    Owner identity is not recomputed here. The symbol extractor already decided
    what the enclosing function is called and what its signature is; this class
    only looks that decision up. Recomputing it would mean two implementations
    of qualified-name construction that could drift, and the failure mode is
    silent -- events attached to symbol ids that do not exist.
    """

    def __init__(self, spans: Iterable[OwnerSpan]) -> None:
        self._spans = sorted(spans, key=lambda span: (span.start_byte, -span.end_byte))
        self._starts = [span.start_byte for span in self._spans]
        # Highest end_byte at or before each position. Lets a node that sits
        # outside every span answer in one step instead of scanning back
        # through the whole file.
        self._highest_end: list[int] = []
        highest = -1
        for span in self._spans:
            highest = max(highest, span.end_byte)
            self._highest_end.append(highest)

    def owner_of(self, start_byte: int, end_byte: int) -> OwnerSpan | None:
        index = bisect_right(self._starts, start_byte) - 1
        if index < 0 or self._highest_end[index] < end_byte:
            return None
        # Walking backwards from the last span that opens at or before the node
        # reaches the innermost container first, because a containing span
        # always opens after the span that contains it.
        while index >= 0:
            span = self._spans[index]
            if span.end_byte >= end_byte:
                return span
            index -= 1
        return None

    @property
    def spans(self) -> tuple[OwnerSpan, ...]:
        """The spans in open order, as identity objects.

        The CFG builder maps each span to its AST node by byte range and holds
        the same object it gets back from `owner_of`, so span comparisons are
        identity comparisons and cannot be fooled by equal-but-distinct spans.
        """
        return tuple(self._spans)


class EventExtractor:
    """Promote the operations a vocabulary names into events.

    Keywords are candidate generators, not judges (design doc §8). A keyword
    hit proposes an event; tree-sitter structure has already confirmed the
    shape by the time a matcher runs, because the matcher only reads the
    `function` field of a call or the `type` field of a declaration. Nothing
    here decides that a defect exists.
    """

    def __init__(
        self,
        source: bytes,
        vocabulary: Vocabulary,
        owners: OwnerIndex,
        line_of: Callable[[int, bool], int],
    ) -> None:
        """`line_of(byte_offset, is_end)` maps a byte offset to a 1-based line."""
        self.source = source
        self.vocabulary = vocabulary
        self.owners = owners
        self.line_of = line_of
        # Maps hold (spec, the vocabulary's own spelling of the keyword). The
        # spelling is kept because the ambiguity report is keyed by it, and
        # canonicalising the key would lose the link back to that report.
        self._by_syntax: dict[str, list[EventSpec]] = {}
        self._by_call: dict[str, list[tuple[EventSpec, str]]] = {}
        self._by_qualified: dict[str, list[tuple[EventSpec, str]]] = {}
        for spec in vocabulary.events.values():
            for node_type in spec.syntax:
                self._by_syntax.setdefault(node_type, []).append(spec)
            for keyword in spec.calls:
                self._by_call.setdefault(_canonical(keyword), []).append((spec, keyword))
            for keyword in spec.qualified:
                self._by_qualified.setdefault(_canonical(keyword), []).append((spec, keyword))
        self._found: list[tuple[int, int, int, EventSpec, str, str, tuple[str, ...]]] = []
        self._visit_index = 0
        # Events that sit outside every symbol. Counted rather than dropped
        # quietly: a `static Foo *g = malloc(...)` at file scope is a real leak
        # candidate, and it must be visible that stage 2 does not see it.
        self.skipped_outside_owner = 0

    def extract_hits(self, root: Any) -> list[EventHit]:
        """Every owned hit in source order, with byte ranges attached."""
        self._walk(root)
        return self._assign_ordinals()

    def extract(self, root: Any) -> list[ParsedEvent]:
        return [hit.event for hit in self.extract_hits(root)]

    def _walk(self, node: Any) -> None:
        self._match(node)
        for child in node.named_children:
            self._walk(child)

    def _name_text(self, node: Any) -> str:
        """Whitespace-free source text, so `obj . method` reads as one name."""
        return _WHITESPACE.sub("", self.source[node.start_byte:node.end_byte].decode("utf-8", "replace"))

    def _match(self, node: Any) -> None:
        self._visit_index += 1
        for spec in self._by_syntax.get(node.type, ()):
            self._record(node, spec, node.type, "syntax", node.type)
        if node.type in self.vocabulary.call_syntax:
            self._match_callee(node)
        if node.type in self.vocabulary.declaration_syntax:
            self._match_declared_type(node)

    def _match_callee(self, node: Any) -> None:
        callee = node.child_by_field_name("function")
        if callee is None:
            return
        parts = _split_name(_strip_template_arguments(self._name_text(callee)))
        if not parts:
            return
        final, full = parts[-1], ".".join(parts)
        # Keyed by event type so one call cannot produce the same event twice.
        # `sys.exit(1)` matches `exit` on the bare-name axis and `sys.exit` on
        # the qualified axis; recording both would report two throws where the
        # source has one. The qualified hit is the more specific claim, so it
        # wins. Two *different* event types both matching is a different
        # situation -- that is real ambiguity and both survive.
        candidates: dict[str, tuple[EventSpec, str, str, str]] = {}
        for spec, keyword in self._by_call.get(final, ()):
            candidates[spec.event_type] = (spec, final, "calls", keyword)
        if full != final:
            for spec, keyword in self._by_qualified.get(full, ()):
                candidates[spec.event_type] = (spec, full, "qualified", keyword)
        for spec, matched_name, matched_via, keyword in candidates.values():
            self._record(node, spec, matched_name, matched_via, keyword)

    def _match_declared_type(self, node: Any) -> None:
        """RAII acquisitions are declarations, not calls.

        `std::lock_guard<std::mutex> guard(m);` acquires in the constructor, so
        the acquisition is the construction site and no call in the body ever
        mentions it. Reading the declared type is the only way to see it.
        """
        declared = node.child_by_field_name("type")
        if declared is None:
            return
        name = _canonical(_strip_template_arguments(self._name_text(declared)))
        if not name:
            return
        # One event per declarator: `std::thread a(f), b(g);` starts two
        # threads, and reporting one would undercount the very thing a
        # thread-leak query counts.
        declarators = list(node.children_by_field_name("declarator"))
        for _ in declarators or [None]:
            for spec, keyword in self._by_qualified.get(name, ()):
                self._record(node, spec, name, "qualified", keyword)

    def _record(
        self, node: Any, spec: EventSpec, matched_name: str, matched_via: str, keyword: str
    ) -> None:
        # A keyword two events both claim produces both events, flagged. The
        # honest reading of `WaitForSingleObject` is "a lock or a join", and
        # picking one would hand every downstream consumer a certainty the
        # source does not support.
        ambiguous = len(self.vocabulary.ambiguous.get(keyword, ())) > 1
        flags = ("ambiguous_keyword",) if ambiguous else ()
        self._found.append(
            (node.start_byte, node.end_byte, self._visit_index, spec, matched_name, matched_via, flags)
        )

    def _assign_ordinals(self) -> list[EventHit]:
        # Source order, not walk order. A pre-order walk visits `f` before `g`
        # in `f(g(x))` even though `g` starts earlier, and an ordinal a reader
        # cannot reproduce by looking at the file is worse than no ordinal.
        ordered = sorted(self._found, key=lambda item: (item[0], item[1], item[2]))
        counters: dict[tuple[str, str, str, str], int] = {}
        hits: list[EventHit] = []
        for start_byte, end_byte, visit_index, spec, matched_name, matched_via, flags in ordered:
            owner = self.owners.owner_of(start_byte, end_byte)
            if owner is None:
                self.skipped_outside_owner += 1
                continue
            key = (owner.kind, owner.qualified_name, owner.signature, spec.event_type)
            ordinal = counters.get(key, 0)
            counters[key] = ordinal + 1
            hits.append(
                EventHit(
                    start_byte=start_byte,
                    end_byte=end_byte,
                    visit_index=visit_index,
                    event=ParsedEvent(
                        event_type=spec.event_type,
                        owner_kind=owner.kind,
                        owner_qualified_name=owner.qualified_name,
                        owner_signature=owner.signature,
                        ordinal=ordinal,
                        start_line=self.line_of(start_byte, False),
                        end_line=self.line_of(end_byte, True),
                        matched_name=matched_name,
                        matched_via=matched_via,
                        flags=flags,
                    ),
                )
            )
        return hits
