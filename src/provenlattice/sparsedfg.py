"""Stage 5B: the sparse data-flow layer.

One chain rule, minted at publish time. Within a method, a WRITE or an ALLOC
defines a value under a flow identity; a RELEASE both starts the free-state
chain and kills the live ALLOC; every READ / DEREFERENCE / CHECK / RETURN
binds to the most recent live definition of its own identity, and a CALL
binds, per argument root, the most recent live definition whose subject root
matches. Each binding mints one direct def-to-use edge -- never a per-hop
edge -- whose metadata keeps the intermediate sites (`via`) so the chain
stays recoverable from evidence. A bare copy (`q = p`, `q = &p`, recorded by
the access layer as `alias_source`) continues the copied name's chain: the
use binds through the copy, and the copy sites ride the edge as `via_copies`.

The identity here is the annotation tuple -- `(subject_decl, subject_storage,
subject_member, subject, subject_site)` -- not the defect layer's assembled
identity string. The tuple is what the events already carry, it is
constructively equivalent to what `_identity` builds from it, and keeping it
local means this module never imports the defect layer.

The relations land in the same `semantic_edges` table the control edges use
(`DATA_FLOW_TO`, `CONSUMES`); the adjacency builder's relation filter is what
keeps them out of every control-path walk. `MAY_ALIAS` never becomes an edge:
it is the `alias` annotation the access layer stamps, a candidate-grade fact.

Registered approximations (plan §九): no reaching-definition bitvectors and
no SSA; same-line ordering is `(start_line, event_type, ordinal)`, so macro
compression onto one line orders by type, and a same-line copy-write of an
allocation's own call defers to the ALLOC as the definition; a RELEASE kills
only the live ALLOC definition, not a plain WRITE's; a RELEASE of an alias
source does not kill the copies' live definitions (the free-state chain stays
name-local, so use-after-free through a copy binds the copy's own def);
intermediates inside an alias hop's own span are not enumerated (the copy
sites are, and the rest is recoverable from positions).

The edge source is the chain root -- the event that defined the flowing
value -- not the newest copy: an alias copy extends a definition, it does not
replace it, so `ALLOC(p) -> use` with the copy sites in `via` is the honest
shape, and it is what lets a consumer ask "did the acquired value reach this
use" by identity alone.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .contracts import parameters as _split_parameters
from .contracts import short_name
from .identity import semantic_edge_id, symbol_id
from .models import PointKey
from .semantics.events import SemanticEdge

DATA_FLOW_TO = "DATA_FLOW_TO"
CONSUMES = "CONSUMES"

# The flow identity: everything the annotation channel says about which
# object an event touches. Two events share a chain exactly when this tuple
# is equal, which is the same judgement the defect layer's identity makes
# from the same facts.
_FLOW_FIELDS = ("subject_decl", "subject_storage", "subject_member", "subject", "subject_site")

# Events that define a value; events that consume one. RELEASE is neither a
# plain def nor a use: it starts the free-state chain (its own "definition"
# is the freed state) and kills the live ALLOC.
DEF_EVENTS = frozenset({"WRITE", "ALLOC"})
USE_EVENTS = frozenset({"READ", "DEREFERENCE", "CHECK", "RETURN"})

ARGUMENT_SEPARATOR = "\x1f"

# Flow-class labels, in the precedence a single edge is tagged with when
# several descriptions could apply. `value_flow` is the residue: a real
# def-to-use edge that is none of the four matrix classes.
CLASS_INPUT_TO_SINK = "input_to_sink"
CLASS_ALLOC_TO_USE = "alloc_to_use"
CLASS_FREE_TO_USE = "free_to_use"
CLASS_NULL_TO_DEREF = "null_to_deref"
CLASS_VALUE_FLOW = "value_flow"


def _field_of(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj[name]
    return getattr(obj, name)


def _key_of(item: Any) -> PointKey:
    return PointKey(
        _field_of(item, "owner_kind"),
        _field_of(item, "owner_qualified_name"),
        _field_of(item, "owner_signature"),
        _field_of(item, "event_type"),
        _field_of(item, "ordinal"),
    )


def _flow_key(info: dict[str, str]) -> tuple[str, ...]:
    return tuple(info.get(field, "") for field in _FLOW_FIELDS)


@dataclass
class _Def:
    """One live definition: the event, its id, its class-relevant facts."""

    event: Any
    event_id: str
    position: tuple
    null_source: bool
    is_alloc: bool
    # A bare copy continues the copied name's chain: the def whose value this
    # one holds, as a `_Def`, plus the copy's own position.
    alias_parent: "_Def | None" = None


def _position(item: Any) -> tuple:
    return (_field_of(item, "start_line"), _field_of(item, "event_type"), _field_of(item, "ordinal"))


def mint_dataflow_edges(
    repo_id: str,
    parsed: Any,
    id_by_key: dict[PointKey, str],
    file_id: str,
    relative_path: str = "",
    shard_path: str = "",
) -> list[SemanticEdge]:
    """Mint the method-local DATA_FLOW_TO / CONSUMES rows for one parsed file.

    Pure with respect to its inputs: the edges are a function of the parsed
    file's events, points and annotations plus the id map the publication
    loop just built. Endpoints are resolved through `id_by_key` only -- an
    event that was never published is a hard error, the same discipline the
    control-edge block applies. Every edge is intra-method by construction:
    both endpoints share the owner triple, so the intra-file invariant the
    ladder tests holds without a special case.

    The extra two parameters (against the plan sketch) carry what the
    control edges also stamp into metadata; `parsed` itself does not know
    the file's path.
    """
    if parsed is None:
        return []
    annotations = parsed.cfg.annotations if parsed.cfg is not None else {}
    items = list(_event_items(parsed, annotations))
    if not items:
        return []

    edges: dict[str, SemanticEdge] = {}
    owners: dict[tuple, list] = {}
    for item in items:
        owners.setdefault(
            (
                _field_of(item, "owner_kind"),
                _field_of(item, "owner_qualified_name"),
                _field_of(item, "owner_signature"),
            ),
            [],
        ).append(item)

    for owner in sorted(owners):
        method = sorted(owners[owner], key=_position)
        edges.update(_mint_method(repo_id, owner, method, parsed, id_by_key, file_id,
                                  relative_path, shard_path))
    return [edges[edge_id] for edge_id in sorted(edges)]


def _event_items(parsed: Any, annotations: dict) -> list[Any]:
    """The vocabulary events and the CFG points, as one uniform list.

    The DFG walks the union: the vocabulary events (ALLOC/RELEASE/CALL/...)
    are not in `cfg.points`, and the access and structural points are not in
    `parsed.events`. Both carry the same owner/position facts and both read
    their subject facts from the one annotation channel. Items without an
    annotation entry simply never match a chain branch -- no need to filter
    them here.
    """
    items = list(parsed.events)
    if parsed.cfg is not None:
        items.extend(parsed.cfg.points)
    return items


def _root_def(definition: "_Def") -> "_Def":
    """The chain's root: the event that defined the value a use reads.

    An alias copy extends a definition instead of replacing it, so the copy
    WRITE is a hop (`via_copies`), and the def the edge names is the root the
    chain started from -- a consumer that asks "did the acquired value reach
    this use" reads the source id alone.
    """
    while definition.alias_parent is not None:
        definition = definition.alias_parent
    return definition


def _mint_method(
    repo_id: str,
    owner: tuple,
    method: list,
    parsed: Any,
    id_by_key: dict[PointKey, str],
    file_id: str,
    relative_path: str,
    shard_path: str,
) -> dict[str, SemanticEdge]:
    annotations = parsed.cfg.annotations if parsed.cfg is not None else {}
    owner_id = symbol_id(repo_id, relative_path, *owner)

    # Control-reachability is computed once per method, but only from the
    # sources that minted an edge: the walk collects pending mints and one
    # BFS per distinct def source runs at the end. (The first draft BFS'd
    # from every adjacency node and profiled at 95% of rebuild wall-clock;
    # defs with uses are far fewer than CFG nodes.) Only the flag rides an
    # edge; no gate reads it in 5B.
    pending: list[tuple[str, str, str, tuple[list[str], list[str]], str]] = []
    live_value: dict[tuple, _Def] = {}
    live_free: dict[tuple, _Def] = {}
    live_root: dict[str, _Def] = {}
    edges: dict[str, SemanticEdge] = {}

    for item in method:
        key = _key_of(item)
        info = annotations.get(key, {})
        event_type = _field_of(item, "event_type")
        subject = info.get("subject", "")
        event_id = id_by_key[key]
        position = _position(item)

        if event_type in DEF_EVENTS and subject:
            flow = _flow_key(info)
            if event_type == "WRITE":
                previous = live_value.get(flow)
                if previous is not None and previous.is_alloc and previous.position[0] == position[0]:
                    # The copy-write of an allocation's own acquiring
                    # expression: the ALLOC is the definition, the write is
                    # its handoff. Same line only -- a later write is a real
                    # redefinition (the kill rule).
                    pass
                else:
                    parent = None
                    alias_root = info.get("alias_source", "")
                    if alias_root and alias_root != subject:
                        parent = live_root.get(alias_root)
                    live_value[flow] = _Def(
                        event=item, event_id=event_id, position=position,
                        null_source=info.get("null_source", "") == "true",
                        is_alloc=False, alias_parent=parent,
                    )
                    live_root[subject] = live_value[flow]
            else:  # ALLOC
                live_value[flow] = _Def(
                    event=item, event_id=event_id, position=position,
                    null_source=False, is_alloc=True,
                )
                live_root[subject] = live_value[flow]
            continue

        if event_type == "RELEASE" and subject:
            flow = _flow_key(info)
            live_free[flow] = _Def(
                event=item, event_id=event_id, position=position,
                null_source=False, is_alloc=False,
            )
            killed = live_value.get(flow)
            if killed is not None and killed.is_alloc:
                del live_value[flow]
                if live_root.get(subject) is killed:
                    del live_root[subject]
            continue

        if event_type in USE_EVENTS and subject:
            flow = _flow_key(info)
            value_def = live_value.get(flow)
            free_def = live_free.get(flow)
            source_def = None
            if value_def is not None and free_def is not None:
                source_def = free_def if free_def.position > value_def.position else value_def
            elif free_def is not None:
                source_def = free_def
            elif value_def is not None:
                source_def = value_def
            if source_def is not None:
                root_def = _root_def(source_def)
                if source_def is free_def:
                    flow_class = CLASS_FREE_TO_USE
                elif root_def.null_source and event_type == "DEREFERENCE":
                    flow_class = CLASS_NULL_TO_DEREF
                elif root_def.is_alloc:
                    flow_class = CLASS_ALLOC_TO_USE
                else:
                    flow_class = CLASS_VALUE_FLOW
                pending.append((root_def.event_id, event_id, flow_class,
                                _via(annotations, id_by_key, method, source_def,
                                     position, flow), ""))
            continue

        if event_type == "CALL" and subject:
            arguments = [name for name in info.get("arguments", "").split(ARGUMENT_SEPARATOR) if name]
            for argument in dict.fromkeys(arguments):
                source_def = live_root.get(argument)
                if source_def is None:
                    continue
                pending.append((_root_def(source_def).event_id, event_id,
                                CLASS_INPUT_TO_SINK,
                                _via(annotations, id_by_key, method, source_def, position,
                                     _flow_key(annotations.get(_key_of(source_def.event), {}))),
                                argument))
            continue

    if pending:
        targets: dict[str, set[str]] = {}
        for item in pending:
            targets.setdefault(item[0], set()).add(item[1])
        reach = _reachability(parsed, id_by_key, targets.keys(), targets)
        for src_id, dst_id, flow_class, via_tail, arg_root in pending:
            _mint(repo_id, owner_id, file_id, src_id, dst_id, flow_class,
                  via_tail=via_tail, edges=edges, relative_path=relative_path,
                  shard_path=shard_path, reach=reach, arg_root=arg_root)

    edges.update(_mint_consumes(repo_id, owner_id, method, annotations, id_by_key, file_id,
                                relative_path, shard_path))
    return edges


def _via(
    annotations: dict,
    id_by_key: dict[PointKey, str],
    method: list,
    source_def: _Def,
    use_position: tuple,
    flow: tuple,
) -> tuple[list[str], list[str]]:
    """The intermediate sites between a definition and its use, recoverable.

    The alias copy sites come first, oldest to newest -- each is itself
    evidence that the value crossed a name -- then the same-identity events
    that sit between the newest definition and the use. Intermediates within
    an earlier alias hop's own span are not enumerated (registered); they
    are recoverable from the positions the ids carry.
    """
    via: list[str] = []
    copies: list[str] = []
    hop = source_def
    while hop is not None:
        if hop.alias_parent is not None:
            copies.append(hop.event_id)
            via.append(hop.event_id)
        hop = hop.alias_parent
    via.reverse()
    copies.reverse()
    newest = source_def
    start = newest.position
    # The method list is position-sorted (mint_dataflow_edges sorts it), so
    # the position window is a slice, not a scan: bisect_right lands past
    # every position equal to `start`, bisect_left before every position
    # equal to the use -- the same strict inequalities the linear filter
    # applied, in the same order.
    left = bisect_right(method, start, key=_position)
    right = bisect_left(method, use_position, key=_position)
    for item in method[left:right]:
        if _flow_key(annotations.get(_key_of(item), {})) != flow:
            continue
        candidate = id_by_key.get(_key_of(item))
        if candidate is not None and candidate not in via:
            via.append(candidate)
    return via, copies


def _mint(
    repo_id: str,
    owner_id: str,
    file_id: str,
    src_id: str,
    dst_id: str,
    flow_class: str,
    *,
    via_tail: tuple[list[str], list[str]],
    edges: dict[str, SemanticEdge],
    relative_path: str,
    shard_path: str,
    reach: dict[str, set[str]],
    arg_root: str = "",
) -> None:
    via, copies = via_tail
    reachable = dst_id in reach.get(src_id, ())
    metadata = {
        "relative_path": relative_path, "shard_path": shard_path,
        "flow_class": flow_class,
        "via": via, "via_copies": copies,
        "control_reachable": "true" if reachable else "false",
    }
    if arg_root:
        metadata["arg_root"] = arg_root
    edge = SemanticEdge(
        edge_id=semantic_edge_id(src_id, dst_id, DATA_FLOW_TO, ""),
        src_event_id=src_id,
        dst_event_id=dst_id,
        relation=DATA_FLOW_TO,
        owner_symbol_id=owner_id,
        file_id=file_id,
        flags=(),
        metadata=metadata,
    )
    edges.setdefault(edge.edge_id, edge)


def _mint_consumes(
    repo_id: str,
    owner_id: str,
    method: list,
    annotations: dict,
    id_by_key: dict[PointKey, str],
    file_id: str,
    relative_path: str,
    shard_path: str,
) -> dict[str, SemanticEdge]:
    """CONSUMES from 5C's `passed_to` fact: ALLOC -> same-line CALL.

    The fact says the allocation went into a call in its own acquiring
    expression; the edge names that call. A CALL row carries no callee name
    (`matched_name` is the node type, the callee lives in references), so
    the fact cannot be joined by name -- the target is positional: a
    same-line CALL whose argument roots include the allocation's subject
    root, the same root list the input_to_sink walk joins on. Registered
    approximations: a macro-compressed line with several such calls mints to
    each, and the callee the fact names is trusted, not re-checked. Zero
    corpus rows are expected where no allocation is passed through
    (redis-50 measured 0) -- the relation ships with fixture coverage, not
    with speculative volume.
    """
    edges: dict[str, SemanticEdge] = {}
    calls = [
        item for item in method
        if _field_of(item, "event_type") == "CALL"
    ]
    for item in method:
        if _field_of(item, "event_type") != "ALLOC":
            continue
        item_anns = annotations.get(_key_of(item), {})
        passed_to = item_anns.get("passed_to", "")
        root = item_anns.get("subject", "")
        if not passed_to or not root:
            continue
        src_id = id_by_key[_key_of(item)]
        for call in calls:
            call_anns = annotations.get(_key_of(call), {})
            arguments = [
                name for name in call_anns.get("arguments", "").split(ARGUMENT_SEPARATOR)
                if name
            ]
            if root not in arguments or _position(call)[0] != _position(item)[0]:
                continue
            dst_id = id_by_key[_key_of(call)]
            edge = SemanticEdge(
                edge_id=semantic_edge_id(src_id, dst_id, CONSUMES, ""),
                src_event_id=src_id,
                dst_event_id=dst_id,
                relation=CONSUMES,
                owner_symbol_id=owner_id,
                file_id=file_id,
                flags=(),
                metadata={
                    "relative_path": relative_path, "shard_path": shard_path,
                    "flow_class": CLASS_INPUT_TO_SINK, "arg_root": root,
                    "via": [], "via_copies": [],
                    "control_reachable": "true",
                },
            )
            edges.setdefault(edge.edge_id, edge)
    return edges


def _reachability(
    parsed: Any,
    id_by_key: dict[PointKey, str],
    sources: Iterable[str],
    targets: dict[str, set[str]],
) -> dict[str, set[str]]:
    """Per-source control reachability over the CFG's own edges, by id.

    Only the requested sources are BFS'd: the distinct def event ids that
    minted edges, which are far fewer than the CFG's node count. Each BFS
    stops once every pending destination of its source is seen -- the map is
    only ever queried for pending pairs, so a truncated `seen` answers them
    exactly as the full closure would (an unreachable target never empties
    `remaining`, so that BFS still runs to exhaustion). A source with no
    outgoing row (a def at the method's end) is simply absent from the map,
    which the mint reads as unreachable. The CFG's edges are all
    CONTROL_REACHES by construction (the publish block is their only
    writer), so no relation filter is needed here -- this runs before
    anything is published. Synthetic exits have no row and cannot be a
    def's target; they are simply unresolvable and skipped.
    """
    if parsed.cfg is None:
        return {}
    adjacency: dict[str, list[str]] = {}
    for edge in parsed.cfg.edges:
        src_id = id_by_key.get(edge.src)
        if src_id is None:
            continue
        dst_id = id_by_key.get(edge.dst) if edge.dst is not None else None
        if dst_id is None:
            continue
        adjacency.setdefault(src_id, []).append(dst_id)
    reach: dict[str, set[str]] = {}
    for src_id in sources:
        if src_id not in adjacency:
            continue
        remaining = set(targets.get(src_id, ()))
        seen: set[str] = set()
        queue = deque([src_id])
        while queue and remaining:
            current = queue.popleft()
            for nxt in adjacency.get(current, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    remaining.discard(nxt)
                    queue.append(nxt)
        reach[src_id] = seen
    return reach


# ----------------------------------------------------------------------
# stage 5B: the query-time parameter join


@dataclass(frozen=True, slots=True)
class ParameterBindingTable:
    """What a caller's argument root and a callee's parameter row share.

    `bindings` maps a parameter-storage WRITE point id to every record that
    composes it: one per call site that passed the argument at the ordinal
    the callee's signature gives the parameter. The record carries the
    caller-side root facts raw -- the composing identity is built by the
    defect layer, which owns `_identity`; this table never assembles one.
    """

    bindings: dict[str, tuple[dict[str, Any], ...]]
    coverage: dict[str, int]


def bind_parameters(
    points: Mapping[str, Mapping[str, Any]],
    *,
    callees: Any,
    signatures: Mapping[str, str] | None = None,
    names: Mapping[str, str] | None = None,
) -> ParameterBindingTable:
    """Join caller arguments to callee parameter rows, without new edges.

    The 5B parameter WRITE rows exist so a callee's write side is in the
    graph; this table is what lets a query compose them with the caller's
    side of the same object. For every CALL point whose callee is known and
    whose short name has one agreed parameter list, each argument root is
    joined at its ordinal to the parameter name the signature gives, and
    every parameter-storage WRITE of that name in the callee's own method
    that goes *through* the parameter records the pair.

    Only through-writes compose: a write with a member path (`p->flags`,
    `cells[0]`) or a whole-object dereference (`*out = v`, read off the
    left-hand spelling) reaches the caller's object, while a bare write to
    the parameter slot rebinds the callee's own name and, parameters being
    by-value, never touches the caller. Composing bare slot writes was the
    first draft's over-claim and the probe caught it at +13k manufactured
    candidates -- the 4.5 frame-local discipline, kept at the cross-method
    boundary. Registered: a parenthesized deref `(*p) = v` does not compose
    either (the left-hand spelling hides the deref), and a C++ reference
    parameter's bare write is a true through-write this rule declines.

    `callees` is the assembled contract table -- the callee join lives there
    (`ContractTable.callee` answers a point, unknown stays `""`). The join is
    registered as name-level throughout: the callee is matched by short
    name, and only when every symbol spelling that way agrees on its
    parameter list; a disagreement stays unbound rather than picking a
    winner, the same discipline as the contracts join. Unnamed parameters
    shift the ordinal mapping invisibly (`parameters` filters them), which
    is the registered approximation this table inherits.
    """
    signatures = signatures or {}
    names = names or {}

    # (owner, subject) -> point ids in id order. Both halves read through
    # this index: the caller's facts about an argument root, and the
    # callee's parameter rows about the joined name.
    by_owner_subject: dict[tuple[str, str], list[str]] = {}
    for point_id, info in sorted(points.items()):
        owner = str(info.get("owner_symbol_id", ""))
        subject = info.get("subject", "")
        if owner and subject:
            by_owner_subject.setdefault((owner, subject), []).append(point_id)

    param_lists: dict[str, set[tuple[str, ...]]] = {}
    ids_by_short: dict[str, list[str]] = {}
    for symbol_id, qualified in names.items():
        signature = signatures.get(symbol_id)
        if not signature:
            continue
        short = short_name(qualified)
        ids_by_short.setdefault(short, []).append(symbol_id)
        param_lists.setdefault(short, set()).add(_split_parameters(signature))
    agreed = {
        short: next(iter(lists))
        for short, lists in param_lists.items() if len(lists) == 1
    }

    bindings: dict[str, list[dict[str, Any]]] = {}
    coverage: dict[str, int] = {}
    counted = Counter()

    def note(name: str, amount: int = 1) -> None:
        counted[name] += amount

    for point_id, info in sorted(points.items()):
        if info["event_type"] != "CALL":
            continue
        note("calls_seen")
        callee = callees.callee(info)
        if not callee:
            note("calls_without_callee")
            continue
        param_list = agreed.get(short_name(callee))
        if param_list is None:
            note("callees_without_agreed_signature")
            continue
        arguments = [
            name for name in info.get("arguments", "").split(ARGUMENT_SEPARATOR) if name
        ]
        caller_owner = str(info.get("owner_symbol_id", ""))
        for ordinal, arg_root in enumerate(arguments):
            note("arguments_seen")
            if ordinal >= len(param_list):
                note("arguments_beyond_arity")
                continue
            parameter = param_list[ordinal]
            if not parameter:
                note("arguments_unnamed_parameter")
                continue
            # The caller's facts about the root, best first: a declared
            # identity outranks the spelling (`_by_root`, the weaker claim
            # stage 4.5 registered), and the member path is stripped -- the
            # composed identity adds the callee row's own member.
            declared = None
            first = None
            for candidate_id in by_owner_subject.get((caller_owner, arg_root), ()):
                candidate = points[candidate_id]
                if first is None:
                    first = candidate
                if candidate.get("subject_decl"):
                    declared = candidate
                    break
            root = declared if declared is not None else first
            join = "declared" if declared is not None else "spelling"
            if root is None:
                note("arguments_without_caller_facts")
                continue
            facts = {
                "subject_decl": root.get("subject_decl", ""),
                "subject_storage": root.get("subject_storage", ""),
                "subject_type": root.get("subject_type", ""),
                "subject": root.get("subject", "") or arg_root,
                "subject_site": root.get("subject_site", ""),
                "subject_member": "",
                "owner_symbol_id": root.get("owner_symbol_id", ""),
            }
            bound = 0
            for callee_id in ids_by_short.get(short_name(callee), ()):
                for write_id in by_owner_subject.get((callee_id, parameter), ()):
                    row = points[write_id]
                    if row.get("subject_storage") != "parameter":
                        continue
                    member = row.get("subject_member", "")
                    if not member and not str(row.get("matched_name", "")).lstrip("(").startswith("*"):
                        note("parameter_slot_writes_skipped")
                        continue
                    bindings.setdefault(write_id, []).append({
                        "callee": callee,
                        "ordinal": ordinal,
                        "parameter": parameter,
                        "arg_root": arg_root,
                        "join": join,
                        "caller_owner": caller_owner,
                        "caller_facts": facts,
                        "call_site": point_id,
                    })
                    bound += 1
            if bound:
                note("arguments_bound")
                note(f"join_{join}")
            else:
                note("arguments_without_parameter_writes")

    coverage.update(sorted(counted.items()))
    return ParameterBindingTable(
        bindings={key: tuple(records) for key, records in bindings.items()},
        coverage=coverage,
    )
