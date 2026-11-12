"""Semantic event levels and the event row model (ported from PR #1, I1-A).

I1-A scope note: only the level-0 vocabulary events are produced on main.
The structural CHECK/BRANCH/DEREFERENCE points and the READ/WRITE access
events belong to the sparse CFG walker (stage 3 / 5A) and stay unported until
I1-B; the constants below are kept so the level taxonomy -- and what is
deliberately not produced yet -- stays nameable and testable.
"""

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
#   CHECK / BRANCH / DEREFERENCE are produced structurally by the sparse CFG
#   walker (no keyword can name them, which is exactly why they are not
#   vocabulary events) -- not ported to main until I1-B.
#
#   OPEN / CLOSE / ACQUIRE / WAIT / SIGNAL / CREATE / DESTROY are level 1 by
#   omission. The design doc §23 lists them as node types, but the stage 2
#   level 0 list in appendix B.4 does not include them. Keeping them here means
#   a matrix entry that needs them -- 4.5 non-memory resource leak, graded A --
#   is reported as "requires an event we do not yet extract" instead of
#   silently looking supported.
#
#   ARITH is here for the same reason, with one more step of honesty: integer
#   overflow (matrix 2.1, graded B) is a fact about a multiplication wrapping,
#   and no event describes an arithmetic operation at all -- not level 0, not
#   level 1. Declaring it lets the 2.1 row name what it needs and be reported as
#   a gap, rather than leaving the row out and the gap unstated.
LEVEL_1_EVENTS = frozenset({
    "READ", "WRITE", "DEREFERENCE", "CHECK", "BRANCH",
    "OPEN", "CLOSE", "ACQUIRE", "WAIT", "SIGNAL", "CREATE", "DESTROY",
    "ARITH",
})

KNOWN_EVENTS = LEVEL_0_EVENTS | LEVEL_1_EVENTS

# The level-1 event types the PR pipeline produces structurally. On main none
# of them are produced yet (I1-B scope); the set is ported for the taxonomy.
#
# ARITH joined this set with matrix 2.1's query: the sparse CFG walker now emits
# a point for each multiplication/shift inside an allocation's size argument, so
# the event is produced by the same structural rule that emits CHECK/BRANCH/
# DEREFERENCE rather than by a vocabulary keyword (no callee name describes
# arithmetic). `defect_patterns.toml`'s 2.1 row names it, and `missing_events()`
# reads this set to decide whether that requirement is met.
STRUCTURAL_EVENTS = frozenset({"BRANCH", "CHECK", "DEREFERENCE", "ARITH"})

# The access events (stage 5A) -- PR-only, not ported (I1-B).
ACCESS_EVENTS = frozenset({"READ", "WRITE"})


@dataclass(frozen=True, slots=True)
class SemanticEdge:
    """One typed relation between two semantic events (I1-C, from PR #1).

    I1-A/I1-B produced only CONTROL_REACHES via the sparse_cfg analyzer; the
    table and id are relation-generic because the DFG relations (I1-C:
    DATA_FLOW_TO / CONSUMES) land in the same analysis_edges rows.
    `dst_event_id` may be a synthetic exit id (`identity.cfg_exit_id`), which
    has no event row -- consumers that need the exit as a node materialise it
    from the edge rows.
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
