"""Optional analysis orchestrator: scope x features -> derived analysis run."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from .analysis_ast import analyze_ast_scope, normalize_scope, scoped_sources
from .analysis_cfg import ANALYZER as CFG_ANALYZER, analyze_cfg_scope
from .identity import path_id
from .scanner import IGNORED_DIRS, SUPPORTED_EXTENSIONS
from .storage import SQLiteStorage

SUPPORTED_FEATURES = ("ast", "cfg")
FEATURE_DEPENDENCIES = {"cfg": ("ast",)}
REASON_PREFIX = "skipped_"


class AnalysisRequestError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _parse_features(features) -> list[str]:
    if isinstance(features, str):
        requested = [item.strip() for item in features.split(",") if item.strip()]
    else:
        requested = [str(item).strip() for item in features]
    if not requested:
        raise AnalysisRequestError("invalid_features", "no features requested")
    unique: list[str] = []
    for feature in requested:
        if feature not in SUPPORTED_FEATURES:
            raise AnalysisRequestError("unknown_feature", f"unsupported feature: {feature}")
        if feature not in unique:
            unique.append(feature)
    return unique


def _plan_features(requested: list[str]) -> list[str]:
    plan = list(requested)
    for feature in requested:
        for dependency in FEATURE_DEPENDENCIES.get(feature, ()):
            if dependency not in plan:
                plan.insert(0, dependency)
    return plan


def _inventory(root: Path, scope_path: PurePosixPath) -> list[dict]:
    entries = []
    for path in sorted(root.joinpath(*scope_path.parts).rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(part in IGNORED_DIRS for part in PurePosixPath(relative).parts[:-1]):
            continue
        entries.append({
            "path": relative,
            "language": SUPPORTED_EXTENSIONS.get(path.suffix),
        })
    return entries


def _cfg_file_metadata(database: Path) -> dict[str, dict]:
    with SQLiteStorage(database) as storage:
        exists = storage.row(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='analysis_state'"
        )
        if exists is None:
            return {}
        return {
            row["file_id"]: json.loads(row["metadata"])
            for row in storage.rows(
                "SELECT file_id, metadata FROM analysis_state WHERE feature=?",
                (CFG_ANALYZER,),
            )
        }


def analyze_scope(repo: str | Path, scope: str, features, database: str | Path | None = None) -> dict:
    root = Path(repo).resolve()
    requested = _parse_features(features)
    try:
        scope_path = normalize_scope(scope)
    except ValueError as error:
        raise AnalysisRequestError("invalid_scope", str(error)) from error
    if not root.joinpath(*scope_path.parts).is_dir():
        raise AnalysisRequestError("invalid_scope", f"scope directory does not exist: {scope}")
    plan = _plan_features(requested)
    database = Path(database).resolve() if database else root / ".provenlattice" / "codegraph.db"
    with SQLiteStorage(database) as storage:
        if storage.repository() is None:
            raise AnalysisRequestError(
                "base_graph_required", "run `provenlattice index` before analyze"
            )
    results = {}
    if "ast" in plan:
        results["ast"] = analyze_ast_scope(root, scope_path.as_posix(), database)
    if "cfg" in plan:
        results["cfg"] = analyze_cfg_scope(root, scope_path.as_posix(), database)
    categories: dict[str, dict[str, tuple[str, str | None]]] = {}
    for feature, result in results.items():
        mapping: dict[str, tuple[str, str | None]] = {}
        for path in result["materialized"]:
            mapping[path] = ("materialized", None)
        for path in result["reused"]:
            mapping[path] = ("reused", None)
        for key, paths in result.items():
            if key.startswith(REASON_PREFIX):
                reason = key[len(REASON_PREFIX):]
                if reason == "unsupported":
                    reason = "unsupported_language"
                for path in paths:
                    mapping[path] = ("skipped", reason)
        categories[feature] = mapping
    if "cfg" in categories:
        metadata_by_file = _cfg_file_metadata(database)
        repo_id = results["cfg"]["repo_id"]
        for path, (status, reason) in list(categories["cfg"].items()):
            if status != "materialized":
                continue
            metadata = metadata_by_file.get(path_id(repo_id, "File", path), {})
            if metadata.get("functions_built", 0) == 0:
                categories["cfg"][path] = ("skipped", "no_functions_built")
    inventory = _inventory(root, scope_path)
    per_file: list[dict] = []
    counts = {"analyzed": 0, "reused": 0, "skipped": 0}
    for entry in inventory:
        path, language = entry["path"], entry["language"]
        record: dict = {"path": path, "language": language}
        reasons: list[str] = []
        requested_statuses: list[str] = []
        for feature in SUPPORTED_FEATURES:
            if feature not in plan:
                record[f"{feature}_status"] = "not_requested"
                continue
            if language is None:
                status, reason = "skipped", "unsupported_language"
            else:
                status, reason = categories[feature].get(
                    path, ("skipped", "unsupported_language"))
            record[f"{feature}_status"] = status
            if reason is not None:
                reasons.append(f"{feature}:{reason}")
            if feature in requested:
                requested_statuses.append(status)
        record["reasons"] = reasons
        if any(status == "skipped" for status in requested_statuses):
            bucket = "skipped"
        elif all(status == "materialized" for status in requested_statuses):
            bucket = "analyzed"
        elif all(status == "reused" for status in requested_statuses):
            bucket = "reused"
        else:
            bucket = "analyzed"
        record["bucket"] = bucket
        counts[bucket] += 1
        per_file.append(record)
    features_out = {}
    for feature in plan:
        mapping = categories[feature]
        materialized, reused, failures = [], [], []
        skipped_grouped: dict[str, list[str]] = {}
        for entry in inventory:
            path = entry["path"]
            if entry["language"] is None:
                status, reason = "skipped", "unsupported_language"
            else:
                status, reason = mapping.get(
                    path, ("skipped", "unsupported_language"))
            if status == "materialized":
                materialized.append(path)
            elif status == "reused":
                reused.append(path)
            elif reason == "payload_too_large":
                failures.append(path)
            else:
                skipped_grouped.setdefault(reason or "unknown", []).append(path)
        features_out[feature] = {
            "materialized": sorted(materialized),
            "reused": sorted(reused),
            "skipped": {reason: sorted(paths) for reason, paths in skipped_grouped.items()},
            "failures": sorted(failures),
        }
    dependencies = {}
    for feature in plan:
        if feature in requested:
            continue
        dependents = [
            requested_feature for requested_feature in requested
            if feature in FEATURE_DEPENDENCIES.get(requested_feature, ())
        ]
        mapping = categories[feature]
        action_counts = {
            action: sum(1 for status, _ in mapping.values() if status == action)
            for action in ("materialized", "reused")
        }
        if action_counts["materialized"]:
            action = "materialized"
        elif action_counts["reused"]:
            action = "reused"
        else:
            action = "failed"
        dependencies[feature] = {
            "reason": f"required_by_{'+'.join(dependents)}",
            "action": action,
            "files": {
                "materialized": action_counts["materialized"],
                "reused": action_counts["reused"],
                "skipped": sum(1 for status, _ in mapping.values() if status == "skipped"),
            },
        }
    eligible = [entry for entry in inventory if entry["language"] == "python"]
    if not eligible:
        status = "failed"
    else:
        produced_any = False
        all_good = True
        for entry in eligible:
            for feature in requested:
                status, _ = categories[feature].get(
                    entry["path"], ("skipped", "unsupported_language"))
                if status in {"materialized", "reused"}:
                    produced_any = True
                else:
                    all_good = False
        status = "complete" if all_good else ("partial" if produced_any else "failed")
    return {
        "status": status,
        "repo_id": next(iter(results.values()))["repo_id"],
        "database": str(database),
        "scope": scope_path.as_posix(),
        "requested_features": requested,
        "executed_features": plan,
        "files": {"seen": len(inventory), **counts},
        "features": features_out,
        "dependencies": dependencies,
        "per_file": per_file,
    }
