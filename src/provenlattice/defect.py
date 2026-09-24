"""Typed defect queries over the sparse semantic graph (stage 4, appendix B.4).

Three queries answer the three patterns the matrix grades A and the stage 3
graph can feed today: `resource_lifetime` (4.1 leaks and the lock half of 4.6),
`lock_order` (1.3 inversions and 1.4 self-deadlock) and `race_condition` (1.1).

Everything here is a pure function over the two published tables --
`(events, edges)` in, `DefectQueryResult` out -- exactly like the pattern
functions in `sparsecfg`. No tree-sitter, no SQLite, no repository. That is
what lets the acceptance harness drive the queries from a read-only connection
and lets the tests drive them from objects with no database at all.

**The three-state contract.** `resolution_status` is what the *graph* resolved
about a decisive relation, and it is never upgraded silently:

* `resolved`   -- every relation the decisive predicate needs is in the graph,
                  and no subject on the decisive path is unresolved;
* `ambiguous`  -- the predicate holds on the graph, but at least one input is a
                  name-level proxy rather than a resolved identity;
* `unresolved` -- the predicate holds but a required identity is empty, so the
                  candidate cannot be attributed at all.

None of these is a verdict about the code. A model's `confirmed` / `likely` /
`insufficient evidence` / `rejected` lives only in the review sheet's
annotation block; a query that produced a `resolved` candidate has decided
nothing about whether the defect is real.

**What this stage deliberately does not do.** READ and WRITE events do not
exist (level 1, awaiting the DFG), so `race_condition` classifies accesses by a
syntactic proxy and can never resolve MAY_PARALLEL; its candidates are all
`ambiguous` by construction, and a shared variable that never appears in an
event-bearing expression (`counter++`) produces no candidate at all. Lock
identity is a name, so a member lock (`m.lock()`) can never resolve to an
object. Every one of those limits is stated in the candidate's own
`missing_evidence`, not just in this docstring.
"""

from __future__ import annotations

import bisect
import inspect
import tomllib
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Collection, Iterable, Mapping, Sequence

from .contracts import FUNCTION_STATIC_STORAGE, ContractTable
from .evidence import DefectEvidenceBundle, Evidence
from .models import (
    STORAGE_FIELD, STORAGE_FILE_STATIC, STORAGE_FUNCTION_STATIC, STORAGE_GLOBAL,
    STORAGE_LOCAL, STORAGE_PARAMETER, STORAGE_UNKNOWN,
)
from .semantics.vocabulary import load_defect_patterns
from .sparsecfg import (
    ARGUMENT_SEPARATOR,
    BRANCH_FALSE,
    BRANCH_TRUE,
    SUBJECT_FROM_ASSIGNMENT,
    ControlPath,
    build_adjacency,
    find_paths,
    materialize_points,
    unreleased_resources,
)
# `_flow_key` is the data-flow graph's own notion of "the same object", and the
# stage 6 queries ask the same question it answers: which definition kills which
# live value. It is imported rather than re-derived from `_FLOW_FIELDS` because
# two definitions of one identity drift, and the drift would show up as a
# query that silently stops finding an intervening definition.
from .sparsedfg import (
    DATA_FLOW_TO,
    DEF_EVENTS,
    USE_EVENTS,
    ParameterBindingTable,
    _flow_key,
)

RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"
UNRESOLVED = "unresolved"

# How many elimination records travel in the coverage block. The *count* is
# never capped -- only the records -- because a coverage block that grows with
# the finding count stops being a coverage block.
#
# Set high enough not to bind on the acceptance repositories: llama.cpp's 742
# candidates eliminate on the order of 120, and the acceptance protocol draws
# its adjudication sample from the full list, so a cap of twenty would silently
# truncate the sampling frame. It is still a bound, so a million-line repository
# cannot put an unbounded list into a coverage block.
DISQUALIFIED_EVIDENCE_CAP = 500

RESOURCE_LIFETIME = "resource_lifetime"
LOCK_ORDER = "lock_order"
RACE_CONDITION = "race_condition"
# Stage 6's four DFG-consuming queries. Each answers one matrix key except
# `taint_path`, which answers none (see `TAINT_KEY`).
NULL_FLOW = "null_flow"
USE_AFTER_FREE = "use_after_free"
DOUBLE_FREE = "double_free"
TAINT_PATH = "taint_path"
ERROR_HANDLING = "error_handling"
QUERY_NAMES = (
    RESOURCE_LIFETIME, LOCK_ORDER, RACE_CONDITION,
    NULL_FLOW, USE_AFTER_FREE, DOUBLE_FREE, TAINT_PATH, ERROR_HANDLING,
)

# The matrix keys the stage 6 queries answer. `taint_path` has no matrix family:
# `defect-evidence-matrix-v1.md` puts a taint analysis outside V2 (:286), and
# the roadmap asked for the registered minimum anyway. The key below is a
# *name* and not a matrix number, which is why `defect_patterns.toml` carries no
# row for it -- a key that looks like `3.1` and is not one would be worse than
# no key at all, so the query is addressed by its query name and nothing else.
NULL_FLOW_KEY = "3.1"
DOUBLE_FREE_KEY = "4.2"
USE_AFTER_FREE_KEY = "4.3"
TAINT_KEY = "taint"
ERROR_HANDLING_KEY = "9.1"

# Confidence is a *band*, not a measurement: it says which of the three states
# the candidate landed in and how much of the pattern's decisive predicate the
# graph could answer, nothing more. Two runs of the same query give the same
# number because the number is a lookup, not an estimate.
RESOURCE_CONFIDENCE = {RESOLVED: 0.6, AMBIGUOUS: 0.4, UNRESOLVED: 0.2}
LOCK_ORDER_CONFIDENCE = {RESOLVED: 0.7, AMBIGUOUS: 0.4}
SELF_DEADLOCK_CONFIDENCE = {RESOLVED: 0.6, AMBIGUOUS: 0.3}
RACE_CONFIDENCE = {0: 0.5, 1: 0.4, 2: 0.3}
# The stage 6 bands, and the taint band is deliberately the lowest in the file:
# a null flow and a double free are DFG facts with a CFG elimination test
# behind them, while a taint path is two declared names and a def-use edge with
# no sanitiser model at all. The number is not a measurement of how often the
# candidate is right; it is a statement of how much of the pattern's predicate
# the graph could answer, and for taint that is very little.
NULL_FLOW_CONFIDENCE = {RESOLVED: 0.5, AMBIGUOUS: 0.3, UNRESOLVED: 0.1}
USE_AFTER_FREE_CONFIDENCE = {RESOLVED: 0.5, AMBIGUOUS: 0.3, UNRESOLVED: 0.1}
DOUBLE_FREE_CONFIDENCE = {RESOLVED: 0.5, AMBIGUOUS: 0.3, UNRESOLVED: 0.1}
TAINT_CONFIDENCE = {RESOLVED: 0.3, AMBIGUOUS: 0.2}
# Two states, not three, and the absence of `unresolved` is a fact about the
# query rather than an omission: its source population is *defined* as the calls
# whose stored value has a resolved declaration, so there is no candidate left
# to call unattributable. The band is the second lowest in the file because the
# predicate is an approximation -- the graph cannot read `fd == -1`, so "was the
# result examined" is answered by "did control branch afterwards".
ERROR_HANDLING_CONFIDENCE = {RESOLVED: 0.4, AMBIGUOUS: 0.2}

# The flow classes stage 5B mints. Spelled as constants because three of the
# four queries select on them and a typo would silently return nothing.
NULL_TO_DEREF = "null_to_deref"
ALLOC_TO_USE = "alloc_to_use"
FREE_TO_USE = "free_to_use"
INPUT_TO_SINK = "input_to_sink"

# The two CHECK polarities. `polarity` is only ever written for a comparison
# whose other operand is the null literal (`sparsecfg` sets it nowhere else), so
# `fd == -1` and `ret != 0` are not CHECK points at all -- which is why the
# error-handling query cannot use this vocabulary and walks branches instead.
NULL_POLARITIES = frozenset({"true_when_null", "true_when_non_null"})

# The three answers a reachability question can have. `unknown` is the one that
# matters: a WRITE point has no position in the control-flow graph, so a flow
# edge sourced at one has nothing to search from, and every caller treats that
# as "cannot eliminate" rather than as "unreachable".
REACHABLE = "reachable"
DISJOINT = "disjoint"
REACH_UNKNOWN = "unknown"

# Acquisitions that can return without acquiring. Under one of these, "the lock
# is held on the error path" is not a fact the graph can assert, so any
# candidate that depends on it is capped at `ambiguous` rather than reported as
# resolved. Spelled out rather than pattern-matched on "try": `WaitForSingleObject`
# and `pthread_mutex_timedlock` belong here and contain no `try` at all.
NON_BLOCKING_ACQUIRES = frozenset({
    "pthread_mutex_trylock", "pthread_mutex_timedlock", "pthread_spin_trylock",
    "pthread_rwlock_tryrdlock", "pthread_rwlock_trywrlock",
    "mtx_trylock", "sem_trywait", "sem_timedwait",
    "TryEnterCriticalSection", "WaitForSingleObject", "WaitForMultipleObjects",
    "try_lock", "try_lock_shared",
})

# The declared types that are a mutex, as the last identifier of the spelling
# (`type_token` has already dropped the namespace and the template arguments, so
# `std::mutex` arrives as `mutex` and `std::shared_mutex` as `shared_mutex`).
# A set and not a substring test: `pool_mutex_holder` contains `mutex` and is
# not one, and a token that a name can accidentally contain is not evidence.
_MUTEX_TYPES = frozenset({
    "mutex", "recursive_mutex", "timed_mutex", "shared_mutex", "shared_timed_mutex",
    "pthread_mutex_t", "pthread_rwlock_t", "pthread_spinlock_t",
    "CRITICAL_SECTION", "SRWLOCK", "GMutex", "GRWLock",
})

# The declared types whose member named `lock` is *known* to be something else.
# Deliberately one entry: `std::weak_ptr::lock()` returns a `shared_ptr` and is
# the standard library's only same-named non-acquisition. The list stays this
# short because the failure directions are not symmetric -- excluding a type the
# graph merely has not seen as a mutex would drop genuine acquisitions, and a
# class that wraps a mutex behind `.lock()` is ordinary (`pool->lock()` where
# `pool` is `HNSW *` must not be excluded). `shared_ptr` and `unique_ptr` are
# not listed: they define no `lock` member at all, so a match on those spellings
# would be a user type, and a spelling is not a type.
_NON_LOCK_RECEIVER_TYPES = frozenset({"weak_ptr"})

# The event types whose subject can name shared *data*. LOCK/UNLOCK/ATOMIC are
# synchronization facilities rather than the data they guard, and are subtracted
# from this set rather than added to it.
RACE_SUBJECT_EVENTS = frozenset({"ALLOC", "RELEASE", "CALL", "DEREFERENCE", "CHECK", "RETURN"})
# The access events (stage 5A). They join the subject index -- an access with a
# subject is a touch of shared data like any other -- but never the protection
# test: `_blocked_by_lock` asks the control-flow graph whether a lock dominates
# an access, and an access event has no position in that graph (see
# `_EDGELESS_EVENT_TYPES` in `sparsecfg.py`). Two indexes per subject, two roles.
RACE_ACCESS_EVENTS = frozenset({"READ", "WRITE"})
SYNCHRONIZATION_EVENTS = frozenset({"LOCK", "UNLOCK", "ATOMIC"})
# C's thread-local objects (C11 §7.5): `errno` is a thread-local lvalue, so two
# methods touching it can never be a cross-method race no matter how the graph
# pairs them. Stage 5B's tuning ruling: exempted at *pairing* time only -- the
# access events stay in the graph (deleting true events would be lying about
# what the source does), the query just declines to pair on them. The count is
# reported, not hidden, and the list is closed pending evidence: the only other
# C/POSIX thread-local object is `h_errno`, which the corpora did not show.
_THREAD_LOCAL_NAMES = frozenset({"errno"})

