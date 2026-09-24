from __future__ import annotations

import json
import sqlite3
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from .models import Edge, Metrics, Node, RawReference, Shard
from .semantics.events import SemanticEdge, SemanticEvent


# SQLite caps host parameters (SQLITE_MAX_VARIABLE_NUMBER = 32,766). An id set
# larger than that cannot be written as an `IN (?, ?, ...)` list at all, which
# is reachable the moment a hub symbol (a base class, a widely imported utility)
# is traversed on a real repository. Below the threshold we keep binding plain
# placeholders: it stays on the index and costs nothing to read.
ID_BIND_LIMIT = 900


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
-- The semantic event layer lives here, NOT in `nodes`/`edges`. At million-line
-- scale it extrapolates to 0.25-1.0 GB -- the same order as `raw_references`
-- and far larger than the code graph itself -- so it cannot go into the hot
-- graph (appendix B.2, gap 4). Stage 3 added `semantic_edges` (the sparse CFG
-- over these events). Stage 4's defect candidates are a pure function of the
-- two and are deliberately not stored here: these tables are replaced as a
-- snapshot, so a `semantic_evidence` table would have to be rebuilt on every
-- semantic change and has no reader yet. It lands with the analysis copy
-- (stage 7), keyed by `candidate_id`.
CREATE TABLE IF NOT EXISTS semantic_events (
    id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, file_id TEXT NOT NULL,
    owner_symbol_id TEXT NOT NULL, event_type TEXT NOT NULL,
    ordinal INTEGER NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
    matched_name TEXT NOT NULL, matched_via TEXT NOT NULL,
    flags TEXT NOT NULL, generation INTEGER NOT NULL, metadata TEXT NOT NULL
);
-- The sparse CFG's CONTROL_REACHES edges (stage 3). `dst_event_id` may be a
-- synthetic exit id (`identity.cfg_exit_id`) that has no `semantic_events`
-- row -- consumers that need the exit as a node materialise it from here.
-- There is deliberately no ordinal column: the sorted flag set is the id
-- discriminator, and equal flag sets between the same pair are the same
-- reachability fact. The relation column is generic because the DFG
-- relations of stage 5 land in this same table.
CREATE TABLE IF NOT EXISTS semantic_edges (
    id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, file_id TEXT NOT NULL,
    owner_symbol_id TEXT NOT NULL, src_event_id TEXT NOT NULL,
    dst_event_id TEXT NOT NULL, relation TEXT NOT NULL,
    flags TEXT NOT NULL, generation INTEGER NOT NULL, metadata TEXT NOT NULL
);
-- The second fingerprint from appendix B.2.2. `api_fingerprint` only covers
-- public signatures, so a pure function-body edit leaves it unchanged and no
-- cross-shard recomputation is triggered -- correct for a code graph, fatal
-- for a semantic one, where every event lives in a body. This is the minimal
-- viable form: any source change inside the shard marks it semantically dirty.
CREATE TABLE IF NOT EXISTS semantic_fingerprint (
    shard_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL, generation INTEGER NOT NULL, metadata TEXT NOT NULL
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
-- `layer` lives in the metadata JSON but is queried as a column. Without these
-- the planner scans the whole table, and for nodes that means reading a
-- metadata blob which is ~95% of the table's bytes.
--
-- Two spellings are in use and they need separate indexes: the bare extract
-- (used by the knowledge layer), and the COALESCE that defaults missing keys
-- to 'code'. Whitespace inside the expression does not matter -- the planner
-- matches on the parsed expression, so `json_extract(metadata,'$.layer')` and
-- `json_extract(metadata, '$.layer')` share one index.
--
-- An equality predicate gets a SEARCH. The `!=` predicate used by
-- replace_snapshot cannot, and is deliberately left as a scan: it removes
-- every non-knowledge row, so it is anti-selective and one sequential pass
-- beats materialising rowids and looking each one up. The COALESCE index
-- therefore earns its keep on the COUNT(*) queries, where the index alone
-- answers the query and the wide metadata column is never touched.
CREATE INDEX IF NOT EXISTS idx_nodes_layer ON nodes(json_extract(metadata, '$.layer'));
CREATE INDEX IF NOT EXISTS idx_nodes_layer_coalesced
    ON nodes(COALESCE(json_extract(metadata, '$.layer'), 'code'));
CREATE INDEX IF NOT EXISTS idx_edges_layer ON edges(json_extract(metadata, '$.layer'));
CREATE INDEX IF NOT EXISTS idx_edges_layer_coalesced
    ON edges(COALESCE(json_extract(metadata, '$.layer'), 'code'));
-- The queries the event layer actually runs: every event of one method (the
-- lifetime and lock-order queries start here), every event of one file (the
-- incremental delta), and every event of one type across the repository.
CREATE INDEX IF NOT EXISTS idx_semantic_events_owner ON semantic_events(owner_symbol_id);
CREATE INDEX IF NOT EXISTS idx_semantic_events_file ON semantic_events(file_id);
CREATE INDEX IF NOT EXISTS idx_semantic_events_type ON semantic_events(event_type);
-- The edge queries mirror the event ones: three-state traversal reads both
-- directions, the pattern queries start from "every edge of one method", the
-- incremental delta reads one file, and the DFG relations of stage 5 will
-- filter on relation.
CREATE INDEX IF NOT EXISTS idx_semantic_edges_src ON semantic_edges(src_event_id);
CREATE INDEX IF NOT EXISTS idx_semantic_edges_dst ON semantic_edges(dst_event_id);
CREATE INDEX IF NOT EXISTS idx_semantic_edges_owner ON semantic_edges(owner_symbol_id);
CREATE INDEX IF NOT EXISTS idx_semantic_edges_file ON semantic_edges(file_id);
CREATE INDEX IF NOT EXISTS idx_semantic_edges_relation ON semantic_edges(relation);
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
        self._id_filter_seq = 0
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
        self.connection.close()

    def __enter__(self) -> "SQLiteStorage":
        return self

    def __exit__(self, *args: object) -> None:
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
        # file_state carries no repo_id of its own; scope it through files so a
        # database holding more than one repository cannot leak states across.
        rows = self.connection.execute(
            """SELECT state.file_id, state.path, state.source_hash, state.generation
               FROM file_state AS state
               JOIN files ON files.file_id = state.file_id
               WHERE files.repo_id = ?""",
            (repo_id,),
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

    def snapshot_columns(self, table: str, columns: tuple[str, ...]) -> dict[str, dict]:
        """Slim cousin of `snapshot_ids`: only the named columns come back.

        `nodes.metadata.parsed` is 94.8% of the table (appendix B.2.3), and an
        incremental update compares ids, lines, hashes and symbol metadata --
        never the parsed blobs. Reading the fat column here and throwing it
        away is the single largest over-read of the update path (P1).
        """
        if table not in {"nodes", "edges"}:
            raise ValueError("unsupported snapshot table")
        quoted = ", ".join(columns)
        return {
            row["id"]: dict(row)
            for row in self.connection.execute(f"SELECT {quoted} FROM {table}")
        }

    def nodes_metadata(self) -> dict[str, str]:
        """`id -> metadata json string` for every non-File node.

        File nodes are excluded on purpose: their metadata is dominated by the
        `parsed` cache, which changes iff the source bytes change, which
        `source_hash` already reports. The updated-node comparison in
        incremental.py can therefore treat File nodes by hash alone.
        """
        return {
            row["id"]: row["metadata"]
            for row in self.connection.execute(
                "SELECT id, metadata FROM nodes WHERE kind != 'File'"
            )
        }

    def nodes_for_files(self, file_ids: set[str]) -> list[dict]:
        """Slim rows of every node defined in the changed files, for the
        symbol-diff step: anything outside these files is irrelevant to it."""
        if not file_ids:
            return []
        with self.id_filter(file_ids) as (sql, params):
            rows = self.connection.execute(
                f"SELECT id, file_id, shard_id, kind, name, qualified_name "
                f"FROM nodes WHERE file_id IN ({sql})",
                tuple(params),
            )
        return [dict(row) for row in rows]

    def boundary_edges(self) -> dict[str, dict]:
        """Only cross-shard edges. The impact frontier (impact.py) reads
        nothing else from the old edge set, and the delta counts need only
        old edge ids, so the full old-edge dump has no remaining reader."""
        return {
            row["id"]: dict(row)
            for row in self.connection.execute(
                "SELECT id, src_id, dst_id, metadata FROM edges "
                "WHERE json_extract(metadata, '$.scope') = 'boundary'"
            )
        }

    def shard_fingerprints(self) -> dict[str, str]:
        return {
            row["shard_id"]: row["api_fingerprint"]
            for row in self.connection.execute("SELECT shard_id, api_fingerprint FROM shards")
        }

    def shard_semantic_fingerprints(self) -> dict[str, str]:
        return {
            row["shard_id"]: row["fingerprint"]
            for row in self.connection.execute(
                "SELECT shard_id, fingerprint FROM semantic_fingerprint"
            )
        }

    def raw_reference_cache(
        self,
        repo_id: str,
        *,
        exclude_file_ids: set[str] | None = None,
        exclude_ids: set[str] | None = None,
    ) -> dict[str, dict]:
        """Old reference records, minus the ones no reuse path can reach.

        The resolver reuses a cached record only when its file was not changed
        and its id is not in the affected set (resolver._reuse_record); every
        other record is re-derived from the re-parsed file. Loading the
        unreusable ones is pure waste, and on this table -- the largest in the
        database -- it is the waste that dominates toggle-time memory (P1).
        """
        exclude_file_ids = exclude_file_ids or set()
        exclude_ids = exclude_ids or set()
        if not exclude_file_ids and not exclude_ids:
            return {
                row["id"]: dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM raw_references WHERE repo_id = ?", (repo_id,)
                )
            }
        pieces = ["repo_id = ?"]
        params: list[object] = [repo_id]
        # The two exclusions may each be large enough to need their own temp
        # table; id_filter nests exactly one level per table so an ExitStack
        # keeps both alive until the outer query has actually run.
        with ExitStack() as stack:
            if exclude_file_ids:
                sql, binds = stack.enter_context(self.id_filter(exclude_file_ids))
                pieces.append(f"file_id NOT IN ({sql})")
                params.extend(binds)
            if exclude_ids:
                sql, binds = stack.enter_context(self.id_filter(exclude_ids))
                pieces.append(f"id NOT IN ({sql})")
                params.extend(binds)
            rows = self.connection.execute(
                "SELECT * FROM raw_references WHERE " + " AND ".join(pieces),
                tuple(params),
            )
        return {row["id"]: dict(row) for row in rows}

    def references_for_names(self, names: set[str]) -> set[str]:
        if not names:
            return set()
        short_names = {name.rsplit(".", 1)[-1] for name in names}
        values = sorted(names | short_names)
        # A mass rename can list more names than the host-parameter limit
        # allows (32,766); id_filter materialises large sets into a temp
        # table so the binding count no longer depends on the rename's size.
        with self.id_filter(values) as (sql, params):
            rows = self.connection.execute(
                f"SELECT id FROM raw_references WHERE raw_name IN ({sql})",
                tuple(params),
            )
        return {row["id"] for row in rows}

    def semantic_events(self, repo_id: str) -> list[SemanticEvent]:
        """Every persisted event for a repository, ordered by id.

        Read back on an incremental update to carry forward the events of files
        that were not re-parsed: the snapshot replace clears the table, and
        those files produce nothing to put back.
        """
        rows = self.connection.execute(
            "SELECT * FROM semantic_events WHERE repo_id=? ORDER BY id", (repo_id,)
        ).fetchall()
        return [
            SemanticEvent(
                event_id=row["id"], event_type=row["event_type"],
                owner_symbol_id=row["owner_symbol_id"], file_id=row["file_id"],
                ordinal=row["ordinal"], start_line=row["start_line"],
                end_line=row["end_line"], matched_name=row["matched_name"],
                matched_via=row["matched_via"],
                flags=tuple(json.loads(row["flags"])),
                metadata=json.loads(row["metadata"]),
            )
            for row in rows
        ]

    def semantic_edges(self, repo_id: str) -> list[SemanticEdge]:
        """Every persisted semantic edge for a repository, ordered by id.

        The table has been relation-generic since stage 3; stage 5B began
        publishing DATA_FLOW_TO and CONSUMES rows into it alongside
        CONTROL_REACHES. Read back on an incremental update for the same
        reason as `semantic_events`: the snapshot replace clears the table,
        and the files that were not re-parsed produce nothing to put back.
        """
        rows = self.connection.execute(
            "SELECT * FROM semantic_edges WHERE repo_id=? ORDER BY id", (repo_id,)
        ).fetchall()
        return [
            SemanticEdge(
                edge_id=row["id"], src_event_id=row["src_event_id"],
                dst_event_id=row["dst_event_id"], relation=row["relation"],
                owner_symbol_id=row["owner_symbol_id"], file_id=row["file_id"],
                flags=tuple(json.loads(row["flags"])),
                metadata=json.loads(row["metadata"]),
            )
            for row in rows
        ]

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
        semantic_events: list[SemanticEvent] | None = None,
        semantic_edges: list[SemanticEdge] | None = None,
        semantic_fingerprints: list[dict] | None = None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM edges WHERE COALESCE(json_extract(metadata, '$.layer'), 'code') != 'knowledge'"
            )
            connection.execute("DELETE FROM shard_edges")
            connection.execute("DELETE FROM raw_references")
            connection.execute("DELETE FROM semantic_events")
            connection.execute("DELETE FROM semantic_edges")
            connection.execute("DELETE FROM semantic_fingerprint")
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
            if semantic_events:
                connection.executemany(
                    "INSERT OR REPLACE INTO semantic_events VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            event.event_id, repo_id, event.file_id, event.owner_symbol_id,
                            event.event_type, event.ordinal, event.start_line, event.end_line,
                            event.matched_name, event.matched_via,
                            json.dumps(list(event.flags), sort_keys=True), generation,
                            json.dumps(event.metadata, sort_keys=True),
                        )
                        for event in semantic_events
                    ],
                )
            if semantic_edges:
                connection.executemany(
                    "INSERT OR REPLACE INTO semantic_edges VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            edge.edge_id, repo_id, edge.file_id, edge.owner_symbol_id,
                            edge.src_event_id, edge.dst_event_id, edge.relation,
                            # `sort_keys` sorts dict keys, not list elements -- the
                            # sorted flag set is the edge id's discriminator, so it
                            # is sorted here explicitly.
                            json.dumps(sorted(edge.flags)), generation,
                            json.dumps(edge.metadata, sort_keys=True),
                        )
                        for edge in semantic_edges
                    ],
                )
            if semantic_fingerprints:
                connection.executemany(
                    "INSERT OR REPLACE INTO semantic_fingerprint VALUES(?, ?, ?, ?, ?)",
                    [
                        (
                            item["shard_id"], repo_id, item["fingerprint"], generation,
                            json.dumps(item.get("metadata", {}), sort_keys=True),
                        )
                        for item in semantic_fingerprints
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

    @contextmanager
    def id_filter(self, values: Iterable[str]) -> Iterator[tuple[str, tuple]]:
        """Yield an `(sql, params)` pair usable inside `IN (...)`, for any id count.

        Small sets bind one placeholder per id. Large sets are materialised into
        a temp table and read back as a subquery, so the number of host
        parameters no longer depends on the caller's set size. Both forms are
        evaluated as a membership test, so results are identical; only the
        evaluation plan differs.
        """
        ordered = sorted(set(values))
        if len(ordered) <= ID_BIND_LIMIT:
            yield ",".join("?" for _ in ordered), tuple(ordered)
            return
        # A fresh name per call so that nesting two filters cannot make the
        # inner DROP pull the table out from under the outer query.
        self._id_filter_seq += 1
        table = f"temp._id_filter_{self._id_filter_seq}"
        self.connection.execute(f"DROP TABLE IF EXISTS {table}")
        self.connection.execute(f"CREATE TEMP TABLE _id_filter_{self._id_filter_seq}"
                                "(id TEXT PRIMARY KEY)")
        try:
            self.connection.executemany(
                f"INSERT OR IGNORE INTO {table}(id) VALUES(?)",
                ((value,) for value in ordered),
            )
            yield f"SELECT id FROM {table}", ()
        finally:
            self.connection.execute(f"DROP TABLE IF EXISTS {table}")

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
