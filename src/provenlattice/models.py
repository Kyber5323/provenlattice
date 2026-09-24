from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class Node:
    id: str
    kind: str
    repo_id: str
    shard_id: str | None
    file_id: str | None
    name: str
    qualified_name: str
    language: str | None
    start_line: int | None
    end_line: int | None
    signature: str | None
    source_hash: str | None
    generation: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Edge:
    id: str
    src_id: str
    dst_id: str
    type: str
    provenance: str = "static_analysis"
    confidence: float = 1.0
    generation: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Shard:
    shard_id: str
    repo_id: str
    path: str
    nodes_count: int
    edges_count: int
    public_symbols: list[str]
    api_fingerprint: str
    generation: int
    boundary_dirty: bool = False
    # Appendix B.2.2. `api_fingerprint` covers only public signatures, so a
    # body-only edit leaves it unchanged and triggers no cross-shard
    # recomputation -- right for a code graph, fatal for the event layer, where
    # every event lives inside a body. This is the coarse second signal.
    semantic_fingerprint: str = ""
    semantic_dirty: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Delta:
    nodes_added: int = 0
    nodes_removed: int = 0
    nodes_updated: int = 0
    edges_added: int = 0
    edges_removed: int = 0
    old_api_fingerprint: dict[str, str] = field(default_factory=dict)
    new_api_fingerprint: dict[str, str] = field(default_factory=dict)
    boundary_dirty: list[str] = field(default_factory=list)
    # Shards whose source changed even though no public signature did. These
    # are the ones `boundary_dirty` cannot see and the event layer cannot
    # afford to miss (appendix B.2.2).
    semantic_dirty: list[str] = field(default_factory=list)
    shards_updated: list[str] = field(default_factory=list)
    references_reprocessed: int = 0
    references_reused: int = 0
    directly_affected_shards: list[str] = field(default_factory=list)
    affected_boundary_edges: list[str] = field(default_factory=list)
    impact_frontier_size: int = 0
    wide_impact: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Metrics:
    files_scanned: int = 0
    files_parsed: int = 0
    parse_failures: int = 0
    syntax_error_files: int = 0
    nodes_created: int = 0
    edges_created: int = 0
    raw_references: int = 0
    resolved_references: int = 0
    ambiguous_references: int = 0
    unresolved_references: int = 0
    shards_created: int = 0
    boundary_edges: int = 0
    files_reparsed: int = 0
    files_reused: int = 0
    references_reprocessed: int = 0
    references_reused: int = 0
    shards_updated: int = 0
    shards_reused: int = 0
    boundary_edges_reprocessed: int = 0
    boundary_edges_reused: int = 0
    # Size of the semantic layer this snapshot wrote: all event rows
    # (vocabulary + structural points) and all CONTROL_REACHES rows, carried
    # forward ones included -- they are in the snapshot either way.
    semantic_points_created: int = 0
    semantic_edges_created: int = 0
    impact_frontier_size: int = 0
    wide_impact: bool = False
    index_time_ms: float = 0.0
    query_time_ms: float = 0.0
    incremental_time_ms: float = 0.0
    changed_files: int = 0
    affected_nodes: int = 0
    affected_edges: int = 0
    database_size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ParsedSymbol:
    kind: str
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    signature: str
    public: bool
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedReference:
    source_qualified_name: str
    type: str
    target: str
    target_module: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    start_line: int | None = None
    end_line: int | None = None


@dataclass(slots=True)
class ParsedImport:
    source_qualified_name: str
    target: str
    module: str
    alias: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedEvent:
    """One defect-relevant operation found in a method body.

    The `owner_*` fields repeat the enclosing symbol's identity instead of
    pointing at it, because the enclosing symbol's id cannot be computed until
    the repository id is known. Repeating the identity also lets the event
    layer be checked against the symbol layer by construction rather than by
    convention -- a mismatch is visible rather than silent.
    """

    event_type: str
    owner_kind: str
    owner_qualified_name: str
    owner_signature: str
    ordinal: int
    start_line: int
    end_line: int
    matched_name: str
    matched_via: str
    flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, order=True)
class PointKey:
    """Identity of one CFG point before the repository id is known.

    Exactly the inputs `identity.event_id` consumes (appendix B.2.1): the
    owner triple locates the method, the event type and ordinal locate the
    operation within it. Edges carry these keys instead of indices into a
    points list so that the publisher resolves endpoints through a dict of
    published ids -- never through list position, which a second ordering
    pass could silently shuffle.
    """

    owner_kind: str
    owner_qualified_name: str
    owner_signature: str
    event_type: str
    ordinal: int


@dataclass(slots=True)
class ParsedPoint:
    """One structurally-produced CFG point (stage 3: CHECK/BRANCH/DEREFERENCE).

    Only the points the vocabulary cannot produce appear here -- vocabulary
    events keep their own `ParsedEvent` rows and are referenced from edges by
    key alone, so no operation is ever represented twice.
    """

    event_type: str
    owner_kind: str
    owner_qualified_name: str
    owner_signature: str
    ordinal: int
    start_line: int
    end_line: int
    matched_name: str
    matched_via: str = "structural"
    flags: tuple[str, ...] = ()

    def key(self) -> PointKey:
        return PointKey(
            self.owner_kind,
            self.owner_qualified_name,
            self.owner_signature,
            self.event_type,
            self.ordinal,
        )