_MISSING_CALLEE_CONTRACT = (
    "the ownership contract of a callee: whether a function the resource is "
    "passed to takes ownership, borrows it, or frees it"
)
_MISSING_RAII = (
    "RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no "
    "RELEASE in the graph, so every owning smart pointer reads as a leak"
)
# Retired in stage 4.5. The gap it registered -- `std::lock_guard<T> lock(m);`
# parsed as a function declaration and promoted to a symbol of its own, so only
# the brace spelling reached the graph with a lock identity -- was measured on a
# real C++ repository and is now closed at the source: the most-vexing-parse is
# demoted rather than promoted, and both spellings carry the mutex's identity.
# What remains of RAII recall is the guard-to-mutex binding below, which is a
# different gap with a different owner (the ownership stage).
_MISSING_GUARD_BINDING = (
    "the guard-to-mutex binding: `std::unique_lock<T> g(m)` releases through "
    "`g`, and `g.unlock()` is an operation on the guard, not on `m`, so a walk "
    "that joins on the mutex's identity cannot see the release"
)
_MISSING_ALIASING = (
    "aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and "
    "`p = realloc(p, n)` are all invisible without a data-flow graph"
)
_MISSING_RELEASE_IN_CALLEE = (
    "a release performed inside a callee: the graph only sees RELEASE points "
    "in the method it walked"
)
_MISSING_ERROR_PATH = (
    "whether the error path is reachable in practice: the graph reports the "
    "path exists, not that the input reaches it"
)
_MISSING_DESTRUCTOR = (
    "whether a destructor or another path releases the lock first"
)
_MISSING_RECURSIVE = (
    "whether the mutex is recursive: under PTHREAD_MUTEX_RECURSIVE a second "
    "acquisition by the same thread is legal, and the graph does not hold the "
    "mutex's type"
)
_MISSING_THREADS = (
    "the execution context: whether the two methods can run concurrently at "
    "all -- nothing in the graph orders them"
)
_MISSING_LOCK_ALIAS = (
    "lock aliasing: two expressions naming the same mutex (`&s->m` and `s->m`) "
    "are two names here, and one name is one lock"
)
_MISSING_TRY_PATH = (
    "the failure path of a non-blocking acquisition: a try-lock that fails "
    "leaves the lock unheld and the graph cannot tell the two arms apart"
)
_MISSING_CONCURRENCY = (
    "whether the two contexts really run concurrently: MAY_PARALLEL is "
    "undecidable from spawn points alone"
)
_MISSING_LOCK_GUARDS = (
    "whether the lock actually guards this variable: holding a lock near an "
    "access is not the same as the lock protecting the data"
)
_MISSING_WEAK_CONSISTENCY = (
    "whether the variable tolerates weak consistency (a statistics counter "
    "may be allowed to race by design)"
)
_MISSING_NULLABILITY = (
    "which calls can return NULL: the graph sees the allocation and the "
    "dereference, and whether this call's failure path is reachable at all is "
    "an API contract the AI side holds"
)
_MISSING_PARTIAL_GUARD = (
    "whether a guard covers every route to the dereference: the graph can say "
    "the null arm still reaches it, not whether some check upstream already "
    "excluded null on the paths that matter"
)
_MISSING_USE_KIND = (
    "whether the use reads the pointer value or the pointee: `if (p)` after a "
    "release compares a dangling value, and the graph does not separate the "
    "two readings"
)
_MISSING_FREE_WRAPPER = (
    "what a release wrapper does: whether it nulls the caller's pointer, "
    "whether it releases the whole object, and whether it releases at all"
)
_MISSING_NULL_CONVENTION = (
    "the repository's release convention: whether a release leaves the pointer "
    "dangling or nulls it, which decides whether the second release is a double "
    "free or a legal `free(NULL)`"
)
_MISSING_ALIAS_PRECISION = (
    "whether two names really address one object: the alias annotation is a "
    "same-statement copy fact, so a pair joined through it is a candidate and "
    "not an identity"
)
_MISSING_SANITIZER = (
    "the sanitiser model: a value that passed through a validation or escaping "
    "function is still reported, because the graph holds no relation for "
    "`this call made the value safe`"
)
_MISSING_SOURCE_TRUST = (
    "whether the source is attacker-controlled: the graph knows the bytes were "
    "not written by this program, not who wrote them -- a deployment question "
    "the query cannot answer"
)
# Matrix 9.1's own "AI-required Semantics" column says this one out loud: which
# APIs must have their return value checked is API semantics, not a graph fact.
# It is also what keeps the candidate population honest -- a call whose result
# is a pointer to a fresh object has no failure to check, and only the API
# contract can say which calls those are. The query's job is to put the *stored
# and unexamined* calls in front of a reader, not to decide which of them
# mattered.
_MISSING_MUST_CHECK = (
    "which APIs must have their result checked: the graph sees a call whose "
    "result was stored and not examined, and whether this call has a failure "
    "mode worth checking is API semantics the AI side holds"
)
_MISSING_DISCARDED_RESULT = (
    "whether a result was discarded outright: a CALL point records where a "
    "result went, never that it went nowhere, so `f();` as a bare statement is "
    "outside this query entirely -- and so is a result passed straight on as an "
    "argument, which no branch examines and no variable holds"
)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One defect candidate: a subject, the graph facts behind it, and the
    status the graph reached about them.

    `discriminator` is deterministic and carries no line number -- identity has
    to survive reformatting, because a candidate whose id moves when the file
    is reindented cannot be cited, diffed or adjudicated twice.

    Every entry in `uncertain_facts` carries a `decisive` flag, and the flag is
    what makes `resolution_status` checkable rather than rhetorical: a
    *decisive* fact that is ambiguous forces the candidate to be ambiguous too.
    A fact that is uncertain but not decisive does not -- MAY_PARALLEL is
    always undecidable and a lock-order inversion is still `resolved`, because
    concurrency is a separate question from whether the two orders exist. The
    distinction is the difference between "the graph could not answer what this
    candidate is about" and "the graph answered, and this is the part of the
    world it cannot speak to".
    """

    defect_key: str
    query: str
    subject: dict[str, Any]
    anchor: str
    discriminator: str
    resolution_status: str
    confidence: float
    facts: tuple[dict[str, Any], ...] = ()
    uncertain_facts: tuple[dict[str, Any], ...] = ()
    paths: tuple[dict[str, Any], ...] = ()
    source_evidence: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def sort_key(self) -> tuple:
        return (self.defect_key, self.anchor, self.discriminator)

    def to_dict(self) -> dict[str, Any]:
        return {
            "defect_key": self.defect_key,
            "query": self.query,
            "subject": dict(self.subject),
            "anchor": self.anchor,
            "discriminator": self.discriminator,
            "resolution_status": self.resolution_status,
            "confidence": self.confidence,
            "facts": [dict(fact) for fact in self.facts],
            "uncertain_facts": [dict(fact) for fact in self.uncertain_facts],
            "paths": [dict(path) for path in self.paths],
            "source_evidence": list(self.source_evidence),
            "missing_evidence": list(self.missing_evidence),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class DefectQueryResult:
    query: str
    candidates: tuple[Candidate, ...]
    coverage: dict[str, Any]
    truncated: bool = False

    @property
    def keys(self) -> tuple[str, ...]:
        """The defect keys this result actually produced candidates for."""
        return tuple(sorted({candidate.defect_key for candidate in self.candidates}))


def query_for(defect_key: str) -> str:
    """Which query produces candidates for a matrix key.

    An unexpanded key is an error, not an empty result: a caller asking for a
    pattern the pipeline cannot attack has made a bad request, while a query
    that ran and found nothing has answered a good one.
    """
    for pattern in load_defect_patterns():
        if pattern.key == defect_key:
            if not pattern.query:
                raise ValueError(
                    f"defect pattern {defect_key} has no query; it is not expanded yet"
                )
            return pattern.query
    raise ValueError(f"unknown defect pattern: {defect_key}")


def run(query: str, events: Iterable[Any], edges: Iterable[Any], **kwargs: Any) -> DefectQueryResult:
    """Dispatch by name, passing each query only the parameters it accepts.

    The queries have deliberately different knobs: `max_paths` bounds a walk,
    and `race_condition` performs no walk -- a race candidate is a pair of
    accesses, so there is no path to cap. Filtering here rather than at every
    call site means a caller can offer the full parameter set without knowing
    which query uses which, and the signature remains the single source of
    truth about what each query honours.
    """
    functions: dict[str, Callable[..., DefectQueryResult]] = {
        RESOURCE_LIFETIME: resource_lifetime,
        LOCK_ORDER: lock_order,
        RACE_CONDITION: race_condition,
        NULL_FLOW: null_flow,
        USE_AFTER_FREE: use_after_free,
        DOUBLE_FREE: double_free,
        TAINT_PATH: taint_path,
        ERROR_HANDLING: error_handling,
    }
    try:
        function = functions[query]
    except KeyError:
        raise ValueError(
            f"unknown defect query: {query!r}; known: {', '.join(QUERY_NAMES)}"
        ) from None
    accepted = set(inspect.signature(function).parameters)
    return function(events, edges, **{k: v for k, v in kwargs.items() if k in accepted})


# ----------------------------------------------------------------------
# shared reading of the published graph


def _identity(info: Mapping[str, Any], *, identity: bool = True) -> str:
    """The object a point names, as one string (stage 4.5).

    Stage 4 joined on `subject`, the *spelling* of a name: `int n` in one
    method and `int n` in another were one object, and the race query returned
    557,315 candidates for redis-50 because of it -- every one a pair of
    same-named locals. The declaration index now records which declaration a
    name resolves to, so the join can be on the object instead.

    The token is assembled here rather than in the parser because the
    repository id does not exist at parse time -- the same reason `PointKey`
    carries the owner triple instead of a symbol id.

    Order matters. A declared identity comes before a type, because `decl`
    separates `p->lock` from `q->lock` while a type only says they are the same
    kind of thing; joining on the type would *raise* the candidate count, since
    two objects of one type would take turns displacing each other in the
    lock-order `held` list.

    Every fallback ends at `name:<spelling>` and never at nothing. An old row
    written before this stage has no `subject_decl`, and a candidate dropped
    silently is a finding lost -- the one failure a review tool cannot have.

    Stage 5B adds one rung between `decl` and the fallbacks: an unknown root
    with a member path keeps the member (`name:server#io_threads_num`). Two
    fields of an object whose type nothing can resolve are still two objects;
    without this rung they collapsed into `name:server`, which once merged 765
    member paths of redis-50 into a single race bucket (92% of the 5A candidate
    growth). The member is part of the spelling the source itself wrote, so
    root+member is a weaker claim than `decl` but a strictly sharper one than
    the bare root.

    `identity=False` returns the bare name, which reproduces the stage 4 join
    exactly. That is what makes the before/after comparison readable: it
    isolates the *extraction* changes from the identity changes.
    """
    name = info.get("subject", "")
    if not identity:
        return name
    decl = info.get("subject_decl", "")
    if decl:
        member = info.get("subject_member", "")
        if info.get("subject_storage", "") in _METHOD_LOCAL_STORAGES:
            # A local, a parameter and a function static belong to the method
            # that declares them, so two methods that each declare `int n` are
            # two objects by construction. The owner id carries the signature,
            # so two overloads do not merge either -- which the declaration
            # token alone cannot express, since it is built from the name.
            base = f"{info.get('owner_symbol_id', '')}#{decl}"
        else:
            base = decl
        return f"{base}#{member}" if member else base
    member = info.get("subject_member", "")
    if member and name:
        # The unknown-root rung: no declaration resolved, but the access
        # itself spells out which member it touches.
        return f"name:{name}#{member}"
    site = info.get("subject_site", "")
    if site:
        # An anonymous allocation: no name to resolve, but an identity all the
        # same -- the site. Two `new X(k)` in one loop are the same site and the
        # same object; two in different methods are different objects.
        return f"site:{site}"
    return f"name:{name}" if name else ""


# The storages whose object is created by the declaration itself, so the
# declaring method is part of the identity. A field belongs to its class and a
# file static to its file -- both already inside the declaration token -- and a
# global has external linkage, so the same one named in two files is one
# object and adding a path would invent two.
_METHOD_LOCAL_STORAGES = frozenset({
    STORAGE_LOCAL, STORAGE_PARAMETER, STORAGE_FUNCTION_STATIC,
})


def _identity_display(info: Mapping[str, Any]) -> str:
    """The spelling a human reads, which is what `--subject` still filters on."""
    return info.get("subject", "")


def _identity_cap(info: Mapping[str, Any], *, identity: bool = True) -> dict[str, Any] | None:
    """The named reason this point's identity cannot carry `resolved`, or None.

    Four ways an identity is a proxy rather than an object, and they are kept
    apart because they are four different claims about the code:

    * a member path (`index` in `&index->slot_locks[i]`) -- the same type with
      two objects gives one token, so the name identifies a path;
    * an allocation site -- the graph knows *which* allocation this is and not
      who owns it, which is a question the ownership stage can act on;
    * a name the file does not declare -- a macro, an enum constant, a global
      from a header this parser never sees. `unknown` is an answer, and the
      candidate stays, capped, because a cross-file global is the most
      important shared state a race query can see;
    * `subject_exact == "false"` without a member path -- `arr[i]`, where the
      subscript is what makes the name a proxy and the member path is empty.
      This one has to be tested separately and not inferred from the path:
      `arr[i]` has no member and is still not an object.

    With `identity=False` only the last test applies, which is the stage 4 gate
    exactly.
    """
    if info.get("subject_exact") == "false":
        return {
            "fact": "SUBJECT_PATH",
            "detail": (
                "the name is the root of a longer expression (`s` in `s->buf`), so "
                "the same name elsewhere may be a different object"
            ),
        }
    if not identity:
        return None
    if info.get("subject_member"):
        return {
            "fact": "SUBJECT_PATH",
            "detail": (
                "the name is a member path (`slot_locks[i]`), and two objects of "
                "one type that share a member name share this token"
            ),
        }
    if info.get("subject_site"):
        return {
            "fact": "OWNERSHIP_UNKNOWN",
            "detail": (
                "an anonymous allocation is identified by its allocation site; "
                "what the graph does not know is who owns the object"
            ),
        }
    if not info.get("subject_decl"):
        return {
            "fact": "IDENTITY_UNKNOWN",
            "detail": (
                "no declaration of this name in this file, so the identity falls "
                "back to the spelling -- a macro, an enum constant, or a global "
                "declared in a header the parser does not read"
            ),
        }
    return None


def _lock_receiver_class(info: Mapping[str, Any]) -> str:
    """What the declared type says about a lock match: `mutex`, `non_mutex`, `unknown`, or "".

    Asked only when the match came from a *name* -- a member call
    (`g_pool_lock.lock()`), an assignment (`pl = pipeline.lock()`), or a RAII
    declaration (`lock_guard<mutex> g(m)`). When the vocabulary matched an API
    call instead (`pthread_mutex_lock(&pool)`), the callee is the evidence and
    the argument's declared type is a struct that merely contains a mutex, so
    classifying on it would exclude every real acquisition.

    An empty type is `unknown` and not a rejection: the name may be declared in
    a header this parser never reads, and "the graph cannot tell" has never been
    grounds for dropping a candidate in this query.
    """
    if info.get("subject_from") not in {"receiver", "assignment", "raii"}:
        return ""
    token = info.get("subject_type", "")
    if not token:
        return "unknown"
    if token in _MUTEX_TYPES:
        return "mutex"
    if token in _NON_LOCK_RECEIVER_TYPES:
        return "non_mutex"
    return "unknown"


def _lock_identity_cap(info: Mapping[str, Any], *, identity: bool = True) -> dict[str, Any] | None:
    """Why this lock cannot carry `resolved`, or None -- the stage 4.5 loosening.

    Stage 4 capped every lock recovered from a receiver or a declaration, and
    the reason was good: the subject of `g_pool_lock.lock()` is an expression,
    and nothing distinguished it from `pool->lock()`, where the name is a struct
    that may merely contain a mutex. Stage 4.5 answers the question with the
    declaration -- the subject now carries the type it was declared with -- so
    the cap is lifted in exactly one case: the identity is a plain declaration
    (no member path, no site, storage known) *and* the declared type is a known
    mutex. This is the only loosening the stage allows, and every other lock
    lands where stage 4 put it.

    `identity=False` reproduces stage 4's gate exactly, including the case it
    got wrong: an assigned subject (`pl = pipeline.lock()`) was never capped,
    because the assignment rule runs before the receiver rule. That column
    exists to isolate the extraction changes, so it must not carry the new rule.
    """
    source = info.get("subject_from", "")
    if not identity:
        if source in {"receiver", "raii"}:
            return {
                "fact": "LOCK_IDENTITY",
                "detail": (
                    "the lock was recovered from a receiver or a declaration, so the "
                    "name identifies an expression, not the object it names"
                ),
            }
        return None
    if source not in {"receiver", "raii", "assignment"}:
        return None
    if _lock_receiver_class(info) == "mutex":
        return None
    return {
        "fact": "LOCK_IDENTITY",
        "detail": (
            "the lock was recovered from a receiver or a declaration and its "
            "declared type is not a known mutex, so the name identifies an "
            "expression rather than the object it names"
        ),
    }


def _wanted(defect_key: str, defect_keys: Collection[str] | None) -> bool:
    """Whether a candidate for this matrix key was asked for.

    The filter lives inside the queries rather than after them because
    truncation happens inside the queries. A caller that asks for 4.6 and gets
    the first `max_candidates` rows of 4.1 filtered afterwards has not asked for
    4.6 at all: on `llama.cpp` the `--type 4.6` request returned zero candidates
    while the same database's coverage block said thirty existed.
    """
    return defect_keys is None or defect_key in defect_keys


def _non_lock_points(points: Mapping[str, Mapping[str, Any]]) -> frozenset[str]:
    """LOCK and UNLOCK points whose declared type says they are not mutexes.

    `vk_pipeline pl = pipeline.lock();` on a `std::weak_ptr` is the shape this
    exists for: the vocabulary matched a member named `lock`, the receiver is a
    smart pointer, and there is no lock in the program. These points are removed
    from the lock universe and counted, rather than kept and capped -- a capped
    candidate is a finding a person has to read, and this one is not a finding.
    """
    return frozenset(
        point_id
        for point_id, info in points.items()
        if info["event_type"] in {"LOCK", "UNLOCK"} and _lock_receiver_class(info) == "non_mutex"
    )


def _transfer_reading(
    info: Mapping[str, Any], name: str, identity_key: str, *, identity: bool = True
) -> str:
    """How this call hands the resource over, or "" if it does not.

    Two readings, and they are not equally good. When the call's own subject is
    the resource, the match is exact: `f(p)` is the resource, resolved to the
    same declaration. When the resource is a later argument the match is by
    spelling, because `arguments` holds root identifiers and the query has no
    declaration index to resolve them with -- so a same-named argument in an
    unrelated call on the path would also count.

    The second reading is kept anyway. It is the only way a later-argument
    hand-off is visible at all, and its failure direction is a candidate capped
    at `ambiguous` rather than a candidate reported `resolved` -- the same
    direction the first-argument rule has always had. The reading is named in
    the fact's detail so a reader can tell the two apart.
    """
    if info.get("subject_from") == "assignment":
        # The CALL row sharing the allocation's own expression
        # (`p = malloc(n)`) is the allocation itself wearing its syntax-axis
        # name; counting it would make every allocation in the repository
        # ambiguous.
        return ""
    if _identity(info, identity=identity) == identity_key and info.get("subject_from") == "argument":
        return "as the call's own subject"
    if not name:
        return ""
    # Not gated on `identity`: recording every argument is an *extraction*
    # change, and the column that isolates extraction (`identity=False`) has to
    # show it. An old row has no `arguments` key at all, so the split yields a
    # single empty string that no real name equals -- which is what keeps the
    # new code reading an old database identical to the old code reading it.
    arguments = info.get("arguments", "").split(ARGUMENT_SEPARATOR)
    if name in arguments:
        return f"as an argument named {name!r}, matched by spelling"
    return ""


def _method_name(info: Mapping[str, Any], names: Mapping[str, str] | None) -> str:
    """The owning symbol's qualified name, when the caller supplied a table.

    The events table stores `owner_symbol_id`, not the name -- identity is a
    hash by design (appendix B.2.1), so a pure function cannot recover a name
    from it. Callers that have the `nodes` table read it and pass it in; a
    caller that does not gets an empty string rather than a made-up name.
    """
    if not names:
        return ""
    return names.get(str(info.get("owner_symbol_id", "")), "")


def _span(info: Mapping[str, Any]) -> str:
    return f"{info.get('relative_path', '')}:{info.get('start_line', 0)}-{info.get('end_line', 0)}"


def _citable(point_id: str, points: Mapping[str, Mapping[str, Any]]) -> bool:
    """Whether a point is an operation in the source rather than an endpoint.

    The synthetic exit has no file and no line -- it is where control flow
    left, not something anyone wrote -- so it belongs in a path's point
    sequence and not in a fact list or a citation.
    """
    info = points.get(point_id)
    return info is not None and bool(info.get("relative_path"))


def _uncertain(fact: str, *, decisive: bool, status: str = AMBIGUOUS, **detail: Any) -> dict[str, Any]:
    """One named uncertainty.

    `decisive` says whether this fact is part of the predicate the candidate is
    about. A decisive fact that is ambiguous forces the candidate's own status
    to be ambiguous; a non-decisive one rides along as context. Every query
    that emits an uncertainty states which it is, because "the graph could not
    answer this candidate's question" and "the graph answered, and here is what
    it cannot speak to" are different claims that read identically without it.
    """
    return {"fact": fact, "status": status, "decisive": decisive, **detail}


def _point_fact(point_id: str, info: Mapping[str, Any], names: Mapping[str, str] | None) -> dict[str, Any]:
    return {
        "event": info["event_type"],
        "method": _method_name(info, names),
        "point": point_id,
        "subject": info.get("subject", ""),
        "subject_from": info.get("subject_from", ""),
        "matched_name": info.get("matched_name", ""),
        "source": _span(info),
    }


def _source_evidence(points: Iterable[str], table: Mapping[str, Mapping[str, Any]]) -> tuple[str, ...]:
    """`path:line-line` per point, deduplicated in path order, never absolute."""
    seen: dict[str, None] = {}
    for point_id in points:
        if not _citable(point_id, table):
            continue
        seen.setdefault(_span(table[point_id]), None)
    return tuple(seen)


def _points_by_type(points: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for info in points.values():
        counts[info["event_type"]] = counts.get(info["event_type"], 0) + 1
    return dict(sorted(counts.items()))


def _count_caps(
    points: Mapping[str, Mapping[str, Any]],
    event_types: Collection[str],
    *,
    identity: bool = True,
) -> dict[str, int]:
    """How many points of each type carry an identity that cannot resolve.

    The count is by *reason*, not by type, because the reasons are four
    different claims about the code and a total would hide which one the stage
    actually moved. It is also the honest denominator for the acceptance table:
    "how many lock points does the graph now hold an identity for" is this
    count subtracted from the number of points with a subject.
    """
    counts: dict[str, int] = {}
    for info in points.values():
        if info["event_type"] not in event_types or not _identity(info, identity=identity):
            continue
        cap = _identity_cap(info, identity=identity)
        if cap is not None:
            counts[cap["fact"]] = counts.get(cap["fact"], 0) + 1
    return dict(sorted(counts.items()))


def _by_owner(points: Mapping[str, Mapping[str, Any]]) -> dict[str, list[str]]:
    owners: dict[str, list[str]] = {}
    for point_id, info in points.items():
        owners.setdefault(str(info.get("owner_symbol_id", "")), []).append(point_id)
    return {owner: sorted(ids) for owner, ids in owners.items()}


def _reverse_adjacency(
    adjacency: Mapping[str, list[tuple[str, str, frozenset[str]]]]
) -> dict[str, list[str]]:
    reverse: dict[str, list[str]] = {}
    for src, neighbours in adjacency.items():
        for _edge_id, dst, _flags in neighbours:
            reverse.setdefault(dst, []).append(src)
    return {node: sorted(set(sources)) for node, sources in reverse.items()}


def _closure(
    start: str, adjacency: Mapping[str, list[tuple[str, str, frozenset[str]]]], max_hops: int
) -> set[str]:
    """Every point reachable from `start`, breadth-first, capped at `max_hops`."""
    reached: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    seen = {start}
    while queue:
        current, hops = queue.popleft()
        if hops >= max_hops:
            continue
        for _edge_id, dst, _flags in adjacency.get(current, ()):
            if dst in seen:
                continue
            seen.add(dst)
            reached.add(dst)
            queue.append((dst, hops + 1))
    return reached


def _ancestors(
    start: str, reverse: Mapping[str, list[str]], max_hops: int
) -> set[str]:
    """Every point that can reach `start`, breadth-first, capped at `max_hops`."""
    reached: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    seen = {start}
    while queue:
        current, hops = queue.popleft()
        if hops >= max_hops:
            continue
        for src in reverse.get(current, ()):
            if src in seen:
                continue
            seen.add(src)
            reached.add(src)
            queue.append((src, hops + 1))
    return reached


def _holds_before(
    adjacency: Mapping[str, list[tuple[str, str, frozenset[str]]]],
    points: Mapping[str, Mapping[str, Any]],
    first: str,
    second: str,
    lock: str,
    unlocks: Mapping[str, frozenset[str]],
    max_hops: int,
) -> bool:
    """Is `lock` still held when `second` is reached from `first`?

    Holding is defined by what would end it: a path that reaches `second`
    without passing through an UNLOCK of the same lock. Blocking the unlock
    points is the whole test -- if the only ways from the first acquisition to
    the second run through a release, the lock was not held.
    """
    paths, _truncated = find_paths(
        adjacency,
        points,
        starts={first},
        is_target=lambda info: info is points[second],
        blocked_points=unlocks.get(lock, frozenset()),
        max_hops=max_hops,
        max_paths=1,
    )
    return bool(paths)


def _acquisition_path(
    adjacency: Mapping[str, list[tuple[str, str, frozenset[str]]]],
    points: Mapping[str, Mapping[str, Any]],
    first: str,
    second: str,
    lock: str,
    unlocks: Mapping[str, frozenset[str]],
    max_hops: int,
    max_paths: int,
) -> tuple[dict[str, Any], ...]:
    """The paths that show the first lock still held at the second.

    Bounded by `max_paths` because this is evidence a human reads, and the
    holding *test* is separate (`_holds_before`, which needs one path to
    answer yes). Recording every path would bloat the bundle without changing
    the answer.
    """
    paths, _truncated = find_paths(
        adjacency,
        points,
        starts={first},
        is_target=lambda info: info is points[second],
        blocked_points=unlocks.get(lock, frozenset()),
        max_hops=max_hops,
        max_paths=max_paths,
    )
    return tuple(path.to_dict() for path in paths)


# ----------------------------------------------------------------------
# 4.1 + 4.6 -- resource lifetime


def resource_lifetime(
    events: Iterable[Any],
    edges: Iterable[Any],
    *,
    subject: str | None = None,
    max_hops: int = 64,
    max_paths: int = 8,
    max_candidates: int = 20,
    include_locks: bool = True,
    names: Mapping[str, str] | None = None,
    identity: bool = True,
    defect_keys: Collection[str] | None = None,
    contracts: ContractTable | None = None,
) -> DefectQueryResult:
    """A resource acquired and never released on a path to the method's exit.

    Two modes share one walk. Mode A is matrix 4.1 (`ALLOC`/`RELEASE`), where
    the null guard of a failed allocation is excluded: `if (!p) return -1;` is
    not a leak. Mode B is the lock half of 4.6 (`LOCK`/`UNLOCK`), where the same
    exclusion is exactly wrong -- a failed lock is one of the error paths 4.6
    exists to report -- and where RAII acquisitions are excluded instead,
    because their release is a destructor the CFG cannot see.

    `resolved` requires that no name on the decisive path could have moved the
    resource elsewhere: a call that takes it as an argument, or a `return p;`
    that hands it to the caller, both cap the candidate at `ambiguous`.
    """
    points = materialize_points(events, edges)
    adjacency = build_adjacency(edges)
    # Stage 5B's alias-aware RETURN_TRANSFER reads the flow edges, and only
    # the contracts-gated rulings ever look at them -- so the adjacency is
    # built exactly there and never on the default path.
    flow_from = _flow_adjacency(edges) if contracts is not None else None
    raii_locks = frozenset(
        point_id
        for point_id, info in points.items()
        if info["event_type"] == "LOCK" and info.get("raii") == "true"
    )
    non_lock_points = _non_lock_points(points)
    groups: dict[tuple[str, str], list[ControlPath]] = {}
    truncated = False

    alloc_paths, alloc_truncated = unreleased_resources(
        edges, points, max_hops=max_hops, max_paths=max_paths
    )
    truncated = truncated or alloc_truncated
    for path in alloc_paths:
        groups.setdefault(("4.1", path.points[0]), []).append(path)

    lock_paths: list[ControlPath] = []
    if include_locks:
        lock_paths, lock_truncated = unreleased_resources(
            edges, points,
            acquire="LOCK", release="UNLOCK",
            exclude_null_guard=False, excluded=raii_locks | non_lock_points,
            max_hops=max_hops, max_paths=max_paths,
        )
        truncated = truncated or lock_truncated
        for path in lock_paths:
            groups.setdefault(("4.6", path.points[0]), []).append(path)

    candidates: list[Candidate] = []
    eliminations: list[dict[str, Any]] = []
    contract_covered = 0
    for (defect_key, anchor), paths in sorted(groups.items()):
        if not _wanted(defect_key, defect_keys):
            continue
        acquire = points[anchor]
        if subject is not None and _identity_display(acquire) != subject:
            continue
        covered = contracts is not None and _contracts_cover(paths, points, contracts)
        candidate = _lifetime_candidate(
            defect_key, anchor, acquire, paths, points, names, identity=identity,
            contracts=contracts, contracts_cover=covered, eliminations=eliminations,
            flow_from=flow_from,
        )
        if candidate is not None:
            candidates.append(candidate)
            contract_covered += 1 if covered else 0

    candidates.sort(key=Candidate.sort_key)
    truncated = truncated or len(candidates) > max_candidates
    coverage = {
        "acquire_points": sum(
            1 for info in points.values() if info["event_type"] == "ALLOC"
        ) + (sum(1 for info in points.values() if info["event_type"] == "LOCK") if include_locks else 0),
        "subjects": len({info.get("subject", "") for info in points.values()
                         if info["event_type"] in {"ALLOC", "LOCK"} and info.get("subject")}),
        "subjects_by_identity": len({
            _identity(info, identity=identity) for info in points.values()
            if info["event_type"] in {"ALLOC", "LOCK"} and _identity(info, identity=identity)
        }),
        "identity_resolved_points": sum(
            1 for info in points.values()
            if info["event_type"] in {"ALLOC", "LOCK"} and _identity(info) and not _identity_cap(info)
        ),
        # Rows written before this stage, or names the file does not declare:
        # they keep producing candidates on the spelling, which is why the
        # fallback exists. Counted so a reader can see how much of a result is
        # identity-backed and how much is a name.
        "identity_legacy_points": sum(
            1 for info in points.values()
            if info["event_type"] in {"ALLOC", "LOCK"} and _identity(info)
            and not info.get("subject_decl") and not info.get("subject_site")
        ),
        "allocation_sites": sum(
            1 for info in points.values() if info.get("subject_site")
        ),
        # Both sides of the stage 5C column. A reader comparing against a
        # pre-5C run needs the denominator: 55 → 42 is a fall in recall unless
        # the eliminated 13 are counted next to it.
        "candidates_before_elimination": len(candidates) + len(eliminations),
        "candidates": len(candidates),
        "candidates_by_key": _count_by_key(candidates),
        # Only counts when the lock walk ran: a coverage block describes the
        # work that happened, and mode A excludes nothing.
        "excluded_raii_locks": len(raii_locks) if include_locks else 0,
        # A member named `lock` on a type that is known not to be a mutex. Not a
        # finding and not a gap: a fact about the vocabulary, counted so the
        # number of LOCK events and the number of locks stay separable.
        "non_mutex_lock_points": len(non_lock_points) if include_locks else 0,
        "paths": len(alloc_paths) + len(lock_paths),
        "include_locks": include_locks,
        "max_hops": max_hops,
        "max_paths": max_paths,
        "semantic_points": len(points),
        "semantic_edges": sum(len(item) for item in adjacency.values()),
        "points_by_type": _points_by_type(points),
        **_contract_coverage(contracts, eliminations, contract_covered),
    }
    return DefectQueryResult(
        query=RESOURCE_LIFETIME, candidates=tuple(candidates[:max_candidates]),
        coverage=coverage, truncated=truncated,
    )


def _owner_ruling(
    defect_key: str, acquire: Mapping[str, Any], contracts: ContractTable
) -> dict[str, str] | None:
    """Rules that answer "who owns this now", for a resource.

    Mode B is excluded, and that is a measured decision rather than a stylistic
    one: on redis-50 seven of the twenty lock candidates keep their mutex in a
    file static, and holding a global mutex until the function returns is
    exactly what 4.6 exists to report. Shared storage makes a lock *more*
    reportable, not less, so storage and wrapper ownership are 4.1 rules only.
    """
    if defect_key != "4.1":
        return None
    storage = str(acquire.get("subject_storage", ""))
    if storage in contracts.storages:
        if storage == FUNCTION_STATIC_STORAGE:
            return {
                "reason": "STORAGE_OWNER_FUNCTION_STATIC",
                "provenance": f"graph:storage:{storage}",
                "detail": (
                    "the allocation is kept in a function-static slot, which outlives this "
                    "frame; whether a later call overwrites that slot without freeing it "
                    "first is not visible from one method"
                ),
            }
        return {
            "reason": "STORAGE_OWNER",
            "provenance": f"graph:storage:{storage}",
            "detail": (
                f"the allocation is kept in a {storage} slot, so it belongs to that owner "
                f"rather than to this frame"
            ),
        }
    type_token = str(acquire.get("subject_type", ""))
    if type_token and contracts.is_wrapper(type_token):
        return {
            "reason": "RAII_WRAPPER_TYPE",
            "provenance": f"declared:wrapper:{type_token}",
            "detail": (
                f"the allocation is held in {type_token}, which frees it on every path out "
                f"of the scope, including exception unwinding"
            ),
        }
    return None


def _pair_ruling(
    defect_key: str, acquire: Mapping[str, Any], contracts: ContractTable
) -> dict[str, str] | None:
    """The release obligation of a lock, when the callee hands it to the caller.

    Mode A is excluded: a resource has no acquire/release pairing whose
    obligation could be handed over.
    """
    if defect_key != "4.6":
        return None
    owner = str(acquire.get("owner_symbol_id", ""))
    if not owner or not contracts.caller_owns(owner):
        return None
    return {
        "reason": "PAIR_CALLER_OWNS",
        "provenance": f"inferred:pair:{contracts.pair_labels.get(owner, owner)}",
        "detail": (
            "this method is the acquire half of an acquire/release pair, so it returns with "
            "the lock held by contract and the release belongs to the caller"
        ),
    }


def _escape_ruling(
    defect_key: str,
    acquire: Mapping[str, Any],
    paths: Sequence[ControlPath],
    points: Mapping[str, Mapping[str, Any]],
) -> dict[str, str] | None:
    """The allocation leaves this frame in, or straight after, its acquisition.

    Two graph facts recorded on the allocation point itself, both invisible to
    the path walk (5C-2's 2.4, 2.11 and 2.12):

    * `returned` -- the acquiring expression *is* the return value
      (`return strdup(x)`, `return new X(k)`). The RETURN point has no subject
      to join on, which is why the walk never saw these; the allocation has
      nowhere to go but the caller, on every path that contains it, because
      the path cannot contain the allocation without containing the return.
    * `escaped_to` -- a plain assignment copies the pointer into storage that
      outlives the frame (`g_name = tmp`), on a statement the walk executed
      outside every conditional construct. The paths still have to agree: a
      path that exits before the write is a leak the fact does not cover, and
      that check is what keeps the ruling honest about error branches.
    """
    if defect_key != "4.1":
        return None
    if acquire.get("returned") == "true":
        return {
            "reason": "ESCAPE_RETURN",
            "provenance": "graph:alloc_return",
            "detail": (
                "the allocation is the method's return value, so it leaves this "
                "frame as it is acquired"
            ),
        }
    targets = str(acquire.get("escaped_to", ""))
    if not targets:
        return None
    line = int(str(acquire.get("escaped_to_line", "0")) or "0")
    if line <= 0 or not paths:
        return None
    for path in paths:
        if not any(
            int(str(points.get(point_id, {}).get("start_line", 0) or 0)) >= line
            for point_id in path.points[1:]
        ):
            return None
    return {
        "reason": "ESCAPE_TO_SHARED",
        "provenance": "graph:escape_write",
        "detail": (
            f"the allocation's pointer is stored into {targets} by plain assignment, "
            "which outlives this frame, and every leak path passes that write"
        ),
    }


def _handover_reading(
    info: Mapping[str, Any], name: str, contracts: ContractTable
) -> dict[str, Any] | None:
    """How this call takes ownership of `name`, or None if it does not.

    The match is at the ordinal the contract names, and by spelling, because
    `arguments` holds root identifiers and the query has no declaration index to
    resolve them with. Both halves have to hold -- the call must carry at least
    that many arguments, and the name in that position must be this one -- so a
    shorter call or a same-shaped call to a different variable is not a hand-off.
    """
    if info.get("subject_from") == "assignment":
        # The CALL row sharing the allocation's own expression (`p = malloc(n)`)
        # is the allocation wearing its syntax-axis name, not a hand-off.
        return None
    callee = contracts.callee(info)
    if not callee:
        return None
    ordinals = contracts.consuming_ordinals(callee)
    if not ordinals:
        return None
    arguments = str(info.get("arguments", "")).split(ARGUMENT_SEPARATOR)
    for ordinal in ordinals:
        if ordinal < len(arguments) and arguments[ordinal] == name:
            return {
                "callee": callee,
                "ordinal": ordinal,
                "declared": callee in contracts.sinks,
            }
    return None


def _consumes_ruling(
    paths: Sequence[ControlPath],
    points: Mapping[str, Mapping[str, Any]],
    name: str,
    contracts: ContractTable,
) -> dict[str, str] | None:
    """Every path hands the resource to a callee that takes ownership.

    Every path, not any. The candidate exists because a path reaches the exit
    with no release, so a path that neither releases nor hands the resource over
    is still a leak: a call on one branch does not cover the other.
    """
    if not name or not paths:
        return None
    example: dict[str, Any] | None = None
    covered = 0
    for path in paths:
        for point_id in path.points[1:]:
            info = points.get(point_id)
            if info is None or info["event_type"] != "CALL":
                continue
            reading = _handover_reading(info, name, contracts)
            if reading:
                if example is None:
                    example = reading
                covered += 1
                break
    if example is None or covered != len(paths):
        return None
    return {
        "reason": "DECLARED_CONSUMES" if example["declared"] else "INFERRED_CONSUMES",
        "provenance": (
            f"{'declared' if example['declared'] else 'inferred'}"
            f":consumes:{example['callee']}"
        ),
        "detail": (
            f"every path passes this name to {example['callee']} as argument "
            f"{example['ordinal']}, which takes ownership of it"
        ),
    }


def _edge_field(edge: Any, name: str) -> Any:
    """Read a field from a `SemanticEdge` object or its row, like the CFG
    loader's own field reader."""
    if isinstance(edge, dict):
        return edge[name]
    return getattr(edge, name)


def _flow_adjacency(
    edges: Iterable[Any],
) -> dict[str, list[tuple[str, tuple[str, ...], tuple[str, ...]]]]:
    """`dst -> [(src, via, via_copies)]` over the DATA_FLOW_TO rows only.

    The control adjacency's flow-side twin: same id-keyed discipline, but a
    flow edge is evidence about values, not reachability, so it never enters
    `build_adjacency` and this never sees a CONTROL_REACHES row. Built once
    per query, only when the contracts-gated rulings will read it.
    """
    flow_from: dict[str, list[tuple[str, tuple[str, ...], tuple[str, ...]]]] = {}
    for edge in edges:
        if _edge_field(edge, "relation") != DATA_FLOW_TO:
            continue
        metadata = _edge_field(edge, "metadata") or {}
        flow_from.setdefault(_edge_field(edge, "dst_event_id"), []).append(
            (
                _edge_field(edge, "src_event_id"),
                tuple(metadata.get("via", ())),
                tuple(metadata.get("via_copies", ())),
            )
        )
    return flow_from


def _alias_return_flows(
    flow_from: Mapping[str, Sequence[tuple[str, Sequence[str], Sequence[str]]]],
    return_id: str,
    points: Mapping[str, Mapping[str, Any]],
    identity_key: str,
    *,
    identity: bool = True,
) -> bool:
    """Whether a DATA_FLOW_TO edge reaches this RETURN from the acquire.

    Stage 5B's registered 4.1 miss (`return q;` after `q = p`): the name-level
    check above cannot see that `q` holds `p`'s value. The flow edge can --
    but only when its whole `via` chain is alias copies. An edge with plain
    intermediates (reads, checks) is value plumbing, not an alias, and stays
    silent here: a non-empty via that is not all copies fails the test.
    """
    for src_id, via, via_copies in flow_from.get(return_id, ()):
        if not via or set(via) != set(via_copies):
            continue
        src = points.get(src_id)
        if src is not None and _identity(src, identity=identity) == identity_key:
            return True
    return False


def _return_ruling(
    paths: Sequence[ControlPath],
    points: Mapping[str, Mapping[str, Any]],
    identity_key: str,
    *,
    identity: bool = True,
    flow_from: Mapping[str, Sequence[tuple[str, Sequence[str], Sequence[str]]]] | None = None,
) -> dict[str, str] | None:
    """Every path returns the resource to the caller.

    Every path, for the same reason as `_consumes_ruling` -- and this gate is
    load-bearing rather than decorative. On llama.cpp three candidates return
    the resource on one path and leak it on another, which is the shape of a
    real defect; requiring only *a* transfer would have deleted all three.

    `flow_from` (stage 5B) widens what counts as a transfer by one
    registered case: the returned name received the resource through a
    DATA_FLOW_TO edge whose intermediates are all alias copies. Without flow
    edges -- or with `flow_from=None`, which is every pre-5B call shape --
    the ruling reads exactly as it did before.
    """
    if not identity_key or not paths:
        return None
    returning = 0
    for path in paths:
        for point_id in path.points[1:]:
            info = points.get(point_id)
            if info is None or info["event_type"] != "RETURN":
                continue
            if _identity(info, identity=identity) == identity_key:
                returning += 1
                break
            if flow_from is not None and _alias_return_flows(
                flow_from, point_id, points, identity_key, identity=identity
            ):
                returning += 1
                break
    if returning != len(paths):
        return None
    return {
        "reason": "RETURN_TRANSFER",
        "provenance": "graph:return",
        "detail": (
            "every path out of this method returns the resource to the caller, so the "
            "release obligation leaves with it"
        ),
    }


def _contracts_cover(
    paths: Sequence[ControlPath],
    points: Mapping[str, Mapping[str, Any]],
    contracts: ContractTable,
) -> bool:
    """Whether every call on these paths is to a callee whose contract is known.

    "Known" means the callee is in the declared table or was inferred to consume
    an argument -- not merely that the call joined to a name. A joined call to a
    callee nobody has a contract for is still an unknown contract, and the
    missing-evidence line has to keep saying so. No calls at all is also not
    coverage: nothing was answered, so nothing is dropped.
    """
    calls = 0
    for path in paths:
        for point_id in path.points[1:]:
            info = points.get(point_id)
            if info is None or info["event_type"] != "CALL":
                continue
            calls += 1
            if not contracts.has_contract(contracts.callee(info)):
                return False
    return calls > 0


def _elimination_record(
    ruling: Mapping[str, str],
    defect_key: str,
    anchor: str,
    acquire: Mapping[str, Any],
    names: Mapping[str, str] | None,
) -> dict[str, Any]:
    """One eliminated candidate, with everything needed to audit the ruling.

    An elimination nobody can check is indistinguishable from a bug, so the
    record carries the same span and method a candidate would have cited, plus
    the `provenance` that says whether the ruling came off the graph, out of the
    declared table, or from a rule applied to the graph.
    """
    return {
        "reason": ruling["reason"],
        "provenance": ruling["provenance"],
        "detail": ruling["detail"],
        "defect_key": defect_key,
        "anchor": anchor,
        "subject": _identity_display(acquire),
        "subject_identity": _identity(acquire),
        # The candidate's own discriminator, so a reader can line an elimination
        # up against a run that did not eliminate it. Note that a discriminator
        # is not unique -- `parseOptions` allocates into the same file static at
        # eight lines -- so the difference between two runs is a lower bound on
        # the eliminations, not a count of them.
        "discriminator": f"{_identity(acquire)}|{acquire.get('owner_symbol_id', '')}",
        "storage": acquire.get("subject_storage", ""),
        "type": acquire.get("subject_type", ""),
        "method": _method_name(acquire, names),
        "span": _span(acquire),
    }


def _lifetime_candidate(
    defect_key: str,
    anchor: str,
    acquire: Mapping[str, Any],
    paths: Sequence[ControlPath],
    points: Mapping[str, Mapping[str, Any]],
    names: Mapping[str, str] | None,
    *,
    identity: bool = True,
    contracts: ContractTable | None = None,
    contracts_cover: bool = False,
    eliminations: list[dict[str, Any]] | None = None,
    flow_from: Mapping[str, Sequence[tuple[str, Sequence[str], Sequence[str]]]] | None = None,
) -> Candidate | None:
    name = _identity_display(acquire)
    identity_key = _identity(acquire, identity=identity)
    is_lock = defect_key == "4.6"

    # Stage 5C's two path-independent rulings come first: they are facts about
    # where the resource is kept and whose obligation it is, so if either holds
    # there is nothing for the walk to add. The escape rulings join them with
    # no contract dependency: they read the allocation's own annotations,
    # which are graph facts and not contract inferences.
    ruling = _escape_ruling(defect_key, acquire, paths, points)
    if ruling is None and contracts is not None:
        ruling = _owner_ruling(defect_key, acquire, contracts) or _pair_ruling(
            defect_key, acquire, contracts
        )
    if ruling is not None:
        if eliminations is not None:
            eliminations.append(
                _elimination_record(ruling, defect_key, anchor, acquire, names)
            )
        return None

    uncertain: list[dict[str, Any]] = []
    missing = [
        _MISSING_CALLEE_CONTRACT,
        _MISSING_RAII,
        _MISSING_ALIASING,
        _MISSING_RELEASE_IN_CALLEE,
    ]
    if is_lock:
        missing.extend([_MISSING_ERROR_PATH, _MISSING_DESTRUCTOR, _MISSING_GUARD_BINDING])
    if contracts is not None and contracts_cover:
        # Every call on these paths goes to a callee whose contract is known,
        # and none of them took the resource -- otherwise this candidate would
        # already have been eliminated. So the two lines that say "a callee
        # might own this" have been answered for this candidate and stop
        # claiming otherwise. `_MISSING_RAII` deliberately stays: a type that is
        # absent from the wrapper table is not evidence that it does not own.
        missing = [item for item in missing if item not in (
            _MISSING_CALLEE_CONTRACT, _MISSING_RELEASE_IN_CALLEE,
        )]

    status = RESOLVED if identity_key else UNRESOLVED
    if not identity_key:
        uncertain.append(_uncertain(
            "SUBJECT_UNRESOLVED", decisive=True,
            detail="the acquisition names no target, so the candidate cannot be attributed",
        ))
    else:
        cap = _identity_cap(acquire, identity=identity)
        if cap is not None:
            # The name identifies a path, a site or a spelling rather than the
            # object, which is the definition of a name-level proxy -- so the
            # candidate cannot be `resolved`.
            uncertain.append(_uncertain(
                cap["fact"], decisive=True, detail=cap["detail"],
                point=anchor, source=_span(acquire),
            ))
            status = AMBIGUOUS

    path_points: list[str] = []
    for path in paths:
        for point_id in path.points:
            if point_id not in path_points:
                path_points.append(point_id)

    for point_id in path_points[1:]:
        info = points.get(point_id)
        if info is None or not identity_key:
            continue
        event_type = info["event_type"]
        if event_type == "CALL" and not is_lock:
            # Tested before the identity guard, and deliberately: the call that
            # hands the resource over need not *name* it as its subject.
            # `RedisModule_SetKeyMeta(cls, key, buffer)` passes the allocation
            # as its third argument, so a call whose own subject is `cls` is
            # still the hand-off -- and reading only the first argument, which
            # is what stage 4 did, called that allocation still-owned.
            how = _transfer_reading(info, name, identity_key, identity=identity)
            if how:
                uncertain.append(_uncertain(
                    "OWNERSHIP_TRANSFER", decisive=True,
                    detail=f"this name is passed to a callee, which may take ownership ({how})",
                    point=point_id, source=_span(info),
                ))
                status = AMBIGUOUS if status == RESOLVED else status
            continue
        if _identity(info, identity=identity) != identity_key:
            continue
        if event_type == "RETURN":
            uncertain.append(_uncertain(
                "RETURN_TRANSFER", decisive=True,
                detail="the method returns this name, so ownership may leave with the caller",
                point=point_id, source=_span(info),
            ))
            status = AMBIGUOUS if status == RESOLVED else status

    # The path-dependent rulings. Each needs *every* path to show the transfer,
    # so they run after the walk rather than inside it: the walk dedups points
    # across paths, and a fact that holds on one path says nothing about the
    # other.
    if contracts is not None and not is_lock:
        ruling = _consumes_ruling(paths, points, name, contracts) or _return_ruling(
            paths, points, identity_key, identity=identity, flow_from=flow_from
        )
        if ruling is not None:
            if eliminations is not None:
                eliminations.append(
                    _elimination_record(ruling, defect_key, anchor, acquire, names)
                )
            return None

    if is_lock:
        lock_cap = _lock_identity_cap(acquire, identity=identity)
        if lock_cap is not None:
            uncertain.append(_uncertain(
                lock_cap["fact"], decisive=True, detail=lock_cap["detail"],
                point=anchor, source=_span(acquire),
            ))
            status = AMBIGUOUS if status == RESOLVED else status
        if acquire.get("matched_name") in NON_BLOCKING_ACQUIRES:
            uncertain.append(_uncertain(
                "BLOCKING_ACQUIRE", decisive=True,
                detail="the acquisition can fail without acquiring",
                point=anchor, source=_span(acquire),
            ))
            status = AMBIGUOUS if status == RESOLVED else status

    facts = tuple(
        _point_fact(point_id, points[point_id], names)
        for point_id in path_points
        if _citable(point_id, points)
    )
    return Candidate(
        defect_key=defect_key,
        query=RESOURCE_LIFETIME,
        subject={
            "kind": "lock" if is_lock else "allocation",
            "name": name,
            # The object, next to the spelling. The discriminator is built from
            # this and not from the name, so two objects that share a spelling
            # in one method mint two evidence ids rather than one.
            "identity": identity_key,
            "storage": acquire.get("subject_storage", ""),
            "type": acquire.get("subject_type", ""),
            "method": _method_name(acquire, names),
            "owner_symbol_id": acquire.get("owner_symbol_id", ""),
        },
        anchor=anchor,
        discriminator=f"{identity_key}|{acquire.get('owner_symbol_id', '')}",
        resolution_status=status,
        confidence=RESOURCE_CONFIDENCE[status],
        facts=facts,
        uncertain_facts=tuple(uncertain),
        paths=tuple(path.to_dict() for path in paths),
        source_evidence=_source_evidence(path_points, points),
        missing_evidence=tuple(missing),
        metadata={
            "exit_point": paths[0].points[-1] if paths else "",
            "anchor_span": {
                "relative_path": acquire.get("relative_path", ""),
                "start_line": acquire.get("start_line", 0),
                "end_line": acquire.get("end_line", 0),
            },
            "methods": [_method_name(acquire, names)] if _method_name(acquire, names) else [],
        },
    )


def _contract_coverage(
    contracts: ContractTable | None,
    eliminations: Sequence[Mapping[str, Any]],
    contract_covered: int,
) -> dict[str, Any]:
    """The stage 5C half of the coverage block, in one place.

    The keys are present even when no contract table was passed, with zero and
    empty values, so a comparison column has one schema rather than two. Every
    elimination is counted by reason; the records themselves are capped at
    `DISQUALIFIED_EVIDENCE_CAP`, which is set above the acceptance repositories'
    elimination counts so the sampling frame stays whole.

    A second, independent view of the same set is the diff between a run with a
    contract table and one without: `candidates_before_elimination` minus
    `candidates`, matched on `discriminator`. It carries no reason, which is why
    the records exist, but it is what makes the count checkable.
    """
    by_reason: dict[str, int] = {}
    for record in eliminations:
        reason = str(record.get("reason", ""))
        by_reason[reason] = by_reason.get(reason, 0) + 1
    table = contracts.coverage if contracts is not None else {}
    joined = int(table.get("call_points_with_callee", 0))
    contracted = int(table.get("call_points_with_contract", 0))
    return {
        "contracts_available": contracts is not None,
        "disqualified": dict(sorted(by_reason.items())),
        "disqualified_evidence": [dict(record) for record in eliminations[:DISQUALIFIED_EVIDENCE_CAP]],
        "disqualified_total": len(eliminations),
        "pairs_inferred": list(table.get("pairs_inferred", [])),
        "contract_anchoring_rejected": list(table.get("contract_anchoring_rejected", [])),
        "call_points_with_callee": joined,
        "call_points_unjoined": int(table.get("call_points_unjoined", 0)),
        "function_effects_declared": int(table.get("summaries_declared", 0)),
        "function_effects_inferred": int(table.get("summaries_inferred", 0)),
        # A joined call to a callee nobody has a contract for. Counted apart
        # from the unjoined ones because the two gaps have different fixes: one
        # is an extraction join, the other is corpus knowledge.
        "function_effects_unknown": max(0, joined - contracted),
        # Candidates that survived with every call on their paths contracted.
        # This is the missing-evidence conditioning made countable: it is how
        # much of the result the contract layer can now speak to in full.
        "candidates_with_known_callee_contract": contract_covered,
    }


def _count_by_key(candidates: Sequence[Candidate]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.defect_key] = counts.get(candidate.defect_key, 0) + 1
    return dict(sorted(counts.items()))


# ----------------------------------------------------------------------
# 1.3 + 1.4 -- lock order


def lock_order(
    events: Iterable[Any],
    edges: Iterable[Any],
    *,
    subject: str | None = None,
    max_hops: int = 64,
    max_paths: int = 8,
    max_candidates: int = 20,
    names: Mapping[str, str] | None = None,
    identity: bool = True,
    defect_keys: Collection[str] | None = None,
) -> DefectQueryResult:
    """Lock acquisitions in one order in one method and the opposite order in
    another, plus the self-deadlock shape of acquiring one lock twice.

    Acquisition order is the method's LOCK points by `ordinal`, which is source
    order by construction. That is a *lexical* order: under branches and loops
    the real order can differ, and the CFG is used for the one thing it can
    decide -- whether the first lock is still held when the second is taken.
    Holding is tested by blocking the UNLOCK points of the first lock, so a
    pair whose only connection runs through a release is not a pair.

    Inversions are indexed by the lock pair rather than by method pairs: with
    M methods taking locks, comparing every method with every other is M^2
    walks, while grouping the (first, second) pairs a method produces and
    looking up the reverse key visits each real inversion once.

    Every 1.3 candidate carries MAY_PARALLEL as `ambiguous` -- whether the two
    methods can run concurrently is not a fact the graph holds. When the
    repository contains no THREAD_SPAWN point at all, that uncertainty becomes
    a disqualification instead: nothing in the graph can run these methods at
    the same time, so the candidates are dropped and counted rather than
    reported as ambiguous.
    """
    points = materialize_points(events, edges)
    adjacency = build_adjacency(edges)
    non_lock_points = _non_lock_points(points)
    locks_by_method = _locks_by_method(points, excluded=non_lock_points)
    unlocks_by_subject = _unlocks_by_subject(points, identity=identity)
    # The identity is what the walk is about; the spelling is what a human
    # reads. Keeping them in two maps rather than one is the point of the
    # stage: `index->slot_locks[i]` and `index->global_lock` are two locks with
    # two identities and one root, and `--subject` still filters on the root a
    # person would type.
    display: dict[str, str] = {}

    pairs_by_key: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
    reentrant: list[tuple[str, str, str, str]] = []
    unidentified = 0
    for owner, entries in sorted(locks_by_method.items()):
        held: list[tuple[str, str]] = []
        for point_id, info in entries:
            name = _identity(info, identity=identity)
            if not name:
                unidentified += 1
                continue
            display.setdefault(name, _identity_display(info))
            if info["event_type"] == "UNLOCK":
                held = [(held_name, held_id) for held_name, held_id in held if held_name != name]
                continue
            if any(held_name == name for held_name, _ in held):
                first = next(held_id for held_name, held_id in held if held_name == name)
                reentrant.append((owner, first, point_id, name))
            else:
                for held_name, held_id in reversed(held):
                    pairs_by_key.setdefault((held_name, name), {})[owner] = (held_id, point_id)
                held.append((name, point_id))

    spawns = [point_id for point_id, info in points.items() if info["event_type"] == "THREAD_SPAWN"]
    candidates: list[Candidate] = []
    disqualified_no_threads = 0
    inversion_pairs = sorted(pairs_by_key) if _wanted("1.3", defect_keys) else ()
    for (first_lock, second_lock) in inversion_pairs:
        if first_lock >= second_lock:
            continue
        reverse = pairs_by_key.get((second_lock, first_lock))
        if not reverse:
            continue
        for owner_a, (a_first, a_second) in sorted(pairs_by_key[(first_lock, second_lock)].items()):
            for owner_b, (b_first, b_second) in sorted(reverse.items()):
                if owner_a == owner_b:
                    continue
                candidate = _lock_order_candidate(
                    first_lock, second_lock, owner_a, owner_b,
                    (a_first, a_second), (b_first, b_second),
                    points, adjacency, unlocks_by_subject, max_hops, max_paths, names,
                    display=display, identity=identity,
                )
                if subject is not None and candidate.subject["name"] != subject:
                    continue
                if not spawns:
                    disqualified_no_threads += 1
                    continue
                candidates.append(candidate)

    reentrant_candidates = sorted(reentrant) if _wanted("1.4", defect_keys) else ()
    for owner, first, second, lock_identity in reentrant_candidates:
        candidate = _self_deadlock_candidate(
            owner, first, second, lock_identity, points, adjacency, unlocks_by_subject,
            max_hops, max_paths, names, display=display, identity=identity,
        )
        if subject is not None and candidate.subject["name"] != subject:
            continue
        candidates.append(candidate)

    candidates.sort(key=Candidate.sort_key)
    truncated = len(candidates) > max_candidates
    coverage = {
        "methods_with_locks": len(locks_by_method),
        "lock_points": sum(
            1 for point_id, info in points.items()
            if info["event_type"] == "LOCK" and point_id not in non_lock_points
        ),
        # A member named `lock` on a type that is known not to be a mutex
        # (`std::weak_ptr`). Counted here as well as in the lifetime query,
        # because it is a fact about the vocabulary and not about either walk.
        "non_mutex_lock_points": len(non_lock_points),
        "unidentified_lock_points": unidentified,
        "lock_identities": len(display),
        # Counted over the points that are locks. A `weak_ptr::lock()` point is
        # excluded here for the same reason it is excluded from the walk: it has
        # no lock identity to be resolved or legacy about, and counting it as
        # identity-less would read as a coverage failure in a place where the
        # graph answered correctly.
        "identity_resolved_lock_points": sum(
            1 for point_id, info in points.items()
            if info["event_type"] in {"LOCK", "UNLOCK"} and point_id not in non_lock_points
            and _identity(info) and not _identity_cap(info)
        ),
        "identity_legacy_lock_points": sum(
            1 for point_id, info in points.items()
            if info["event_type"] in {"LOCK", "UNLOCK"} and point_id not in non_lock_points
            and _identity(info)
            and not info.get("subject_decl") and not info.get("subject_site")
        ),
        "lock_identity_caps": _count_caps(
            {point_id: info for point_id, info in points.items() if point_id not in non_lock_points},
            {"LOCK", "UNLOCK"}, identity=identity,
        ),
        "lock_pairs": sum(len(owners) for owners in pairs_by_key.values()),
        "inverted_pairs": sum(
            1 for (first, second) in pairs_by_key
            if first < second and (second, first) in pairs_by_key
        ),
        "reentrant_acquires": len(reentrant),
        "spawn_points": len(spawns),
        "candidates": len(candidates),
        "candidates_by_key": _count_by_key(candidates),
        "disqualified_no_threads": disqualified_no_threads,
        "max_hops": max_hops,
        "semantic_points": len(points),
        "semantic_edges": sum(len(item) for item in adjacency.values()),
        "points_by_type": _points_by_type(points),
    }
    return DefectQueryResult(
        query=LOCK_ORDER, candidates=tuple(candidates[:max_candidates]),
        coverage=coverage, truncated=truncated,
    )


def _locks_by_method(
    points: Mapping[str, Mapping[str, Any]], *, excluded: Collection[str] = ()
) -> dict[str, list[tuple[str, Mapping[str, Any]]]]:
    """LOCK and UNLOCK points per method, in acquisition order.

    Ordered by `start_line`, not by `ordinal`. Ordinal counts within
    `(owner, event_type)`, so LOCK ordinals and UNLOCK ordinals are two
    independent counters -- sorting the two types together by ordinal
    interleaves them and destroys the acquisition sequence (a method locking
    `a` then `b` and unlocking `b` then `a` would read as lock a, unlock b,
    lock b, unlock a). Source position is the only key that orders the two
    types against each other, and it is the honest one: acquisition order
    *is* a fact about where the calls are written.

    Two acquisitions on one line tie and fall back to the point id. That
    ordering is arbitrary but deterministic, and it is the same approximation
    the query's docstring already registers.

    `excluded` holds the points that are not acquisitions at all -- a member
    named `lock` on a type known not to be a mutex. They leave the sequence
    entirely rather than being capped downstream: they are not locks that the
    graph is unsure about, they are not locks.
    """
    grouped: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for point_id, info in points.items():
        if info["event_type"] not in {"LOCK", "UNLOCK"} or point_id in excluded:
            continue
        grouped.setdefault(str(info.get("owner_symbol_id", "")), []).append((point_id, info))
    return {
        owner: sorted(entries, key=lambda item: (item[1].get("start_line", 0), item[0]))
        for owner, entries in grouped.items()
    }


def _unlocks_by_subject(
    points: Mapping[str, Mapping[str, Any]], *, identity: bool = True
) -> dict[str, frozenset[str]]:
    """Unlock points per lock identity.

    Keyed by identity and not by spelling, because this map is what decides
    whether a lock is still held: a method that locks `index->a` and unlocks
    `index->b` holds the first one, and a name-keyed map would say it released
    it -- turning a real inversion into a pair that never overlaps.
    """
    unlocks: dict[str, set[str]] = {}
    for point_id, info in points.items():
        if info["event_type"] != "UNLOCK":
            continue
        key = _identity(info, identity=identity)
        if key:
            unlocks.setdefault(key, set()).add(point_id)
    return {name: frozenset(ids) for name, ids in unlocks.items()}


def _lock_order_candidate(
    first_lock: str,
    second_lock: str,
    owner_a: str,
    owner_b: str,
    side_a: tuple[str, str],
    side_b: tuple[str, str],
    points: Mapping[str, Mapping[str, Any]],
    adjacency: Mapping[str, list[tuple[str, str, frozenset[str]]]],
    unlocks: Mapping[str, frozenset[str]],
    max_hops: int,
    max_paths: int,
    names: Mapping[str, str] | None,
    *,
    display: Mapping[str, str] | None = None,
    identity: bool = True,
) -> Candidate:
    display = display or {}

    def _display_of(key: str) -> str:
        return display.get(key, key)

    sides = ((owner_a, first_lock, second_lock, side_a), (owner_b, second_lock, first_lock, side_b))
    facts: list[dict[str, Any]] = []
    uncertain: list[dict[str, Any]] = []
    paths: list[dict[str, Any]] = []
    confirmed_both = True
    named_both = True
    exact_both = True
    for owner, first_key, second_key, (first_point, second_point) in sides:
        confirmed = _holds_before(
            adjacency, points, first_point, second_point, first_key, unlocks, max_hops
        )
        confirmed_both = confirmed_both and confirmed
        for role, lock_key, point_id in (
            ("first", first_key, first_point), ("second", second_key, second_point)
        ):
            info = points[point_id]
            facts.append({
                "side": "a" if owner == owner_a else "b",
                "role": role,
                "method": _method_name(info, names),
                "method_symbol_id": owner,
                "lock": display.get(lock_key, _identity_display(info)),
                "lock_identity": lock_key,
                # What the declaration said about this lock's type, which is
                # what decides whether the identity above may carry `resolved`.
                # Empty for a lock the vocabulary matched by API name: the
                # callee is the evidence there, not the argument's type.
                "lock_type": _lock_receiver_class(info),
                "point": point_id,
                "source": _span(info),
                "holding_confirmed": confirmed,
            })
            if _lock_identity_cap(info, identity=identity) is not None:
                named_both = False
            if _identity_cap(info, identity=identity) is not None:
                exact_both = False
        if confirmed:
            for path in _acquisition_path(
                adjacency, points, first_point, second_point, first_key, unlocks,
                max_hops, max_paths,
            ):
                paths.append({"side": "a" if owner == owner_a else "b", **path})
        else:
            uncertain.append(_uncertain(
                "HOLDING_CONFIRMED", decisive=True,
                detail=(
                    "no path from the first acquisition to the second avoids "
                    "releasing the first lock, so the two may not overlap"
                ),
                method=_method_name(points[first_point], names),
                lock=display.get(first_key, _identity_display(points[first_point])),
            ))
    if not named_both:
        uncertain.append(_uncertain(
            "LOCK_IDENTITY", decisive=True,
            detail=(
                "at least one lock was recovered from a receiver or a declaration "
                "whose declared type is not a known mutex"
            ),
        ))
    if not exact_both:
        # Two locks in one method ordered `index->a` then `index->b` are two
        # different objects named by the same root, and the inversion the graph
        # sees between them is between two names, not between two locks.
        uncertain.append(_uncertain(
            "SUBJECT_PATH", decisive=True,
            detail=(
                "at least one lock's identity is a path, a site or a spelling "
                "rather than a declaration, so two different locks can share it"
            ),
        ))
    for _owner, _first, _second, (first_point, second_point) in sides:
        for point_id in (first_point, second_point):
            if points[point_id].get("matched_name") in NON_BLOCKING_ACQUIRES:
                uncertain.append(_uncertain(
                    "BLOCKING_ACQUIRE", decisive=True,
                    detail="a non-blocking acquisition can fail without acquiring",
                    point=point_id, source=_span(points[point_id]),
                ))
    # Not decisive: whether the two methods can run at once is a separate
    # question from whether the two orders exist, and the graph answers the
    # second one. Marking it decisive would force every inversion to ambiguous
    # and the status would stop distinguishing anything.
    uncertain.append(_uncertain(
        "MAY_PARALLEL", decisive=False,
        detail="whether these two methods can run concurrently is not in the graph",
    ))

    status = RESOLVED if confirmed_both and named_both and exact_both else AMBIGUOUS
    anchor_owner = min(owner_a, owner_b)
    anchor = side_a[0] if anchor_owner == owner_a else side_b[0]
    other_owner = owner_b if anchor_owner == owner_a else owner_a
    return Candidate(
        defect_key="1.3",
        query=LOCK_ORDER,
        subject={
            "kind": "lock_pair",
            # The spellings a person would type, and the identities the walk
            # actually used. Two locks can share a spelling (`index->a` in one
            # method, `index->a` in another) and not share an identity.
            "name": f"{_display_of(first_lock)}|{_display_of(second_lock)}",
            "locks": [_display_of(first_lock), _display_of(second_lock)],
            "identities": [first_lock, second_lock],
            "method": _method_name(points[anchor], names),
            "owner_symbol_id": anchor_owner,
        },
        anchor=anchor,
        discriminator=f"{first_lock}|{second_lock}|{other_owner}",
        resolution_status=status,
        confidence=LOCK_ORDER_CONFIDENCE[status],
        facts=tuple(facts),
        uncertain_facts=tuple(uncertain),
        paths=tuple(paths),
        source_evidence=_source_evidence(
            [side_a[0], side_a[1], side_b[0], side_b[1]], points
        ),
        missing_evidence=(
            _MISSING_THREADS, _MISSING_LOCK_ALIAS, _MISSING_TRY_PATH, _MISSING_GUARD_BINDING,
        ),
        metadata={
            "methods": [
                name for name in (
                    _method_name(points[side_a[0]], names), _method_name(points[side_b[0]], names)
                ) if name
            ],
            "anchor_span": {
                "relative_path": points[anchor].get("relative_path", ""),
                "start_line": points[anchor].get("start_line", 0),
                "end_line": points[anchor].get("end_line", 0),
            },
            "exit_point": "",
        },
    )


def _self_deadlock_candidate(
    owner: str,
    first: str,
    second: str,
    lock_identity: str,
    points: Mapping[str, Mapping[str, Any]],
    adjacency: Mapping[str, list[tuple[str, str, frozenset[str]]]],
    unlocks: Mapping[str, frozenset[str]],
    max_hops: int,
    max_paths: int,
    names: Mapping[str, str] | None,
    *,
    display: Mapping[str, str] | None = None,
    identity: bool = True,
) -> Candidate:
    display = display or {}
    name = display.get(lock_identity, _identity_display(points[first]))
    confirmed = _holds_before(adjacency, points, first, second, lock_identity, unlocks, max_hops)
    lock_cap = _lock_identity_cap(points[first], identity=identity)
    cap = _identity_cap(points[first], identity=identity)
    status = RESOLVED if confirmed and lock_cap is None and cap is None else AMBIGUOUS
    # A recursive mutex is not decisive: it can exonerate the candidate, but
    # it does not stop the graph answering whether the same lock was taken
    # twice while held, which is what this candidate is about.
    uncertain: list[dict[str, Any]] = [_uncertain(
        "MUTEX_RECURSIVE", decisive=False,
        detail=(
            "a recursive mutex (PTHREAD_MUTEX_RECURSIVE) makes a second "
            "acquisition by the same thread legal"
        ),
    )]
    if not confirmed:
        uncertain.append(_uncertain(
            "HOLDING_CONFIRMED", decisive=True,
            detail="no path reaches the second acquisition without releasing the lock",
        ))
    if lock_cap is not None:
        uncertain.append(_uncertain(
            lock_cap["fact"], decisive=True, detail=lock_cap["detail"],
        ))
    if cap is not None:
        # `pthread_mutex_trylock(&index->slot_locks[i])` and
        # `pthread_rwlock_rdlock(&index->global_lock)` share the root `index`,
        # and a re-entrant acquisition of one lock is what 1.4 reports. The
        # member path in the identity keeps them apart; what it cannot do is
        # prove the object, so the candidate stays capped.
        uncertain.append(_uncertain(
            cap["fact"], decisive=True, detail=cap["detail"],
            point=first, source=_span(points[first]),
        ))
    paths: list[dict[str, Any]] = []
    if confirmed:
        paths.extend(
            _acquisition_path(adjacency, points, first, second, name, unlocks, max_hops, max_paths)
        )
    facts = tuple(
        {
            "side": "single",
            "role": role,
            "method": _method_name(points[point_id], names),
            "method_symbol_id": owner,
            "lock": name,
            "point": point_id,
            "source": _span(points[point_id]),
            "holding_confirmed": confirmed,
        }
        for role, point_id in (("first", first), ("second", second))
    )
    return Candidate(
        defect_key="1.4",
        query=LOCK_ORDER,
        subject={
            "kind": "lock",
            "name": name,
            "identity": lock_identity,
            "storage": points[first].get("subject_storage", ""),
            "type": points[first].get("subject_type", ""),
            "method": _method_name(points[first], names),
            "owner_symbol_id": owner,
        },
        anchor=first,
        discriminator=f"{lock_identity}|{owner}",
        resolution_status=status,
        confidence=SELF_DEADLOCK_CONFIDENCE[status],
        facts=facts,
        uncertain_facts=tuple(uncertain),
        paths=tuple(paths),
        source_evidence=_source_evidence([first, second], points),
        # The recursive-mutex gap leads for 1.4: it is the one fact that would
        # turn this candidate from a defect into correct code, and it is the
        # fact the graph is furthest from holding.
        missing_evidence=(
            _MISSING_RECURSIVE, _MISSING_THREADS, _MISSING_LOCK_ALIAS, _MISSING_TRY_PATH,
        ),
        metadata={
            "methods": [
                method for method in (_method_name(points[first], names),) if method
            ],
            "anchor_span": {
                "relative_path": points[first].get("relative_path", ""),
                "start_line": points[first].get("start_line", 0),
                "end_line": points[first].get("end_line", 0),
            },
            "exit_point": "",
        },
    )


# ----------------------------------------------------------------------
# 1.1 -- shared-data race


def race_condition(
    events: Iterable[Any],
    edges: Iterable[Any],
    *,
    subject: str | None = None,
    max_hops: int = 64,
    max_candidates: int = 20,
    names: Mapping[str, str] | None = None,
    identity: bool = True,
    defect_keys: Collection[str] | None = None,
    bindings: ParameterBindingTable | None = None,
) -> DefectQueryResult:
    """One object touched by two methods, at least one touch a write, where the
    graph cannot show the two touches are ordered or protected.

    This query is where the stage's falsifiability question is answered, and
    the answer is visible in its output rather than in a footnote: every
    candidate is `ambiguous`, because MAY_PARALLEL is not a relation the graph
    holds. Since stage 5A the access kind is a fact when the walker saw the
    access (READ/WRITE events) and a syntactic proxy only when it did not --
    the candidate's `access` metadata says which, per side, and `decided=False`
    marks the proxy. A shared variable that appears only in plain statements
    (`counter++`, `s->flag = true`) now produces an event; what is still lost
    is the access with no recoverable subject at all.

    The filters that remain are the ones a sparse graph can actually decide:
    an object that is only ever read on both sides is dropped, two methods that
    both hold the same resolved lock are dropped, and two methods that both
    touch the object atomically are dropped. Since stage 5B the pairing also
    declines on C's thread-local objects (`errno`): the events stay in the
    graph, the coverage reports how many subjects the exemption declined.

    `bindings` (stage 5B, default off) composes the cross-method pairs the
    sparse graph cannot see on its own: a callee's parameter write joined, at
    query time, to the caller's argument root. The composed key is the
    caller's own identity for the root plus the callee row's member path --
    declared facts outrank the spelling (`_by_root`, the weaker claim, named
    in the candidate metadata and never upgraded). The callee's rows keep
    their own parameter identity too; nothing moves, the composed key is
    additional, and with `bindings=None` every number below is the pre-5B
    one.

    What the candidates are grouped by is the *object* and not the spelling,
    which is the whole of stage 4.5's effect here: `int n` in one method and
    `int n` in another are two variables, so they cannot be a race, and the
    557,315 candidates this query returned for redis-50 -- every one of them a
    pair of same-named locals in two methods -- were pairs of objects that had
    nothing to do with each other. A name the file does not declare is grouped
    by spelling, exactly as before, because a cross-file global is the most
    important shared state this query can see.
    """
    points = materialize_points(events, edges)
    adjacency = build_adjacency(edges)
    reverse = _reverse_adjacency(adjacency)
    methods_index = _method_index(points, reverse, identity=identity)

    # The synchronization facilities, by identity. Subtracting them by spelling
    # is what stage 4 did, and it subtracted too much: the LOCK subject and the
    # race subject were both the root of a member path (`index`), so a lock on
    # `index->slot_locks[0]` read as proof that `index->field` was protected.
    # The root set below is measured rather than applied -- see the coverage key
    # it feeds -- because "a lock on the object a member path starts from
    # protects every member of it" is a policy, not a fact.
    synchronization: set[str] = set()
    synchronization_roots: set[str] = set()
    for info in points.values():
        if info["event_type"] in SYNCHRONIZATION_EVENTS and info.get("subject"):
            key = _identity(info, identity=identity)
            synchronization.add(key)
            synchronization_roots.add(key.split("#")[0])

    by_subject: dict[str, dict[str, list[str]]] = {}
    display: dict[str, str] = {}
    considered = 0
    root_matched = 0
    exempt_subjects: set[str] = set()
    exempt_methods: set[str] = set()
    by_storage: Counter = Counter()
    subject_events = RACE_SUBJECT_EVENTS | RACE_ACCESS_EVENTS
    for point_id, info in sorted(points.items()):
        name = info.get("subject", "")
        if not name or info["event_type"] not in subject_events:
            continue
        considered += 1
        by_storage[info.get("subject_storage") or STORAGE_UNKNOWN] += 1
        key = _identity(info, identity=identity)
        display.setdefault(key, name)
        if name in _THREAD_LOCAL_NAMES:
            # A thread-local cannot be the shared state of a cross-method
            # race; the events stay in the graph, only the pairing declines.
            exempt_subjects.add(key)
            exempt_methods.add(str(info.get("owner_symbol_id", "")))
            continue
        if key in synchronization:
            continue
        if key.split("#")[0] in synchronization_roots:
            root_matched += 1
        by_subject.setdefault(key, {}).setdefault(
            str(info.get("owner_symbol_id", "")), []
        ).append(point_id)

    access_events = 0
    access_events_by_storage: Counter = Counter()
    for info in points.values():
        if info["event_type"] in RACE_ACCESS_EVENTS:
            access_events += 1
            access_events_by_storage[info.get("subject_storage") or STORAGE_UNKNOWN] += 1

    spawns = [point_id for point_id, info in points.items() if info["event_type"] == "THREAD_SPAWN"]
    joins = [point_id for point_id, info in points.items() if info["event_type"] == "THREAD_JOIN"]
    thread_functions = {
        info.get("thread_function", "")
        for info in points.values()
        if info.get("thread_function")
    }

    spawn_index = _spawn_index(points)

    candidates: list[Candidate] = []
    read_read_pairs = 0
    disqualified_atomic = 0
    disqualified_same_lock = 0
    access_decided_pairs = 0
    access_proxy_pairs = 0
    multi_method = 0
    multi_method_by_storage: Counter = Counter()
    storage_of_identity: dict[str, str] = {}
    for point_id, info in sorted(points.items()):
        if info.get("subject") and info["event_type"] in subject_events:
            storage_of_identity.setdefault(
                _identity(info, identity=identity),
                info.get("subject_storage") or STORAGE_UNKNOWN,
            )

    # The stage 5B query-time composition: a callee's through-the-parameter
    # write joins to the caller's argument root. The composed key is built
    # here, with this module's identity, from the caller-side facts the
    # table carried raw plus the callee row's own member path -- so
    # `p->flags` written through a parameter groups with the caller's
    # `r->flags` accesses and the pair the sparse graph could not see
    # becomes a candidate. The table only binds writes that go through the
    # parameter (a member path, or a whole-object deref): a bare slot write
    # rebinds the callee's own name and composing it would claim a write to
    # the caller's object that never happened. The rows keep their own
    # parameter identity above: nothing moves, this only adds. Two call
    # sites that pass the same root compose the same key; a row is appended
    # once per key.
    composed_keys: dict[str, list[dict[str, Any]]] = {}
    if bindings is not None:
        for point_id, records in sorted(bindings.bindings.items()):
            info = points.get(point_id)
            if info is None:
                continue
            for record in records:
                root = dict(record["caller_facts"])
                root["subject_member"] = info.get("subject_member", "")
                key = _identity(root, identity=identity)
                if not key:
                    continue
                owner = str(info.get("owner_symbol_id", ""))
                members = by_subject.setdefault(key, {}).setdefault(owner, [])
                if point_id not in members:
                    members.append(point_id)
                display.setdefault(key, record["arg_root"])
                storage_of_identity.setdefault(
                    key, record["caller_facts"].get("subject_storage") or STORAGE_UNKNOWN
                )
                composed_keys.setdefault(key, []).append({**record, "event": point_id})
    for key, methods in sorted(by_subject.items()):
        if len(methods) < 2:
            continue
        multi_method += 1
        multi_method_by_storage[storage_of_identity.get(key, STORAGE_UNKNOWN)] += 1
        name = display[key]
        # `--subject` still filters on the spelling. It is the entry point a
        # person types, and it was never a promise about which object they
        # meant; the candidate's identity is what says that.
        if subject is not None and name != subject:
            continue
        # Everything the pair loop asks about one side depends on that side
        # alone: the access kind (and whether it was decided or is a proxy),
        # whether the name is touched atomically, and which lock gates it.
        # Computing them inside the pair loop made the cost of a name quadratic
        # in the number of methods touching it, and on a real repository that
        # is the whole query's runtime.
        #
        # `_protection` takes only the method's control-flow points. An access
        # event has no position in the graph, so domination is untestable for
        # it and feeding it in would read as "not gated" -- which would strip
        # `resolved` from subjects the graph has genuinely proven protected.
        # The access events ride in the same list for kind and evidence.
        sides: dict[str, tuple[str, bool, bool, dict]] = {}

        def cf_points(point_ids: list[str]) -> list[str]:
            return [
                point_id for point_id in point_ids
                if points[point_id]["event_type"] not in RACE_ACCESS_EVENTS
            ]

        def side(owner: str) -> tuple[str, bool, bool, dict]:
            if owner not in sides:
                point_ids = methods[owner]
                kind, decided = _access_kind(points, point_ids)
                sides[owner] = (
                    kind, decided,
                    _touches_atomically(points, point_ids, key, identity=identity),
                    _protection(
                        methods_index[owner], points, adjacency, cf_points(point_ids), max_hops,
                        identity=identity,
                    ),
                )
            return sides[owner]

        method_list = sorted(methods)
        for index, owner_a in enumerate(method_list):
            for owner_b in method_list[index + 1:]:
                kind_a, decided_a, atomic_a, protection_a = side(owner_a)
                kind_b, decided_b, atomic_b, protection_b = side(owner_b)
                if kind_a == "read" and kind_b == "read":
                    read_read_pairs += 1
                    continue
                if atomic_a and atomic_b:
                    disqualified_atomic += 1
                    continue
                if (
                    protection_a["status"] == RESOLVED
                    and protection_b["status"] == RESOLVED
                    and protection_a["lock"] == protection_b["lock"]
                ):
                    disqualified_same_lock += 1
                    continue
                # A pair is decided only when *both* sides read real access
                # events; one proxy side makes the whole pair's kind claim a
                # proxy claim, and the split has to say so.
                if decided_a and decided_b:
                    access_decided_pairs += 1
                else:
                    access_proxy_pairs += 1
                candidates.append(
                    _race_candidate(
                        name, key, owner_a, owner_b, methods[owner_a], methods[owner_b],
                        kind_a, kind_b, decided_a, decided_b,
                        protection_a, protection_b, points, names,
                        spawn_index,
                    )
                )

    candidates.sort(key=Candidate.sort_key)
    if bindings is not None:
        # Tag the candidates the composition created, with the records that
        # made them: an auditor reads the join strength (`declared` against
        # the registered `_by_root` spelling) straight off the candidate.
        for candidate in candidates:
            records = composed_keys.get(candidate.subject["identity"])
            if records:
                candidate.metadata["parameter_binding"] = records
    truncated = len(candidates) > max_candidates
    coverage = {
        "subjects_considered": considered,
        "subjects_multi_method": multi_method,
        # The denominator the stage's headline number has to be read against.
        # A multi-method subject is what a race candidate is made of, and the
        # storage class says what kind of object it is: a `local` or a
        # `parameter` cannot appear in two methods under a declaration
        # identity -- that is the whole of the 1.1 reduction -- so whatever is
        # left is `field`, `file_static`, `global`, or a name this file never
        # declares. Without this breakdown "817 subjects survived" is a number
        # with no explanation attached.
        "subjects_by_storage": dict(sorted(by_storage.items())),
        "multi_method_subjects_by_storage": dict(sorted(multi_method_by_storage.items())),
        "synchronization_subjects": len(synchronization),
        # The counterweight to `subjects_multi_method`, and the number that says
        # how much of the stage's 1.1 reduction is the identity and how much is
        # this subtraction. Under a name join the two sets overlapped by
        # spelling; under an identity join they are two sets of objects, and the
        # overlap is a fact worth counting rather than assuming.
        "synchronization_subjects_matched_by_identity": len(
            synchronization & set(by_subject)
        ),
        # What the loose policy would have removed -- a race on `index->field`
        # counts as protected because a lock somewhere is on `index->something`.
        # Reported and *not* applied: the lock is on a different member, and a
        # policy that quietens real races is not one this query adopts without
        # a number in front of it.
        "synchronization_subjects_matched_by_root": root_matched,
        # The 5B thread-local exemption's own accounting: how many subjects and
        # methods the pairing declined on. The events themselves stay counted
        # in `subjects_considered` and `access_events` -- the exemption is a
        # pairing policy, not a deletion.
        "exempt_thread_local_subjects": len(exempt_subjects),
        "exempt_thread_local_methods": len(exempt_methods),
        "read_read_pairs": read_read_pairs,
        # The access layer's own accounting. `access_events` is every READ/WRITE
        # row the database holds -- the denominator for how much of the pair
        # space the layer covers. `access_proxy_pairs` is the part of the
        # candidate space still decided by the syntactic proxy; it should fall
        # as the layer fills in, and it must never be reported as zero unless
        # it really is.
        "access_events": access_events,
        "access_events_by_storage": dict(sorted(access_events_by_storage.items())),
        "access_decided_pairs": access_decided_pairs,
        "access_proxy_pairs": access_proxy_pairs,
        # The unknown-storage bucket mixes cross-file globals (the valuable
        # subjects) with names the declaration index cannot resolve (type
        # names, macro arguments). Reporting how many candidates stand on each
        # is what keeps that mixture visible instead of buried in a total.
        "candidates_by_identity_storage": dict(sorted(
            Counter(
                storage_of_identity.get(candidate.subject["identity"], STORAGE_UNKNOWN)
                for candidate in candidates
            ).items()
        )),
        "disqualified_atomic_both_sides": disqualified_atomic,
        "disqualified_same_lock": disqualified_same_lock,
        "spawn_points": len(spawns),
        "join_points": len(joins),
        "resolved_thread_functions": len(thread_functions),
        "may_parallel_decided": bool(spawns),
        "candidates": len(candidates),
        "candidates_by_key": _count_by_key(candidates),
        "max_hops": max_hops,
        "semantic_points": len(points),
        "semantic_edges": sum(len(item) for item in adjacency.values()),
        "points_by_type": _points_by_type(points),
    }
    if bindings is not None:
        # The 5B composition's own accounting, absent from every pre-5B run:
        # how many candidates stand on a composed pair, how many arguments
        # the join declined on, and the join's full counter set.
        coverage["parameter_binding_pairs"] = sum(
            1 for candidate in candidates
            if candidate.subject["identity"] in composed_keys
        )
        coverage["parameter_binding_unjoined"] = (
            bindings.coverage.get("arguments_seen", 0)
            - bindings.coverage.get("arguments_bound", 0)
        )
        coverage["parameter_binding_join"] = dict(sorted(bindings.coverage.items()))
    return DefectQueryResult(
        query=RACE_CONDITION, candidates=tuple(candidates[:max_candidates]),
        coverage=coverage, truncated=truncated,
    )


def _access_kind(
    points: Mapping[str, Mapping[str, Any]], point_ids: Sequence[str]
) -> tuple[str, bool]:
    """The access kind of one side, and whether the answer was really decided.

    Stage 5A made READ/WRITE events, so the kind is a fact when the walker saw
    the access: any WRITE makes the side a write, otherwise a seen access makes
    it a read. When the side has no access event at all -- a database built
    before 5A, or a subject whose every touch sits in an expression the walker
    did not visit -- the syntactic proxy answers, and the returned flag says
    so. The flag is load-bearing: the candidate's metadata reports which sides
    are facts, and the coverage split counts proxy pairs rather than letting
    them pass silently as decided ones.
    """
    decided = False
    for point_id in point_ids:
        info = points[point_id]
        if info["event_type"] not in RACE_ACCESS_EVENTS:
            continue
        decided = True
        if info["event_type"] == "WRITE":
            return "write", True
    if decided:
        return "read", True
    # The proxy: the only signal that a touch mutates, on a database with no
    # access events, is that the source assigned to it, allocated it, or
    # released it.
    for point_id in point_ids:
        info = points[point_id]
        if info["event_type"] in {"ALLOC", "RELEASE"} or info.get("subject_from") == "assignment":
            return "write", False
    return "read", False


def _touches_atomically(
    points: Mapping[str, Mapping[str, Any]],
    point_ids: Sequence[str],
    identity_key: str,
    *,
    identity: bool = True,
) -> bool:
    return any(
        points[point_id]["event_type"] == "ATOMIC"
        and _identity(points[point_id], identity=identity) == identity_key
        for point_id in point_ids
    )


def _method_index(
    points: Mapping[str, Mapping[str, Any]],
    reverse: Mapping[str, list[str]],
    *,
    identity: bool = True,
) -> dict[str, dict[str, Any]]:
    """Per-method facts the protection test needs, computed once.

    The race query asks "does one lock gate this access" for every (subject,
    method) pair, and each ask walks the method. Indexing the method's points,
    its entry points and its unlock points once turns a per-pair cost into a
    per-method one -- which is what keeps the pair loop affordable on a
    repository with thousands of methods.

    Entry points are the method's points with no predecessor inside the
    method: edges are intra-owner, so "no incoming edge" is exactly "control
    flow can start here".
    """
    owners: dict[str, list[str]] = {}
    for point_id, info in points.items():
        owners.setdefault(str(info.get("owner_symbol_id", "")), []).append(point_id)

    index: dict[str, dict[str, Any]] = {}
    for owner, point_ids in owners.items():
        owned = set(point_ids)
        entries = sorted(
            point_id for point_id in point_ids
            if not (set(reverse.get(point_id, ())) & owned)
        )
        locks = sorted(
            point_id for point_id in point_ids if points[point_id]["event_type"] == "LOCK"
        )
        unlocks: dict[str, set[str]] = {}
        for point_id in point_ids:
            info = points[point_id]
            if info["event_type"] == "UNLOCK" and info.get("subject"):
                unlocks.setdefault(_identity(info, identity=identity), set()).add(point_id)
        index[owner] = {
            "points": sorted(point_ids),
            "entries": entries,
            "locks": locks,
            "unlocks": {name: frozenset(ids) for name, ids in unlocks.items()},
        }
    return index


def _blocked_by_lock(
    adjacency: Mapping[str, list[tuple[str, str, frozenset[str]]]],
    points: Mapping[str, Mapping[str, Any]],
    entries: Sequence[str],
    target: str,
    lock_point: str,
    max_hops: int,
) -> bool:
    """Does every path from the method's entries to `target` go through
    `lock_point`?

    This is domination, and the definition is the test: block the lock and ask
    whether the target is still reachable. The ancestor-set formulation is the
    one that looks right and is not -- a point before the lock is always an
    ancestor of an access after it, so "all ancestors are inside the lock's
    closure" is never true for a lock taken mid-method.
    """
    paths, _truncated = find_paths(
        adjacency, points,
        starts=entries,
        is_target=lambda info: info is points[target],
        blocked_points=(lock_point,),
        max_hops=max_hops,
        max_paths=1,
    )
    return not paths


def _protection(
    method: Mapping[str, Any],
    points: Mapping[str, Mapping[str, Any]],
    adjacency: Mapping[str, list[tuple[str, str, frozenset[str]]]],
    accesses: Sequence[str],
    max_hops: int,
    *,
    identity: bool = True,
) -> dict[str, Any]:
    """Whether one lock dominates every access of a name inside one method.

    `resolved` means a single LOCK point gates every access -- blocking it
    disconnects the access from the method's entry -- and is still held when
    the access happens. That is the only form of "protected" the graph can
    prove: a lock merely taken somewhere in the method says nothing.

    `ambiguous` means locks exist but none gates the access. `unresolved` means
    the method holds a lock whose subject could not be recovered, so what it
    guards is unknown -- which is a different claim from knowing it guards
    nothing.

    The method's own LOCK points are what get tested, not the subject's: a
    lock's subject is the mutex, never the data it guards, so a method that
    locks `g_lock` around `g_total` has no LOCK point carrying `g_total`.

    Two approximations are real and named in `missing_evidence`: a lock taken
    by a callee is invisible here, and so is a lock the caller already holds.
    """
    lock_points = method["locks"]
    if not lock_points:
        return {"status": AMBIGUOUS, "lock": "", "display": "", "detail": "no lock in this method"}
    if not accesses:
        # Stage 5A: an access event has no position in the control-flow graph,
        # so no lock can be tested against it. This must not fall through to
        # the `all(...)` below -- `all` over an empty sequence is True, and a
        # subject whose only touches are plain writes (`counter++`) would read
        # as gated by every lock in the method: a false `resolved` with the
        # confidence bump to match, on exactly the population the access layer
        # added. "Nothing can be said" is the honest status, and it is
        # `ambiguous`, not `unresolved` -- the lock subjects are fine; it is
        # the access that has nothing for the test to reach.
        return {
            "status": AMBIGUOUS, "lock": "", "display": "",
            "detail": "the access has no control-flow point, so nothing can be "
                      "said about what gates it",
        }

    for lock_point in lock_points:
        lock_key = _identity(points[lock_point], identity=identity)
        if not lock_key:
            continue
        if all(
            _blocked_by_lock(
                adjacency, points, method["entries"], access, lock_point, max_hops
            )
            and _holds_before(
                adjacency, points, lock_point, access, lock_key,
                method["unlocks"], max_hops,
            )
            for access in accesses
        ):
            # `lock` is the identity, because the caller compares two sides'
            # protections to decide whether both hold *the same* lock. `display`
            # is what the fact prints: a reader is owed the spelling.
            return {
                "status": RESOLVED, "lock": lock_key, "detail": "",
                "display": points[lock_point].get("subject", ""),
            }

    if any(not points[point_id].get("subject") for point_id in lock_points):
        return {
            "status": UNRESOLVED, "lock": "", "display": "",
            "detail": "a lock with no recoverable subject",
        }
    reachable = [
        lock_point
        for lock_point in lock_points
        if any(access in _closure(lock_point, adjacency, max_hops) for access in accesses)
    ]
    return {
        "status": AMBIGUOUS,
        "lock": _identity(points[reachable[0]], identity=identity) if reachable else "",
        "display": points[reachable[0]].get("subject", "") if reachable else "",
        # The detail has to match what was actually established: when no lock
        # reaches any access there is no reachability claim to retract.
        "detail": (
            "a lock reaches the access but does not gate it" if reachable
            else "no lock in this method reaches the access"
        ),
    }


def _access_summary(
    points: Mapping[str, Mapping[str, Any]],
    point_ids: Sequence[str],
    kind: str,
    decided: bool,
) -> dict[str, Any]:
    """One side's access evidence: the kind, whether it is a fact, and the
    event ids that say so.

    `writer_event` / `reader_event` are what the acceptance's evidence bundle
    quotes -- an id with a span, not a re-derivation. Empty when the side has
    no access event, which is exactly when `decided` is False and the kind is
    the syntactic proxy's answer.
    """
    writer = reader = ""
    for point_id in sorted(point_ids):
        event_type = points[point_id]["event_type"]
        if event_type == "WRITE" and not writer:
            writer = point_id
        elif event_type == "READ" and not reader:
            reader = point_id
    return {"kind": kind, "decided": decided, "writer_event": writer, "reader_event": reader}


def _race_candidate(
    name: str,
    identity_key: str,
    owner_a: str,
    owner_b: str,
    points_a: Sequence[str],
    points_b: Sequence[str],
    kind_a: str,
    kind_b: str,
    decided_a: bool,
    decided_b: bool,
    protection_a: Mapping[str, Any],
    protection_b: Mapping[str, Any],
    points: Mapping[str, Mapping[str, Any]],
    names: Mapping[str, str] | None,
    spawn_index: Mapping[str, Any],
) -> Candidate:
    protected = sum(
        1 for protection in (protection_a, protection_b) if protection["status"] == RESOLVED
    )
    anchor = min(points_a[0], points_b[0])
    facts = tuple(
        _point_fact(point_id, points[point_id], names)
        for point_id in sorted({*points_a, *points_b})
        if points[point_id]["event_type"] != "LOCK"
    )
    uncertain = [
        _uncertain(
            "MAY_PARALLEL", decisive=True,
            detail=_may_parallel_detail(spawn_index, owner_a, owner_b, names),
        ),
        _uncertain(
            "PROTECTED_BY", decisive=True, status=protection_a["status"],
            method=_method_name(points[points_a[0]], names),
            lock=protection_a["display"], detail=protection_a["detail"],
        ),
        _uncertain(
            "PROTECTED_BY", decisive=True, status=protection_b["status"],
            method=_method_name(points[points_b[0]], names),
            lock=protection_b["display"], detail=protection_b["detail"],
        ),
        _uncertain(
            "ACCESS_KIND", decisive=True,
            detail=(
                f"access kind from READ/WRITE events: {kind_a} vs {kind_b}"
                if decided_a and decided_b
                else f"write/read decided syntactically: {kind_a} vs {kind_b}"
            ),
        ),
    ]
    return Candidate(
        defect_key="1.1",
        query=RACE_CONDITION,
        subject={
            "kind": "shared_name",
            "name": name,
            "identity": identity_key,
            "method": _method_name(points[anchor], names),
            "owner_symbol_id": str(points[anchor].get("owner_symbol_id", "")),
        },
        anchor=anchor,
        # Both methods, canonically ordered, because the anchor does not say
        # which side it came from: it is the smaller *point* id, and point ids
        # are content hashes with no relation to the owner ids. Naming only the
        # larger owner -- which is often not the anchor's side -- gave every
        # pair that shared an anchor the same discriminator, and with it the
        # same evidence id: on redis-50, 889 distinct candidates minted one
        # citation. The pair is the candidate, so the pair is the name.
        #
        # Built from the identity rather than the spelling for the same reason:
        # `int n` in `f` and `int n` in `g` was a candidate on redis-50 and is
        # two variables here, so the id has to move with the object.
        discriminator=f"{identity_key}|{min(owner_a, owner_b)}|{max(owner_a, owner_b)}",
        resolution_status=AMBIGUOUS,
        confidence=RACE_CONFIDENCE[protected],
        facts=facts,
        uncertain_facts=tuple(uncertain),
        paths=(),
        source_evidence=_source_evidence(sorted({*points_a, *points_b}), points),
        # The READ/WRITE half of the evidence is no longer missing: the events
        # exist and are cited in `facts` and `metadata["access"]`. What is
        # missing is the relation side -- whether the contexts can run
        # concurrently, whether the lock actually guards the object, whether
        # the object tolerates the race -- which is the DFG's (stage 5B).
        missing_evidence=(
            _MISSING_CONCURRENCY, _MISSING_LOCK_GUARDS, _MISSING_WEAK_CONSISTENCY,
        ),
        metadata={
            "methods": [
                item for item in (
                    _method_name(points[points_a[0]], names),
                    _method_name(points[points_b[0]], names),
                ) if item
            ],
            "anchor_span": {
                "relative_path": points[anchor].get("relative_path", ""),
                "start_line": points[anchor].get("start_line", 0),
                "end_line": points[anchor].get("end_line", 0),
            },
            "exit_point": "",
            # `access_kinds` keeps its original shape ({owner: kind}) for the
            # consumers it already has; `access` is the stage 5A widening --
            # per side, whether the kind is a fact, and which events say so.
            "access_kinds": {owner_a: kind_a, owner_b: kind_b},
            "access": {
                owner_a: _access_summary(points, points_a, kind_a, decided_a),
                owner_b: _access_summary(points, points_b, kind_b, decided_b),
            },
        },
    )


def _short_name(qualified_name: str) -> str:
    return qualified_name.rsplit(".", 1)[-1] if qualified_name else ""


def _spawn_index(points: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Which methods start which thread entry points, read once per query.

    Rebuilding this per candidate made the query's cost the product of its
    candidate count and the repository's point count.
    """
    entry_of: dict[str, set[str]] = {}
    entries: set[str] = set()
    for info in points.values():
        if info["event_type"] != "THREAD_SPAWN":
            continue
        function = info.get("thread_function", "")
        if not function:
            continue
        entry_of.setdefault(str(info.get("owner_symbol_id", "")), set()).add(function)
        entries.add(function)
    return {"entry_of": entry_of, "entries": entries}


def _may_parallel_detail(
    spawn_index: Mapping[str, Any],
    owner_a: str,
    owner_b: str,
    names: Mapping[str, str] | None,
) -> str:
    """The strongest true statement about concurrency the spawn points support.

    The status is `ambiguous` either way -- a spawn point is not a schedule --
    but the *reason* is worth distinguishing, because "one method starts the
    other" is a much shorter leap to a real race than "threads exist somewhere
    in this repository". Matching a spawn's entry point to a method needs the
    names table: the spawn records the identifier as written, and a method is
    a symbol id, so without names the two cannot be lined up and the weakest
    reason is the honest one.
    """
    entry_of, all_entries = spawn_index["entry_of"], spawn_index["entries"]

    short_a = _short_name((names or {}).get(owner_a, ""))
    short_b = _short_name((names or {}).get(owner_b, ""))
    if short_b and short_b in entry_of.get(owner_a, ()):
        return f"this method starts {short_b}, which owns the other access"
    if short_a and short_a in entry_of.get(owner_b, ()):
        return f"the other method starts {short_a}"
    if short_a and short_b and {short_a, short_b} <= all_entries:
        return "both methods are thread entry points"
    if entry_of.get(owner_a) or entry_of.get(owner_b):
        return "one of the methods starts a thread"
    return "the repository starts threads somewhere, so neither method is ordered"


# ----------------------------------------------------------------------
# stage 6 -- the DFG-consuming queries (3.1, 4.3, 4.2, and taint)
#
# Four queries, one shared question. Stage 5B minted the data-flow edges and
# nothing consumed them but the contracts layer; these four read them directly,
# and each one's decisive test is a control-flow question asked *about* a flow
# edge: can the definition reach the use at all, and does anything stand
# between them that makes the pair legitimate.
#
# The elimination discipline is the one stage 5C set: a candidate is removed
# only when a graph fact proves the pattern cannot hold, and a question the
# graph cannot answer leaves the candidate standing with the question named in
# its `missing_evidence`. `_Reachability` below is what makes the first half of
# that affordable; the second half is why every query here has a state called
# `ambiguous` and a coverage block that counts what it declined to decide.


class _Reachability:
    """Node reachability over the control-flow graph, memoised per source.

    `find_paths` enumerates *paths*: every queue entry carries its own visited
    set, because two genuinely different ways round a loop are two facts. That
    is the right instrument for an evidence path and the wrong one for an
    unreachability question -- the search cannot collapse the exponential set
    of routes into one node visit, and a `max_hops=4096` call on the acceptance
    corpora never returned. What the eliminations below ask is the node
    question ("can this release reach this use at all"), which is one
    breadth-first pass per source, exact at any depth, and memoised because one
    source is asked about many targets.

    `state` answers in three values and the third is not a detail. A point with
    no position in the control-flow graph has nothing to search from and nothing
    to be found; `REACH_UNKNOWN` says exactly that, and every caller reads it as
    "cannot eliminate" rather than as "unreachable".

    Reading it the other way is not hypothetical, and it happened twice. A first
    cut eliminated all 52 of redis-50's null-flow candidates by treating the
    structural `false` on the flow edge as a verdict. Then the target side
    turned out to have the same shape: READ and WRITE points are in *no*
    control-flow edge at all (`_EDGELESS_EVENT_TYPES`), so "is this node in the
    graph" answers no for every one of them, and a query reading that as
    "unreachable" eliminates them structurally. On llama.cpp-69 that is 27 of
    the 47 `free_to_use` edges -- and a read through a released pointer is the
    defect the query exists to find, so the mistake deletes findings.
    """

    def __init__(
        self,
        adjacency: Mapping[str, list[tuple[str, str, frozenset[str]]]],
        points: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self._adjacency = adjacency
        self._points = points
        self._reached: dict[str, frozenset[str]] = {}
        # Every point the control-flow graph mentions, as a source or as a
        # destination. Membership is the test for "this point has a position",
        # and it is read off the graph rather than off a list of event types:
        # the list lives in the walker and a second copy here would drift.
        self._nodes: frozenset[str] = frozenset(
            set(adjacency)
            | {dst for neighbours in adjacency.values() for _e, dst, _f in neighbours}
        )

    def has_position(self, point_id: str) -> bool:
        return point_id in self._nodes

    def from_(self, source: str) -> frozenset[str]:
        """Every point reachable from `source`, in one pass, computed once."""
        reached = self._reached.get(source)
        if reached is None:
            seen: set[str] = set()
            queue: deque[str] = deque([source])
            while queue:
                for _edge_id, dst, _flags in self._adjacency.get(queue.popleft(), ()):
                    if dst not in seen:
                        seen.add(dst)
                        queue.append(dst)
            reached = frozenset(seen)
            self._reached[source] = reached
        return reached

    def state(self, source: str, target: str) -> str:
        """`reachable`, `disjoint`, or `unknown` when the graph cannot say.

        The two `unknown` clauses are different facts and both mean "do not
        eliminate". A source with no outgoing edge is a point the walker does
        not step through -- a WRITE, or a node the graph only ever arrives at --
        so there is nothing to search from. A target the graph never mentions
        has no position to be reached.
        """
        if source not in self._adjacency or target not in self._nodes:
            return REACH_UNKNOWN
        return REACHABLE if target in self.from_(source) else DISJOINT

    def null_side_reaches(self, check: str, target: str) -> bool:
        """Whether the check's *null* arm can still reach `target`.

        The seeding is `unchecked_dereferences`' own, read in the opposite
        direction: `if (p == NULL)` leaves through the true branch when the
        subject is null and `if (p != NULL)` through the false one, which is
        what `polarity` records. Callers pass only points whose polarity is one
        of the two null spellings -- a comparison against anything else is not
        a CHECK point at all.

        A target with no position in the graph answers True, which reads oddly
        until you look at what the caller does with it: the answer is used to
        *eliminate* the candidate as already guarded, and a question the graph
        cannot answer must not eliminate anything.
        """
        if target not in self._nodes:
            return True
        info = self._points.get(check, {})
        wanted = "branch=true" if info.get("polarity") == "true_when_null" else "branch=false"
        for _edge_id, dst, flags in self._adjacency.get(check, ()):
            if wanted in flags and (dst == target or target in self.from_(dst)):
                return True
        return False


def _alias_groups(points: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, frozenset[str]]]:
    """owner -> {root: roots the annotation ties to it}, in one pass.

    The `alias` annotation is a semicolon-joined list of the roots a bare copy
    joined (`q = p` stamps both ends), and it is per *owner* because two
    methods can each have a `p` and a `q` that have nothing to do with each
    other. Grouping in one pass is not a micro-optimisation: a per-owner
    builder that rescans every point is O(owners x points), which on
    llama.cpp-69 is two thousand owners against two hundred thousand points.
    The first version of the stage 6 probe never returned.
    """
    groups: dict[str, dict[str, set[str]]] = {}
    for point_id in sorted(points):
        info = points[point_id]
        annotation = str(info.get("alias", ""))
        subject = str(info.get("subject", ""))
        if not annotation or not subject:
            continue
        owner = str(info.get("owner_symbol_id", ""))
        for other in annotation.split(";"):
            if not other or other == subject:
                continue
            group = groups.setdefault(owner, {})
            group.setdefault(subject, set()).add(other)
            group.setdefault(other, set()).add(subject)
    return {
        owner: {root: frozenset(roots) for root, roots in group.items()}
        for owner, group in groups.items()
    }


def _alias_closure(
    group: Mapping[str, frozenset[str]], root: str
) -> frozenset[str]:
    """Every spelling the copy annotation ties to `root`, transitively.

    Takes *one owner's* group, not the owner-keyed table: the first version took
    the table and looked the owner up itself, and both callers passed the group
    they had already looked up, so every closure came back as the bare root and
    neither `double_free`'s alias pairing nor `use_after_free`'s alias-expanded
    call composition ever fired. The corpora did not show it -- no release pair
    in either one is joined by the annotation -- and a fixture did. The shape
    is narrowed to the one callers actually hold so it cannot be passed wrong.

    Includes `root` itself, so a caller can ask "is this spelling one of them"
    without a separate equality test. An unknown root closes to just the root,
    which is the honest answer: no annotation joined it to anything.
    """
    links = group
    seen = {root}
    stack = [root]
    while stack:
        for other in links.get(stack.pop(), ()):
            if other not in seen:
                seen.add(other)
                stack.append(other)
    return frozenset(seen)


def _flow_edges(edges: Iterable[Any]) -> list[tuple[str, str, Mapping[str, Any]]]:
    """`(src, dst, metadata)` for every DATA_FLOW_TO row, in edge order.

    In edge order rather than sorted, because the order is the writer's and the
    queries below sort what they build. The metadata is where `flow_class`,
    `via`, `via_copies` and `arg_root` live -- the facts that make an edge a
    candidate for one pattern and not another.
    """
    rows: list[tuple[str, str, Mapping[str, Any]]] = []
    for edge in edges:
        if _edge_field(edge, "relation") != DATA_FLOW_TO:
            continue
        rows.append((
            str(_edge_field(edge, "src_event_id")),
            str(_edge_field(edge, "dst_event_id")),
            _edge_field(edge, "metadata") or {},
        ))
    return rows


def _position(info: Mapping[str, Any]) -> tuple[int, str, int]:
    """A point's source order inside its method.

    Line first, because a reader's intuition is the written order; then the
    event type and the per-(owner, type) ordinal, which is what orders two
    points on one line (`free(p); free(q);`). This is for *deciding* order
    inside one method and never for a discriminator: a line number moves when
    the file is reindented, and an id that moves cannot be cited twice.
    """
    return (
        int(info.get("start_line", 0) or 0),
        str(info.get("event_type", "")),
        int(info.get("ordinal", 0) or 0),
    )


def _step(info: Mapping[str, Any]) -> str:
    """`TYPE:ordinal`, a stable name for a point inside its method.

    Ordinal is a per-(owner, event type) source-order counter, so this moves
    when a point moves and not when the file is reindented -- the property a
    discriminator needs, and the reason no discriminator here carries a line
    number.
    """
    return f"{info.get('event_type', '')}:{int(info.get('ordinal', 0) or 0)}"


def _statement_key(info: Mapping[str, Any]) -> tuple[str, str, int]:
    return (
        str(info.get("file_id", "")),
        str(info.get("owner_symbol_id", "")),
        int(info.get("start_line", 0) or 0),
    )


def _record_elimination(
    ruling: Mapping[str, str],
    defect_key: str,
    anchor: str,
    info: Mapping[str, Any],
    names: Mapping[str, str] | None,
    by_reason: Counter,
    records: list[dict[str, Any]],
) -> None:
    """Count one elimination by reason and keep its record, up to the cap.

    The count has to be complete -- a coverage block whose totals move with the
    cap is not a coverage block -- while the records are the sampling frame,
    and `DISQUALIFIED_EVIDENCE_CAP` is set above the acceptance corpora's
    counts so the frame stays whole there.
    """
    by_reason[str(ruling["reason"])] += 1
    if len(records) < DISQUALIFIED_EVIDENCE_CAP:
        records.append(_elimination_record(ruling, defect_key, anchor, info, names))


def _disqualification_coverage(
    by_reason: Mapping[str, int], records: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """The elimination half of a stage 6 coverage block.

    The same three keys the contracts layer uses, so the eight queries have one
    schema and a comparison column does not need a per-query reader.
    """
    return {
        "disqualified": dict(sorted(by_reason.items())),
        "disqualified_evidence": [dict(record) for record in records],
        "disqualified_total": sum(by_reason.values()),
    }


def _flow_path(
    adjacency: Mapping[str, list[tuple[str, str, frozenset[str]]]],
    points: Mapping[str, Mapping[str, Any]],
    source: str,
    target: str,
    max_hops: int,
    max_paths: int,
) -> tuple[tuple[dict[str, Any], ...], bool]:
    """The evidence paths from `source` to `target`, and whether the walk was
    cut short.

    Evidence only. The *decision* that the two are connected is
    `_Reachability`'s, because this enumerates paths and a bounded enumeration
    that finds nothing is not a proof of disconnection. A source with no
    control-flow position yields nothing here rather than an error: it is a
    fact about the graph, not a failure.
    """
    if source not in adjacency:
        return (), False
    target_info = points.get(target)
    if target_info is None:
        return (), False
    paths, truncated = find_paths(
        dict(adjacency),
        dict(points),
        starts={source},
        is_target=lambda info: info is target_info,
        max_hops=max_hops,
        max_paths=max_paths,
    )
    return tuple(path.to_dict() for path in paths), truncated


_PATH_DISJOINT = {
    "reason": "PATH_DISJOINT",
    "provenance": "graph:control_reaches",
    "detail": (
        "the definition cannot reach this use on the control-flow graph, so the "
        "two are not on one path and the pattern cannot hold"
    ),
}
_GUARDED = {
    "reason": "GUARDED",
    "provenance": "graph:null_side",
    "detail": (
        "every check on this identity has a null arm that cannot reach the "
        "dereference, so the dereference runs only when the value is non-null"
    ),
}
_CALL_MEMBER_MISMATCH = {
    "reason": "CALL_MEMBER_MISMATCH",
    "provenance": "graph:subject_member",
    "detail": (
        "the released object is a member path and the call's first argument is a "
        "provably different member of the same root, which is a different object"
    ),
}
_CALL_PATH_DISJOINT = {
    "reason": "CALL_PATH_DISJOINT",
    "provenance": "graph:control_reaches",
    "detail": (
        "the release cannot reach the call on the control-flow graph, so the call "
        "does not execute after the release on any path"
    ),
}
_CALL_INTERVENING_DEF = {
    "reason": "CALL_INTERVENING_DEF",
    "provenance": "graph:flow_key",
    "detail": (
        "the identity is defined again between the release and the call, so the "
        "value the call uses is not the released one"
    ),
}
_USE_IS_RELEASE = {
    "reason": "USE_IS_RELEASE",
    "provenance": "graph:control_reaches",
    "detail": (
        "the call releases an object aliasing the one already released, which is "
        "matrix 4.2's question rather than this one"
    ),
}
_MEMBER_MISMATCH = {
    "reason": "MEMBER_MISMATCH",
    "provenance": "graph:subject_member",
    "detail": (
        "the two releases name different members of the same root, and a member "
        "is part of the object"
    ),
}
_INTERVENING_DEF = {
    "reason": "INTERVENING_DEF",
    "provenance": "graph:flow_key",
    "detail": (
        "the identity is defined again between the two releases, so the second "
        "release is of a new value"
    ),
}
_USE_READS_POINTER_VALUE = {
    "reason": "USE_READS_POINTER_VALUE",
    "provenance": "graph:event_type",
    "detail": (
        "the use is a null comparison, which reads the pointer's value rather "
        "than the object it pointed at"
    ),
}
_ARGUMENT_POSITION_UNVERIFIABLE = {
    "reason": "ARGUMENT_POSITION_UNVERIFIABLE",
    "provenance": "graph:arguments",
    "detail": (
        "the rooted argument list is compacted -- arguments with no root "
        "identifier are dropped -- so this index is not provably the declared "
        "position, and a position claim that cannot be checked is not made"
    ),
}
_ORDINAL_NOT_DECLARED = {
    "reason": "ORDINAL_NOT_DECLARED",
    "provenance": "declared:taint.toml",
    "detail": "the argument sits at a position the sink does not declare",
}
_SOURCE_AMBIGUOUS_LINE = {
    "reason": "SOURCE_AMBIGUOUS_LINE",
    "provenance": "graph:raw_references",
    "detail": "the defining line carries more than one declared source name",
}
_SINK_AMBIGUOUS_LINE = {
    "reason": "SINK_AMBIGUOUS_LINE",
    "provenance": "graph:raw_references",
    "detail": "the call's line carries more than one declared sink name",
}
_ARG_ROOT_NOT_LISTED = {
    "reason": "ARG_ROOT_NOT_LISTED",
    "provenance": "graph:arguments",
    "detail": (
        "the edge's argument root is not in the call point's argument list, so "
        "the position cannot be located at all"
    ),
}


def null_flow(
    events: Iterable[Any],
    edges: Iterable[Any],
    *,
    subject: str | None = None,
    max_hops: int = 64,
    max_paths: int = 8,
    max_candidates: int = 20,
    names: Mapping[str, str] | None = None,
    identity: bool = True,
    defect_keys: Collection[str] | None = None,
) -> DefectQueryResult:
    """Matrix 3.1: a null value reaching a dereference.

    Two sources, because C spells the pattern two ways. `null_to_deref` is the
    literal -- `p = NULL; *p`, or a chain of copies ending in one. The second is
    `alloc_to_use` whose use is a DEREFERENCE, which is the far more common
    shape: `p = malloc(n); *p` with the failure path never checked. On
    llama.cpp-69 that second source is 618 of the 626 edges.

    Two facts eliminate a candidate. A dereference the definition cannot reach
    is not this definition's dereference (`p = malloc(); if (err) return; *p`
    puts the two on different arms). And a dereference that only executes on the
    non-null side of a check is the checked pattern, not the defect -- that test
    is `unchecked_dereferences` read backwards, seeding from each check's null
    arm instead of looking for dereferences beyond it.

    The middle state is where most candidates land, and the reason is a
    modelling limit rather than a doubt about the code: a check on the identity
    that does *not* eliminate the candidate leaves it `ambiguous` even when the
    null arm still reaches the dereference, because that arm may leave the
    function through a call the graph cannot call `noreturn`. Only "no check at
    all on this identity" is `resolved`.
    """
    points = materialize_points(events, edges)
    adjacency = build_adjacency(edges)
    reach = _Reachability(adjacency, points)
    checks_by_owner: dict[str, list[str]] = {}
    for point_id in sorted(points):
        info = points[point_id]
        if (info["event_type"] == "CHECK" and info.get("subject")
                and info.get("polarity") in NULL_POLARITIES):
            checks_by_owner.setdefault(str(info.get("owner_symbol_id", "")), []).append(point_id)

    population: Counter = Counter()
    states: Counter = Counter()
    by_reason: Counter = Counter()
    records: list[dict[str, Any]] = []
    candidates: list[Candidate] = []
    wanted = _wanted(NULL_FLOW_KEY, defect_keys)
    for source_id, target_id, metadata in _flow_edges(edges):
        target = points.get(target_id)
        if target is None:
            continue
        flow_class = str(metadata.get("flow_class", ""))
        if flow_class == NULL_TO_DEREF:
            kind = NULL_TO_DEREF
        elif flow_class == ALLOC_TO_USE and target["event_type"] == "DEREFERENCE":
            kind = "alloc_to_deref"
        else:
            continue
        population[kind] += 1
        if not wanted or (subject is not None and _identity_display(target) != subject):
            continue
        state = reach.state(source_id, target_id)
        if state == DISJOINT:
            _record_elimination(
                _PATH_DISJOINT, NULL_FLOW_KEY, target_id, target, names, by_reason, records
            )
            continue
        identity_key = _identity(target, identity=identity)
        owner = str(target.get("owner_symbol_id", ""))
        checks = [
            check_id for check_id in checks_by_owner.get(owner, ())
            if identity_key and _identity(points[check_id], identity=identity) == identity_key
        ]
        if checks and not any(reach.null_side_reaches(check_id, target_id) for check_id in checks):
            _record_elimination(
                _GUARDED, NULL_FLOW_KEY, target_id, target, names, by_reason, records
            )
            continue

        uncertain: list[dict[str, Any]] = []
        if not identity_key:
            status = UNRESOLVED
            uncertain.append(_uncertain(
                "SUBJECT_UNRESOLVED", decisive=True,
                detail="the dereference names no target, so the candidate cannot be attributed",
            ))
        else:
            status = RESOLVED
            cap = _identity_cap(target, identity=identity)
            if cap is not None:
                uncertain.append(_uncertain(
                    cap["fact"], decisive=True, detail=cap["detail"],
                    point=target_id, source=_span(target),
                ))
                status = AMBIGUOUS
            if tuple(metadata.get("via_copies", ()) or ()):
                uncertain.append(_uncertain(
                    "ALIAS_PATH", decisive=True,
                    detail=(
                        "the value reached this dereference through a copy, so the "
                        "identity is a same-statement join and not a proof of one object"
                    ),
                    point=target_id, source=_span(target),
                ))
                status = AMBIGUOUS
            if checks:
                uncertain.append(_uncertain(
                    "PARTIAL_GUARD", decisive=True,
                    detail=(
                        "a check on this identity exists but its null arm still reaches "
                        "the dereference, so some route to it is unguarded"
                    ),
                    point=checks[0], source=_span(points[checks[0]]),
                ))
                status = AMBIGUOUS
            if state == REACH_UNKNOWN:
                uncertain.append(_uncertain(
                    "REACH_UNKNOWN", decisive=True,
                    detail=(
                        "the definition has no position in the control-flow graph (a "
                        "write is not an operation the walker steps through), so the "
                        "graph cannot say the dereference runs after it"
                    ),
                    point=source_id,
                    source=_span(points[source_id]) if source_id in points else "",
                ))
                status = AMBIGUOUS

        states[status] += 1
        paths, truncated = _flow_path(
            adjacency, points, source_id, target_id, max_hops, max_paths
        )
        source_info = points.get(source_id, {})
        steps = [source_id, target_id]
        candidates.append(Candidate(
            defect_key=NULL_FLOW_KEY,
            query=NULL_FLOW,
            subject={
                "kind": "dereference",
                "name": _identity_display(target),
                "identity": identity_key,
                "storage": target.get("subject_storage", ""),
                "type": target.get("subject_type", ""),
                "member": target.get("subject_member", ""),
                "method": _method_name(target, names),
                "owner_symbol_id": owner,
            },
            anchor=target_id,
            discriminator=(
                f"{identity_key or 'anonymous'}|{owner}|"
                f"{_step(source_info)}|{_step(target)}"
            ),
            resolution_status=status,
            confidence=NULL_FLOW_CONFIDENCE[status],
            facts=tuple(
                _point_fact(point_id, points[point_id], names)
                for point_id in steps if _citable(point_id, points)
            ),
            uncertain_facts=tuple(uncertain),
            paths=paths,
            source_evidence=_source_evidence(steps, points),
            missing_evidence=(_MISSING_NULLABILITY, _MISSING_PARTIAL_GUARD),
            metadata={
                "flow_class": kind,
                "definition": {
                    "point": source_id,
                    "event": source_info.get("event_type", ""),
                    "source": _span(source_info) if source_info else "",
                    "via_copies": list(metadata.get("via_copies", ()) or ()),
                    "via": list(metadata.get("via", ()) or ()),
                },
                "reach": state,
                "path_recorded": bool(paths),
                "paths_truncated": truncated,
                "anchor_span": {
                    "relative_path": target.get("relative_path", ""),
                    "start_line": target.get("start_line", 0),
                    "end_line": target.get("end_line", 0),
                },
                "methods": [item for item in [_method_name(target, names)] if item],
                "exit_point": "",
            },
        ))

    candidates.sort(key=Candidate.sort_key)
    truncated = len(candidates) > max_candidates
    coverage = {
        "population": dict(sorted(population.items())),
        "states": dict(sorted(states.items())),
        "candidates": len(candidates),
        "candidates_by_key": _count_by_key(candidates),
        "check_points": sum(len(ids) for ids in checks_by_owner.values()),
        "max_hops": max_hops,
        "semantic_points": len(points),
        "semantic_edges": sum(len(item) for item in adjacency.values()),
        "points_by_type": _points_by_type(points),
        **_disqualification_coverage(by_reason, records),
    }
    return DefectQueryResult(
        query=NULL_FLOW, candidates=tuple(candidates[:max_candidates]),
        coverage=coverage, truncated=truncated,
    )


def use_after_free(
    events: Iterable[Any],
    edges: Iterable[Any],
    *,
    subject: str | None = None,
    max_hops: int = 64,
    max_paths: int = 8,
    max_candidates: int = 20,
    names: Mapping[str, str] | None = None,
    identity: bool = True,
    defect_keys: Collection[str] | None = None,
) -> DefectQueryResult:
    """Matrix 4.3: a use of an object after the same identity was released.

    Two halves, and the second exists because the first is nearly empty.
    Stage 5B's `free_to_use` edges are minted in *source order* and are
    path-blind -- `live_free` is never cleared, so a release on one arm pairs
    with a use on another -- and on the acceptance corpora almost every such
    edge connects no path at all: 11 of 11 on redis-50, 46 of 47 on
    llama.cpp-69, and the two sampled by hand were `free(x); return;` cleanup
    arms. The reachability test is therefore not a refinement here; it is what
    separates the flow class from a UAF.

    The second half is the shape 5B does not mint an edge for at all:
    `free(p); sink(p)`. A CALL is not a USE event (stage 3's registered gap), so
    the release kills the live definition and the argument path finds nothing.
    The composition here closes that at query time: for every release, every
    call in the same method whose argument roots include a spelling the copy
    annotation ties to the released root, positioned after it, reachable from
    it, with no redefinition of the identity in between.

    Every candidate from that composition is `ambiguous`, and the reason is
    structural: `_root_identifier` strips `&`, so `f(p)` and `f(&p)` are the
    same fact here, and one of them passes the object out while the other only
    reads it. On redis-50 this composition is not a corner case -- it is the
    bulk of the result, because the corpus's dominant shape is a callee that
    re-allocates through a pointer argument (`len = redisFormatCommand(&cmd,
    ...)`), which no fact in the graph distinguishes from a use.
    """
    points = materialize_points(events, edges)
    adjacency = build_adjacency(edges)
    reach = _Reachability(adjacency, points)
    population: Counter = Counter()
    states: Counter = Counter()
    by_reason: Counter = Counter()
    records: list[dict[str, Any]] = []
    candidates: list[Candidate] = []
    wanted = _wanted(USE_AFTER_FREE_KEY, defect_keys)

    for source_id, target_id, metadata in _flow_edges(edges):
        if str(metadata.get("flow_class", "")) != FREE_TO_USE:
            continue
        target = points.get(target_id)
        if target is None:
            continue
        population["edge"] += 1
        if not wanted or (subject is not None and _identity_display(target) != subject):
            continue
        state = reach.state(source_id, target_id)
        if state == DISJOINT:
            _record_elimination(
                _PATH_DISJOINT, USE_AFTER_FREE_KEY, target_id, target, names, by_reason, records
            )
            continue
        if target["event_type"] == "CHECK":
            _record_elimination(
                _USE_READS_POINTER_VALUE, USE_AFTER_FREE_KEY, target_id, target,
                names, by_reason, records,
            )
            continue
        status = _use_status(
            target, metadata, state, identity=identity, escaping=target["event_type"] == "RETURN"
        )
        candidate = _use_candidate(
            source_id, target_id, target, points, names, status, identity=identity,
            adjacency=adjacency, max_hops=max_hops, max_paths=max_paths,
            flow_class=FREE_TO_USE, composition=False,
        )
        states[status] += 1
        candidates.append(candidate)

    # The query-time composition: `free(p); sink(p)`, which 5B mints no edge
    # for. Grouped per owner because the alias annotation and the point ids are
    # method-local, and built in one pass for the reason `_alias_groups` gives.
    by_owner = _by_owner(points)
    alias_groups = _alias_groups(points)
    releases_by_statement: dict[tuple[str, str, int], list[str]] = {}
    for point_id in sorted(points):
        info = points[point_id]
        if info["event_type"] == "RELEASE" and info.get("subject"):
            releases_by_statement.setdefault(_statement_key(info), []).append(point_id)
    for owner in sorted(by_owner):
        point_ids = by_owner[owner]
        groups = alias_groups.get(owner, {})
        releases = [
            point_id for point_id in point_ids
            if points[point_id]["event_type"] == "RELEASE" and points[point_id].get("subject")
        ]
        if not releases:
            continue
        calls_by_root: dict[str, list[tuple[str, int]]] = {}
        definitions: dict[tuple[str, ...], list[tuple[int, str, int]]] = {}
        for point_id in point_ids:
            info = points[point_id]
            if info["event_type"] == "CALL" and info.get("arguments"):
                for index, argument in enumerate(str(info["arguments"]).split(ARGUMENT_SEPARATOR)):
                    if argument:
                        calls_by_root.setdefault(argument, []).append((point_id, index))
            elif info["event_type"] in DEF_EVENTS:
                definitions.setdefault(_flow_key(info), []).append(_position(info))
        for positions in definitions.values():
            positions.sort()
        for release_id in releases:
            release = points[release_id]
            roots = _alias_closure(groups, str(release["subject"]))
            release_position = _position(release)
            release_member = str(release.get("subject_member", ""))
            between = definitions.get(_flow_key(release), ())
            matching: dict[str, list[int]] = {}
            for root in sorted(roots):
                for call_id, index in calls_by_root.get(root, ()):
                    matching.setdefault(call_id, []).append(index)
            for call_id in sorted(matching):
                call = points[call_id]
                call_position = _position(call)
                if call_position <= release_position:
                    continue
                population["call-composition"] += 1
                if not wanted or (subject is not None and _identity_display(call) != subject):
                    continue
                # A member-expression release is eliminated only when the one
                # argument whose member is recoverable proves a different
                # object. `subject_member` describes the true first argument
                # exactly when `subject_from` is `argument` -- for an assignment
                # right-hand side the subject is the *target*, so
                # `len = redisFormatCommand(&cmd, ...)` has subject `len`.
                if (release_member and sorted(matching[call_id]) == [0]
                        and call.get("subject_from") == "argument"
                        and str(call.get("subject_member", "")) != release_member):
                    _record_elimination(
                        _CALL_MEMBER_MISMATCH, USE_AFTER_FREE_KEY, call_id, call,
                        names, by_reason, records,
                    )
                    continue
                # `free(p); free(q)` with `q` aliasing `p` is matrix 4.2's
                # question. The test is graph-only: a release call has a RELEASE
                # point on its own statement, so a call that shares a statement
                # with a release of an aliasing root is a release and not a use.
                if any(
                    str(points[other].get("subject", "")) in roots
                    for other in releases_by_statement.get(_statement_key(call), ())
                ):
                    _record_elimination(
                        _USE_IS_RELEASE, USE_AFTER_FREE_KEY, call_id, call,
                        names, by_reason, records,
                    )
                    continue
                if reach.state(release_id, call_id) == DISJOINT:
                    _record_elimination(
                        _CALL_PATH_DISJOINT, USE_AFTER_FREE_KEY, call_id, call,
                        names, by_reason, records,
                    )
                    continue
                index_after = bisect.bisect_right(between, release_position)
                if index_after < len(between) and between[index_after] < call_position:
                    _record_elimination(
                        _CALL_INTERVENING_DEF, USE_AFTER_FREE_KEY, call_id, call,
                        names, by_reason, records,
                    )
                    continue
                candidates.append(_use_candidate(
                    release_id, call_id, call, points, names, AMBIGUOUS,
                    identity=identity, adjacency=adjacency, max_hops=max_hops,
                    max_paths=max_paths, flow_class=FREE_TO_USE, composition=True,
                    reach_state=reach.state(release_id, call_id),
                ))
                states[AMBIGUOUS] += 1

    candidates.sort(key=Candidate.sort_key)
    truncated = len(candidates) > max_candidates
    coverage = {
        "population": dict(sorted(population.items())),
        "states": dict(sorted(states.items())),
        "candidates": len(candidates),
        "candidates_by_key": _count_by_key(candidates),
        "call_composition_candidates": sum(
            1 for candidate in candidates
            if candidate.metadata.get("use_from") == "call-composition"
        ),
        "max_hops": max_hops,
        "semantic_points": len(points),
        "semantic_edges": sum(len(item) for item in adjacency.values()),
        "points_by_type": _points_by_type(points),
        **_disqualification_coverage(by_reason, records),
    }
    return DefectQueryResult(
        query=USE_AFTER_FREE, candidates=tuple(candidates[:max_candidates]),
        coverage=coverage, truncated=truncated,
    )


def _use_status(
    target: Mapping[str, Any],
    metadata: Mapping[str, Any],
    state: str,
    *,
    identity: bool,
    escaping: bool,
) -> str:
    """The state a use can carry, given what the graph answered about it.

    `escaping` is the RETURN case: a method that returns a dangling pointer has
    handed the decision to its caller, which is a real finding and not one this
    graph can call resolved. The copy hop and the unanswerable reachability
    question are the same caps the null-flow query applies, for the same
    reason: both are name-level joins rather than proofs of one object.
    """
    if not _identity(target, identity=identity):
        return UNRESOLVED
    if _identity_cap(target, identity=identity) is not None:
        return AMBIGUOUS
    if tuple(metadata.get("via_copies", ()) or ()):
        return AMBIGUOUS
    if state == REACH_UNKNOWN or escaping:
        return AMBIGUOUS
    return RESOLVED


def _use_candidate(
    release_id: str,
    use_id: str,
    use: Mapping[str, Any],
    points: Mapping[str, Mapping[str, Any]],
    names: Mapping[str, str] | None,
    status: str,
    *,
    identity: bool,
    adjacency: Mapping[str, list[tuple[str, str, frozenset[str]]]],
    max_hops: int,
    max_paths: int,
    flow_class: str,
    composition: bool,
    reach_state: str = "",
) -> Candidate:
    """One UAF candidate, from either half of the query.

    The subject is the *use*, because that is where the defect happens: the
    release is a legitimate operation and the finding is that something ran
    after it. The release is carried in the facts and the metadata so a reader
    can see both ends without opening the graph.
    """
    identity_key = _identity(use, identity=identity)
    owner = str(use.get("owner_symbol_id", ""))
    release = points.get(release_id, {})
    paths, truncated = _flow_path(adjacency, points, release_id, use_id, max_hops, max_paths)
    uncertain: list[dict[str, Any]] = []
    if not identity_key:
        uncertain.append(_uncertain(
            "SUBJECT_UNRESOLVED", decisive=True,
            detail="the use names no target, so the candidate cannot be attributed",
        ))
    else:
        cap = _identity_cap(use, identity=identity)
        if cap is not None:
            uncertain.append(_uncertain(
                cap["fact"], decisive=True, detail=cap["detail"],
                point=use_id, source=_span(use),
            ))
    if composition:
        uncertain.append(_uncertain(
            "USE_KIND_UNKNOWN", decisive=True,
            detail=(
                "the use is a call argument, and the graph does not distinguish "
                "passing the pointer (`f(p)`) from passing its address (`f(&p)`)"
            ),
            point=use_id, source=_span(use),
        ))
    elif use["event_type"] == "RETURN":
        uncertain.append(_uncertain(
            "ESCAPING_USE", decisive=True,
            detail="the use returns the released pointer to the caller",
            point=use_id, source=_span(use),
        ))
    if reach_state == REACH_UNKNOWN:
        uncertain.append(_uncertain(
            "REACH_UNKNOWN", decisive=True,
            detail=(
                "the release has no position in the control-flow graph, so the "
                "graph cannot say the use runs after it"
            ),
            point=release_id,
            source=_span(release) if release else "",
        ))
    steps = [release_id, use_id]
    return Candidate(
        defect_key=USE_AFTER_FREE_KEY,
        query=USE_AFTER_FREE,
        subject={
            "kind": "use-after-release",
            "name": _identity_display(use),
            "identity": identity_key,
            "storage": use.get("subject_storage", ""),
            "type": use.get("subject_type", ""),
            "member": use.get("subject_member", ""),
            "method": _method_name(use, names),
            "owner_symbol_id": owner,
        },
        anchor=use_id,
        discriminator=(
            f"{identity_key or 'anonymous'}|{owner}|{_step(release)}|{_step(use)}"
        ),
        resolution_status=status,
        confidence=USE_AFTER_FREE_CONFIDENCE[status],
        facts=tuple(
            _point_fact(point_id, points[point_id], names)
            for point_id in steps if _citable(point_id, points)
        ),
        uncertain_facts=tuple(uncertain),
        paths=paths,
        source_evidence=_source_evidence(steps, points),
        missing_evidence=(_MISSING_USE_KIND, _MISSING_FREE_WRAPPER, _MISSING_ALIASING),
        metadata={
            "flow_class": flow_class,
            "use_from": "call-composition" if composition else "flow-edge",
            "release": {
                "point": release_id,
                "source": _span(release) if release else "",
                "subject": release.get("subject", ""),
            },
            "reach": reach_state,
            "path_recorded": bool(paths),
            "paths_truncated": truncated,
            "anchor_span": {
                "relative_path": use.get("relative_path", ""),
                "start_line": use.get("start_line", 0),
                "end_line": use.get("end_line", 0),
            },
            "methods": [item for item in [_method_name(use, names)] if item],
            "exit_point": "",
        },
    )


def double_free(
    events: Iterable[Any],
    edges: Iterable[Any],
    *,
    subject: str | None = None,
    max_hops: int = 64,
    max_paths: int = 8,
    max_candidates: int = 20,
    names: Mapping[str, str] | None = None,
    identity: bool = True,
    defect_keys: Collection[str] | None = None,
) -> DefectQueryResult:
    """Matrix 4.2: two releases of the same object on one path.

    This query reads no flow edge, because stage 5B mints none between two
    releases -- there is no def-use relation between a release and a release.
    It pairs RELEASE points inside one method instead, which is the same
    grouping the walker used to mint them.

    Two facts eliminate a pair, and one rule catches both shapes of the first.
    A definition of the identity between the releases means the second release
    is of a new value: `free(p); p = malloc(n); free(p)` is legal, and so is
    `free(p); p = NULL; free(p)`, since freeing null is defined behaviour. The
    second fact is reachability, and it is what makes the matrix's
    "disqualifying: mutually exclusive branches" true without a branch flag
    anywhere: two arms of a branch have no control path between them, so
    `if (x) free(p); else free(p);` produces no candidate at all.

    The member path is part of the identity here, and the corpus is why:
    adlist.c frees `node->value` on one line and `node` on the next, two
    different objects that share a root. A pair whose members differ is not a
    double free, and `subject_member` is what says so.
    """
    points = materialize_points(events, edges)
    adjacency = build_adjacency(edges)
    reach = _Reachability(adjacency, points)
    by_owner = _by_owner(points)
    alias_groups = _alias_groups(points)
    population: Counter = Counter()
    states: Counter = Counter()
    by_reason: Counter = Counter()
    records: list[dict[str, Any]] = []
    candidates: list[Candidate] = []
    wanted = _wanted(DOUBLE_FREE_KEY, defect_keys)

    for owner in sorted(by_owner):
        point_ids = by_owner[owner]
        groups = alias_groups.get(owner, {})
        releases = [
            point_id for point_id in point_ids
            if points[point_id]["event_type"] == "RELEASE" and points[point_id].get("subject")
        ]
        if len(releases) < 2:
            continue
        definitions: dict[tuple[str, ...], list[tuple[int, str, int]]] = {}
        for point_id in point_ids:
            info = points[point_id]
            if info["event_type"] in DEF_EVENTS:
                definitions.setdefault(_flow_key(info), []).append(_position(info))
        for positions in definitions.values():
            positions.sort()
        positions_of = {point_id: _position(points[point_id]) for point_id in releases}
        ordered = sorted(releases, key=lambda point_id: positions_of[point_id])
        for index, first_id in enumerate(ordered):
            first = points[first_id]
            roots = _alias_closure(groups, str(first["subject"]))
            first_position = positions_of[first_id]
            between = definitions.get(_flow_key(first), ())
            for second_id in ordered[index + 1:]:
                second = points[second_id]
                second_position = positions_of[second_id]
                if second_position <= first_position:
                    continue
                population["same-owner-pair"] += 1
                if str(second["subject"]) not in roots:
                    continue
                population["same-identity-pair"] += 1
                if not wanted or (subject is not None and _identity_display(second) != subject):
                    continue
                if str(first.get("subject_member", "")) != str(second.get("subject_member", "")):
                    _record_elimination(
                        _MEMBER_MISMATCH, DOUBLE_FREE_KEY, second_id, second,
                        names, by_reason, records,
                    )
                    continue
                state = reach.state(first_id, second_id)
                if state == DISJOINT:
                    _record_elimination(
                        _PATH_DISJOINT, DOUBLE_FREE_KEY, second_id, second,
                        names, by_reason, records,
                    )
                    continue
                index_after = bisect.bisect_right(between, first_position)
                if index_after < len(between) and between[index_after] < second_position:
                    _record_elimination(
                        _INTERVENING_DEF, DOUBLE_FREE_KEY, second_id, second,
                        names, by_reason, records,
                    )
                    continue
                status = _release_pair_status(
                    first, second, state, identity=identity
                )
                states[status] += 1
                candidates.append(_double_free_candidate(
                    first_id, second_id, first, second, points, names, status,
                    identity=identity, adjacency=adjacency, max_hops=max_hops,
                    max_paths=max_paths, reach_state=state,
                ))

    candidates.sort(key=Candidate.sort_key)
    truncated = len(candidates) > max_candidates
    coverage = {
        "population": dict(sorted(population.items())),
        "states": dict(sorted(states.items())),
        "candidates": len(candidates),
        "candidates_by_key": _count_by_key(candidates),
        "max_hops": max_hops,
        "semantic_points": len(points),
        "semantic_edges": sum(len(item) for item in adjacency.values()),
        "points_by_type": _points_by_type(points),
        **_disqualification_coverage(by_reason, records),
    }
    return DefectQueryResult(
        query=DOUBLE_FREE, candidates=tuple(candidates[:max_candidates]),
        coverage=coverage, truncated=truncated,
    )


def _release_pair_status(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    state: str,
    *,
    identity: bool,
) -> str:
    """The state two releases of one root can carry.

    `resolved` needs all three of: both points declare an object, the two
    declarations are the *same* object, and the graph confirmed the first can
    reach the second. A pair joined only through the copy annotation is a
    candidate and not an identity, so it is `ambiguous`; so is a pair whose
    reachability question the graph could not answer at all. Either side
    without a declaration cannot be attributed, which is `unresolved` -- and on
    llama.cpp-69 that is most of the population, because a range-for loop
    variable and a lambda capture are released without a declaration identity.
    """
    first_key = _identity(first, identity=identity)
    second_key = _identity(second, identity=identity)
    if not first_key or not second_key:
        return UNRESOLVED
    if not (first.get("subject_decl") and second.get("subject_decl")):
        return UNRESOLVED
    if first_key != second_key:
        return AMBIGUOUS
    if _identity_cap(first, identity=identity) or _identity_cap(second, identity=identity):
        return AMBIGUOUS
    if state != REACHABLE:
        return AMBIGUOUS
    return RESOLVED


def _double_free_candidate(
    first_id: str,
    second_id: str,
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    points: Mapping[str, Mapping[str, Any]],
    names: Mapping[str, str] | None,
    status: str,
    *,
    identity: bool,
    adjacency: Mapping[str, list[tuple[str, str, frozenset[str]]]],
    max_hops: int,
    max_paths: int,
    reach_state: str,
) -> Candidate:
    """One double-free candidate, anchored on the *second* release.

    The second release is the defect -- the first was legitimate -- so the
    anchor, the subject and the citation are the second point's, and the first
    travels in the facts and the metadata as its antecedent.
    """
    identity_key = _identity(second, identity=identity)
    owner = str(second.get("owner_symbol_id", ""))
    paths, truncated = _flow_path(adjacency, points, first_id, second_id, max_hops, max_paths)
    uncertain: list[dict[str, Any]] = []
    if not identity_key:
        uncertain.append(_uncertain(
            "SUBJECT_UNRESOLVED", decisive=True,
            detail="the release names no target, so the candidate cannot be attributed",
        ))
    elif not (first.get("subject_decl") and second.get("subject_decl")):
        uncertain.append(_uncertain(
            "IDENTITY_UNKNOWN", decisive=True,
            detail=(
                "a release on this path names no declaration, so the two releases "
                "are joined by spelling rather than by object"
            ),
            point=second_id, source=_span(second),
        ))
    elif identity_key != _identity(first, identity=identity):
        uncertain.append(_uncertain(
            "ALIAS_PAIR", decisive=True,
            detail=(
                "the two releases are joined through a same-statement copy "
                "annotation, which is a candidate-level alias and not an identity"
            ),
            point=first_id, source=_span(first),
        ))
    else:
        cap = _identity_cap(second, identity=identity)
        if cap is not None:
            uncertain.append(_uncertain(
                cap["fact"], decisive=True, detail=cap["detail"],
                point=second_id, source=_span(second),
            ))
    if reach_state == REACH_UNKNOWN:
        uncertain.append(_uncertain(
            "REACH_UNKNOWN", decisive=True,
            detail=(
                "the first release has no position in the control-flow graph, so "
                "the graph cannot say the second runs after it"
            ),
            point=first_id, source=_span(first),
        ))
    steps = [first_id, second_id]
    return Candidate(
        defect_key=DOUBLE_FREE_KEY,
        query=DOUBLE_FREE,
        subject={
            "kind": "release",
            "name": _identity_display(second),
            "identity": identity_key,
            "storage": second.get("subject_storage", ""),
            "type": second.get("subject_type", ""),
            "member": second.get("subject_member", ""),
            "method": _method_name(second, names),
            "owner_symbol_id": owner,
        },
        anchor=second_id,
        discriminator=(
            f"{identity_key or 'anonymous'}|{owner}|{_step(first)}|{_step(second)}"
        ),
        resolution_status=status,
        confidence=DOUBLE_FREE_CONFIDENCE[status],
        facts=tuple(
            _point_fact(point_id, points[point_id], names)
            for point_id in steps if _citable(point_id, points)
        ),
        uncertain_facts=tuple(uncertain),
        paths=paths,
        source_evidence=_source_evidence(steps, points),
        missing_evidence=(_MISSING_NULL_CONVENTION, _MISSING_ALIAS_PRECISION),
        metadata={
            "first_release": {
                "point": first_id,
                "source": _span(first),
                "subject": first.get("subject", ""),
                "declared": bool(first.get("subject_decl")),
            },
            "second_release": {
                "point": second_id,
                "source": _span(second),
                "subject": second.get("subject", ""),
                "declared": bool(second.get("subject_decl")),
            },
            "reach": reach_state,
            "path_recorded": bool(paths),
            "paths_truncated": truncated,
            "anchor_span": {
                "relative_path": second.get("relative_path", ""),
                "start_line": second.get("start_line", 0),
                "end_line": second.get("end_line", 0),
            },
            "methods": [item for item in [_method_name(second, names)] if item],
            "exit_point": "",
        },
    )


@dataclass(frozen=True, slots=True)
class TaintSink:
    """One declared sink: the callee, the positions that interpret, and how
    well the argument list can prove a position.

    `arity` is not decoration. `arguments` holds the *rooted* arguments --
    `_call_arguments` ends with `[name for name in names if name]`, so every
    argument with no root identifier is dropped -- which makes an index the true
    argument position only when nothing before it was dropped. The argument
    count is what proves that for a non-variadic callee. A variadic one cannot
    be proven this way at all (a literal format plus one variadic argument has
    the same length as a rooted format), so only position 0 is declarable, and
    only when the call's own subject says the first argument is rooted.
    """

    name: str
    ordinals: tuple[int, ...]
    arity: int
    variadic: bool


@dataclass(frozen=True, slots=True)
class TaintVocabulary:
    """The declared source/sink vocabulary, and where it came from.

    `path` travels with the table so an evidence bundle can say which file
    produced a candidate: a query that silently reads whatever happens to sit
    next to the module is a query nobody can reproduce.
    """

    sources: Mapping[str, str]
    sinks: Mapping[str, TaintSink]
    path: str

    def source_name(self, spelling: str) -> str:
        return self.sources.get(spelling, "")

    def sink(self, spelling: str) -> TaintSink | None:
        return self.sinks.get(spelling)


TAINT_FILE = Path(__file__).with_name("semantics") / "taint.toml"


def load_taint(path: Path | None = None) -> TaintVocabulary:
    """Read the declared taint vocabulary, refusing anything it cannot audit.

    Every entry must carry `evidence`, exactly as `contracts.load_declared`
    requires: an entry that silently *creates* findings is worse than no entry,
    and folklore with a citation is at least checkable folklore.

    Two invariants are enforced here rather than trusted, because both protect
    the one claim that makes a candidate checkable -- that the argument index is
    the declared position. A variadic sink may declare position 0 and nothing
    else; a non-variadic sink may not declare an ordinal beyond its arity. A
    declaration that breaks either is a table bug, and a table bug that quietly
    produces candidates is the failure mode this whole file exists to avoid.
    """
    source = path or TAINT_FILE
    if not source.exists():
        return TaintVocabulary(sources={}, sinks={}, path=str(source))
    try:
        payload = tomllib.loads(source.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"{source.name}: {error}") from error
    unknown = sorted(set(payload) - {"source", "sink", "note"})
    if unknown:
        raise ValueError(
            f"{source.name}: unknown top-level keys {', '.join(unknown)}; "
            f"expected source, sink, note"
        )
    sources: dict[str, str] = {}
    sinks: dict[str, TaintSink] = {}
    for entry in payload.get("source", []):
        name = str(entry.get("name", ""))
        spellings = entry.get("spellings")
        if not name or not isinstance(spellings, list) or not spellings:
            raise ValueError(f"{source.name}: every source needs a name and spellings")
        if not entry.get("evidence"):
            raise ValueError(f"{source.name}: source {name!r} has no evidence")
        for spelling in spellings:
            _claim_spelling(sources, sinks, str(spelling), source.name)
            sources[str(spelling)] = name
    for entry in payload.get("sink", []):
        name = str(entry.get("name", ""))
        spellings = entry.get("spellings")
        ordinals = entry.get("ordinals")
        if not name or not isinstance(spellings, list) or not spellings:
            raise ValueError(f"{source.name}: every sink needs a name and spellings")
        if not isinstance(ordinals, list) or not ordinals or not all(
            isinstance(ordinal, int) for ordinal in ordinals
        ):
            raise ValueError(f"{source.name}: sink {name!r} needs integer ordinals")
        if not isinstance(entry.get("arity"), int):
            raise ValueError(f"{source.name}: sink {name!r} needs an integer arity")
        if not entry.get("evidence"):
            raise ValueError(f"{source.name}: sink {name!r} has no evidence")
        variadic = bool(entry.get("variadic", False))
        declared = tuple(sorted(set(ordinals)))
        if variadic and declared != (0,):
            raise ValueError(
                f"{source.name}: {name}: a variadic sink can only declare position 0"
            )
        if not variadic and max(declared) >= int(entry["arity"]):
            raise ValueError(
                f"{source.name}: {name}: ordinal {max(declared)} beyond arity "
                f"{int(entry['arity'])}"
            )
        sink = TaintSink(
            name=name, ordinals=declared, arity=int(entry["arity"]), variadic=variadic
        )
        for spelling in spellings:
            _claim_spelling(sources, sinks, str(spelling), source.name)
            sinks[str(spelling)] = sink
    return TaintVocabulary(sources=sources, sinks=sinks, path=str(source))


def _claim_spelling(
    sources: Mapping[str, str], sinks: Mapping[str, TaintSink], spelling: str, name: str
) -> None:
    """Refuse a spelling that already names something else.

    One spelling has to mean one function: `printf` cannot be both a source and
    a sink, and two sinks cannot both own the same name. The failure direction
    is what decides this -- a duplicate would silently let the last entry win,
    and the losing entry's evidence would still be quoted in the table.
    """
    if spelling in sources or spelling in sinks:
        raise ValueError(f"{name}: spelling {spelling!r} is declared twice")


def taint_path(
    events: Iterable[Any],
    edges: Iterable[Any],
    *,
    references: Iterable[Any] = (),
    taint: TaintVocabulary | None = None,
    subject: str | None = None,
    max_hops: int = 64,
    max_paths: int = 8,
    max_candidates: int = 20,
    names: Mapping[str, str] | None = None,
    identity: bool = True,
    defect_keys: Collection[str] | None = None,
) -> DefectQueryResult:
    """A value from a declared source reaching a declared sink position.

    **This query has no matrix family.** `defect-evidence-matrix-v1.md` puts a
    full taint analysis outside V2 (:286: SOURCE/SINK, DATA_FLOW_TO *and*
    sanitiser identification), and this is the registered minimum the roadmap
    asked for instead of it: two declared name lists and the `input_to_sink`
    edges stage 5B already mints. The conflict is stated rather than papered
    over -- the matrix's line is not changed by an implementation, and the
    candidate rule here is structurally weak.

    Three things follow from that, and all three are visible in the output
    rather than in a footnote:

    * There is **no sanitiser model**. A value that passed through a validation
      or escaping function is still reported. `missing_evidence` says so in the
      bundle, and no elimination rule pretends otherwise.
    * A **CHECK on the path only downgrades** a candidate to `ambiguous`. "A
      check ran" is not "the value was made safe", and the graph cannot tell
      the two apart.
    * The **callee name is not in the event**. A CALL point's `matched_name` is
      the node type (`call_expression`) and its subject is the first argument
      root, so both ends are resolved through `raw_references` by
      `(file_id, owner_symbol_id, start_line)`, and a line carrying more than
      one declared name is eliminated and counted rather than guessed at. A
      spelling only matches a *global* call: `target_module` must be empty,
      which is what keeps Python's `platform.system()` out of C's `system`.

    On both acceptance corpora this query produces no candidate at all. That is
    a fact about the corpora and not about the mechanism: llama.cpp-69's 36
    source-side hits are all `getenv` results passed straight to `atoi` -- a
    conversion, which the declared table deliberately does not treat as a
    source -- and no declared sink is ever reached from one.
    """
    vocabulary = taint if taint is not None else load_taint()
    points = materialize_points(events, edges)
    adjacency = build_adjacency(edges)
    reach = _Reachability(adjacency, points)
    by_line: dict[tuple[str, str, int], set[str]] = {}
    reference_rows = 0
    for row in references:
        reference_rows += 1
        if _row_field(row, "target_module"):
            continue
        name = str(_row_field(row, "raw_name") or "")
        if not name:
            continue
        by_line.setdefault((
            str(_row_field(row, "file_id") or ""),
            str(_row_field(row, "owner_symbol_id") or ""),
            int(_row_field(row, "start_line") or 0),
        ), set()).add(name)

    def names_at(point: Mapping[str, Any]) -> set[str]:
        return by_line.get(_statement_key(point), set())

    # Two indexes for the check-on-path downgrade: the checks of each owner,
    # for the sources the walk can start from, and the checks each definition
    # flowed into, for the ones it cannot. Both are one pass over what the query
    # already reads.
    checks_by_owner: dict[str, list[str]] = {}
    for point_id in sorted(points):
        info = points[point_id]
        if info["event_type"] == "CHECK":
            checks_by_owner.setdefault(str(info.get("owner_symbol_id", "")), []).append(point_id)
    flow = _flow_edges(edges)
    flowed: dict[str, set[str]] = {}
    for source_id, target_id, _metadata in flow:
        if points.get(target_id, {}).get("event_type") == "CHECK":
            flowed.setdefault(source_id, set()).add(target_id)
    flowed_checks: dict[str, frozenset[str]] = {
        key: frozenset(value) for key, value in flowed.items()
    }

    population: Counter = Counter()
    states: Counter = Counter()
    by_reason: Counter = Counter()
    records: list[dict[str, Any]] = []
    candidates: list[Candidate] = []
    wanted = _wanted(TAINT_KEY, defect_keys)
    for source_id, target_id, metadata in flow:
        if str(metadata.get("flow_class", "")) != INPUT_TO_SINK:
            continue
        population["input_to_sink"] += 1
        source_point = points.get(source_id)
        target = points.get(target_id)
        if source_point is None or target is None:
            continue
        declared_sources = sorted(
            name for name in names_at(source_point) if vocabulary.source_name(name)
        )
        if not declared_sources:
            continue
        population["source-side"] += 1
        if not wanted or (subject is not None and _identity_display(target) != subject):
            continue
        if len(declared_sources) > 1:
            _record_elimination(
                _SOURCE_AMBIGUOUS_LINE, TAINT_KEY, target_id, target,
                names, by_reason, records,
            )
            continue
        declared_sinks = sorted(
            name for name in names_at(target) if vocabulary.sink(name) is not None
        )
        if not declared_sinks:
            continue
        population["sink-side"] += 1
        if len(declared_sinks) > 1:
            _record_elimination(
                _SINK_AMBIGUOUS_LINE, TAINT_KEY, target_id, target,
                names, by_reason, records,
            )
            continue
        sink = vocabulary.sink(declared_sinks[0])
        assert sink is not None
        arguments = [item for item in str(target.get("arguments", "")).split(ARGUMENT_SEPARATOR) if item]
        root = str(metadata.get("arg_root", ""))
        if root not in arguments:
            _record_elimination(
                _ARG_ROOT_NOT_LISTED, TAINT_KEY, target_id, target, names, by_reason, records
            )
            continue
        index = arguments.index(root)
        # The position claim, and the only place in this query where the graph
        # can be wrong about *where* the value landed. See `TaintSink`.
        if sink.variadic:
            position_proven = index == 0 and target.get("subject_from") == "argument"
        else:
            position_proven = len(arguments) == sink.arity
        if not position_proven:
            _record_elimination(
                _ARGUMENT_POSITION_UNVERIFIABLE, TAINT_KEY, target_id, target,
                names, by_reason, records,
            )
            continue
        if index not in sink.ordinals:
            _record_elimination(
                _ORDINAL_NOT_DECLARED, TAINT_KEY, target_id, target, names, by_reason, records
            )
            continue

        identity_key = _identity(target, identity=identity)
        uncertain: list[dict[str, Any]] = []
        if not identity_key:
            status = UNRESOLVED
            uncertain.append(_uncertain(
                "SUBJECT_UNRESOLVED", decisive=True,
                detail="the sink call names no target, so the candidate cannot be attributed",
            ))
        else:
            status = RESOLVED
            cap = _identity_cap(target, identity=identity)
            if cap is not None:
                uncertain.append(_uncertain(
                    cap["fact"], decisive=True, detail=cap["detail"],
                    point=target_id, source=_span(target),
                ))
                status = AMBIGUOUS
            if tuple(metadata.get("via_copies", ()) or ()):
                uncertain.append(_uncertain(
                    "ALIAS_PATH", decisive=True,
                    detail="the value reached the sink through a copy",
                    point=target_id, source=_span(target),
                ))
                status = AMBIGUOUS
            if _checks_on_path(
                reach, points, source_id, target_id,
                checks_by_owner.get(str(source_point.get("owner_symbol_id", "")), ()),
                flowed_checks,
            ):
                uncertain.append(_uncertain(
                    "CHECK_ON_PATH", decisive=True,
                    detail=(
                        "a check sits between the source and the sink; a check is not "
                        "a sanitiser, and the graph cannot tell the two apart"
                    ),
                    point=target_id, source=_span(target),
                ))
                status = AMBIGUOUS
        states[status] += 1
        paths, truncated = _flow_path(
            adjacency, points, source_id, target_id, max_hops, max_paths
        )
        steps = [source_id, target_id]
        candidates.append(Candidate(
            defect_key=TAINT_KEY,
            query=TAINT_PATH,
            subject={
                "kind": "taint",
                "name": _identity_display(target),
                "identity": identity_key,
                "storage": target.get("subject_storage", ""),
                "type": target.get("subject_type", ""),
                "member": target.get("subject_member", ""),
                "method": _method_name(target, names),
                "owner_symbol_id": str(target.get("owner_symbol_id", "")),
            },
            anchor=target_id,
            discriminator=(
                f"{vocabulary.source_name(declared_sources[0])}|{sink.name}|"
                f"{_step(source_point)}|{_step(target)}"
            ),
            resolution_status=status,
            confidence=TAINT_CONFIDENCE[status],
            facts=tuple(
                _point_fact(point_id, points[point_id], names)
                for point_id in steps if _citable(point_id, points)
            ),
            uncertain_facts=tuple(uncertain),
            paths=paths,
            source_evidence=_source_evidence(steps, points),
            missing_evidence=(_MISSING_SANITIZER, _MISSING_SOURCE_TRUST),
            metadata={
                "source": vocabulary.source_name(declared_sources[0]),
                "source_spelling": declared_sources[0],
                "sink": sink.name,
                "sink_spelling": declared_sinks[0],
                "ordinal": index,
                "declared_ordinals": list(sink.ordinals),
                "variadic": sink.variadic,
                "arguments": arguments,
                "arg_root": root,
                "vocabulary": vocabulary.path,
                "reach": reach.state(source_id, target_id),
                "path_recorded": bool(paths),
                "paths_truncated": truncated,
                "anchor_span": {
                    "relative_path": target.get("relative_path", ""),
                    "start_line": target.get("start_line", 0),
                    "end_line": target.get("end_line", 0),
                },
                "methods": [item for item in [_method_name(target, names)] if item],
                "exit_point": "",
            },
        ))

    candidates.sort(key=Candidate.sort_key)
    truncated = len(candidates) > max_candidates
    coverage = {
        "population": dict(sorted(population.items())),
        "states": dict(sorted(states.items())),
        "candidates": len(candidates),
        "candidates_by_key": _count_by_key(candidates),
        "sources_declared": sorted(set(vocabulary.sources.values())),
        "sinks_declared": sorted({sink.name for sink in vocabulary.sinks.values()}),
        "vocabulary": vocabulary.path,
        "reference_rows": reference_rows,
        "reference_lines": len(by_line),
        "max_hops": max_hops,
        "semantic_points": len(points),
        "semantic_edges": sum(len(item) for item in adjacency.values()),
        "points_by_type": _points_by_type(points),
        **_disqualification_coverage(by_reason, records),
    }
    return DefectQueryResult(
        query=TAINT_PATH, candidates=tuple(candidates[:max_candidates]),
        coverage=coverage, truncated=truncated,
    )


def _checks_on_path(
    reach: _Reachability,
    points: Mapping[str, Mapping[str, Any]],
    source: str,
    target: str,
    checks: Iterable[str],
    flowed_checks: Mapping[str, frozenset[str]],
) -> bool:
    """Whether any check stands between the definition and the sink.

    Two shapes, because a definition has two. An ALLOC or RELEASE has a position
    in the control-flow graph and the question is a walk: a check is between the
    two when the definition reaches it and it reaches the sink, and both halves
    are the memoised node search, so the answer is exact at any depth. A WRITE
    has no position at all -- the walker does not step through a write -- so the
    walk has nothing to start from, and the answer comes from the flow edges
    instead: a check this definition flowed into, ordered before the sink.

    The second shape is the common one, and that is why it is written down.
    3,454 of redis-50's 3,491 `input_to_sink` edges are write-sourced, so a rule
    that only walked would be inert on almost every taint candidate. Neither
    acceptance corpus reaches a declared sink at all, so no frozen number could
    have shown the difference -- the fixture did.

    A check *near* the path is not the question and this does not ask it. The
    answer only ever downgrades a candidate -- "a check ran" is not "the value
    was sanitised", and a query with no sanitiser model has no business
    eliminating on one. A check on the sink's own line is not *between* the two
    and does not downgrade: the rule is about ordering, and a same-line pair has
    none.
    """
    if reach.has_position(source):
        reached = reach.from_(source)
        return any(
            check in reached and target in reach.from_(check) for check in checks
        )
    if target not in points:
        return False
    flowed = flowed_checks.get(source, frozenset())
    target_position = _position(points[target])
    return any(
        check in flowed and check in points
        and _position(points[check]) < target_position
        for check in checks
    )


def _row_field(row: Any, name: str) -> Any:
    """Read a field from a mapping row or an object, like `_edge_field`."""
    if isinstance(row, Mapping):
        return row.get(name, "")
    return getattr(row, name, "")


# ----------------------------------------------------------------------
# D5: error_handling (matrix 9.1)


# The three verdicts one forward walk from a call can reach, plus the two the
# elimination records use.
SCAN_CHECKED = "checked"
SCAN_UNCHECKED = "unchecked"
SCAN_TRUNCATED = "truncated"

_BRANCHED_BEFORE_USE = {
    "reason": "BRANCHED_BEFORE_USE",
    "provenance": "graph:control_reaches",
    "detail": (
        "control branches between the call and the next use of the value it was "
        "stored into, so something examined the result before it was used"
    ),
}


def _branch_scan(
    call_id: str,
    points: Mapping[str, Mapping[str, Any]],
    adjacency: Mapping[str, list[tuple[str, str, frozenset[str]]]],
    max_hops: int,
) -> tuple[str, str, int, int, tuple[str, ...], tuple[str, ...]]:
    """Walk forward from a call to the first branch point or window end.

    The window is the plan's: from the call to the next use of the variable the
    result was stored into, or to the method's exit, whichever comes first.
    Three verdicts come out of it. `checked` means control branched inside the
    window, which is the only reading of "was the result examined" the graph can
    support -- `fd == -1` is not a CHECK point, because a CHECK is a null
    comparison, so the test has to be branch dominance rather than the presence
    of a comparison. `unchecked` means the value was used, or the method ended,
    with no branch in between: the candidate. `truncated` means the hop ceiling
    was reached without either, and it is reported rather than guessed at.

    Two guards, both facts about the shape rather than tuning knobs.

    A branch counts only when it sits at or after the call's own line. The
    control-flow graph reaches backwards as readily as forwards -- a call inside
    a loop body reaches the loop header -- and the first cut of this rule
    counted that header as "the result was examined" in 2 of 6 sampled verdicts.
    A check on a value cannot precede the value. On redis-50 that guard rejects
    2,064 branch points and on llama.cpp-69 51,377, which is the measure of how
    much of the naive rule was reading the enclosing loop rather than a check.
    (The frozen T2 probe counted 2,006 and 50,403 for the same population: its
    walk returned at the deciding point, so backward branches later on the same
    level were never visited. This walk finishes the level before it returns,
    and the difference between the two counts is exactly that -- each walk
    reproduces its own number from the same population.)

    The walk is level by level, and the verdict comes from the first deciding
    point it reaches -- a forward branch, a use of the stored value, or the
    method's exit. Finishing the level before returning changes no verdict; it
    is what makes the rejected-branch count complete. The one place the reading
    is order-sensitive is a use (or an exit) and a forward branch landing on
    the same level: whichever the walk reaches first decides. That is 2 windows
    on redis-50 and 26 on llama.cpp-69, all read as `unchecked` here and by the
    frozen probe alike; a branch-wins tie-break would read them `checked`.

    The route to the deciding point comes back with the verdict, as the points
    and the edges between them. It is read off the walk's own breadth-first
    tree, which is why it costs nothing and why it cannot disagree with the
    verdict: it *is* the route the decision was made along. The obvious
    alternative -- handing the two ends to `find_paths` -- is what the first cut
    of this query did, and it is the one place in the file where that helper
    must not be used. `find_paths` enumerates simple paths with a per-path
    visited set and caps only what it *returns*, so a window that closes after a
    loop costs 2^iterations to describe; redis-50 has 3,989 such windows and the
    query did not finish in ten minutes, at 13 GB and climbing. Bounded by a
    32-hop ceiling it still cost 43 s to record 3,930 paths. The walk above has
    the answer already.
    """
    call = points[call_id]
    identity = _identity(call)
    line = call["start_line"]
    seen = {call_id}
    level = [call_id]
    hops = 0
    backward = 0
    predecessor: dict[str, tuple[str, str]] = {}
    while level:
        if hops > max_hops:
            return SCAN_TRUNCATED, "", hops, backward, (), ()
        following: list[str] = []
        deciding: str = ""
        for point_id in level:
            for edge_id, dst, _flags in adjacency.get(point_id, ()):
                if dst in seen:
                    continue
                seen.add(dst)
                predecessor[dst] = (point_id, edge_id)
                hops += 1
                info = points.get(dst)
                if info is None:
                    continue
                if _branches(dst, adjacency):
                    if info.get("start_line", 0) < line:
                        backward += 1
                        following.append(dst)  # not a check on this value
                        continue
                    if not deciding:
                        deciding = dst
                    continue
                if (info.get("event_type") in USE_EVENTS
                        and _identity(info) == identity):
                    if not deciding:
                        deciding = dst
                    continue
                if info.get("event_type") == "EXIT":
                    if not deciding:
                        deciding = dst
                    continue
                following.append(dst)
        if deciding:
            route_points, route_edges = _route(predecessor, call_id, deciding)
            if _branches(deciding, adjacency):
                return SCAN_CHECKED, deciding, hops, backward, route_points, route_edges
            return SCAN_UNCHECKED, deciding, hops, backward, route_points, route_edges
        level = following
    # Nothing left to walk and no use found: the method ends here.
    return SCAN_UNCHECKED, "", hops, backward, (), ()


def _route(
    predecessor: Mapping[str, tuple[str, str]], start: str, end: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The walk's own route from `start` to `end`, as points and edge ids.

    Read off the breadth-first tree rather than searched for: every point the
    walk entered has exactly one predecessor, so following them back is linear
    and gives the shortest route the walk could have taken.
    """
    route_points = [end]
    route_edges: list[str] = []
    cursor = end
    while cursor != start:
        step = predecessor.get(cursor)
        if step is None:  # unreachable by construction; reported as no route
            return (), ()
        cursor, edge_id = step
        route_edges.append(edge_id)
        route_points.append(cursor)
    route_points.reverse()
    route_edges.reverse()
    return tuple(route_points), tuple(route_edges)


def _branches(
    point_id: str, adjacency: Mapping[str, list[tuple[str, str, frozenset[str]]]]
) -> bool:
    """Whether a point's outgoing edges carry a branch arm.

    The flags hang off the source of the edge (`_Open(check, (BRANCH_TRUE,))`),
    so the test reads the node's own outgoing edges rather than its type.
    """
    return any(
        BRANCH_TRUE in flags or BRANCH_FALSE in flags
        for _edge_id, _dst, flags in adjacency.get(point_id, ())
    )


def _error_status(
    info: Mapping[str, Any], verdict: str, *, identity: bool
) -> str:
    """`resolved` unless the identity or the walk says otherwise.

    There is no `unresolved` here, and that is a property of the population
    rather than a missing state: a source is a call whose stored value has a
    resolved declaration, so every candidate names an object. What can still
    weaken it is the identity behind the name -- an unknown root or a member
    path the declaration index could not resolve -- and a walk that hit the hop
    ceiling before it could say whether anything branched.
    """
    if _identity_cap(info, identity=identity) is not None:
        return AMBIGUOUS
    if verdict == SCAN_TRUNCATED:
        return AMBIGUOUS
    return RESOLVED


def _error_handling_candidate(
    call_id: str,
    end_id: str,
    info: Mapping[str, Any],
    points: Mapping[str, Mapping[str, Any]],
    names: Mapping[str, str] | None,
    status: str,
    *,
    identity: bool,
    verdict: str,
    hops: int,
    backward: int,
    route: tuple[tuple[str, ...], tuple[str, ...]],
) -> Candidate:
    """One unchecked-result candidate, anchored on the call that produced it.

    The call is the defect's site -- it is where a result entered the program
    without being examined -- so the anchor, the subject and the citation are
    the call's, and the point that closed the window travels in the facts and
    the metadata.
    """
    identity_key = _identity(info, identity=identity)
    owner = str(info.get("owner_symbol_id", ""))
    uncertain: list[dict[str, Any]] = []
    cap = _identity_cap(info, identity=identity)
    if cap is not None:
        uncertain.append(_uncertain(
            cap["fact"], decisive=True, detail=cap["detail"],
            point=call_id, source=_span(info),
        ))
    if verdict == SCAN_TRUNCATED:
        uncertain.append(_uncertain(
            "SCAN_TRUNCATED", decisive=True,
            detail=(
                "the forward walk reached its hop ceiling before finding a "
                "branch point or a use, so whether anything examined the result "
                "is unknown rather than answered"
            ),
            point=call_id, source=_span(info),
        ))
    if backward:
        uncertain.append(_uncertain(
            "BACKWARD_BRANCH_SKIPPED", decisive=False,
            detail=(
                "the walk passed branch points that sit before the call's own "
                "line; a check on a value cannot precede the value, so they were "
                "not read as examinations of it"
            ),
            count=backward,
        ))
    # One route, not a set of them: the claim this candidate makes is universal
    # -- nothing branched anywhere in the window -- so the route to the point
    # that closed it is an illustration of the window, not the evidence for it.
    # The evidence for it is the scan, which is why `scan` travels in the
    # metadata and the verdict is what a reviewer checks.
    route_points, route_edges = route
    paths: tuple[dict[str, Any], ...] = ()
    if route_points:
        paths = (ControlPath(
            route_points,
            tuple(points.get(point_id, {}).get("event_type", "") for point_id in route_points),
            route_edges,
        ).to_dict(),)
    steps = [call_id] + ([end_id] if end_id else [])
    end = points.get(end_id) if end_id else None
    return Candidate(
        defect_key=ERROR_HANDLING_KEY,
        query=ERROR_HANDLING,
        subject={
            "kind": "call",
            "name": _identity_display(info),
            "identity": identity_key,
            "storage": info.get("subject_storage", ""),
            "type": info.get("subject_type", ""),
            "member": info.get("subject_member", ""),
            "method": _method_name(info, names),
            "owner_symbol_id": owner,
        },
        anchor=call_id,
        discriminator=f"{identity_key or 'anonymous'}|{owner}|{_step(info)}",
        resolution_status=status,
        confidence=ERROR_HANDLING_CONFIDENCE[status],
        facts=tuple(
            _point_fact(point_id, points[point_id], names)
            for point_id in steps if _citable(point_id, points)
        ),
        uncertain_facts=tuple(uncertain),
        paths=paths,
        source_evidence=_source_evidence(steps, points),
        missing_evidence=(_MISSING_MUST_CHECK, _MISSING_DISCARDED_RESULT),
        metadata={
            "call": {
                "point": call_id,
                "source": _span(info),
                "subject": info.get("subject", ""),
                "declared": bool(info.get("subject_decl")),
                "subject_from": info.get("subject_from", ""),
            },
            "window_end": (
                {
                    "point": end_id,
                    "event": end.get("event_type", "") if end else "",
                    "source": _span(end) if end else "",
                }
                if end_id else {}
            ),
            "scan": {
                "verdict": verdict,
                "hops": hops,
                "backward_branches_skipped": backward,
            },
            "path_recorded": bool(paths),
            "anchor_span": {
                "relative_path": info.get("relative_path", ""),
                "start_line": info.get("start_line", 0),
                "end_line": info.get("end_line", 0),
            },
            "methods": [item for item in [_method_name(info, names)] if item],
            "exit_point": "",
        },
    )


# The window ceiling is the frozen T2 rule. It is a fact about the verdict, not
# a tuning knob: a walk that stops early reads "no branch found yet" where the
# rule reads "checked", so a caller's smaller ceiling would manufacture
# candidates. On llama.cpp-69, the delivery layer's 64-hop default -- tuned for
# the evidence-path walks of the six sibling queries -- turned 6,189 windows
# into spurious candidates that the frozen rule eliminates. Callers may raise
# the ceiling; they may not lower it.
_WINDOW_MAX_HOPS = 4096


def error_handling(
    events: Iterable[Any],
    edges: Iterable[Any],
    *,
    subject: str | None = None,
    max_hops: int = 4096,
    max_candidates: int = 20,
    names: Mapping[str, str] | None = None,
    identity: bool = True,
    defect_keys: Collection[str] | None = None,
) -> DefectQueryResult:
    """Matrix 9.1: a call's result was stored and never examined.

    The fact this query runs on was already in the graph and had been read as
    something else. A CALL point on the right-hand side of an assignment
    carries the *lvalue* the result is stored into as its subject --
    `subject_from == "assignment"`, with a resolved declaration -- which is the
    join `x = f(); ... x is used without a branch in between` needs. The plan
    originally called for a new `result_of` annotation on WRITE points instead,
    and the frozen corpora are why that was dropped: WRITE points do not exist
    for locals (`ACCESS_STORAGES` has no `local`), so `int n = read(...)` -- the
    shape this pattern is mostly about -- would have been invisible. Of
    redis-50's 17,227 assignments with a call on the right, 10,128 store into a
    local; of llama.cpp-69's 59,811, 41,795 do. The CALL-side fact reaches
    14,507 and 87,498 of them, and needed no rebuild to do it.

    Two things this query does not do, both registered rather than hidden. It
    cannot see a result that was discarded outright (`f();` as a bare
    statement): a CALL point records where a result went, never that it went
    nowhere. And it cannot tell a call that has a failure to check from one that
    does not -- `p = malloc(n)` and `n = read(fd, buf, len)` are the same shape
    to the graph, and matrix 9.1 says in its own semantics column that telling
    them apart is the AI side's job. So the population is large by construction
    and the candidate is a question, not an accusation.

    There is no `max_paths` knob, unlike its four siblings. The evidence route
    is the one the scan walked, read off its own breadth-first tree, so there is
    nothing to enumerate and nothing to cap. This is also the query's one
    performance cliff, and the reason it is written that way: describing a
    window with `find_paths` costs 2^loop-iterations, and at 3,989 windows on
    redis-50 the first cut of this query did not finish in ten minutes (13 GB and
    climbing). `_branch_scan`'s docstring carries the measurements.

    Unlike its siblings, this query does not accept a smaller `max_hops` than
    the frozen ceiling: the hop ceiling decides verdicts here, not evidence
    prettiness, and `_WINDOW_MAX_HOPS` above records why. The six sibling
    queries keep their 64-hop evidence walks -- that is the behaviour the
    frozen five-column comparison describes.
    """
    max_hops = max(max_hops, _WINDOW_MAX_HOPS)
    points = materialize_points(events, edges)
    adjacency = build_adjacency(edges)
    by_owner = _by_owner(points)
    population: Counter = Counter()
    states: Counter = Counter()
    by_reason: Counter = Counter()
    records: list[dict[str, Any]] = []
    candidates: list[Candidate] = []
    wanted = _wanted(ERROR_HANDLING_KEY, defect_keys)
    verdicts: Counter = Counter()
    backward_total = 0

    for owner in sorted(by_owner):
        for call_id in by_owner[owner]:
            info = points[call_id]
            if (info.get("event_type") != "CALL"
                    or info.get("subject_from") != SUBJECT_FROM_ASSIGNMENT):
                continue
            population["stored-call-result"] += 1
            if not info.get("subject_decl"):
                # No declaration behind the name, so the value has no object to
                # follow and the window has no end to look for. Counted, not
                # guessed at.
                population["stored-call-result-undeclared"] += 1
                continue
            if not wanted or (subject is not None and _identity_display(info) != subject):
                continue
            verdict, end_id, hops, backward, route_points, route_edges = _branch_scan(
                call_id, points, adjacency, max_hops
            )
            verdicts[verdict] += 1
            backward_total += backward
            if verdict == SCAN_CHECKED:
                _record_elimination(
                    _BRANCHED_BEFORE_USE, ERROR_HANDLING_KEY, call_id, info,
                    names, by_reason, records,
                )
                continue
            status = _error_status(info, verdict, identity=identity)
            states[status] += 1
            candidates.append(_error_handling_candidate(
                call_id, end_id, info, points, names, status,
                identity=identity, verdict=verdict, hops=hops, backward=backward,
                route=(route_points, route_edges),
            ))

    candidates.sort(key=Candidate.sort_key)
    truncated = len(candidates) > max_candidates
    coverage = {
        "population": dict(sorted(population.items())),
        "states": dict(sorted(states.items())),
        "candidates": len(candidates),
        "candidates_by_key": _count_by_key(candidates),
        "scan": dict(sorted(verdicts.items())),
        "backward_branches_skipped": backward_total,
        "max_hops": max_hops,
        "semantic_points": len(points),
        "semantic_edges": sum(len(item) for item in adjacency.values()),
        "points_by_type": _points_by_type(points),
        **_disqualification_coverage(by_reason, records),
    }
    return DefectQueryResult(
        query=ERROR_HANDLING, candidates=tuple(candidates[:max_candidates]),
        coverage=coverage, truncated=truncated,
    )


# ----------------------------------------------------------------------
# evidence


def candidate_evidence(
    candidate: Candidate,
    *,
    repository: str,
    commit: str,
    generation: int,
) -> Evidence:
    """The citable form of a candidate: one `E-DEFECT-...` id per candidate.

    `relation` carries the defect key rather than a constant. Two candidates
    anchored on the same event -- a 4.1 leak and a 4.6 unreleased lock on the
    same statement -- are different findings, and an id that ignored that would
    give them the same name.
    """
    span = candidate.metadata.get("anchor_span", {})
    start_line = span.get("start_line") or None
    end_line = span.get("end_line") or None
    methods = candidate.metadata.get("methods") or []
    return Evidence.create(
        kind="DEFECT_CANDIDATE",
        source_id=candidate.anchor,
        relation=candidate.defect_key,
        target_id=candidate.metadata.get("exit_point", ""),
        repository=repository,
        commit=commit,
        generation=generation,
        provenance="sparse_cfg",
        confidence=candidate.confidence,
        source_path=span.get("relative_path") or None,
        start_line=start_line,
        end_line=end_line,
        summary=(
            f"{candidate.defect_key} {candidate.query} candidate on "
            f"{candidate.subject.get('name', '')!r} ({candidate.resolution_status})"
        ),
        fact_id=candidate.discriminator,
        metadata={
            "defect_key": candidate.defect_key,
            "query": candidate.query,
            "resolution_status": candidate.resolution_status,
            "direct": True,
            "distance": 0,
            "boundary_relevant": False,
            "methods": list(methods),
        },
    )


def candidate_bundle(
    candidate: Candidate,
    *,
    evidence_id: str,
    generation: int,
    extra_missing: Iterable[str] = (),
) -> DefectEvidenceBundle:
    """The design doc §15 bundle for one candidate.

    `extra_missing` is where the caller states a gap it knows about and the
    query cannot: an active overlay, for instance, means every candidate here
    was computed against the base snapshot.
    """
    return DefectEvidenceBundle(
        defect_type=candidate.query,
        defect_key=candidate.defect_key,
        subject=dict(candidate.subject),
        facts=candidate.facts,
        uncertain_facts=candidate.uncertain_facts,
        paths=candidate.paths,
        source_evidence=candidate.source_evidence,
        missing_evidence=tuple([*candidate.missing_evidence, *extra_missing]),
        candidate_id=evidence_id,
        anchor=candidate.anchor,
        resolution_status=candidate.resolution_status,
        confidence=candidate.confidence,
        generation=generation,
    )
