"""Derived-analysis store: per file x analyzer cache with fail-closed freshness."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .storage import SQLiteStorage


ANALYSIS_SCHEMA = """
-- Schema note (I2A): the node/edge primary keys are (id, feature) because
-- two analyzers may publish the SAME derived fact under different features
-- by design -- events:cpp and sparse_cfg:cpp share identity.event_id for the
-- same operation, and the shared id IS the cross-analyzer consistency
-- guarantee.
-- Migration (I2A-R1): databases created before the composite-PK change carry
-- the old single-column PK, which silently rejects the second analyzer's
-- write of a shared id. AnalysisStore.__init__ detects the old PK and
-- migrates the derived cache in place (create new, copy, drop old, rename) --
-- the data is a rebuildable cache, so the migration preserves rows rather
-- than destroying them.
CREATE TABLE IF NOT EXISTS analysis_state (
    file_id TEXT NOT NULL, feature TEXT NOT NULL,
    source_hash TEXT NOT NULL, feature_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('current', 'stale')),
    metadata TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(file_id, feature)
);
CREATE TABLE IF NOT EXISTS analysis_nodes (
    analysis_node_id TEXT NOT NULL, file_id TEXT NOT NULL,
    feature TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(analysis_node_id, feature)
);
CREATE TABLE IF NOT EXISTS analysis_edges (
    analysis_edge_id TEXT NOT NULL, file_id TEXT NOT NULL,
    feature TEXT NOT NULL, src_id TEXT NOT NULL, dst_id TEXT NOT NULL,
    kind TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(analysis_edge_id, feature)
);
CREATE INDEX IF NOT EXISTS idx_analysis_nodes_file_feature
    ON analysis_nodes(file_id, feature);
CREATE INDEX IF NOT EXISTS idx_analysis_edges_file_feature
    ON analysis_edges(file_id, feature);
