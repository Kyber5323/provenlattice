"""Level-0 semantic event materializer (I1-A) over the derived-analysis store.

Ported from PR #1's semantic layer, re-shaped for main's architecture:

* Events are extracted **on demand only** -- the default index never runs the
  extractor. The analyzer follows the same shape as the C CFG analyzer:
  scope-driven, file x analyzer cached in AnalysisStore, fail-closed
  freshness.
* Owner binding reuses main's L1 symbol identity: every tree-sitter
  ``function_definition`` span is aligned against the base graph's
  Function/Method symbols (same-line collisions disambiguated by the
  definition flag, then by name -- the I0R1 rule); the helpers are shared
  with the sparse_cfg analyzer (analysis_semantic_common).
* Level scope: vocabulary events only (CALL/RETURN/THROW/ALLOC/RELEASE/
  LOCK/UNLOCK/ATOMIC/THREAD_SPAWN/THREAD_JOIN). The structural
  CHECK/BRANCH/DEREFERENCE and READ/WRITE access events are sparse-CFG-walker
  products and live in the sparse_cfg analyzer (I1-B). The capability is
  therefore registered as ``partially_implemented`` (level 0), never as fully
  implemented.
* One analyzer covers the whole C/C++ family: the vocabulary table
  (``semantics/vocabularies/cpp.toml``) names the extensions, and the parse
  uses the C++ grammar for every covered file, matching the PR extraction the
  vocabulary was written against.
"""

from __future__ import annotations

import time
from pathlib import Path

from .analysis import (
    ANALYZER_VERSIONS,
    AnalysisNode,
    AnalysisStore,
    PayloadTooLargeError,
    analyzer_contract,
    analyzer_id,
)
from .analysis_ast import normalize_scope, scoped_sources
from .analysis_semantic_common import (
    _LineIndex,
    owner_index,
    owner_symbol_id,
    tree_diagnostics,
)
from .identity import event_id, path_id
from .parsing.cpp import CppTreeSitterParser
from .parsing.events import EventExtractor
from .semantics.vocabulary import load_vocabulary
# Read-only reuse of the sparse_cfg walker's subject-recovery convention
# (module-level pure helpers): the events layer records the same lock "root"
# identifier and "exact" judgement the walker already computes, so the two
# layers agree instead of drifting.
from .sparsecfg import _first_call_argument, _names_the_object, _root_identifier
from .storage import SQLiteStorage

ANALYZER = analyzer_id("events", "cpp")
EVENTS_VERSION = ANALYZER_VERSIONS[ANALYZER]
EVENTS_CONTRACT = analyzer_contract("events", "cpp", EVENTS_VERSION)
VOCABULARY = load_vocabulary("cpp")
EVENTS_SUFFIXES = frozenset(VOCABULARY.extensions)
CAPABILITY_LEVEL = "level0"

# Acquire-kind classification for the LOCK/UNLOCK vocabulary. A lock acquisition
# can block (the common case), fail immediately (`try_lock`), fail after a
# timeout (`timed_lock`), or be a blocking wait on a condition/semaphore rather
# than a lock proper. The split lives here as data over the vocabulary's own
# callee names -- the same `matched_name` the event already records -- so it is
# one fact, computed once, and never a second registry. Names that are not in
# any of the three sets are blocking acquisitions.
ACQUIRE_KIND_TRY = "try"
ACQUIRE_KIND_TIMED = "timed"
ACQUIRE_KIND_WAIT = "wait"
ACQUIRE_KIND_BLOCKING = "blocking"

_ACQUIRE_NAME_TRY = frozenset({
    "pthread_mutex_trylock", "pthread_rwlock_tryrdlock", "pthread_rwlock_trywrlock",
    "pthread_spin_trylock", "sem_trywait", "mtx_trylock",
    "TryEnterCriticalSection", "try_lock", "try_lock_shared",
})
_ACQUIRE_NAME_TIMED = frozenset({
    "pthread_mutex_timedlock", "sem_timedwait",
})
_ACQUIRE_NAME_WAIT = frozenset({
    "pthread_cond_wait", "pthread_cond_timedwait", "cnd_wait",
    "sem_wait", "WaitForSingleObject", "WaitForMultipleObjects",
})


