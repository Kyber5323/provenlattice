from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .events import ACCESS_EVENTS, KNOWN_EVENTS, LEVEL_0_EVENTS, STRUCTURAL_EVENTS, event_level

VOCABULARY_DIR = Path(__file__).with_name("vocabularies")
DEFECT_PATTERN_FILE = Path(__file__).with_name("defect_patterns.toml")

MATCHER_AXES = ("syntax", "calls", "qualified")
# Node types that carry a name the matchers can read. These are grammar facts,
# so they live in the vocabulary file rather than in the extractor: `call` in
# Python and `call_expression` in C++ are the same idea spelled differently.
GRAMMAR_AXES = ("call_syntax", "declaration_syntax")
# A table with no matchers is normally a mistake, so it is rejected. `absent`
# is the escape hatch: it lets a vocabulary state "this language genuinely has
# no such event" as a reasoned claim that a coverage report can quote, instead
# of leaving an empty table that reads like an unfinished one.
ABSENCE_KEY = "absent"

# Every event the pipeline can emit today: the level 0 extraction (stage 2),
# the structural CHECK/BRANCH/DEREFERENCE points the CFG walker adds (stage 3),
# and the READ/WRITE accesses it adds (stage 5A). A defect requirement outside
# this set is a gap, and naming the set once is what keeps `missing_events` and
# `candidate_missing_events` from drifting apart.
#
# This set is a claim about the *pipeline*, not about every occurrence, and not
# about every language: the walker emits these for the languages whose
# LanguageProfile is implemented (C and C++ today; Python's profile is a
# registered gap of stage 3), and an access is emitted only for subjects whose
# storage class says the object outlives the method. Both are narrower than
# "READ exists", and neither narrows to nothing.
PRODUCIBLE_EVENTS = LEVEL_0_EVENTS | STRUCTURAL_EVENTS | ACCESS_EVENTS

# Every Defect Semantic Relation design doc §5.3 defines. A pattern may name
# any of them -- that is how a requirement is recorded before the analysis
# that builds it exists -- but a name outside this set is a typo, not a gap.
KNOWN_RELATIONS = frozenset({
    "ACQUIRES", "CALLS", "CHECKS", "CONSUMES", "CONTROL_REACHES", "DATA_FLOW_TO",
    "DOMINATES_CHECK", "EXECUTES_IN", "MAY_ALIAS", "MAY_PARALLEL", "PRODUCES",
    "PROTECTED_BY", "READS", "RELEASES", "RETURNS", "THROWS_TO", "WRITES",
})
# What the graph actually holds. Stage 3 built CONTROL_REACHES and nothing
# else. Stage 5B adds DATA_FLOW_TO (the method-local def-use edges) and
# CONSUMES (ALLOC -> same-line CALL, minted from the 5C `passed_to` fact).
# The other names a stage-5 reading of the matrix could suggest stay absent
# on purpose, and their implementation mapping is: MAY_ALIAS is candidate
# metadata (the `alias` annotation), never a fact edge; DOMINATES_CHECK is
# the set of CHECK entries on a DATA_FLOW_TO edge's `via` chain, not a
# relation; and PRODUCES/RELEASES/RETURNS/READS/WRITES/ACQUIRES/EXECUTES_IN
# remain event-subject identity facts -- so 4.1's `missing_relations()` does
# not reach zero after 5B.
#
# Stage 6 leaves this set unchanged, and that is the honest outcome rather than
# an omission: it adds four *consumers* of the relations above, and every place
# a query looks like it is reading a missing relation it is reading something
# weaker. `CHECKS` and `DOMINATES_CHECK` are implemented for 3.1 as a seeding of
# the CFG walker's null arms and a reverse reachability test from a CHECK --
# facts about control flow, which is what those relations were always a
# shorthand for, and not new rows in a table. `PRODUCES` is implemented as the
# `result_of` annotation stage 6 adds to a WRITE whose right-hand side is a call
# (the query reads the annotation; nothing mints a PRODUCES edge). `RELEASES`
# stays an event-subject identity fact in the double-free pairing. The
# consequence is the one that matters for reporting: 3.1, 4.2, 4.3 and 4.6 all
# generate candidates while their `missing_relations()` stays non-empty, so
# "the pipeline proposes candidates for 3.1" and "the pipeline can confirm 3.1"
# remain two different sentences.
AVAILABLE_RELATIONS = frozenset({"CONTROL_REACHES", "DATA_FLOW_TO", "CONSUMES"})


