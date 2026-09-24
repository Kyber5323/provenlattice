"""Ownership contracts: what a callee does with what it is handed.

Stage 4's twenty adjudicated candidates had one shape in common. Every fact in
the graph was right -- the allocation happened, and no release followed on a
path to exit -- and what was missing sat above the facts: whether the callee the
resource was handed to takes ownership, whether the type it was kept in is a
wrapper that owns it, whether the release obligation of this lock belongs to the
caller. The evidence matrix calls that layer a contract; this module assembles
it.

Three sources, and every entry says which one it came from:

* **Declared** (`semantics/contracts.toml`) -- the corpus knowledge that cannot
  be read out of the graph at all, such as `RedisModule_SetKeyMeta` taking
  ownership of its third argument. Each entry carries the `evidence` that makes
  it checkable against the source, because a hand-written table nobody can
  audit is folklore.
* **Inferred from a pair** -- two functions whose names share a stem once the
  acquire/release morphemes are removed and whose lock subjects agree. The
  acquire half hands the release obligation to its caller, so a lock still held
  at its exit is not this frame's leak.
* **Inferred from a body** -- a function that releases its own parameter
  consumes it, at the ordinal its parameter list gives. The ordinal is checked
  against the signature and the entry is dropped rather than guessed when the
  check disagrees.

Nothing here promotes a candidate. Stage 5C may only eliminate or leave alone:
the acceptance is that a cross-function acquire/release sample stops forming a
*high-confidence* conclusion from "this frame has no release" alone, and that an
unknown contract stays three-state. A callee the graph cannot name is an unknown
contract, and an unknown contract eliminates nothing.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import sparsecfg
from .models import (
    STORAGE_FIELD, STORAGE_FILE_STATIC, STORAGE_FUNCTION_STATIC, STORAGE_GLOBAL,
)

DECLARED_FILE = Path(__file__).with_name("semantics") / "contracts.toml"

# Storage durations whose owner outlives the frame that allocated. An
# allocation kept in a field, a file static or a global is not this frame's
# leak; it belongs to whatever owns that slot.
OWNER_STORAGES = frozenset({STORAGE_FIELD, STORAGE_FILE_STATIC, STORAGE_GLOBAL})

# A function-static slot outlives the frame too, but the caveat is different and
# is recorded rather than hidden: a later call that overwrites the slot leaks
# the previous allocation, and no walk of one method can see that.
FUNCTION_STATIC_STORAGE = STORAGE_FUNCTION_STATIC

# The morphemes that mark the two halves of an acquire/release pair, matched
# against whole words rather than as substrings, with their inflections spelled
# out rather than stemmed. Two reasons, both measured on redis-50:
#
# * as substrings, `lock` matches `unblock`, `blocked` and `blocking`, so
#   `moduleUnblockClientByHandle` reads as an acquire half. Whole words fix it,
#   and `xBlockY` / `xUnblockY` would otherwise share a stem and pair.
# * `try` is dropped as a word because it only ever modifies the acquire verb
#   (`moduleTryAcquireGIL` has to share a stem with `moduleAcquireGIL`), and
#   spelling out `locked` / `locking` / `released` avoids a stemmer turning
#   `released` into `releas`.
#
# Kept deliberately short: `free`, `delete` and `close` are release words too,
# but they appear in ordinary names (`freeClient`) often enough that stripping
# them would start pairing unrelated functions -- and a wrong pair silently
# deletes a real finding.
_ACQUIRE_WORDS = frozenset(
    {"acquire", "acquires", "acquired", "acquiring", "tryacquire", "try",
     "lock", "locks", "locked", "locking"}
)
_RELEASE_WORDS = frozenset(
    {"release", "releases", "released", "releasing", "unlock", "unlocks",
     "unlocked", "unlocking"}
)

# Split a name into words at `_` and at camelCase boundaries. `GIL` stays one
# word, and so does `GILAcquire` -- the second alternative catches the acronym
# that a following capital begins.
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")

# Trailing tokens that are part of a parameter's *type*, not its name.
_QUALIFIERS = frozenset({"const", "noexcept", "override", "final", "volatile"})

_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_PLAIN_NAME = re.compile(r"[A-Za-z_]\w*")


def short_name(qualified_name: str) -> str:
    """The last component of a qualified name, which is what a call spells."""
    return qualified_name.rsplit(".", 1)[-1]


def pair_stem(name: str) -> tuple[str, str] | None:
    """`(stem, half)` for a name that reads as an acquire or release half.

    `None` when the name carries neither morpheme, or carries both -- a name
    that says both things is not evidence of either.
    """
    words = [word.lower() for word in _WORDS.findall(name)]
    acquires = any(word in _ACQUIRE_WORDS for word in words)
    releases = any(word in _RELEASE_WORDS for word in words)
    if acquires == releases:
        return None
    stem = "".join(
        word for word in words if word not in _ACQUIRE_WORDS and word not in _RELEASE_WORDS
    )
    if not stem:
        # A function named exactly `lock` carries no stem, so every such name
        # would share the empty stem and pair with every `unlock`. Nothing left
        # after removing the morphemes means the name said nothing else.
        return None
    return stem, "acquire" if acquires else "release"


def _subject_key(info: Mapping[str, Any]) -> str:
    """What two functions must agree on for a lock to be the same lock.

    The declaration token plus the member path, and deliberately *not* the
    owner. For a file static that is an identity -- `file_static:src/module.c:
    moduleGIL` names one object wherever it is written. For a parameter it is a
    spelling, which is the weaker claim stage 4.5 registered as `_by_root` and
    reported without adopting. Both strengths are wanted here: the identity case
    pairs `moduleAcquireGIL` with `moduleReleaseGIL`, and the spelling case pairs
    `hnsw_acquire_read_slot` with `hnsw_release_read_slot`, whose `index`
    parameters are two different objects by construction.
    """
    return f"{info.get('subject_decl', '')}#{info.get('subject_member', '')}"


def parameters(signature: str) -> tuple[str, ...]:
    """Split a parameter list at top-level commas.

    Depth-aware over `<>`, `()` and `[]` so `std::map<int, int> m` stays one
    parameter. Trailing qualifiers are stripped from each piece, because
    `void f(int n) const` and `void f(int n) noexcept` otherwise read as a
    second parameter named `const` -- which is exactly what the anchoring check
    below is for, and it caught four of them in llama.cpp.
    """
    text = signature.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    pieces: list[str] = []
    depth = 0
    current: list[str] = []
    for character in text:
        if character in "<([":
            depth += 1
        elif character in ">)]":
            depth -= 1
        if character == "," and depth == 0:
            pieces.append("".join(current))
            current = []
            continue
        current.append(character)
    pieces.append("".join(current))

    stripped: list[str] = []
    for piece in pieces:
        tokens = _IDENTIFIER.findall(piece)
        while tokens and tokens[-1] in _QUALIFIERS:
            tokens.pop()
        stripped.append(tokens[-1] if tokens else "")
    return tuple(piece for piece in stripped if piece)


@dataclass(frozen=True, slots=True)
class FunctionEffectSummary:
    """What one function does to one of its arguments, and how we know.

    `provenance` is the field that keeps this honest: an entry read out of the
    corpus and an entry typed into a table are different kinds of claim, and a
    reader who cannot tell them apart cannot weigh the conclusion.
    """

    function_id: str
    effect: str
    resource_kind: str
    argument_ordinal: int
    ownership_effect: str
    confidence: float
    provenance: str


@dataclass(frozen=True, slots=True)
class ContractTable:
    """The assembled contracts, plus what assembling them cost.

    The lookups are methods rather than bare dictionaries because the callers
    are query code that must read the same way at every site: `callee(info)`
    answers "who is this call to" with `""` for unknown, and unknown is a
    first-class answer everywhere here -- it is what makes elimination safe.
    """

    callees: Mapping[tuple[str, str, int], str]
    sinks: Mapping[str, tuple[int, ...]]
    consumes: Mapping[str, int]
    wrappers: frozenset[str]
    pairs: Mapping[str, str]
    pair_labels: Mapping[str, str]
    storages: frozenset[str]
    summaries: tuple[FunctionEffectSummary, ...]
    coverage: Mapping[str, Any]

    # -- the four questions `defect` asks ---------------------------------

    def callee(self, info: Mapping[str, Any]) -> str:
        """The name this call spells, or `""` when the graph cannot say."""
        key = (
            str(info.get("file_id", "")),
            str(info.get("owner_symbol_id", "")),
            int(info.get("start_line", 0) or 0),
        )
        return self.callees.get(key, "")

    def consuming_ordinals(self, callee: str) -> tuple[int, ...]:
        """Ordinals this callee takes ownership of. Empty means "unknown"."""
        if not callee:
            return ()
        declared = self.sinks.get(callee)
        inferred = self.consumes.get(short_name(callee))
        ordinals: list[int] = []
        if declared:
            ordinals.extend(declared)
        if inferred is not None and inferred not in ordinals:
            ordinals.append(inferred)
        return tuple(ordinals)

    def caller_owns(self, owner_symbol_id: str) -> bool:
        """Whether this function is the acquire half of an inferred pair."""
        return owner_symbol_id in self.pairs

    def has_contract(self, callee: str) -> bool:
        """Whether anything at all is known about what this callee does.

        `False` is not "this callee does not take ownership" -- it is "no one
        knows", and the two must not be confused: only the first can eliminate
        a candidate.
        """
        return bool(callee) and (
            callee in self.sinks or short_name(callee) in self.consumes
        )

    def owns_storage(self, storage: str) -> bool:
        return storage in self.storages

    def is_wrapper(self, type_token: str) -> bool:
        return type_token in self.wrappers


EMPTY = ContractTable(
    callees={},
    sinks={},
    consumes={},
    wrappers=frozenset(),
    pairs={},
    pair_labels={},
    storages=frozenset(),
    summaries=(),
    coverage={},
)


def load_declared(path: Path | None = None) -> dict[str, Any]:
    """Read the declared table. Missing file is an empty table, not an error.

    A repository with no declared entries is a legitimate state -- the inferred
    sources carry the layer on their own -- and failing to load would turn that
    into a crash at query time.
    """
    source = path or DECLARED_FILE
    if not source.exists():
        return {"sink": [], "wrapper": []}
    try:
        payload = tomllib.loads(source.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"{source.name}: {error}") from error
    unknown = sorted(set(payload) - {"sink", "wrapper", "note"})
    if unknown:
        raise ValueError(
            f"{source.name}: unknown top-level keys {', '.join(unknown)}; "
            f"expected sink, wrapper, note"
        )
    for entry in payload.get("sink", []):
        if not isinstance(entry.get("name"), str) or not isinstance(entry.get("ordinal"), int):
            raise ValueError(f"{source.name}: every sink needs a name and an integer ordinal")
        if not entry.get("evidence"):
            raise ValueError(f"{source.name}: sink {entry['name']!r} has no evidence")
    for entry in payload.get("wrapper", []):
        if not isinstance(entry.get("type"), str):
            raise ValueError(f"{source.name}: every wrapper needs a type")
        if not entry.get("evidence"):
            raise ValueError(f"{source.name}: wrapper {entry['type']!r} has no evidence")
    return payload


def build(
    events: Iterable[Any],
    edges: Iterable[Any],
    *,
    references: Iterable[Mapping[str, Any]] = (),
    names: Mapping[str, str] | None = None,
    signatures: Mapping[str, str] | None = None,
    declared: Mapping[str, Any] | None = None,
) -> ContractTable:
    """Assemble the contracts. Pure: everything it reads is passed in.

    `references` are the `raw_references` CALLS rows, which is where a call's
    callee name lives -- a CALL semantic point is structural and its
    `matched_name` is the node type (`call_expression`), not the callee. The
    join is `(file_id, owner_symbol_id, start_line)`; a key carrying more than
    one name is ambiguous and stays unknown rather than picking a winner.
    """
    points = sparsecfg.materialize_points(events, edges)
    table = declared if declared is not None else {"sink": [], "wrapper": []}
    names = names or {}

    callees, ambiguous_spans = _callees(references)
    pairs, pair_labels, pair_details = _pairs(points, names)
    consumes, rejected, released_parameters = _consumes(points, names, signatures or {})

    sinks: dict[str, tuple[int, ...]] = {}
    for entry in table.get("sink", []):
        name = str(entry["name"])
        sinks[name] = tuple(sorted({*sinks.get(name, ()), int(entry["ordinal"])}))
    wrappers = frozenset(str(entry["type"]) for entry in table.get("wrapper", []))

    call_points = sum(1 for info in points.values() if info["event_type"] == "CALL")
    summaries = _summaries(sinks, consumes, pairs, names)
    coverage = {
        "call_points": call_points,
        "call_points_with_callee": sum(
            1
            for info in points.values()
            if info["event_type"] == "CALL" and callee_for(info, callees)
        ),
        "call_points_unjoined": sum(
            1
            for info in points.values()
            if info["event_type"] == "CALL" and not callee_for(info, callees)
        ),
        "callee_spans_ambiguous": len(ambiguous_spans),
        "call_points_with_contract": sum(
            1
            for info in points.values()
            if info["event_type"] == "CALL" and has_contract_for(info, callees, sinks, consumes)
        ),
        "pairs_inferred": pair_details,
        "consumes_inferred": len(consumes),
        "released_parameters": released_parameters,
        "contract_anchoring_rejected": rejected,
        "declared_sinks": len(sinks),
        "declared_wrappers": len(wrappers),
        "summaries": len(summaries),
        "summaries_inferred": sum(1 for s in summaries if s.provenance.startswith("inferred:")),
        "summaries_declared": sum(1 for s in summaries if s.provenance.startswith("declared:")),
    }
    return ContractTable(
        callees=callees,
        sinks=sinks,
        consumes=consumes,
        wrappers=wrappers,
        pairs=pairs,
        pair_labels=pair_labels,
        storages=OWNER_STORAGES | {FUNCTION_STATIC_STORAGE},
        summaries=summaries,
        coverage=coverage,
    )


def callee_for(
    info: Mapping[str, Any], callees: Mapping[tuple[str, str, int], str]
) -> str:
    """The join, in one place so `build` and `ContractTable.callee` agree."""
    return callees.get(
        (
            str(info.get("file_id", "")),
            str(info.get("owner_symbol_id", "")),
            int(info.get("start_line", 0) or 0),
        ),
        "",
    )


def has_contract_for(
    info: Mapping[str, Any],
    callees: Mapping[tuple[str, str, int], str],
    sinks: Mapping[str, tuple[int, ...]],
    consumes: Mapping[str, int],
) -> bool:
    """`ContractTable.has_contract` over a point, for use during `build`."""
    callee = callee_for(info, callees)
    return bool(callee) and (callee in sinks or short_name(callee) in consumes)


def _callees(
    references: Iterable[Mapping[str, Any]],
) -> tuple[dict[tuple[str, str, int], str], list[str]]:
    """Join key to callee name, keeping only keys that name one callee."""
    seen: dict[tuple[str, str, int], set[str]] = {}
    for row in references:
        name = row.get("raw_name")
        if not name:
            continue
        key = (
            str(row.get("file_id", "")),
            str(row.get("owner_symbol_id", "")),
            int(row.get("start_line", 0) or 0),
        )
        seen.setdefault(key, set()).add(str(name))
    resolved = {key: next(iter(value)) for key, value in seen.items() if len(value) == 1}
    ambiguous = [
        f"{'/'.join(sorted(value))}@{key[0]}:{key[2]}"
        for key, value in seen.items()
        if len(value) > 1
    ]
    return resolved, sorted(ambiguous)


def _pairs(
    points: Mapping[str, Mapping[str, Any]], names: Mapping[str, str]
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Acquire half to release half, for name stems whose lock subjects agree."""
    locked: dict[str, set[str]] = {}
    unlocked: dict[str, set[str]] = {}
    for info in points.values():
        owner = str(info.get("owner_symbol_id", ""))
        if not owner:
            continue
        if info["event_type"] == "LOCK":
            locked.setdefault(owner, set()).add(_subject_key(info))
        elif info["event_type"] == "UNLOCK":
            unlocked.setdefault(owner, set()).add(_subject_key(info))

    halves: dict[str, list[str]] = {}
    for owner in set(locked) | set(unlocked):
        stem = pair_stem(short_name(names.get(owner, "")))
        if stem is not None:
            # Keyed by the stem alone: the two halves have to land in the same
            # bucket for anything to pair. Keying by `(stem, half)` puts them in
            # different buckets and the rule silently infers nothing.
            halves.setdefault(stem[0], []).append(owner)

    pairs: dict[str, str] = {}
    labels: dict[str, str] = {}
    details: list[str] = []
    for stem, owners in sorted(halves.items()):
        acquires = [o for o in owners if pair_stem(short_name(names.get(o, "")))[1] == "acquire"]
        releases = [o for o in owners if pair_stem(short_name(names.get(o, "")))[1] == "release"]
        for acquire in sorted(acquires):
            for release in sorted(releases):
                if acquire == release:
                    continue
                if not locked.get(acquire, set()) & unlocked.get(release, set()):
                    continue
                pairs[acquire] = release
                labels[acquire] = (
                    f"{names.get(acquire, acquire)} -> {names.get(release, release)}"
                )
                details.append(labels[acquire])
    return pairs, labels, details