def _acquire_kind(matched_name: str) -> str:
    if matched_name in _ACQUIRE_NAME_TRY:
        return ACQUIRE_KIND_TRY
    if matched_name in _ACQUIRE_NAME_TIMED:
        return ACQUIRE_KIND_TIMED
    if matched_name in _ACQUIRE_NAME_WAIT:
        return ACQUIRE_KIND_WAIT
    return ACQUIRE_KIND_BLOCKING


def _node_by_span(root) -> dict:
    """Map every named node's byte span to the node, for subject recovery.

    The event extractor returns hits keyed by the byte range of the node that
    matched; this index answers "which node was that" so a LOCK/UNLOCK hit can
    recover its subject without a second matcher implementation.
    """
    index: dict[tuple[int, int], object] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        index[(node.start_byte, node.end_byte)] = node
        stack.extend(node.named_children)
    return index


# Matrix 2.1/2.2's shape -- the multiplication inside an allocation's size
# argument -- is extracted by the sparse CFG walker, which is the feature the
# defect queries read. It lives there (`sparsecfg.size_arithmetic`) rather than
# here because an ARITH point is a graph fact about a statement, and the events
# layer publishes vocabulary hits, not statement points.


def _single_declarator(node) -> object | None:
    """The declaration's one declarator, or None when identity is unclear.

    `lock_guard<std::mutex> a{m}, b{n};` acquires twice; reporting one argument
    for both would be a guess about which handle a lock point refers to.
    """
    declarators = list(node.children_by_field_name("declarator"))
    if len(declarators) != 1:
        return None
    return declarators[0]


