"""Defect query service (I2A): candidate + evidence over the derived graph.

Pure query-time reader over the sparse_cfg / sparse_dfg rows the analyzers
materialized -- it never writes, never rebuilds, and never imports the PR
storage architecture. Fail-closed by contract: every file's sparse_cfg and
sparse_dfg states must be current (unified freshness, dependencies included)
before any candidate is produced; a stale or missing upstream answers
stale / not_built for the whole query instead of partial results.

Scope (I2A): two of the eight PR queries -- ``null_flow`` and
``double_free``. The capability contract records defect as
partially_implemented with that exact surface; nothing here may read as "the
defect layer is complete". ``resolved`` is an evidence-resolution status, not
a Verified Finding, and ``confidence`` is the PR's heuristic score, not a
calibrated defect probability.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, TypedDict

from . import contracts, defect
from .analysis import AnalysisStore, dependency_staleness
from .analysis_concurrency_context import build_concurrency_context
from .analysis_query import AnalysisRequestError
from .concurrency_lock_scenarios import build_access_lockset
from .concurrency_race_scenarios import build_race_scenarios
from .evidence import DefectEvidenceBundle, assemble_evidence_v2
from .semantics.vocabulary import load_defect_patterns
from .storage import SQLiteStorage

SUPPORTED_DEFECT_QUERIES = tuple(defect.QUERY_NAMES)
UPSTREAM_FEATURES = ("events:cpp", "sparse_cfg:cpp", "sparse_dfg:cpp")
CAPABILITY_GRADE = "local-only, diagnostic"


class RaceFactBundle(TypedDict):
    """One producer-owned fact set shared by race verdict and evidence."""

    events: list[dict]
    lock_facts: list[dict]
    access_locksets: list[dict]
    relations: list[dict]
    competing_access: list[dict]
    may_parallel: list[dict]

# SUB-06 / R10: the non-race mechanism queries whose already-computed results
# are exposed as the additive ``mechanism_facts`` block (cwe1123_v2_query_contract.md
# §4). ``unlock_discipline`` has no existing query state, so it is deliberately
# absent -- not filled, not diverged (contract §4.1 / §6 U3).
MECHANISM_QUERIES = (
    defect.RACE_CONDITION,
    defect.LOCK_ORDER,
    defect.RESOURCE_LIFETIME,
)

# Category selection: a named group of detectors. Each value is an ordered
# sequence of ``(query_name, extra_kwargs)`` pairs, and ``extra_kwargs`` is
# forwarded to ``defect.run`` (which drops keys a query does not accept). The
# ``defect_keys`` entries reuse the queries' built-in matrix-key filter that
# runs *before* truncation, so `resource_lifetime` answers ``memory`` with
# matrix 4.1 only and ``concurrency`` with 4.6 only -- the one query whose two
# halves belong to different categories is split at the matrix-key level, never
# by query name. ``include_locks=False`` on the memory half turns the lock walk
# off entirely rather than computing it and filtering afterwards.
#
# The R1-A / R1-B / R1-C wiring is deliberately *not* exposed here as a user
# switch: those remain query-internal abilities. A category only selects which
# detectors run.
CATEGORY_QUERIES: dict[str, tuple[tuple[str, dict[str, object]], ...]] = {
    "memory": (
        (defect.RESOURCE_LIFETIME, {"defect_keys": ("4.1",), "include_locks": False}),
        (defect.NULL_FLOW, {}),
        (defect.USE_AFTER_FREE, {}),
        (defect.DOUBLE_FREE, {}),
        (defect.MISMATCHED_FREE, {}),
    ),
    "concurrency": (
        (defect.RACE_CONDITION, {}),
        (defect.LOCK_ORDER, {}),
        (defect.RESOURCE_LIFETIME, {"defect_keys": ("4.6",)}),
    ),
    "dataflow": (
        (defect.TAINT_PATH, {}),
    ),
    "reliability": (
        (defect.ERROR_HANDLING, {}),
    ),
}
# Sorted for stable error text and discovery order; "all" is not listed because
# it is the no-selection default, not a distinct group.
KNOWN_CATEGORIES = tuple(sorted(CATEGORY_QUERIES))


def _expand_categories(category: str) -> list[tuple[str, dict[str, object]]]:
    """Parse a comma-separated category list into ordered ``(query, kwargs)``.

    Deterministic: a category keeps its declared order, and ``resource_lifetime``
    named under both ``memory`` and ``concurrency`` yields two specs (4.1 and
    4.6) instead of a merged one -- the two matrix halves are selected
    independently. ``all`` expands to every supported query; an unknown name
    fails closed instead of silently degrading.
    """
    names: list[str] = []
    for item in category.split(","):
        name = item.strip()
        if not name:
            continue
        if name != "all" and name not in KNOWN_CATEGORIES:
            raise AnalysisRequestError(
                "invalid_request",
                f"unknown category {name!r}; known: {', '.join((*KNOWN_CATEGORIES, 'all'))}",
            )
        if name not in names:
            names.append(name)
    if not names:
        raise AnalysisRequestError("invalid_request", "no category requested")
    specs: list[tuple[str, dict[str, object]]] = []
    for name in names:
        if name == "all":
            specs.extend((query_name, {}) for query_name in SUPPORTED_DEFECT_QUERIES)
        else:
            specs.extend(CATEGORY_QUERIES[name])
    return specs


def _run_query_specs(
    specs: list[tuple[str, dict[str, object]]],
    events: list[dict],
    control_edges: list[dict],
    *,
    subject: str | None,
    max_hops: int,
    max_paths: int,
    max_candidates: int,
    names: dict,
    contract_table: object,
    signatures: dict,
    race_facts: Mapping[str, Any] | None = None,
) -> tuple[list, list, bool, dict, dict]:
    """Run ``(query, kwargs)`` specs and merge their results.

    Shared by the ``all`` and ``category`` paths; the single-query path keeps
    its own shape because it exposes one query's coverage block verbatim rather
    than the additive merge.
    """
    results = []
    truncated = False
    coverage: dict = {}
    for query_name, extra in specs:
        one = defect.run(
            query_name, events, control_edges,
            subject=subject, max_hops=max_hops, max_paths=max_paths,
            max_candidates=max_candidates, names=names,
            callees=contract_table, signatures=signatures,
            contracts=contract_table,
            race_facts=race_facts,
            **extra,
        )
        results.append(one)
        truncated = truncated or one.truncated
        for key, value in one.coverage.items():
            if isinstance(value, (int, float)):
                coverage[key] = coverage.get(key, 0) + value
            else:
                coverage.setdefault(key, value)
    candidates = sorted(
        (c for r in results for c in r.candidates),
        key=lambda c: c.sort_key())
    mechanism_facts = {
        one.query: [candidate.to_dict() for candidate in one.candidates]
        for one in results
        if one.query in MECHANISM_QUERIES
    }
    return results, candidates, truncated, coverage, mechanism_facts


def _upstream_state(store: AnalysisStore) -> dict:
    """The freshness of every upstream feature, per file, fail-closed.

    Coverage is part of freshness: a base file whose suffix the vocabulary
    covers but that has no current upstream state row means the derived graph
    does not cover the tree, and the query must say so instead of answering
    over a partial graph.
    """
    stale: list[dict] = []
    missing: list[str] = []
    from .analysis_c_events import EVENTS_SUFFIXES  # noqa: PLC0415

    for feature in UPSTREAM_FEATURES:
        rows = store.storage.rows(
            "SELECT s.file_id, s.status, s.source_hash, s.feature_version, "
            "s.metadata, f.path FROM analysis_state s "
            "JOIN files f ON f.file_id=s.file_id WHERE s.feature=?",
            (feature,))
        seen_files = set()
        for row in rows:
            seen_files.add(row["path"])
            state = dict(row)
            state["metadata"] = json_loads(row["metadata"])
            reasons = []
            if state["status"] != "current":
                reasons.append("status_not_current")
            base = store.storage.row(
                "SELECT source_hash FROM file_state WHERE file_id=?", (row["file_id"],))
            if base is None:
                reasons.append("file_missing_from_base")
            elif base["source_hash"] != state["source_hash"]:
                reasons.append("source_hash_changed")
            dependency_reason = dependency_staleness(state["metadata"])
            if dependency_reason is not None:
                reasons.append(dependency_reason)
            if reasons:
                stale.append({"file": row["path"], "feature": feature,
                              "reason": "+".join(reasons)})
        # coverage: every base file the vocabulary covers needs a state row
        expected = {
            row["path"]
            for row in store.storage.rows("SELECT path FROM files")
            if Path(row["path"]).suffix.lower() in EVENTS_SUFFIXES
        }
        uncovered = sorted(expected - seen_files)
        if uncovered:
            stale.append({"feature": feature, "reason": "files_not_covered",
                          "files": uncovered[:20], "count": len(uncovered)})
        if not rows:
            missing.append(feature)
    return {"stale": stale, "missing_features": missing}


def json_loads(text: str) -> dict:
    import json  # noqa: PLC0415

    return json.loads(text or "{}")


def _apply_confidence_gate(
    candidates: list,
    *,
    gate: float | None,
    suppress_below: bool,
) -> tuple[list, list]:
    """Partition ``candidates`` into (kept, suppressed) at the band cut.

    SUB-11 / R11 precision guardrail. ``Candidate.confidence`` is a *band* (see
    ``defect.py``), not a probability, so the gate is a band cut applied
    *after* ``defect.run`` has produced and top-K'd its candidates: it never
    re-sorts, never re-runs top-K, never mutates a candidate, and never changes
    the surviving order. The decision of whether a candidate *is* a candidate
    is made upstream and is left untouched -- this only drops the ones whose
    band the caller asked to suppress.

    When the gate is disabled (``suppress_below`` false, or ``gate`` is None)
    nothing is suppressed and ``kept`` preserves the input order.
    """
    if not suppress_below or gate is None:
        return list(candidates), []
    kept: list = []
    suppressed: list = []
    for candidate in candidates:
        (kept if candidate.confidence >= gate else suppressed).append(candidate)
    return kept, suppressed


def _gate_mechanism_facts(
    mechanism_facts: dict,
    *,
    gate: float | None,
    suppress_below: bool,
) -> dict:
    """Apply the same band cut to the ``mechanism_facts`` block.

    The mechanism block re-groups already-serialized candidates, so the gate
    must trim it with the same predicate the flattened ``candidates`` list
    uses, or the two outputs would disagree about what survived.
    """
    if not suppress_below or gate is None:
        return mechanism_facts
    return {
        name: [candidate for candidate in facts if candidate["confidence"] >= gate]
        for name, facts in mechanism_facts.items()
    }


def _race_related_accesses(
    candidate: Mapping[str, Any],
    event_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The race candidate's access ids, in ``assemble_evidence_v2``'s shape.

    The defect race candidate already names each side's WRITE/READ event in
    ``metadata["access"][owner].writer_event`` / ``reader_event`` (SUB-02's
    already-computed ``_access_summary``). This only re-packages those citations
    into the ``related_accesses`` list the evidence-v2 adapter joins on -- every
    sub-fact (``access_kind``, ``is_write``, ``owner``, span) is read off the
    already-materialized sparse-cfg event, never re-derived.

    The events layer and sparse_cfg share one event id per access
    (``identity.event_id``), so the lockset projection (``build_access_lockset``)
    joins on the same ids this list exposes.
    """
    access_meta = (candidate.get("metadata") or {}).get("access") or {}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for side in access_meta.values():
        if not isinstance(side, Mapping):
            continue
        for field in ("writer_event", "reader_event"):
            event_id = side.get(field)
            if not event_id or event_id in seen:
                continue
            event = event_by_id.get(event_id)
            if event is None:
                continue
            seen.add(event_id)
            annotations = event.get("annotations") or {}
            items.append({
                "event_id": event_id,
                "event_type": event.get("event_type"),
                "access_kind": annotations.get("access_kind", ""),
                "is_write": annotations.get("is_write", ""),
                "owner": event.get("owner_qualified_name")
                or event.get("owner_symbol_id") or "",
                "start_line": int(event.get("start_line") or 0),
                "end_line": int(event.get("end_line") or 0),
            })
    return items