def _consumes(
    points: Mapping[str, Mapping[str, Any]],
    names: Mapping[str, str],
    signatures: Mapping[str, str],
) -> tuple[dict[str, int], list[str], int]:
    """Functions that release a whole parameter, and at which ordinal.

    A release of a *member* (`free(v->datai)`) is not a parameter release and is
    never counted here: the function freed something the parameter points at,
    which says nothing about who owns the parameter.
    """
    released: dict[str, set[str]] = {}
    released_parameters = 0
    for info in points.values():
        if info["event_type"] != "RELEASE":
            continue
        token = str(info.get("subject_decl", ""))
        if not token.startswith("parameter:") or info.get("subject_member"):
            continue
        released_parameters += 1
        owner = str(info.get("owner_symbol_id", ""))
        if owner:
            # Every release is kept, not one per function: a function that frees
            # two of its parameters has released two things, and collapsing that
            # to the first one seen would hide the second.
            released.setdefault(owner, set()).add(token.split(":", 1)[1])

    by_short: dict[str, list[tuple[str, int]]] = {}
    rejected: list[str] = []
    for owner, parameters_released in sorted(released.items()):
        qualified = names.get(owner, "")
        if not qualified:
            rejected.append(f"{owner}: releases {sorted(parameters_released)}, owner has no name")
            continue
        if not _PLAIN_NAME.fullmatch(short_name(qualified)):
            # `operator()` and friends are spellings many unrelated functions
            # share, so "this callee frees its first argument" is not a claim
            # about anything. A contract key has to be a name that names one
            # thing; anything else is counted and dropped rather than guessed.
            rejected.append(
                f"{qualified}: releases {sorted(parameters_released)}, "
                f"but {short_name(qualified)!r} is not a plain identifier"
            )
            continue
        pieces = parameters(signatures.get(owner, ""))
        ordinals: set[int] = set()
        for parameter in sorted(parameters_released):
            if parameter not in pieces:
                rejected.append(f"{qualified}: releases {parameter!r}, not in {pieces!r}")
                continue
            ordinals.add(pieces.index(parameter))
        if not ordinals:
            continue
        if len(ordinals) > 1:
            rejected.append(
                f"{qualified}: releases {sorted(parameters_released)} at ordinals "
                f"{sorted(ordinals)}; taking the lowest and recording the rest"
            )
        by_short.setdefault(short_name(qualified), []).append((owner, min(ordinals)))

    consumes: dict[str, int] = {}
    for short, entries in sorted(by_short.items()):
        ordinals = {ordinal for _owner, ordinal in entries}
        if len(ordinals) > 1:
            # Two functions that share a spelling disagree about which argument
            # they consume. Picking one would be a guess, so neither is used.
            rejected.append(f"{short}: conflicting ordinals {sorted(ordinals)}")
            continue
        consumes[short] = next(iter(ordinals))
    return consumes, rejected, released_parameters