def _mmp_argument_node(declarator) -> object | None:
    """The argument of a most-vexing-parse declaration, or None.

    `guard(m)` and `lk(m, defer_lock)` spell their constructor arguments as a
    parameter list; the first parameter that is not a function type is the
    argument, read back through its `type` node.
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


def _declaration_argument_node(node) -> object | None:
    """The expression a RAII declaration locks, not its handle.

    `lock_guard<std::mutex> guard(g_pool_lock);` locks `g_pool_lock`; the name
    `guard` is a local handle worthless to a lock query. Mirrors the
    sparse_cfg walker's read of the two tree-sitter shapes (init_declarator
    value and the most-vexing-parse function_declarator).
    """
    declarator = _single_declarator(node)
    if declarator is None:
        return None
    if declarator.type == "function_declarator":
        return _mmp_argument_node(declarator)
    if declarator.type != "init_declarator":
        return None
    value = declarator.child_by_field_name("value")
    if value is None:
        return None
    if value.type in {"initializer_list", "argument_list"}:
        value = value.named_children[0] if value.named_children else None
    return value


def _call_receiver_node(node) -> object | None:
    """The object a member lock call is made on (`m.lock()` -> `m`)."""
    callee = node.child_by_field_name("function")
    if callee is None or callee.type != "field_expression":
        return None
    return callee.child_by_field_name("argument")


def _lock_subject(source: bytes, node, event_type: str) -> tuple[str, bool]:
    """The root identifier of the object a LOCK/UNLOCK acts on, and exactness.

    Same order the sparse_cfg walker applies: a RAII declaration names its
    constructor argument; a free-function call names its first argument
    (`pthread_mutex_lock(&m)` -> `m`); a member call names its receiver
    (`m.lock()` -> `m`). ``exact`` is False when the name is only the root of a
    longer designation (``index->slot_locks[i]`` names `index`, not one lock).
    """
    if event_type in {"LOCK", "UNLOCK"} and node.type == "declaration":
        argument = _declaration_argument_node(node)
        return _root_identifier(source, argument), _names_the_object(argument)
    argument = _first_call_argument(node)
    if argument is not None:
        return _root_identifier(source, argument), _names_the_object(argument)
    receiver = _call_receiver_node(node)
    if receiver is not None:
        return _root_identifier(source, receiver), _names_the_object(receiver)
    return "", False


def _raii_scope_end(node) -> int | None:
    """The byte offset where a RAII declaration's guard is released.

    A RAII guard lives until its innermost lexical block scope closes: the
    nearest enclosing ``compound_statement`` (a bare block or a function body).
    Returns that statement's end byte -- the first offset the guard is no
    longer held -- or ``None`` when there is no enclosing block (a namespace/
    class-scope declaration, which is not a local acquisition).

    ``node`` is the ``declaration`` node the LOCK event was matched on; the walk
    is lexical only, so it never guesses about control flow or exception
    unwinding -- the destructor runs on every exit path, which is exactly what
    makes the scope a deterministic fact.
    """
    while node is not None:
        node = node.parent
        if node is not None and node.type == "compound_statement":
            return node.end_byte
    return None


def _lock_metadata(source: bytes, node, event_type: str, matched_name: str) -> dict:
    """The lock facts a LOCK/UNLOCK event carries; empty for other events.

    Kept empty for non-lock events so their payloads stay byte-identical to the
    pre-lock-semantics baseline.
    """
    if event_type not in {"LOCK", "UNLOCK"}:
        return {}
    raii = event_type == "LOCK" and node is not None and node.type == "declaration"
    if node is None:
        return {
            "lock_identity": "",
            "subject_exact": False,
            "acquire_kind": _acquire_kind(matched_name),
            "raii": raii,
        }
    identity, exact = _lock_subject(source, node, event_type)
    payload = {
        "lock_identity": identity,
        "subject_exact": exact,
        "acquire_kind": _acquire_kind(matched_name),
        "raii": raii,
    }
    if raii:
        # Issue #69: the guard's lexical scope end, so a consumer can hold the
        # lock only from the declaration to this byte and not beyond it. Absent
        # (null) when no enclosing block exists -- a degenerate, non-local
        # declaration that is not a real acquisition.
        payload["raii_scope_end"] = _raii_scope_end(node)
    return payload


def analyze_c_events_scope(
    repo: str | Path, scope: str, database: str | Path | None = None
) -> dict:
    started = time.perf_counter()
    root = Path(repo).resolve()
    scope_path = normalize_scope(scope)
    if not (root.joinpath(*scope_path.parts)).is_dir():
        raise ValueError(f"scope directory does not exist: {scope}")
    database = Path(database).resolve() if database else root / ".provenlattice" / "codegraph.db"
    scoped = scoped_sources(root, scope_path)
    materialized: list[str] = []
    reused: list[str] = []
    skipped_unsupported: list[str] = []
    skipped_not_in_base: list[str] = []
    skipped_base_out_of_date: list[str] = []
    skipped_no_events: list[str] = []
    skipped_payload_too_large: list[str] = []
    counters = {
        "events_extracted": 0, "events_outside_owner": 0,
        "functions_bound": 0, "functions_unbound": 0,
    }
    nodes_written = 0
    parser = CppTreeSitterParser(language="cpp")
    with SQLiteStorage(database) as storage:
        repository = storage.repository()
        if not repository:
            raise RuntimeError("repository is not indexed; run `provenlattice index` first")
        repo_id = repository["repo_id"]
        store = AnalysisStore(storage)
        # One transaction for the whole file loop instead of one commit per
        # file: the storage scope commits on exit, and a failure rolls the scope
        # back. See AnalysisStore.defer_commit for the measurement.
        store.defer_commit()
        for source in scoped:
            if Path(source.relative_path).suffix.lower() not in EVENTS_SUFFIXES:
                skipped_unsupported.append(source.relative_path)
                continue
            file_id = path_id(repo_id, "File", source.relative_path)
            base_hash = store.base_source_hash(file_id)
            if base_hash is None:
                skipped_not_in_base.append(source.relative_path)
                continue
            if base_hash != source.source_hash:
                skipped_base_out_of_date.append(source.relative_path)
                continue
            state = store.get_analysis_state(file_id, ANALYZER)
            if (
                state is not None
                and state.get("metadata", {}).get("contract") == EVENTS_CONTRACT
                and state.get("metadata", {}).get("analyzer") == ANALYZER
                and store.is_analysis_fresh(file_id, ANALYZER, base_hash, EVENTS_VERSION)
            ):
                reused.append(source.relative_path)
                continue
            source_bytes = source.path.read_bytes()
            outcome = parser.parse_bytes(source_bytes, source.relative_path)
            lines = _LineIndex(source_bytes)
            owners, owner_counters, owner_ids = owner_index(
                store, file_id, outcome.tree.root_node, source_bytes)
            extractor = EventExtractor(source_bytes, VOCABULARY, owners, lines.line_of)
            hits = extractor.extract_hits(outcome.tree.root_node)
            index = _node_by_span(outcome.tree.root_node)
            nodes: list[AnalysisNode] = []
            for hit in hits:
                payload = {
                    "owner_symbol_id": owner_symbol_id(owner_ids, hit.event),
                    "owner_kind": hit.event.owner_kind,
                    "owner_qualified_name": hit.event.owner_qualified_name,
                    "owner_signature": hit.event.owner_signature,
                    "ordinal": hit.event.ordinal,
                    "start_byte": hit.start_byte, "end_byte": hit.end_byte,
                    "start_line": hit.event.start_line,
                    "end_line": hit.event.end_line,
                    "matched_name": hit.event.matched_name,
                    "matched_via": hit.event.matched_via,
                    "flags": list(hit.event.flags),
                    "analyzer": ANALYZER, "contract": EVENTS_CONTRACT,
                    "language": VOCABULARY.language, "level": CAPABILITY_LEVEL,
                }
                payload.update(_lock_metadata(
                    source_bytes,
                    index.get((hit.start_byte, hit.end_byte)),
                    hit.event.event_type,
                    hit.event.matched_name,
                ))
                nodes.append(AnalysisNode(
                    event_id(
                        repo_id,
                        owner_symbol_id(owner_ids, hit.event),
                        hit.event.event_type,
                        hit.event.ordinal,
                    ),
                    hit.event.event_type,
                    payload,
                ))
            diagnostics = tree_diagnostics(outcome.tree.root_node)
            try:
                store.replace_analysis_for_file(
                    file_id, ANALYZER, base_hash, EVENTS_VERSION, nodes, [],
                    metadata={
                        "analyzer": ANALYZER, "contract": EVENTS_CONTRACT,
                        "language": VOCABULARY.language, "level": CAPABILITY_LEVEL,
                        "scope": scope_path.as_posix(),
                        "event_count": len(nodes),
                        "events_outside_owner": extractor.skipped_outside_owner,
                        **owner_counters,
                        **diagnostics,
                        "analysis_status": "degraded" if diagnostics["partial"] else "complete",
                    },
                )
            except PayloadTooLargeError:
                skipped_payload_too_large.append(source.relative_path)
                continue
            if not nodes:
                # A file with zero level-0 events is a legitimate outcome, and
                # the empty state is persisted so the freshness contract (not
                # repeated extraction) answers repeat requests.
                skipped_no_events.append(source.relative_path)
            materialized.append(source.relative_path)
            counters["events_extracted"] += len(nodes)
            counters["events_outside_owner"] += extractor.skipped_outside_owner
            counters["functions_bound"] += owner_counters["functions_bound"]
            counters["functions_unbound"] += owner_counters["functions_unbound"]
            nodes_written += len(nodes)
    return {
        "repo_id": repo_id,
        "database": str(database),
        "scope": scope_path.as_posix(),
        "analyzer": ANALYZER,
        "contract": EVENTS_CONTRACT,
        "feature_version": EVENTS_VERSION,
        "level": CAPABILITY_LEVEL,
        "scope_source_files": len(scoped),
        "materialized": sorted(materialized),
        "reused": sorted(reused),
        "skipped_unsupported": sorted(skipped_unsupported),
        "skipped_not_in_base": sorted(skipped_not_in_base),
        "skipped_base_out_of_date": sorted(skipped_base_out_of_date),
        "skipped_no_events": sorted(skipped_no_events),
        "skipped_payload_too_large": sorted(skipped_payload_too_large),
        "events_extracted": counters["events_extracted"],
        "events_outside_owner": counters["events_outside_owner"],
        "functions_bound": counters["functions_bound"],
        "functions_unbound": counters["functions_unbound"],
        "nodes_written": nodes_written,
        "analyze_time_ms": (time.perf_counter() - started) * 1000,
    }
