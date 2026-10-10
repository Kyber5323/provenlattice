"""Derived-analysis store: per file x analyzer cache with fail-closed freshness."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .storage import SQLiteStorage


ANALYSIS_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_state (
    file_id TEXT NOT NULL, feature TEXT NOT NULL,
    source_hash TEXT NOT NULL, feature_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('current', 'stale')),
    metadata TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(file_id, feature)
);
CREATE TABLE IF NOT EXISTS analysis_nodes (
    analysis_node_id TEXT PRIMARY KEY, file_id TEXT NOT NULL,
    feature TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_edges (
    analysis_edge_id TEXT PRIMARY KEY, file_id TEXT NOT NULL,
    feature TEXT NOT NULL, src_id TEXT NOT NULL, dst_id TEXT NOT NULL,
    kind TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analysis_nodes_file_feature
    ON analysis_nodes(file_id, feature);
CREATE INDEX IF NOT EXISTS idx_analysis_edges_file_feature
    ON analysis_edges(file_id, feature);
"""

ANALYSIS_TABLES = frozenset({"analysis_state", "analysis_nodes", "analysis_edges"})
KNOWN_FEATURES = frozenset({"ast", "cfg"})
LANGUAGE_PATTERN = re.compile(r"[a-z0-9_\-]+")
ANALYZER_VERSIONS = {"ast:python": 1, "cfg:python": 1, "ast:c": 1}
STATUS_CURRENT = "current"
MAX_ANALYSIS_NODE_PAYLOAD_BYTES = 4096


class PayloadTooLargeError(ValueError):
    def __init__(
        self, *, analyzer: str, file_id: str, item_id: str, size: int, limit: int
    ) -> None:
        self.analyzer = analyzer
        self.file_id = file_id
        self.item_id = item_id
        self.size = size
        self.limit = limit
        super().__init__(
            f"payload_too_large: analyzer={analyzer} file_id={file_id} "
            f"item={item_id} size={size} limit={limit}"
        )


@dataclass(slots=True)
class AnalysisNode:
    analysis_node_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AnalysisEdge:
    analysis_edge_id: str
    src_id: str
    dst_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def analyzer_id(feature: str, language: str) -> str:
    if feature not in KNOWN_FEATURES:
        raise ValueError(f"unsupported analysis feature: {feature}")
    if not language or not LANGUAGE_PATTERN.fullmatch(language):
        raise ValueError(f"invalid analyzer language: {language!r}")
    return f"{feature}:{language}"


def analyzer_contract(feature: str, language: str, version: int) -> str:
    return f"{language}-{feature}-v{int(version)}"


def register_analyzer(identifier: str, version: int) -> None:
    _validate_analyzer(identifier)
    if int(version) < 1:
        raise ValueError("analyzer version must be >= 1")
    current = ANALYZER_VERSIONS.get(identifier)
    if current is not None and current != int(version):
        raise ValueError(
            f"analyzer already registered with version {current}: {identifier}"
        )
    ANALYZER_VERSIONS[identifier] = int(version)


def _validate_analyzer(analyzer: str) -> str:
    feature, separator, language = analyzer.partition(":")
    if not separator or not language:
        raise ValueError(
            f"invalid analyzer id (expected '<feature>:<language>'): {analyzer!r}"
        )
    if feature not in KNOWN_FEATURES:
        raise ValueError(f"unsupported analysis feature: {feature}")
    if not LANGUAGE_PATTERN.fullmatch(language):
        raise ValueError(f"invalid analyzer language in id: {analyzer!r}")
    return analyzer


def _canonical_version(analyzer: str) -> int:
    _validate_analyzer(analyzer)
    version = ANALYZER_VERSIONS.get(analyzer)
    if version is None:
        raise ValueError(f"analyzer is not registered: {analyzer!r}")
    return version


def _payload_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True)


def encoded_payload_size(payload: dict[str, Any]) -> int:
    return len(_payload_json(payload).encode("utf-8"))