def _summaries(
    sinks: Mapping[str, tuple[int, ...]],
    consumes: Mapping[str, int],
    pairs: Mapping[str, str],
    names: Mapping[str, str],
) -> tuple[FunctionEffectSummary, ...]:
    """The `FunctionEffectSummary` entries the stage is defined to produce.

    RAII wrapper *types* are deliberately not summarised here: they are types,
    not functions, and a `function_id` that is really a type name would make the
    field mean two things. They live in `wrappers`, where the query reads them.
    """
    summaries: list[FunctionEffectSummary] = []
    for name, ordinals in sorted(sinks.items()):
        for ordinal in ordinals:
            summaries.append(
                FunctionEffectSummary(
                    function_id=f"declared:{name}",
                    effect="consumes",
                    resource_kind="memory",
                    argument_ordinal=ordinal,
                    ownership_effect="takes",
                    confidence=0.6,
                    provenance=f"declared:sink:{name}",
                )
            )
    for short, ordinal in sorted(consumes.items()):
        summaries.append(
            FunctionEffectSummary(
                function_id=f"inferred:{short}",
                effect="consumes",
                resource_kind="memory",
                argument_ordinal=ordinal,
                ownership_effect="takes",
                confidence=0.6,
                provenance=f"inferred:consumes:{short}",
            )
        )
    for acquire, release in sorted(pairs.items()):
        for owner, other, effect in ((acquire, release, "acquires"), (release, acquire, "releases")):
            summaries.append(
                FunctionEffectSummary(
                    function_id=owner,
                    effect=effect,
                    resource_kind="lock",
                    argument_ordinal=-1,
                    ownership_effect="borrows",
                    confidence=0.6,
                    provenance=(
                        f"inferred:pair:{names.get(owner, owner)}"
                        f"<->{names.get(other, other)}"
                    ),
                )
            )
    return tuple(summaries)


