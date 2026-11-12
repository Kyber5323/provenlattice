from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import Edge, Metrics, Node, RawReference, Shard


SCHEMA = """
PRAGMA foreign_keys = OFF;
CREATE TABLE IF NOT EXISTS repositories (
    repo_id TEXT PRIMARY KEY, root_path TEXT NOT NULL, name TEXT NOT NULL,
    current_generation INTEGER NOT NULL, metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    file_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, shard_id TEXT NOT NULL,
    path TEXT NOT NULL, language TEXT NOT NULL, source_hash TEXT NOT NULL,
    generation INTEGER NOT NULL, UNIQUE(repo_id, path)
);
CREATE TABLE IF NOT EXISTS shards (
    shard_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, path TEXT NOT NULL,
    nodes_count INTEGER NOT NULL, edges_count INTEGER NOT NULL,
    public_symbols TEXT NOT NULL, api_fingerprint TEXT NOT NULL,
    generation INTEGER NOT NULL, boundary_dirty INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, repo_id TEXT NOT NULL,
    shard_id TEXT, file_id TEXT, name TEXT NOT NULL, qualified_name TEXT NOT NULL,
    language TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
    source_hash TEXT, generation INTEGER NOT NULL, metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY, src_id TEXT NOT NULL, dst_id TEXT NOT NULL,
    type TEXT NOT NULL, provenance TEXT NOT NULL, confidence REAL NOT NULL,
    generation INTEGER NOT NULL, metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shard_edges (
    edge_id TEXT PRIMARY KEY, src_shard_id TEXT NOT NULL,
    dst_shard_id TEXT NOT NULL, edge_type TEXT NOT NULL,
    raw_reference_id TEXT
);
CREATE TABLE IF NOT EXISTS raw_references (
    id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, file_id TEXT NOT NULL,
    owner_symbol_id TEXT NOT NULL, raw_name TEXT NOT NULL,
    reference_type TEXT NOT NULL, target_module TEXT,
    start_line INTEGER, end_line INTEGER,
    status TEXT NOT NULL CHECK(status IN ('resolved', 'ambiguous', 'unresolved')),
    candidate_symbols TEXT NOT NULL, resolved_symbol_id TEXT,
    resolution_strategy TEXT NOT NULL, provenance TEXT NOT NULL,
    confidence REAL NOT NULL, generation INTEGER NOT NULL, metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS raw_evidence_links (
    id TEXT PRIMARY KEY, repository_id TEXT NOT NULL,
    source_node_id TEXT NOT NULL, raw_anchor TEXT NOT NULL,
    anchor_type TEXT NOT NULL, candidate_targets TEXT NOT NULL,
    resolved_target_id TEXT,
    resolution_status TEXT NOT NULL CHECK(resolution_status IN ('resolved','ambiguous','unresolved')),
    resolution_strategy TEXT NOT NULL, provenance TEXT NOT NULL,
    confidence REAL NOT NULL, generation INTEGER NOT NULL, metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_state (
    document_id TEXT PRIMARY KEY, repository_id TEXT NOT NULL,
    path TEXT NOT NULL, content_hash TEXT NOT NULL,
    document_version INTEGER NOT NULL, generation INTEGER NOT NULL,
    UNIQUE(repository_id, path)
);
CREATE TABLE IF NOT EXISTS file_state (
    file_id TEXT PRIMARY KEY, path TEXT NOT NULL, source_hash TEXT NOT NULL,
    generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS graph_generation (
    repo_id TEXT NOT NULL, generation INTEGER NOT NULL, created_at TEXT NOT NULL,
    mode TEXT NOT NULL, metrics TEXT NOT NULL, PRIMARY KEY(repo_id, generation)
);
CREATE INDEX IF NOT EXISTS idx_nodes_qualified_name ON nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_nodes_file_id ON nodes(file_id);
CREATE INDEX IF NOT EXISTS idx_nodes_shard_id ON nodes(shard_id);
CREATE INDEX IF NOT EXISTS idx_edges_src_id ON edges(src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst_id ON edges(dst_id);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);
CREATE INDEX IF NOT EXISTS idx_shard_edges_src ON shard_edges(src_shard_id);
CREATE INDEX IF NOT EXISTS idx_shard_edges_dst ON shard_edges(dst_shard_id);
CREATE INDEX IF NOT EXISTS idx_shard_edges_raw_reference ON shard_edges(raw_reference_id);
CREATE INDEX IF NOT EXISTS idx_files_shard_id ON files(shard_id);
CREATE INDEX IF NOT EXISTS idx_raw_references_name ON raw_references(raw_name);
CREATE INDEX IF NOT EXISTS idx_raw_references_file ON raw_references(file_id);
CREATE INDEX IF NOT EXISTS idx_raw_references_owner ON raw_references(owner_symbol_id);
CREATE INDEX IF NOT EXISTS idx_raw_references_resolved ON raw_references(resolved_symbol_id);
CREATE INDEX IF NOT EXISTS idx_evidence_source ON raw_evidence_links(source_node_id);
CREATE INDEX IF NOT EXISTS idx_evidence_anchor ON raw_evidence_links(raw_anchor);
CREATE INDEX IF NOT EXISTS idx_evidence_resolved ON raw_evidence_links(resolved_target_id);
CREATE INDEX IF NOT EXISTS idx_document_state_path ON document_state(repository_id, path);
"""