"""

ANALYSIS_TABLES = frozenset({"analysis_state", "analysis_nodes", "analysis_edges"})
KNOWN_FEATURES = frozenset({"ast", "cfg", "events", "sparse_cfg", "sparse_dfg"})
LANGUAGE_PATTERN = re.compile(r"[a-z0-9_\-]+")
ANALYZER_VERSIONS = {
    "ast:python": 1, "cfg:python": 2, "ast:c": 4, "cfg:c": 3, "events:cpp": 1,
    "sparse_cfg:cpp": 4, "sparse_dfg:cpp": 2,
    "ast:java": 1, "cfg:java": 2,
    "sparse_cfg:java": 1, "sparse_dfg:java": 1,
}
STATUS_CURRENT = "current"
MAX_ANALYSIS_NODE_PAYLOAD_BYTES = 4096

# ---------------------------------------------------------------------------
# Capability contract (I0 freeze, 2026-10-12; I1-A updates the events row) --
# single source of truth for optional-analysis capability selection.
#
# status="implemented" capabilities are the only ones KNOWN_FEATURES admits
# into this store as fully-delivered: they own analyzer ids in
# ANALYZER_VERSIONS and real materializers behind the orchestrator.
#
# status="partially_implemented" (I1-A: `events`) owns real analyzer ids and
# runs on explicit request, but delivers a declared subset -- the events row
# records its `level`. The orchestrator accepts it; the contract must not
# present the subset as the whole capability.
#
# status="registered" capabilities are *named but not implemented* in this
# build (selective PR #1 integration phases per
# docs/pr1-selective-integration-plan.md). They have no analyzer id, no store
# tables and no code here; the orchestrator rejects them fail-closed before
# any mutation (capability_not_available) and the read-only query surface
# reports them unavailable (capability_not_implemented) -- never a fake
# success. Admitting one into the store starts by flipping its status and
# registering real analyzer ids; nothing else in this file needs to change.
#
# Full CFG (`cfg`) and Sparse CFG (`sparse_cfg`) are deliberately separate
# capabilities: no unified CFG implementation exists in this build, and the
# contract must not pretend otherwise (Gold Target V2 keeps Full/Sparse as two
# views of a future canonical model).
#
# `defect` additionally consumes the ownership-contract TOML data files of the
# future defect chain; that is an input of its analyzers, not a capability of
# its own, so it is recorded here as prose and not as a contract row.
CAPABILITY_CONTRACT: dict[str, dict] = {
    "ast": {
        "status": "implemented", "domain": "agent",
        "languages": ("python", "c", "java"), "depends_on": (), "cost_class": "low",
        "analyzer_ids": ("ast:python", "ast:c", "ast:java"), "planned_integration": None,
    },
    "cfg": {
        "status": "implemented", "domain": "agent",
        "languages": ("python", "c", "java"), "depends_on": ("ast",), "cost_class": "medium",
        "analyzer_ids": ("cfg:python", "cfg:c", "cfg:java"), "planned_integration": None,
    },
    "events": {
        # I1-A: level-0 vocabulary events only (CALL/RETURN/THROW/ALLOC/
        # RELEASE/LOCK/UNLOCK/ATOMIC/THREAD_SPAWN/THREAD_JOIN via
        # semantics/vocabularies/cpp.toml). The structural CHECK/BRANCH/
        # DEREFERENCE and READ/WRITE access events are sparse-CFG-walker
        # products and stay unported (I1-B) -- hence partially_implemented,
        # never a full "implemented" claim.
        "status": "partially_implemented", "domain": "inspection",
        "languages": ("c", "cpp"), "depends_on": (), "cost_class": "high",
        "analyzer_ids": ("events:cpp",), "planned_integration": "I1-B (level 1)",
        "level": "level0",
    },
    "sparse_cfg": {
        # I1-B: the sparse CFG walker is delivered -- CONTROL_REACHES edges
        # plus the walker's structural (CHECK/BRANCH/DEREFERENCE) and access
        # (READ/WRITE) points, on demand. The Sparse DFG (DATA_FLOW_TO /
        # CONSUMES) and the Defect Queries that consume it stay in later
        # phases; the contract must not read this row as more than the CFG.
        "status": "implemented", "domain": "inspection",
        "languages": ("c", "cpp", "java"), "depends_on": ("events",), "cost_class": "high",
        "analyzer_ids": ("sparse_cfg:cpp", "sparse_cfg:java"), "planned_integration": None,
        "level": "level0+structural+access",
    },
    "sparse_dfg": {
        # I1-C: the method-local sparse DFG is delivered -- DATA_FLOW_TO
        # (def->use under the annotation flow identity, with alias chains and
        # via intermediates) and CONSUMES (allocation handed through its own
        # acquiring expression). Registered approximations travel with it: no
        # SSA / reaching-definitions, no points-to, no interprocedural edges
        # (the cross-function join stays a defect-layer query, I2). The grade
        # field carries that scope honestly.
        "status": "implemented", "domain": "inspection",
        "languages": ("c", "cpp", "java"),
        "depends_on": ("events", "sparse_cfg"), "cost_class": "high",
        "analyzer_ids": ("sparse_dfg:cpp", "sparse_dfg:java"), "planned_integration": None,
        "level": "level0+structural+access+dfg",
        "grade": "local-only, diagnostic",
    },
    "defect": {
        # I2A: the defect QUERY layer is delivered for two of the eight
        # patterns (null_flow, double_free) -- a query-time reader over the
        # events/sparse_cfg/sparse_dfg rows, not a materializing analyzer.
        # The other six patterns stay unported; resolved is an evidence
        # state, never a Verified Finding, and confidence is the PR
        # heuristic score.
        "status": "implemented", "domain": "inspection",
        "languages": ("c", "cpp"),
        "depends_on": ("events", "sparse_cfg", "sparse_dfg"),
        "cost_class": "high", "analyzer_ids": (),
        "planned_integration": None,
        "supported_queries": (
            "resource_lifetime", "lock_order", "race_condition",
            "null_flow", "use_after_free", "double_free", "taint_path",
            "error_handling",
        ),
        "query_time": True,
        "grade": "local-only, diagnostic",
    },
    "semantic_java": {
        # P2-D: optional Eclipse JDT exact-resolution enrichment for Java
        # CALLS. Query-time only (java-semantic command, defect precedent):
        # the default index never invokes it, the L1 graph is never written,
        # and a missing JDK/JDT classpath yields an explicit unavailable
        # state. Recovered bindings are dropped; ambiguity is never
        # upgraded. Runs as a separate JVM process (P2-B budget: -Xmx1g,
        # RSS <= 1.6GB, jgit <= 15s -- pending human budget approval).
        "status": "implemented", "domain": "agent",
        "languages": ("java",), "depends_on": (), "cost_class": "high",
        "analyzer_ids": (), "planned_integration": None,
        "query_time": True, "optional_runtime": "jdk+jdt",
        "grade": "local-only, diagnostic",
    },
    "concurrency_context": {
        # S2: a query-time reader over the events/sparse_cfg rows
        # (analysis_concurrency_context.py). It turns THREAD_SPAWN/THREAD_JOIN
        # vocabulary events and READ/WRITE access facts into a reusable, finite,
        # provable synchronization-order bundle: same-method spawn-before-join
        # and cross-method MAY_PARALLEL candidates over shared subjects.
        # Partial by contract: no complete happens-before, no memory model, and
        # an unproven MAY_PARALLEL is never upgraded to a resolved fact.
        "status": "partially_implemented", "domain": "inspection",
        "languages": ("c", "cpp"),
        "depends_on": ("events", "sparse_cfg"),
        "cost_class": "medium", "analyzer_ids": (),
        "planned_integration": None,
        "query_time": True,
        "level": "level0+spawn-join",
        "grade": "local-only, diagnostic",
    },
}


def implemented_features() -> frozenset:
    """Features this build fully delivers (subset of KNOWN_FEATURES)."""
    return frozenset(
        feature for feature, record in CAPABILITY_CONTRACT.items()
        if record["status"] == "implemented"
    )


def selectable_features() -> frozenset:
    """Features analyze may materialize: fully implemented + partial subsets."""
    return frozenset(
        feature for feature, record in CAPABILITY_CONTRACT.items()
        if record["status"] in {"implemented", "partially_implemented"}
    )


def capability_record(feature: str) -> dict | None:
    """The contract row for `feature`, or None for an unknown name."""
    return CAPABILITY_CONTRACT.get(str(feature).strip().lower())


def dependency_staleness(metadata: dict | None) -> str | None:
    """Unified dependency-freshness check over a cached analyzer's metadata.

    A build that consumes another analyzer's output records the dependency
    contracts it was built under (``metadata["dependencies"]``). This is the
    single rule both read paths (AnalysisStore and the query surface) apply:
    if any recorded dependency contract no longer matches the current
    registry, the cache is stale no matter how fresh its own version and
    source hash are -- the I1-A/I1-B cfg:c lesson, applied once instead of
    once per reader.
    """
    for analyzer, contract in sorted(((metadata or {}).get("dependencies") or {}).items()):
        version = ANALYZER_VERSIONS.get(analyzer)
        if version is None:
            return f"dependency_unregistered:{analyzer}"
        feature, _, language = analyzer.partition(":")
        if analyzer_contract(feature, language, version) != contract:
            return f"dependency_changed:{analyzer}"
    return None


def _pk_columns(connection, table: str) -> list[str]:
    """The primary-key column names of `table`, in PK order (empty if absent)."""
    info = connection.execute(f"PRAGMA table_info({table})").fetchall()
    if not info:
        return []
    return [
        row[1] for row in sorted(info, key=lambda r: r[5])
        if row[5] > 0
    ]


def _migrate_pk_schema(connection) -> None:
    """Migrate pre-composite-PK analysis tables to the (id, feature) shape.

    Old databases (created before I2A) carry ``analysis_node_id TEXT PRIMARY
    KEY`` -- a single-column PK that silently rejects the second analyzer's
    write of a shared event id. The migration recreates the tables with the
    composite PK and copies every row across; the data is a rebuildable
    cache, so the copy preserves rows rather than destroying them. Tables
    that don't exist yet are left for ``executescript(ANALYSIS_SCHEMA)``.
    """
    for table, id_column in (("analysis_nodes", "analysis_node_id"),
                             ("analysis_edges", "analysis_edge_id")):
        pk = _pk_columns(connection, table)
        if not pk or pk == [id_column, "feature"]:
            continue
        # old single-column PK detected: migrate in place
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        col_list = ", ".join(columns)
        connection.execute(f"ALTER TABLE {table} RENAME TO {table}_old_pk")
        connection.execute(
            f"CREATE TABLE {table} (\n"
            f"    {id_column} TEXT NOT NULL, file_id TEXT NOT NULL,\n"
            f"    feature TEXT NOT NULL, "
            + ("kind TEXT NOT NULL, payload TEXT NOT NULL,\n"
               if table == "analysis_nodes" else
               "src_id TEXT NOT NULL, dst_id TEXT NOT NULL,\n"
               "kind TEXT NOT NULL, payload TEXT NOT NULL,\n")
            + f"    PRIMARY KEY({id_column}, feature)\n"
            f")"
        )
        connection.execute(
            f"INSERT OR IGNORE INTO {table} ({col_list}) "
            f"SELECT {col_list} FROM {table}_old_pk"
        )
        connection.execute(f"DROP TABLE {table}_old_pk")
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_file_feature "
            f"ON {table}(file_id, feature)"
        )
    connection.commit()


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
        _migrate_pk_schema(storage.connection)
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
            and dependency_staleness(state["metadata"]) is None
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
        dependency_reason = dependency_staleness(state["metadata"])
        if dependency_reason is not None:
            reasons.append(dependency_reason)
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
