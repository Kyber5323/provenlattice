"""S6 concurrency Profile Registry and L5 integration adapter.

A single registry of the ``concurrency-v1`` Profile and the one execution
adapter that turns the L3 Planner's deterministic plan into per-scenario results:
it runs the delivered S4/S5 scenario rules, converts their unified Candidate
contract into the existing L4 Evidence model, and reports each scenario's
``current`` / ``partial`` / ``unsupported`` / ``stale`` / ``not_built`` state.

The per-scenario state is propagated, not re-invented: the capability state
comes from the Planner (``plan.items``), the per-file state comes from the
query surface, and a deterministic severity merge combines them. A skipped,
unsupported or stale scenario is reported explicitly, never as an empty
success, and a candidate's evidence carries the real source file when it can be
recovered.

Nothing here changes the default index, `race_condition`, `lock_order`, CLI/MCP
defaults or the default query envelope: the profile is opt-in through an
explicit entry only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .analysis_ast import normalize_scope, scoped_sources
from .analysis_query import query_analysis
from .concurrency_lock_scenarios import build_lock_scenarios
from .concurrency_race_scenarios import build_race_scenarios
from .evidence import Evidence
from .orchestrator import analyze_scope
from .planner import PlannerRequest, plan_capabilities
from .storage import SQLiteStorage
from .trace_chain import assemble_trace_chain

CONCURRENCY_PROFILE_ID = "concurrency-v1"

_RACE_SCENARIOS = frozenset({"non_atomic_incdec", "bool_flag_race", "shared_state_race"})
_LOCK_SCENARIOS = frozenset({"lock_pairing", "lock_discipline"})

# A race scenario consumes S1/S2's sparse_cfg access facts; a lock scenario
# consumes S3's events lock facts. This is the one place a scenario is tied to
# the capability whose state it inherits.
_RACE_CAPABILITY = "sparse_cfg"
_LOCK_CAPABILITY = "events"

# Deterministic severity merge for the per-scenario state. Lower is healthier;
# the reported state is the most-severe signal among the capability state and
# every per-file state. A non-``available`` file state (unsupported language,
# stale, not-built) raises the scenario state accordingly and is never folded
# into ``not_built`` silently.
_SEVERITY = {
    "current": 0, "partial": 1, "stale": 2, "unsupported": 3, "not_built": 4,
}

# Query-surface statuses that are not one of the five scenario states are
# mapped to the nearest honest scenario state rather than dropped.
_FILE_STATUS_TO_SCENARIO = {
    "available": "current",
    "current": "current",
    "partial": "partial",
    "stale": "stale",
    "unsupported": "unsupported",
    "not_built": "not_built",
    "unavailable": "not_built",
    "corrupt": "not_built",
}

# The single registry: each profile declares the fields frozen in requirements §5.
CONCURRENCY_V1 = {
    "profile_id": CONCURRENCY_PROFILE_ID,
    "profile_version": "1",
    "query_set_version": "1",
    "scenario_ids": ("non_atomic_incdec", "bool_flag_race", "shared_state_race",
                     "lock_pairing", "lock_discipline"),
    "requested_scope": "src",
    "language_policy": ("c", "cpp"),
    "minimum_maturity": "partially_implemented",
    "budgets": {},
    "required_capabilities": ("events", "sparse_cfg"),
    "optional_capabilities": ("sparse_dfg",),
    "evidence_policy": "candidate",
}

PROFILES: dict[str, dict[str, Any]] = {CONCURRENCY_PROFILE_ID: CONCURRENCY_V1}


def get_profile(profile_id: str) -> dict[str, Any] | None:
    return PROFILES.get(str(profile_id).strip())


def _scenario_capability(scenario_id: str) -> str:
    return _RACE_CAPABILITY if scenario_id in _RACE_SCENARIOS else _LOCK_CAPABILITY


def _capability_state(plan, capability_id: str) -> tuple[str, str | None]:
    """The Planner's state and reason for one capability, or a fail-closed
    ``not_built`` when the capability was not planned."""
    for item in plan.items:
        if item.capability_id == capability_id:
            return item.status, item.reason
    return "not_built", "capability_not_planned"


def _merge_scenario_state(capability_status: str, file_statuses: Mapping[str, list[str]]) -> tuple[str, str | None]:
    """Merge the capability state and per-file states into one scenario state.

    Deterministic: the most-severe signal wins; a tie keeps the capability
    status first. The caller keeps the raw per-file states alongside the merged
    state so nothing is silently folded.
    """
    signals = [capability_status]
    signals.extend(
        _FILE_STATUS_TO_SCENARIO.get(status, "not_built")
        for status in file_statuses
        if status != "available"
    )
    merged = max(signals, key=lambda status: _SEVERITY.get(status, 4))
    if merged == capability_status:
        return merged, None
    return merged, "file_state_dominates"


def _collect_events(
    root: Path, scope: str, feature: str, database: Path,
) -> tuple[list[dict], dict[str, str], dict[str, list[str]]]:
    """Collect events, their source-file provenance, and per-file query states.

    Returns ``(events, event_paths, file_statuses)`` where ``event_paths`` maps
    an event id to its repo-relative source path and ``file_statuses`` maps a
    query-surface status to the list of files that reported it (deterministic
    order). The provenance map is what later restores a candidate's source file.
    """
    events: list[dict] = []
    event_paths: dict[str, str] = {}
    file_statuses: dict[str, list[str]] = {}
    for source in scoped_sources(root, normalize_scope(scope)):
        env = query_analysis(root, feature, file_path=source.relative_path,
                             database=database)
        status = env.get("status")
        file_statuses.setdefault(status, []).append(source.relative_path)
        if status == "available":
            for event in env.get("events", []):
                event_paths[event.get("event_id")] = source.relative_path
                events.append(event)
    for status in file_statuses:
        file_statuses[status].sort()
    return events, event_paths, file_statuses


def _candidate_to_evidence(
    candidate: Mapping[str, Any], repository: str, generation: int,
    event_paths: Mapping[str, str],
) -> Evidence:
    scenario_id = candidate["scenario_id"]
    identity = candidate.get("identity") or candidate.get("subject") or "(unresolved)"
    subject = candidate.get("subject") or identity
    spans = candidate.get("source_spans") or []
    start_line = spans[0][0] if spans and spans[0][0] else None
    end_line = spans[-1][1] if spans and spans[-1][1] else None
    references = list(candidate.get("evidence_references", []))
    related_files = sorted({event_paths[ref] for ref in references if ref in event_paths})
    file_path = related_files[0] if related_files else None
    return Evidence.create(
        kind="DEFECT_CANDIDATE",
        source_id=identity,
        relation=scenario_id,
        target_id=identity,
        repository=repository,
        commit="",
        generation=generation,
        provenance=f"{CONCURRENCY_PROFILE_ID}:{scenario_id}",
        confidence=0.0,
        source_path=file_path,
        start_line=start_line,
        end_line=end_line,
        summary=f"{scenario_id} candidate on {subject}",
        fact_id=references[0] if references else f"{start_line}:{end_line}",
        metadata={
            "scenario_id": scenario_id,
            "candidate_state": candidate.get("candidate_state"),
            "unresolved": bool(candidate.get("unresolved")),
            "exact": candidate.get("exact"),
            "missing_evidence": list(candidate.get("missing_evidence", [])),
            "profile": CONCURRENCY_PROFILE_ID,
            "query_set_version": CONCURRENCY_V1["query_set_version"],
            "related_files": related_files,
            "file_path_missing": not related_files,
        },
    )


def run_concurrency_profile(
    repo, profile_id: str = CONCURRENCY_PROFILE_ID, *,
    database=None, scope: str | None = None,
) -> dict:
    """Run the concurrency-v1 Profile over a scope and return per-scenario results.

    Returns ``{"status", "profile", "plan", "scenarios", "evidence"}``. Each
    scenario entry carries its merged ``current``/``partial``/``unsupported``/
    ``stale``/``not_built`` state together with the raw capability and per-file
    states; a skipped, unsupported or stale scenario is reported explicitly,
    never as an empty success.
    """
    profile = get_profile(profile_id)
    if profile is None:
        return {"status": "unknown_profile", "profile_id": profile_id}

    root = Path(repo).resolve()
    database = Path(database).resolve() if database else root / ".provenlattice" / "codegraph.db"
    scope = scope or profile["requested_scope"]

    plan = plan_capabilities(PlannerRequest(
        capabilities=profile["required_capabilities"],
        language="cpp",
        scope=scope,
        scenario_ids=profile["scenario_ids"],
        minimum_maturity=profile["minimum_maturity"],
    ))

    with SQLiteStorage(database) as storage:
        meta = storage.repository()
        if meta is None:
            return {"status": "not_built", "reason": "base_graph_required"}
        repo_id = meta["repo_id"]
        generation = int(meta.get("current_generation") or 0)

    analyze_scope(root, scope, ("sparse_cfg", "events"), database=database)

    sparse_events, sparse_paths, sparse_file_statuses = _collect_events(
        root, scope, "sparse_cfg", database)
    event_events, event_paths, event_file_statuses = _collect_events(
        root, scope, "events", database)

    race = build_race_scenarios(sparse_events)
    lock = build_lock_scenarios(event_events)

    candidates_by_scenario: dict[str, list[dict]] = {
        scenario_id: [] for scenario_id in profile["scenario_ids"]
    }
    for candidate in (*race["candidates"], *lock["candidates"]):
        candidates_by_scenario.setdefault(candidate["scenario_id"], []).append(candidate)

    evidence: list[Evidence] = []
    scenarios: dict[str, dict[str, Any]] = {}
    for scenario_id in profile["scenario_ids"]:
        capability = _scenario_capability(scenario_id)
        capability_status, capability_reason = _capability_state(plan, capability)
        if capability == _RACE_CAPABILITY:
            event_paths_for_scenario = sparse_paths
            file_statuses = sparse_file_statuses
        else:
            event_paths_for_scenario = event_paths
            file_statuses = event_file_statuses
        status, reason = _merge_scenario_state(capability_status, file_statuses)
        candidates = candidates_by_scenario.get(scenario_id, [])
        scenarios[scenario_id] = {
            "status": status,
            "reason": reason or capability_reason,
            "capability": capability,
            "capability_status": capability_status,
            "capability_reason": capability_reason,
            "file_status": {key: list(value) for key, value in file_statuses.items()},
            "candidate_count": len(candidates),
            "candidates": candidates,
        }
        for candidate in candidates:
            item = _candidate_to_evidence(
                candidate, repo_id, generation, event_paths_for_scenario)
            candidate["evidence_id"] = item.evidence_id
            candidate["trace_evidence"] = assemble_trace_chain(
                candidate, item.evidence_id, event_paths_for_scenario)
            evidence.append(item)

    return {
        "status": "complete",
        "profile": {
            "profile_id": profile["profile_id"],
            "profile_version": profile["profile_version"],
            "query_set_version": profile["query_set_version"],
            "scenario_ids": list(profile["scenario_ids"]),
        },
        "plan": plan.to_dict(),
        "scenarios": scenarios,
        "evidence": [item.to_dict() for item in evidence],
        "coverage": {
            "race": race["coverage"],
            "lock": lock["coverage"],
        },
    }


__all__ = [
    "CONCURRENCY_PROFILE_ID", "CONCURRENCY_V1", "PROFILES",
    "get_profile", "run_concurrency_profile",
]