class VocabularyError(ValueError):
    """A vocabulary file is malformed or declares something unknown."""


@dataclass(frozen=True, slots=True)
class EventSpec:
    """What a single event type looks like in one language.

    The keyword lists are candidate generators, not judges (design doc §8): a
    hit proposes an event, and tree-sitter structure confirms its shape. A name
    landing here never by itself asserts that a defect exists.
    """

    event_type: str
    syntax: frozenset[str]
    calls: frozenset[str]
    qualified: frozenset[str]
    # Set only when this language has no such event. The text is the reason,
    # and it is the whole payload -- a spec that is absent carries no matchers.
    absent: str | None = None

    @property
    def level(self) -> int:
        return event_level(self.event_type)

    def keywords(self) -> tuple[str, ...]:
        """Every literal this spec matches on. Sorted, so callers stay deterministic."""
        return tuple(sorted(self.calls | self.qualified))

    def has_matchers(self) -> bool:
        return bool(self.syntax or self.calls or self.qualified)

    def is_absent(self) -> bool:
        return self.absent is not None


@dataclass(frozen=True, slots=True)
class Vocabulary:
    language: str
    display: str
    extensions: frozenset[str]
    # Where a callee name can be read from, and where a declared type name can
    # be read from. Both may be empty -- a language whose grammar this
    # vocabulary has not been taught yet simply matches on `syntax` alone.
    call_syntax: frozenset[str]
    declaration_syntax: frozenset[str]
    events: Mapping[str, EventSpec]
    # keyword -> every event type claiming it. `WaitForSingleObject` really is
    # both a lock acquisition and a thread join; the loader records the clash
    # instead of picking a winner, matching the three-state evidence contract.
    ambiguous: Mapping[str, tuple[str, ...]]

    def spec_for(self, event_type: str) -> EventSpec | None:
        return self.events.get(event_type)

    def at_level(self, level: int) -> tuple[EventSpec, ...]:
        return tuple(spec for _, spec in sorted(self.events.items()) if spec.level == level)

    def level_0(self) -> tuple[EventSpec, ...]:
        return self.at_level(0)

    def keywords(self) -> tuple[str, ...]:
        return tuple(sorted({word for spec in self.events.values() for word in spec.keywords()}))

    def absent_events(self) -> tuple[EventSpec, ...]:
        """Events this language genuinely does not have. Findings, not gaps."""
        return tuple(spec for _, spec in sorted(self.events.items()) if spec.is_absent())


def _string_list(payload: dict, key: str, source: Path, event_type: str) -> frozenset[str]:
    value = payload.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise VocabularyError(
            f"{source.name}: [events.{event_type}].{key} must be a list of strings"
        )
    if any(not item.strip() for item in value):
        raise VocabularyError(f"{source.name}: [events.{event_type}].{key} contains a blank entry")
    return frozenset(value)


def _node_types(payload: dict, key: str, source: Path) -> frozenset[str]:
    value = payload.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise VocabularyError(f"{source.name}: {key} must be a list of node type names")
    return frozenset(value)


def _parse(path: Path) -> Vocabulary:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise VocabularyError(f"{path.name}: {error}") from error

    known_top_level = {"language", "display", "extensions", "events", *GRAMMAR_AXES}
    unknown_top_level = sorted(set(payload) - known_top_level)
    if unknown_top_level:
        raise VocabularyError(
            f"{path.name}: unknown top-level keys {', '.join(unknown_top_level)}; "
            f"expected {', '.join(sorted(known_top_level))}"
        )

    language = payload.get("language")
    if not isinstance(language, str) or language != path.stem:
        raise VocabularyError(
            f"{path.name}: language must be {path.stem!r} and match the filename"
        )

    extensions = payload.get("extensions")
    if not isinstance(extensions, list) or not extensions:
        raise VocabularyError(f"{path.name}: extensions must be a non-empty list")
    if any(not isinstance(item, str) or not item.startswith(".") for item in extensions):
        raise VocabularyError(f"{path.name}: every extension must be a string starting with '.'")

    raw_events = payload.get("events")
    if not isinstance(raw_events, dict) or not raw_events:
        raise VocabularyError(f"{path.name}: at least one [events.*] table is required")

    events: dict[str, EventSpec] = {}
    claimants: dict[str, list[str]] = {}
    for event_type, body in sorted(raw_events.items()):
        if event_type not in KNOWN_EVENTS:
            raise VocabularyError(
                f"{path.name}: unknown event type {event_type!r}; "
                f"known: {', '.join(sorted(KNOWN_EVENTS))}"
            )
        if not isinstance(body, dict):
            raise VocabularyError(f"{path.name}: [events.{event_type}] must be a table")
        expected = (*MATCHER_AXES, ABSENCE_KEY)
        unknown_keys = sorted(set(body) - set(expected))
        if unknown_keys:
            raise VocabularyError(
                f"{path.name}: [events.{event_type}] has unknown keys "
                f"{', '.join(unknown_keys)}; expected {', '.join(expected)}"
            )
        absent = body.get(ABSENCE_KEY)
        if absent is not None and (not isinstance(absent, str) or not absent.strip()):
            raise VocabularyError(
                f"{path.name}: [events.{event_type}].absent must be a non-empty reason"
            )
        spec = EventSpec(
            event_type=event_type,
            syntax=_string_list(body, "syntax", path, event_type),
            calls=_string_list(body, "calls", path, event_type),
            qualified=_string_list(body, "qualified", path, event_type),
            absent=absent,
        )
        # The two ways a table can lie about itself. Both are rejected, because
        # a coverage report built on either one would be wrong in a way nobody
        # reading the report could see.
        if spec.is_absent() and spec.has_matchers():
            raise VocabularyError(
                f"{path.name}: [events.{event_type}] claims to be absent but lists "
                "matchers; drop one or the other"
            )
        if not spec.is_absent() and not spec.has_matchers():
            raise VocabularyError(
                f"{path.name}: [events.{event_type}] declares no matchers; "
                f"remove the table, or state why with {ABSENCE_KEY} = \"...\""
            )
        events[event_type] = spec
        for word in spec.keywords():
            claimants.setdefault(word, []).append(event_type)

    ambiguous = {
        word: tuple(sorted(types)) for word, types in sorted(claimants.items()) if len(types) > 1
    }
    return Vocabulary(
        language=language,
        display=str(payload.get("display", language)),
        extensions=frozenset(extensions),
        call_syntax=_node_types(payload, "call_syntax", path),
        declaration_syntax=_node_types(payload, "declaration_syntax", path),
        events=MappingProxyType(events),
        ambiguous=MappingProxyType(ambiguous),
    )


@lru_cache(maxsize=None)
def load_vocabulary(language: str) -> Vocabulary:
    path = VOCABULARY_DIR / f"{language}.toml"
    if not path.is_file():
        raise VocabularyError(
            f"no vocabulary for {language!r}; available: {', '.join(available_languages())}"
        )
    return _parse(path)


def clear_vocabulary_cache() -> None:
    load_vocabulary.cache_clear()


def available_languages() -> tuple[str, ...]:
    return tuple(sorted(path.stem for path in VOCABULARY_DIR.glob("*.toml")))


def vocabulary_for_path(path: str) -> Vocabulary | None:
    """Pick a vocabulary by file extension. Ambiguity raises rather than guessing."""
    suffix = Path(path).suffix.lower()
    matches = [lang for lang in available_languages() if suffix in load_vocabulary(lang).extensions]
    if not matches:
        return None
    if len(matches) > 1:
        raise VocabularyError(
            f"extension {suffix!r} is claimed by {', '.join(matches)}; "
            "a file cannot be assigned to one language deterministically"
        )
    return load_vocabulary(matches[0])


@dataclass(frozen=True, slots=True)
class DefectPattern:
    """One row of the defect matrix, as far as the pipeline can act on it.

    Two pairs of requirements, because a defect is answered in two stages that
    need different things. `requires` / `relations` are what *confirming* the
    defect needs; `candidate_requires` / `candidate_relations` are what
    *proposing* a candidate needs. Stage 4 is the reason they are separate: a
    race candidate is proposable from events the pipeline already produces,
    while confirming the race needs READ/WRITE and four relations the DFG has
    not built. Collapsing the two made the coverage report say "not
    attackable" about a pattern the query layer does attack.

    `query` is the typed query that produces candidates, or None when the
    pattern is not expanded yet -- an absent key, never an empty one, because
    "unexpanded" and "expanded and needing nothing" are different claims.
    """

    key: str
    name: str
    grade: str
    requires: tuple[str, ...]
    relations: tuple[str, ...] = ()
    candidate_requires: tuple[str, ...] = ()
    candidate_relations: tuple[str, ...] = ()
    query: str | None = None

    @property
    def expanded(self) -> bool:
        return self.query is not None

    def missing_events(self) -> tuple[str, ...]:
        """Required events the pipeline cannot yet produce, sorted.

        "Produce" covers the vocabulary's level 0 extraction (stage 2), the
        structural CHECK/BRANCH/DEREFERENCE points the CFG walker emits
        (stage 3), and the READ/WRITE access events the walker's positional
        rule emits (stage 5A, matched_via="access"). What is still missing is
        the DFG's event vocabulary -- OPEN/CLOSE and friends -- not READ or
        WRITE.
        """
        return tuple(sorted(set(self.requires) - PRODUCIBLE_EVENTS))

    def missing_relations(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.relations) - AVAILABLE_RELATIONS))

    def candidate_missing_events(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.candidate_requires) - PRODUCIBLE_EVENTS))

    def candidate_missing_relations(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.candidate_relations) - AVAILABLE_RELATIONS))

    def coverable_now(self) -> bool:
        """Can this pattern's *confirmation* be answered from the graph today?"""
        return self.expanded and not self.missing_events() and not self.missing_relations()

    def candidate_coverable_now(self) -> bool:
        """Can this pattern's *candidate generation* run today?

        True for 1.1 while `coverable_now()` is False, and that pair of answers
        is the stage 4 falsifiability result stated mechanically rather than in
        prose.
        """
        return (
            self.expanded
            and not self.candidate_missing_events()
            and not self.candidate_missing_relations()
        )


def load_defect_patterns() -> tuple[DefectPattern, ...]:
    payload = tomllib.loads(DEFECT_PATTERN_FILE.read_text(encoding="utf-8"))
    patterns = []
    for key, body in sorted(payload.get("patterns", {}).items()):
        requires = tuple(sorted(body.get("requires", [])))
        relations = tuple(sorted(body.get("relations", [])))
        candidate_requires = tuple(sorted(body.get("candidate_requires", [])))
        candidate_relations = tuple(sorted(body.get("candidate_relations", [])))
        for label, names, known in (
            ("events", requires + candidate_requires, KNOWN_EVENTS),
            ("relations", relations + candidate_relations, KNOWN_RELATIONS),
        ):
            unknown = sorted(set(names) - known)
            if unknown:
                # A typo must not look like a gap: `MAY_PARALELL` would
                # otherwise report as a missing relation forever.
                raise VocabularyError(
                    f"defect pattern {key}: unknown {label} {', '.join(unknown)}"
                )
        query = body.get("query")
        if query is not None and (not isinstance(query, str) or not query.strip()):
            raise VocabularyError(
                f"defect pattern {key}: query must be a non-empty name, or absent"
            )
        patterns.append(
            DefectPattern(
                key=key,
                name=str(body.get("name", key)),
                grade=str(body.get("grade", "?")),
                requires=requires,
                relations=relations,
                candidate_requires=candidate_requires,
                candidate_relations=candidate_relations,
                query=query,
            )
        )
    return tuple(patterns)