def describe(table: ContractTable, *, limit: int = 40) -> list[str]:
    """A human-readable dump, for the acceptance record and for `--list`."""
    lines = [f"callees joined: {table.coverage.get('call_points_with_callee', 0)}"]
    lines.append(f"callees unjoined: {table.coverage.get('call_points_unjoined', 0)}")
    lines.append(f"pairs inferred ({len(table.pairs)}):")
    lines.extend(f"  {item}" for item in table.coverage.get("pairs_inferred", [])[:limit])
    lines.append(f"consumes inferred ({len(table.consumes)}):")
    lines.extend(
        f"  {short} ordinal {ordinal}" for short, ordinal in sorted(table.consumes.items())[:limit]
    )
    lines.append(f"declared sinks ({len(table.sinks)}):")
    lines.extend(
        f"  {name} ordinals {list(ordinals)}" for name, ordinals in sorted(table.sinks.items())[:limit]
    )
    lines.append(f"declared wrappers ({len(table.wrappers)}):")
    lines.extend(f"  {token}" for token in sorted(table.wrappers)[:limit])
    rejected = table.coverage.get("contract_anchoring_rejected", [])
    lines.append(f"anchoring rejected ({len(rejected)}):")
    lines.extend(f"  {item}" for item in rejected[:limit])
    return lines