class SQLiteStorage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        # Set when a component (the analysis store) writes inside a transaction
        # it expects this object to close. Committing on close turns a scope of
        # per-file publishes into one durable transaction -- see
        # AnalysisStore.__init__ for the measurement behind it.
        self._commit_on_close = False
        self.connection.executescript(SCHEMA)
        # V0.3 introduced the reverse boundary index. Backfill snapshots created
        # by V0.2 once, before they are frozen as an Overlay base.
        self.connection.execute(
            """INSERT OR IGNORE INTO shard_edges
               SELECT id, json_extract(metadata, '$.src_shard_id'),
                      json_extract(metadata, '$.dst_shard_id'), type,
                      json_extract(metadata, '$.raw_reference_id')
               FROM edges
               WHERE json_extract(metadata, '$.scope')='boundary'
                 AND json_extract(metadata, '$.src_shard_id') IS NOT NULL
                 AND json_extract(metadata, '$.dst_shard_id') IS NOT NULL"""
        )
        self.connection.commit()

    def close(self) -> None:
        if self._commit_on_close:
            self._commit_on_close = False
            self.connection.commit()
        self.connection.close()

    def __enter__(self) -> "SQLiteStorage":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is not None:
            # A failure anywhere in the scope discards the whole scope rather
            # than publishing the files it had already reached: the analysis
            # freshness contract is per file, but a partially applied scope is
            # still a state nobody asked for.
            self._commit_on_close = False
            try:
                self.connection.rollback()
            finally:
                self.connection.close()
            return
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    @property
    def database_size(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    def current_generation(self, repo_id: str) -> int:
        row = self.connection.execute(
            "SELECT current_generation FROM repositories WHERE repo_id = ?", (repo_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def repository(self) -> dict | None:
        row = self.connection.execute("SELECT * FROM repositories LIMIT 1").fetchone()
        return dict(row) if row else None

    def file_states(self, repo_id: str) -> dict[str, dict]:
        rows = self.connection.execute(
            "SELECT file_id, path, source_hash, generation FROM file_state"
        ).fetchall()
        return {row["path"]: dict(row) for row in rows}

    def parser_cache(self, repo_id: str) -> dict[str, dict]:
        rows = self.connection.execute(
            "SELECT qualified_name, metadata FROM nodes WHERE repo_id = ? AND kind = 'File'",
            (repo_id,),
        ).fetchall()
        result: dict[str, dict] = {}
        for row in rows:
            metadata = json.loads(row["metadata"])
            result[metadata["relative_path"]] = metadata.get("parsed", {})
        return result

    def snapshot_ids(self, table: str) -> dict[str, dict]:
        if table not in {"nodes", "edges"}:
            raise ValueError("unsupported snapshot table")
        return {row["id"]: dict(row) for row in self.connection.execute(f"SELECT * FROM {table}")}

    def shard_fingerprints(self) -> dict[str, str]:
        return {
            row["shard_id"]: row["api_fingerprint"]
            for row in self.connection.execute("SELECT shard_id, api_fingerprint FROM shards")
        }

    def raw_reference_cache(self, repo_id: str) -> dict[str, dict]:
        return {
            row["id"]: dict(row)
            for row in self.connection.execute(
                "SELECT * FROM raw_references WHERE repo_id = ?", (repo_id,)
            )
        }

    def references_for_names(self, names: set[str]) -> set[str]:
        if not names:
            return set()
        short_names = {name.rsplit(".", 1)[-1] for name in names}
        values = sorted(names | short_names)
        placeholders = ",".join("?" for _ in values)
        rows = self.connection.execute(
            f"SELECT id FROM raw_references WHERE raw_name IN ({placeholders})",
            tuple(values),
        )
        return {row["id"] for row in rows}

    def replace_snapshot(
        self,
        *,
        repo_id: str,
        root: Path,
        generation: int,
        mode: str,
        files: list[dict],
        nodes: list[Node],
        edges: list[Edge],
        shards: list[Shard],
        raw_references: list[RawReference],
        metrics: Metrics,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM edges WHERE COALESCE(json_extract(metadata, '$.layer'), 'code') != 'knowledge'"
            )
            connection.execute("DELETE FROM shard_edges")
            connection.execute("DELETE FROM raw_references")
            connection.execute(
                "DELETE FROM nodes WHERE COALESCE(json_extract(metadata, '$.layer'), 'code') != 'knowledge'"
            )
            connection.execute("DELETE FROM shards")
            connection.execute("DELETE FROM files")
            connection.execute("DELETE FROM file_state")
            connection.execute(
                """INSERT INTO repositories(repo_id, root_path, name, current_generation, metadata)
                   VALUES(?, ?, ?, ?, '{}')
                   ON CONFLICT(repo_id) DO UPDATE SET root_path=excluded.root_path,
                   name=excluded.name, current_generation=excluded.current_generation""",
                (repo_id, str(root), root.name, generation),
            )
            connection.executemany(
                "INSERT INTO files VALUES(?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        item["file_id"], repo_id, item["shard_id"], item["path"],
                        item["language"], item["source_hash"], generation,
                    )
                    for item in files
                ],
            )
            connection.executemany(
                "INSERT INTO file_state VALUES(?, ?, ?, ?)",
                [(item["file_id"], item["path"], item["source_hash"], generation) for item in files],
            )
            connection.executemany(
                "INSERT INTO nodes VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        node.id, node.kind, node.repo_id, node.shard_id, node.file_id,
                        node.name, node.qualified_name, node.language, node.start_line,
                        node.end_line, node.signature, node.source_hash, node.generation,
                        json.dumps(node.metadata, sort_keys=True),
                    )
                    for node in nodes
                ],
            )
            connection.executemany(
                "INSERT INTO edges VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        edge.id, edge.src_id, edge.dst_id, edge.type, edge.provenance,
                        edge.confidence, edge.generation, json.dumps(edge.metadata, sort_keys=True),
                    )
                    for edge in edges
                ],
            )
            connection.executemany(
                "INSERT INTO shard_edges VALUES(?, ?, ?, ?, ?)",
                [
                    (
                        edge.id, edge.metadata["src_shard_id"], edge.metadata["dst_shard_id"],
                        edge.type, edge.metadata.get("raw_reference_id"),
                    )
                    for edge in edges
                    if edge.metadata.get("scope") == "boundary"
                    and edge.metadata.get("src_shard_id")
                    and edge.metadata.get("dst_shard_id")
                ],
            )
            connection.executemany(
                "INSERT INTO raw_references VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        reference.id, reference.repo_id, reference.file_id,
                        reference.owner_symbol_id, reference.raw_name,
                        reference.reference_type, reference.target_module,
                        reference.start_line, reference.end_line, reference.status,
                        json.dumps(reference.candidate_symbols), reference.resolved_symbol_id,
                        reference.resolution_strategy, reference.provenance,
                        reference.confidence, reference.generation,
                        json.dumps(reference.metadata, sort_keys=True),
                    )
                    for reference in raw_references
                ],
            )
            connection.executemany(
                "INSERT INTO shards VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        shard.shard_id, shard.repo_id, shard.path, shard.nodes_count,
                        shard.edges_count, json.dumps(shard.public_symbols),
                        shard.api_fingerprint, shard.generation, int(shard.boundary_dirty),
                    )
                    for shard in shards
                ],
            )
            connection.execute(
                "INSERT INTO graph_generation VALUES(?, ?, datetime('now'), ?, ?)",
                (repo_id, generation, mode, json.dumps(metrics.to_dict(), sort_keys=True)),
            )

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def row(self, sql: str, params: tuple = ()) -> dict | None:
        result = self.connection.execute(sql, params).fetchone()
        return dict(result) if result else None


def decode_row(row: dict) -> dict:
    result = dict(row)
    for key in ("metadata", "public_symbols", "metrics", "candidate_symbols",
                "candidate_targets"):
        if key in result and isinstance(result[key], str):
            result[key] = json.loads(result[key])
    return result
