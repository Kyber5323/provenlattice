from __future__ import annotations

from dataclasses import dataclass, field


# Design doc §13 splits semantic events into levels. Level 0 is extracted
# unconditionally; level 1 waits until a defect rule asks for it; level 2 is
# rebuilt on demand as a local deep graph rather than stored.
LEVEL_0_EVENTS = frozenset({
    "CALL", "RETURN", "THROW",
    "ALLOC", "RELEASE",
    "LOCK", "UNLOCK", "ATOMIC",
    "THREAD_SPAWN", "THREAD_JOIN",
})

# Level 1 holds two different things, and the distinction matters:
#
#   READ / WRITE are level 1 by design -- they wait for the DFG (stage 5) and
#   are only extracted for symbols a defect rule has already flagged.
#
#   CHECK / BRANCH / DEREFERENCE stopped being "not yet extracted" in stage 3:
#   the sparse CFG walker produces them structurally (no keyword can name them,
#   which is exactly why they are not vocabulary events) and publishes them as
#   `matched_via="structural"` event rows.
#
#   OPEN / CLOSE / ACQUIRE / WAIT / SIGNAL / CREATE / DESTROY are level 1 by
#   omission. The design doc §23 lists them as node types, but the stage 2
#   level 0 list in appendix B.4 does not include them. Keeping them here means
#   a matrix entry that needs them -- 4.5 non-memory resource leak, graded A --
#   is reported as "requires an event we do not yet extract" instead of
#   silently looking supported.
LEVEL_1_EVENTS = frozenset({
    "READ", "WRITE", "DEREFERENCE", "CHECK", "BRANCH",
    "OPEN", "CLOSE", "ACQUIRE", "WAIT", "SIGNAL", "CREATE", "DESTROY",
})

KNOWN_EVENTS = LEVEL_0_EVENTS | LEVEL_1_EVENTS

# The level 1 event types the pipeline already produces today, structurally.
# `DefectPattern.missing_events` consults this set, so a coverage report says
# "attackable now" for a pattern the CFG actually supports.
STRUCTURAL_EVENTS = frozenset({"BRANCH", "CHECK", "DEREFERENCE"})

# The access events (stage 5A). A third category rather than an addition to
# STRUCTURAL_EVENTS, and the split is load-bearing: the publication test pins
# `matched_via == "structural"` to membership in STRUCTURAL_EVENTS, and an
# access is published as `matched_via == "access"` -- it is a different kind of
# finding and the evidence bundle has to be able to say which one it is.
#
# They are also not vocabulary events in the way CHECK and DEREFERENCE are not:
# no keyword names a read, only position does, so the walker produces them from
# the assignment target it already computes. Unlike the structural three they
# are *published and never connected* -- see `_EDGELESS_EVENT_TYPES` in
# `sparsecfg.py` for why, and for what that protects.
ACCESS_EVENTS = frozenset({"READ", "WRITE"})


def event_level(event_type: str) -> int:
    if event_type in LEVEL_0_EVENTS:
        return 0
    if event_type in LEVEL_1_EVENTS:
        return 1
    raise KeyError(f"unknown event type: {event_type}")


@dataclass(frozen=True, slots=True)
class SemanticEvent:
    """One defect-relevant operation inside a method body.

    Identity is method-anchored, not position-anchored (appendix B.2.1): the id
    derives from the owning symbol plus the event's ordinal within that method,
    never from a line number. Editing a different method therefore leaves this
    id untouched, which is what keeps incremental deltas bounded.
    """

    event_id: str
    event_type: str
    owner_symbol_id: str
    file_id: str
    ordinal: int
    start_line: int
    end_line: int
    # How this event was found, kept as evidence rather than discarded: which
    # keyword or node type matched, and by which axis.
    matched_name: str
    matched_via: str
    flags: tuple[str, ...] = field(default=())
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "owner_symbol_id": self.owner_symbol_id,
            "file_id": self.file_id,
            "ordinal": self.ordinal,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "matched_name": self.matched_name,
            "matched_via": self.matched_via,
            "flags": list(self.flags),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SemanticEdge:
    """One typed relation between two semantic events (stage 3).

    Stage 3 produces only `CONTROL_REACHES`; the table and id are relation-
    generic because the DFG relations of stage 5 land in the same two tables.
    `dst_event_id` may be a synthetic exit id (`identity.cfg_exit_id`), which
    has no `semantic_events` row -- consumers that need the exit as a node
    materialise it from the edge rows. The flag set (`branch=true`, ...,
    sorted) is the id discriminator: two edges between the same pair of points
    are a real possibility, and equal flag sets are the same reachability fact.
    """

    edge_id: str
    src_event_id: str
    dst_event_id: str
    relation: str
    owner_symbol_id: str
    file_id: str
    flags: tuple[str, ...] = field(default=())
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "edge_id": self.edge_id,
            "src_event_id": self.src_event_id,
            "dst_event_id": self.dst_event_id,
            "relation": self.relation,
            "owner_symbol_id": self.owner_symbol_id,
            "file_id": self.file_id,
            "flags": list(self.flags),
            "metadata": dict(self.metadata),
        }