def query_defect_candidates(
    repo: str | Path,
    *,
    defect_type: str = "all",
    category: str | None = None,
    subject: str | None = None,
    max_hops: int = 64,
    max_paths: int = 8,
    max_candidates: int = 20,
    database: str | Path | None = None,
    confidence_gate: float | None = defect.DEFAULT_CONFIDENCE_THRESHOLD,
    suppress_below: bool = True,
) -> dict:
    """Run the ported defect queries over the cached derived graph.

    Pure reader: no table is created, rebuilt or refreshed. The upstream
    chain (events -> sparse_cfg -> sparse_dfg) must already be materialized
    and fresh -- ``analyze --features sparse_dfg`` does that; a stale or
    missing upstream fails the whole query closed rather than yielding
    candidates over partial facts.

    ``category`` selects a group of detectors (``memory`` / ``concurrency`` /
    ``dataflow`` / ``reliability`` / ``all``, comma-separated) and is an
    alternative axis to ``defect_type``: they are mutually exclusive, and naming
    a category together with a specific ``defect_type`` fails closed as an
    ``invalid_request`` rather than defining an implicit union/intersection.

    ``confidence_gate`` / ``suppress_below`` are the SUB-11 / R11 precision
    guardrail: by default candidates whose confidence *band* falls below
    ``DEFAULT_CONFIDENCE_THRESHOLD`` are suppressed from both ``candidates``
    and ``mechanism_facts``, and reported in ``suppressed``. This is a
    post-filter over the already top-K'd output of ``defect.run`` -- pass
    ``confidence_gate=None`` (or ``suppress_below=False``) to see the
    unfiltered set.
    """
    if category and defect_type not in ("all", ""):
        raise AnalysisRequestError(
            "invalid_request",
            "--type and --category are mutually exclusive; specify exactly one",
        )
    category_specs = _expand_categories(category) if category else None
    root = Path(repo).resolve()
    database = Path(database).resolve() if database else root / ".provenlattice" / "codegraph.db"
    started_payload: dict = {}
    with SQLiteStorage(database) as storage:
        repository = storage.repository()
        if not repository:
            raise AnalysisRequestError(
                "base_graph_required", "run `provenlattice index` before defect queries")
        repo_id = repository["repo_id"]
        store = AnalysisStore(storage)
        upstream = _upstream_state(store)
        if upstream["missing_features"]:
            # fail closed with the same shape as the stale answer: no
            # candidate may be produced over a partial upstream
            return {
                "query": defect_type,
                "status": "stale",
                "reason": "upstream_not_built",
                "missing_features": upstream["missing_features"],
                "candidates": [],
            }
        if upstream["stale"]:
            return {
                "query": defect_type,
                "status": "stale",
                "reason": "upstream_analysis_stale",
                "stale": upstream["stale"][:20],
                "candidates": [],
            }

        events: list[dict] = []
        control_edges: list[dict] = []
        file_paths = {
            row["file_id"]: row["path"]
            for row in store.storage.rows("SELECT file_id, path FROM files")
        }
        names = {
            row["id"]: row["qualified_name"]
            for row in store.storage.rows(
                "SELECT id, qualified_name FROM nodes WHERE kind IN ('Function','Method')")
        }
        signatures = {
            row["id"]: row["signature"]
            for row in store.storage.rows(
                "SELECT id, signature FROM nodes WHERE kind IN ('Function','Method')")
        }
        for row in store.storage.rows(
            "SELECT analysis_node_id, kind, payload FROM analysis_nodes "
            "WHERE feature='sparse_cfg:cpp'"
        ):
            payload = json_loads(row["payload"])
            path = file_paths.get(payload.get("file_id", ""), "")
            # the annotation channel materialize_points reads lives in the
            # event's metadata (PR: cfg.annotations merged at publish); the
            # citation keys (relative_path) ride the same dict, and the owner
            # name table is passed to run() separately
            events.append({
                "event_id": row["analysis_node_id"],
                "event_type": row["kind"],
                "metadata": {**payload.get("annotations", {}),
                             "relative_path": path, "shard_path": ""},
                "file_id": payload.get("file_id", ""),
                "relative_path": path,
                **payload,
            })
        for row in store.storage.rows(
            "SELECT analysis_edge_id, src_id, dst_id, kind, payload "
            "FROM analysis_edges WHERE feature='sparse_cfg:cpp'"
        ):
            payload = json_loads(row["payload"])
            control_edges.append({
                "edge_id": row["analysis_edge_id"],
                "src_event_id": row["src_id"],
                "dst_event_id": row["dst_id"],
                "relation": row["kind"],
                "flags": tuple(payload.get("flags", ())),
                "metadata": payload,
            })
        for row in store.storage.rows(
            "SELECT analysis_edge_id, src_id, dst_id, kind, payload "
            "FROM analysis_edges WHERE feature='sparse_dfg:cpp'"
        ):
            payload = json_loads(row["payload"])
            control_edges.append({
                "edge_id": row["analysis_edge_id"],
                "src_event_id": row["src_id"],
                "dst_event_id": row["dst_id"],
                "relation": row["kind"],
                "flags": tuple(payload.get("flags", ())),
                "metadata": payload,
            })

        # The events:cpp lock facts the race query now consumes (P0/P1): the
        # LOCK/UNLOCK vocabulary events carry lock_identity / subject_exact /
        # acquire_kind / raii, which the sparse_cfg side does not. They join the
        # shared producer by event id (identity.event_id) and are passed through,
        # never re-derived.
        lock_facts: list[dict] = []
        for row in store.storage.rows(
            "SELECT analysis_node_id, kind, payload FROM analysis_nodes "
            "WHERE feature='events:cpp' AND kind IN ('LOCK','UNLOCK')"
        ):
            payload = json_loads(row["payload"])
            lock_facts.append({
                "event_id": row["analysis_node_id"],
                "event_type": row["kind"],
                **payload,
            })

        # The shared producer's edge shape differs from this service's
        # control_edges (kind/src_id/dst_id/payload vs relation/src_event_id/
        # dst_event_id/metadata): a thin field mapping, no algorithm. Only
        # CONTROL_REACHES edges participate.
        lockset_edges = [
            {
                "src_id": edge["src_event_id"],
                "dst_id": edge["dst_event_id"],
                "kind": edge["relation"],
                "payload": edge["metadata"],
            }
            for edge in control_edges
            if edge["relation"] == "CONTROL_REACHES"
        ]
        event_by_id = {event["event_id"]: event for event in events}
        # Build one shared race-fact bundle lazily. The verdict and Evidence V2
        # consume this same object; neither maintains a second lock analysis.
        lockset: dict[str, Any] | None = None
        race_scenarios: dict[str, Any] | None = None
        race_facts: RaceFactBundle | None = None
        race_requested = (
            defect_type in ("all", "", defect.RACE_CONDITION)
            and category_specs is None
        ) or any(
            query_name == defect.RACE_CONDITION
            for query_name, _ in (category_specs or ())
        )
        if race_requested:
            lockset = build_access_lockset(events, lockset_edges, lock_facts)
            concurrency_context = build_concurrency_context(events)
            race_scenarios = build_race_scenarios(
                events, concurrency_context, edges=lockset_edges,
                lock_facts=lock_facts, top_k=None,
            )
            race_facts = {
                "events": events,
                "lock_facts": lock_facts,
                "access_locksets": lockset["access_locksets"],
                "relations": lockset["relations"],
                "competing_access": race_scenarios["competing_access"],
                "may_parallel": concurrency_context["may_parallel"],
            }

        started_payload = {
            "events": len(events), "edges": len(control_edges),
            "lock_facts": len(lock_facts),
        }

        # R1-C: assemble the *full* contract table once and hand it to the
        # queries that read it. The CALLS raw references carry a call's callee
        # name, joined on (file_id, owner_symbol_id, start_line); the join keeps
        # only keys that name one callee, so unresolved / ambiguous / indirect
        # calls stay unknown ("") and never compose. `resource_lifetime` reads
        # `sinks` / `wrappers` / `consumes` for its owner rulings, and
        # `mismatched_free` reads `allocator_pairs` for the pair verdict.
        contract_table = contracts.build(
            events, control_edges,
            references=store.storage.rows(
                "SELECT file_id, owner_symbol_id, start_line, raw_name "
                "FROM raw_references WHERE reference_type='CALLS'"),
            names=names, signatures=signatures,
            declared=contracts.load_declared(),
        )

        query = defect_type
        if category:
            specs = category_specs or []
            _, candidates, truncated, coverage, mechanism_facts = _run_query_specs(
                specs, events, control_edges,
                subject=subject, max_hops=max_hops, max_paths=max_paths,
                max_candidates=max_candidates, names=names,
                contract_table=contract_table, signatures=signatures,
                race_facts=race_facts,
            )
            query = "category " + category + " (" + ", ".join(
                dict.fromkeys(name for name, _ in specs)) + ")"
        elif defect_type in ("all", ""):
            specs = [(query_name, {}) for query_name in SUPPORTED_DEFECT_QUERIES]
            _, candidates, truncated, coverage, mechanism_facts = _run_query_specs(
                specs, events, control_edges,
                subject=subject, max_hops=max_hops, max_paths=max_paths,
                max_candidates=max_candidates, names=names,
                contract_table=contract_table, signatures=signatures,
                race_facts=race_facts,
            )
            query = "all (" + ", ".join(SUPPORTED_DEFECT_QUERIES) + ")"
        else:
            try:
                result = defect.run(
                    query, events, control_edges,
                    subject=subject, max_hops=max_hops, max_paths=max_paths,
                    max_candidates=max_candidates, names=names,
                    callees=contract_table, signatures=signatures,
                    contracts=contract_table, race_facts=race_facts,
                )
            except ValueError as error:
                raise AnalysisRequestError(
                    "unknown_defect_query", str(error)) from error
            candidates = list(result.candidates)
            truncated = result.truncated
            coverage = dict(result.coverage)
            # Same additive block as the "all" path, but only the one query that
            # was actually run can have computed state to expose.
            mechanism_facts = (
                {result.query: [candidate.to_dict() for candidate in result.candidates]}
                if result.query in MECHANISM_QUERIES
                else {}
            )

        # SUB-11 / R11: post-filter at the band cut. Determinism and order are
        # preserved -- ``_apply_confidence_gate`` only partitions. Applied once,
        # to the flattened candidates and to the mechanism block, so the two
        # outputs never disagree about what survived.
        candidates, suppressed = _apply_confidence_gate(
            candidates, gate=confidence_gate, suppress_below=suppress_below)
        mechanism_facts = _gate_mechanism_facts(
            mechanism_facts, gate=confidence_gate, suppress_below=suppress_below)

    with SQLiteStorage(database) as storage:
        generation = storage.current_generation(repo_id)

    evidence_pairs = []
    for candidate in candidates:
        evidence = defect.candidate_evidence(
            candidate, repository=repo_id, commit="base", generation=generation)
        bundle = defect.candidate_bundle(
            candidate, evidence_id=evidence.evidence_id, generation=generation,
            extra_missing=(
                "control reachability is a three-state evidence answer; "
                "unknown must be resolved before any finding is confirmed",
            ),
        )
        candidate_dict = {**candidate.to_dict(),
                          "evidence_id": evidence.evidence_id}
        if candidate.query == defect.RACE_CONDITION and race_facts is not None:
            # P1: the race candidate now exposes its access citations and the
            # shared producer's held-lockset/relations, so evidence_v2's access
            # and lock blocks join on the same access event ids the
            # concurrency-profile path already uses.
            candidate_dict["related_accesses"] = _race_related_accesses(
                candidate_dict, event_by_id)
            candidate_dict["evidence_v2"] = assemble_evidence_v2(
                candidate_dict,
                access_locksets=race_facts["access_locksets"],
                lock_relations=race_facts["relations"],
                competing_access=race_facts["competing_access"],
            )
        else:
            candidate_dict["evidence_v2"] = assemble_evidence_v2(candidate_dict)
        evidence_pairs.append((candidate_dict, bundle.to_dict()))

    patterns = {p.key: p for p in load_defect_patterns()}
    return {
        "query": query,
        "defect_type": defect_type,
        "category": category,
        "status": "available",
        "candidates": [pair[0] for pair in evidence_pairs],
        "evidence_bundles": [pair[1] for pair in evidence_pairs],
        "mechanism_facts": mechanism_facts,
        "confidence_gate": confidence_gate,
        "suppressed": [candidate.to_dict() for candidate in suppressed],
        "suppressed_count": len(suppressed),
        "coverage": coverage,
        "truncated": truncated,
        "grade": CAPABILITY_GRADE,
        "level": "level0+structural+access+dfg",
        "matrix_keys": {
            key: {"query": pattern.query, "expanded": pattern.expanded,
                  "missing_events": list(pattern.missing_events()),
                  "missing_relations": list(pattern.missing_relations())}
            for key, pattern in sorted(patterns.items())
        },
        "provenance": {
            "queries": list(SUPPORTED_DEFECT_QUERIES),
            "upstream_features": list(UPSTREAM_FEATURES),
            "grade": "local-only, diagnostic",
            "resolved_is_not_verified": (
                "resolution_status is an evidence-resolution state; confidence "
                "is the PR heuristic score, not a calibrated probability"),
            **started_payload,
        },
    }