@dataclass(frozen=True, slots=True)
class ParsedEdge:
    """One `CONTROL_REACHES` edge between two CFG points.

    `dst is None` is the method's synthetic exit: not an event, published as
    `identity.cfg_exit_id(repo_id, owner_symbol_id)` of the *source* point's
    owner. Flags are stored sorted; the sorted set is the id discriminator.
    """

    src: PointKey
    dst: PointKey | None
    flags: tuple[str, ...] = ()


@dataclass(slots=True)
class ParsedCfg:
    """The compressed control-flow of one parsed file (stage 3).

    `annotations` maps a point key to the subject metadata that makes the
    pattern queries pair operations across events (`RELEASE`'s matched_name is
    the keyword `free`, not the freed variable). It covers vocabulary events
    too -- those annotations have nowhere else to travel, and `metadata` is a
    JSON column that takes them without a schema change.
    """

    points: list[ParsedPoint] = field(default_factory=list)
    edges: list[ParsedEdge] = field(default_factory=list)
    annotations: dict[PointKey, dict[str, str]] = field(default_factory=dict)


# Storage classes of a declared name (stage 4.5). The vocabulary lives here
# because two modules that must not import each other need it:
# `parsing.declarations` produces it and `sparsecfg` consumes it, and
# `sparsecfg` cannot import from `parsing` without a cycle (`parsing.cpp`
# already imports `sparsecfg`).
STORAGE_LOCAL = "local"
STORAGE_PARAMETER = "parameter"
STORAGE_FUNCTION_STATIC = "function_static"
STORAGE_FIELD = "field"
STORAGE_FILE_STATIC = "file_static"
STORAGE_GLOBAL = "global"
# Not a storage class: the name has no declaration in this file. Kept as a
# first-class value so a consumer can tell "declared elsewhere" from "the index
# failed to find it", and so neither is silently dropped.
STORAGE_UNKNOWN = "unknown"


@dataclass(slots=True)
class ParsedFile:
    symbols: list[ParsedSymbol]
    references: list[ParsedReference]
    imports: list[ParsedImport]
    parser: str = "tree-sitter"
    events: list[ParsedEvent] = field(default_factory=list)
    # Events found outside every symbol, which stage 2 has no identity to
    # anchor. Counted rather than dropped quietly: a file-scope
    # `static Foo *g = malloc(8);` is a real leak candidate, and a report that
    # does not mention it looks like a clean file.
    unowned_events: int = 0
    # Declarations inside a function body that were *not* promoted to a symbol,
    # because the statement declares a variable even though its declarator
    # reads as a prototype (`std::lock_guard<std::mutex> lock(m);`). The count
    # is the price of that heuristic: the shapes it gives up are C's local
    # prototypes (`int helper(int);` inside a body), which used to become
    # one-line phantom Function symbols.
    demoted_local_prototypes: int = 0
    # Parameters declared as `type * QUALIFIER name`, where neither grammar can
    # parse the macro sitting between the `*` and the name and both recover by
    # reading the macro as the declarator. The count is how many names had to be
    # read back out of the grammar's own error node -- 2,161 in llama.cpp, all
    # of them `GGML_RESTRICT` or `RESTRICT`, and zero in redis-50.
    recovered_qualifier_parameters: int = 0
    # The sparse CFG of stage 3, when the parser produces one. Kept out of the
    # parse cache on purpose (see `parsed_to_dict`): the cache holds ~95% of
    # the `nodes` table and the CFG is cheap to rebuild from source.
    cfg: ParsedCfg | None = None


@dataclass(slots=True)
class RawReference:
    id: str
    repo_id: str
    file_id: str
    owner_symbol_id: str
    raw_name: str
    reference_type: str
    target_module: str | None
    start_line: int | None
    end_line: int | None
    status: str
    candidate_symbols: list[str]
    resolved_symbol_id: str | None
    resolution_strategy: str
    provenance: str
    confidence: float
    generation: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class OverlayMetadata:
    overlay_id: str
    overlay_type: str
    repository_id: str
    base_commit: str
    base_generation: int
    branch_name: str | None
    parent_overlay_id: str | None
    status: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class OverlayDelta:
    overlay_id: str
    entity_type: str
    entity_id: str
    operation: str
    base_version: str | None
    new_value: dict[str, Any] | None
    generation: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class OverlayConflict:
    conflict_type: str
    entity_type: str | None = None
    entity_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RawEvidenceLink:
    id: str
    repository_id: str
    source_node_id: str
    raw_anchor: str
    anchor_type: str
    candidate_targets: list[str]
    resolved_target_id: str | None
    resolution_status: str
    resolution_strategy: str
    provenance: str
    confidence: float
    generation: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Compatibility names for the initial V0 public surface.
SymbolDraft = ParsedSymbol
RelationDraft = ParsedReference