class AnalysisStore:
    """Independent derived tables inside the base SQLite file.

    Replacement and freshness granularity is file x analyzer, where the
    analyzer id '<feature>:<language>' uniquely determines feature, language
    and the registered contract version. Every read is gated by an explicit
    freshness check against a caller supplied (or base graph derived) source
    hash; stale payloads are never returned as current.
    """

    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage
        storage.connection.executescript(ANALYSIS_SCHEMA)
        storage.connection.commit()

    def get_analysis_state(self, file_id: str, analyzer: str) -> dict | None:
        row = self.storage.row(
            "SELECT * FROM analysis_state WHERE file_id=? AND feature=?",
            (file_id, _validate_analyzer(analyzer)),
        )
        if row is None:
            return None
        state = dict(row)
        state["metadata"] = json.loads(state["metadata"])
        state["feature_version"] = int(state["feature_version"])
        return state

    def is_analysis_fresh(
        self, file_id: str, analyzer: str, source_hash: str, feature_version: int
    ) -> bool:
        state = self.get_analysis_state(file_id, analyzer)
        if state is None:
            return False
        return (
            state["status"] == STATUS_CURRENT
            and state["source_hash"] == source_hash
            and state["feature_version"] == int(feature_version)
        )

    def base_source_hash(self, file_id: str) -> str | None:
        row = self.storage.row(
            "SELECT source_hash FROM file_state WHERE file_id=?", (file_id,)
        )
        return row["source_hash"] if row else None

    def freshness_against_base(self, file_id: str, analyzer: str) -> dict:
        analyzer = _validate_analyzer(analyzer)
        current_version = _canonical_version(analyzer)
        state = self.get_analysis_state(file_id, analyzer)
        base_hash = self.base_source_hash(file_id)
        if state is None:
            return {
                "fresh": False, "reason": "not_built", "stored_source_hash": None,
                "base_source_hash": base_hash, "stored_feature_version": None,
                "current_feature_version": current_version,
            }
        reasons: list[str] = []
        if state["status"] != STATUS_CURRENT:
            reasons.append("status_not_current")
        if base_hash is None:
            reasons.append("file_missing_from_base")
        elif state["source_hash"] != base_hash:
            reasons.append("source_hash_changed")
        if state["feature_version"] != current_version:
            reasons.append("feature_version_changed")
        return {
            "fresh": not reasons,
            "reason": "+".join(reasons) if reasons else None,
            "stored_source_hash": state["source_hash"],
            "base_source_hash": base_hash,
            "stored_feature_version": state["feature_version"],
            "current_feature_version": current_version,
        }

    def replace_analysis_for_file(
        self,
        file_id: str,
        analyzer: str,
        source_hash: str,
        feature_version: int,
        nodes: list[AnalysisNode],
        edges: list[AnalysisEdge],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        analyzer = _validate_analyzer(analyzer)
        for node in nodes:
            size = encoded_payload_size(node.payload)
            if size > MAX_ANALYSIS_NODE_PAYLOAD_BYTES:
                raise PayloadTooLargeError(
                    analyzer=analyzer, file_id=file_id,
                    item_id=node.analysis_node_id, size=size,
                    limit=MAX_ANALYSIS_NODE_PAYLOAD_BYTES,
                )
        for edge in edges:
            size = encoded_payload_size(edge.payload)
            if size > MAX_ANALYSIS_NODE_PAYLOAD_BYTES:
                raise PayloadTooLargeError(
                    analyzer=analyzer, file_id=file_id,
                    item_id=edge.analysis_edge_id, size=size,
                    limit=MAX_ANALYSIS_NODE_PAYLOAD_BYTES,
                )
        node_rows = [
            (node.analysis_node_id, file_id, analyzer, node.kind,
             _payload_json(node.payload))
            for node in nodes
        ]
        edge_rows = [
            (edge.analysis_edge_id, file_id, analyzer, edge.src_id, edge.dst_id,
             edge.kind, _payload_json(edge.payload))
            for edge in edges
        ]
        with self.storage.transaction() as connection:
            connection.execute(
                "DELETE FROM analysis_nodes WHERE file_id=? AND feature=?",
                (file_id, analyzer),
            )
            connection.execute(
                "DELETE FROM analysis_edges WHERE file_id=? AND feature=?",
                (file_id, analyzer),
            )
            connection.execute(
                "DELETE FROM analysis_state WHERE file_id=? AND feature=?",
                (file_id, analyzer),
            )
            connection.execute(
                "INSERT INTO analysis_state VALUES(?,?,?,?,?,?,?)",
                (file_id, analyzer, source_hash, int(feature_version), STATUS_CURRENT,
                 json.dumps(metadata or {}, sort_keys=True), _now()),
            )
            connection.executemany(
                "INSERT INTO analysis_nodes VALUES(?,?,?,?,?)", node_rows
            )
            connection.executemany(
                "INSERT INTO analysis_edges VALUES(?,?,?,?,?,?,?)", edge_rows
            )

    def delete_analysis_for_file(self, file_id: str, analyzer: str) -> bool:
        analyzer = _validate_analyzer(analyzer)
        existed = self.get_analysis_state(file_id, analyzer) is not None
        with self.storage.transaction() as connection:
            connection.execute(
                "DELETE FROM analysis_nodes WHERE file_id=? AND feature=?",
                (file_id, analyzer),
            )
            connection.execute(
                "DELETE FROM analysis_edges WHERE file_id=? AND feature=?",
                (file_id, analyzer),
            )
            connection.execute(
                "DELETE FROM analysis_state WHERE file_id=? AND feature=?",
                (file_id, analyzer),
            )
        return existed

    def read_analysis_for_file(
        self, file_id: str, analyzer: str, *, source_hash: str, feature_version: int,
        allow_stale: bool = False,
    ) -> dict | None:
        state = self.get_analysis_state(file_id, analyzer)
        if state is None:
            return None
        fresh = self.is_analysis_fresh(file_id, analyzer, source_hash, feature_version)
        return self._read_payload(state, fresh, allow_stale,
                                  None if fresh else "source_hash_or_feature_version_mismatch")

    def read_current_analysis(
        self, file_id: str, analyzer: str, *, allow_stale: bool = False
    ) -> dict | None:
        _canonical_version(analyzer)
        state = self.get_analysis_state(file_id, analyzer)
        if state is None:
            return None
        verdict = self.freshness_against_base(file_id, analyzer)
        return self._read_payload(state, verdict["fresh"], allow_stale, verdict["reason"])

    def _read_payload(
        self, state: dict, fresh: bool, allow_stale: bool, reason: str | None
    ) -> dict:
        include = fresh or allow_stale
        nodes = self._nodes(state["file_id"], state["feature"]) if include else []
        edges = self._edges(state["file_id"], state["feature"]) if include else []
        return {"fresh": fresh, "reason": reason, "state": state,
                "nodes": nodes, "edges": edges}

    def _nodes(self, file_id: str, analyzer: str) -> list[dict]:
        rows = self.storage.rows(
            "SELECT * FROM analysis_nodes WHERE file_id=? AND feature=? ORDER BY analysis_node_id",
            (file_id, analyzer),
        )
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def _edges(self, file_id: str, analyzer: str) -> list[dict]:
        rows = self.storage.rows(
            "SELECT * FROM analysis_edges WHERE file_id=? AND feature=? ORDER BY analysis_edge_id",
            (file_id, analyzer),
        )
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows
