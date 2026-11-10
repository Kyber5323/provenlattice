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

import dataclasses
from pathlib import Path

from . import contracts, defect
from .analysis import AnalysisStore, dependency_staleness
from .analysis_query import AnalysisRequestError
from .evidence import DefectEvidenceBundle, assemble_evidence_v2
from .semantics.vocabulary import load_defect_patterns
from .storage import SQLiteStorage

SUPPORTED_DEFECT_QUERIES = tuple(defect.QUERY_NAMES)
UPSTREAM_FEATURES = ("events:cpp", "sparse_cfg:cpp", "sparse_dfg:cpp")
CAPABILITY_GRADE = "local-only, diagnostic"

# SUB-06 / R10: the non-race mechanism queries whose already-computed results
# are exposed as the additive ``mechanism_facts`` block (cwe1123_v2_query_contract.md
# §4). ``unlock_discipline`` has no existing query state, so it is deliberately
# absent -- not filled, not diverged (contract §4.1 / §6 U3).
MECHANISM_QUERIES = (
    defect.RACE_CONDITION,
    defect.LOCK_ORDER,
    defect.RESOURCE_LIFETIME,
)


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


def query_defect_candidates(
    repo: str | Path,
    *,
    defect_type: str = "all",
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

    ``confidence_gate`` / ``suppress_below`` are the SUB-11 / R11 precision
    guardrail: by default candidates whose confidence *band* falls below
    ``DEFAULT_CONFIDENCE_THRESHOLD`` are suppressed from both ``candidates``
    and ``mechanism_facts``, and reported in ``suppressed``. This is a
    post-filter over the already top-K'd output of ``defect.run`` -- pass
    ``confidence_gate=None`` (or ``suppress_below=False``) to see the
    unfiltered set.
    """
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
        # The CALLS raw references: a call's callee name lives here, joined on
        # (file_id, owner_symbol_id, start_line); the contract table's join keeps
        # only keys that name one callee, so unresolved / ambiguous / indirect
        # calls stay unknown ("") and never compose.
        callee_names, _ambiguous_spans = contracts._callees(
            store.storage.rows(
                "SELECT file_id, owner_symbol_id, start_line, raw_name "
                "FROM raw_references WHERE reference_type='CALLS'")
        )
        callees = dataclasses.replace(contracts.EMPTY, callees=callee_names)
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

        started_payload = {
            "events": len(events), "edges": len(control_edges),
        }

        query = defect_type
        if defect_type in ("all", ""):
            # "all" over the I2A surface means the two ported queries; PR's
            # dispatcher handled "all" in GraphQuery.get_defect_candidates,
            # which is this service's counterpart.
            results = []
            truncated = False
            coverage: dict = {}
            for query_name in SUPPORTED_DEFECT_QUERIES:
                one = defect.run(
                    query_name, events, control_edges,
                    subject=subject, max_hops=max_hops, max_paths=max_paths,
                    max_candidates=max_candidates, names=names,
                    callees=callees, signatures=signatures,
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
            query = "all (" + ", ".join(SUPPORTED_DEFECT_QUERIES) + ")"
            # SUB-06 / R10: expose the non-race mechanism results as their own
            # additive block, keyed by mechanism name. This is a re-grouping of
            # the already-serialized candidates (no new computation); the block
            # is present even when race_condition produced zero candidates, so
            # lock_order / resource_lifetime state stays visible.
            mechanism_facts = {
                one.query: [candidate.to_dict() for candidate in one.candidates]
                for one in results
                if one.query in MECHANISM_QUERIES
            }
        else:
            try:
                result = defect.run(
                    query, events, control_edges,
                    subject=subject, max_hops=max_hops, max_paths=max_paths,
                    max_candidates=max_candidates, names=names,
                    callees=callees, signatures=signatures,
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
        candidate_dict["evidence_v2"] = assemble_evidence_v2(candidate_dict)
        evidence_pairs.append((candidate_dict, bundle.to_dict()))

    patterns = {p.key: p for p in load_defect_patterns()}
    return {
        "query": query,
        "defect_type": defect_type,
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
