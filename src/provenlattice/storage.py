from __future__ import annotations

import json
import shutil
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
-- `raw_references` is NOT here: it is the cold layer (stage 7A). It lives in a
-- sibling database file (COLD_SCHEMA below) that this class ATTACHes as `cold`
-- and re-exposes to readers through a TEMP VIEW of the same name, so every
-- existing `SELECT ... FROM raw_references` keeps working unchanged while the
-- main file -- the one every query opens -- carries only hot graph facts. A
-- database that still has the real table (anything indexed before stage 7A,
-- the frozen 5B snapshots included) stays on the legacy layout and is not
-- touched.
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


# The cold layer (stage 7A). Same DDL as the legacy in-main `raw_references`
# so rows move between the two layouts verbatim, but every statement is
# schema-qualified for the ATTACHed `cold` database. `id_filter`'s temp tables
# live in the TEMP schema and are therefore shared by both.
COLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS cold.raw_references (
    id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, file_id TEXT NOT NULL,
    owner_symbol_id TEXT NOT NULL, raw_name TEXT NOT NULL,
    reference_type TEXT NOT NULL, target_module TEXT,
    start_line INTEGER, end_line INTEGER,
    status TEXT NOT NULL CHECK(status IN ('resolved', 'ambiguous', 'unresolved')),
    candidate_symbols TEXT NOT NULL, resolved_symbol_id TEXT,
    resolution_strategy TEXT NOT NULL, provenance TEXT NOT NULL,
    confidence REAL NOT NULL, generation INTEGER NOT NULL, metadata TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cold.idx_raw_references_name ON raw_references(raw_name);
CREATE INDEX IF NOT EXISTS cold.idx_raw_references_file ON raw_references(file_id);
CREATE INDEX IF NOT EXISTS cold.idx_raw_references_owner ON raw_references(owner_symbol_id);
CREATE INDEX IF NOT EXISTS cold.idx_raw_references_resolved ON raw_references(resolved_symbol_id);

-- The parse cache (stage 7A-3). It is a cache: `parsed` is a pure function of
-- the file's bytes, which `files.source_hash` already reports, and the only
-- reader is the update path, which needs the whole thing or none of it. It was
-- `nodes.metadata.parsed` until the split, where it was 94.8% of the nodes
-- table and of no use to any query.
CREATE TABLE IF NOT EXISTS cold.parse_cache (
    file_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, relative_path TEXT NOT NULL,
    payload TEXT NOT NULL, generation INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS cold.idx_parse_cache_repo ON parse_cache(repo_id);
"""


def cold_path_for(database: str | Path) -> Path:
    """The cold store that belongs to a main database path.

    A fixed suffix rather than a directory: the two files travel together
    (`copy_database`), `*.db` is already ignored by git, and a database renamed
    or moved by hand keeps its partner discoverable by the same rule.
    """
    database = Path(database).resolve()
    return database.with_name(database.stem + ".cold.db")


def copy_database(source: str | Path, target: str | Path) -> list[Path]:
    """Copy a database, cold store included, to `target`; return what was copied.

    Every copy of a database has to go through here: a split layout whose cold
    half was left behind reads as an empty reference layer, and the overlay
    path (`apply_repository_overlay`) copies the base snapshot before every
    incremental apply.
    """
    source, target = Path(source).resolve(), Path(target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    copied = [target]
    source_cold = cold_path_for(source)
    if source_cold.exists():
        target_cold = cold_path_for(target)
        shutil.copy2(source_cold, target_cold)
        copied.append(target_cold)
    return copied


class SQLiteStorage:
    def __init__(self, path: str | Path, *, cold: str | Path | None = None) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._id_filter_seq = 0
        self.cold_path = Path(cold).resolve() if cold is not None else cold_path_for(self.path)
        self.connection.executescript(SCHEMA)
        self._open_reference_layer()
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

    def _open_reference_layer(self) -> None:
        """Attach the cold store, or stay on the legacy in-main table.

        Legacy (a database indexed before stage 7A) still has the real
        `raw_references` table: nothing is attached and nothing is created, so
        a frozen snapshot reads exactly as it did before the split. A split
        database attaches its cold file and re-exposes it as a TEMP VIEW under
        the old name -- TEMP, not stored, because a stored view's `cold.`
        prefix only resolves on a connection that attached, and a reader
        without the cold store should fail loudly on a missing table rather
        than confusingly on a missing schema.
        """
        row = self.connection.execute(
            "SELECT type FROM sqlite_master WHERE name = 'raw_references'"
        ).fetchone()
        if row is not None and row["type"] == "table":
            self._reference_schema = "main"
            return
        if not self.cold_path.exists():
            # ATTACH would create the file silently, and every read afterwards
            # would answer "no references" instead of failing. Only a database
            # that is being built may bring its cold store into existence.
            populated = self.connection.execute("SELECT COUNT(*) FROM repositories").fetchone()[0]
            if populated:
                # Close before raising: an abandoned connection keeps a
                # Windows lock on the main file the caller may want to move.
                self.connection.close()
                raise RuntimeError(
                    f"cold store missing for {self.path}: expected {self.cold_path}"
                )
        self.connection.execute("ATTACH DATABASE ? AS cold", (str(self.cold_path),))
        self.connection.executescript(COLD_SCHEMA)
        self.connection.execute(
            "CREATE TEMP VIEW raw_references AS SELECT * FROM cold.raw_references"
        )
        self._reference_schema = "cold"

    @property
    def reference_schema(self) -> str:
        return self._reference_schema

    @property
    def reference_table(self) -> str:
        """The writable name of the reference table.

        Reads go through the unqualified name (the TEMP VIEW in a split
        layout, the real table in a legacy one); writes must name the cold
        schema explicitly, because the view is read-only.
        """
        return "cold.raw_references" if self._reference_schema == "cold" else "raw_references"

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
        """The main (hot) file. The cold store is reported separately.

        The two numbers are not comparable with a legacy single-file snapshot:
        the split is precisely the act of moving bytes from this number to
        `cold_database_size`, and the frozen artifacts stay legacy.
        """
        return self.path.stat().st_size if self.path.exists() else 0

    @property
    def cold_database_size(self) -> int:
        """The cold file, or 0 when the references still live in the main one."""
        return self.cold_path.stat().st_size if self.cold_path.exists() else 0

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
        """`relative_path -> parsed payload`, from wherever this database keeps it.

        A split database holds the cache in the cold store, where stage 7A-3
        moved it; a legacy one still holds it in `nodes.metadata.parsed`, where
        every version before that wrote it. The layout marker is the same one
        `reference_table` uses -- a database keeps its cold layer in one place.
        """
        if self._reference_schema == "cold":
            rows = self.connection.execute(
                "SELECT relative_path, payload FROM cold.parse_cache WHERE repo_id = ?",
                (repo_id,),
            )
            return {row["relative_path"]: json.loads(row["payload"]) for row in rows}
        rows = self.connection.execute(
            "SELECT qualified_name, metadata FROM nodes WHERE repo_id = ? AND kind = 'File'",
            (repo_id,),
        ).fetchall()
        result: dict[str, dict] = {}
        for row in rows:
            metadata = json.loads(row["metadata"])
            if "parsed" not in metadata:
                # A File node without its payload is a database whose cache is
                # gone, not a file that parses to nothing. Leaving the path out
                # makes `incremental_update` fail loudly on it; answering `{}`
                # would rebuild that file as an empty graph and call it success.
                continue
            result[metadata["relative_path"]] = metadata["parsed"]
        return result

    def snapshot_ids(self, table: str) -> dict[str, dict]:
        if table not in {"nodes", "edges"}:
            raise ValueError("unsupported snapshot table")
        return {row["id"]: dict(row) for row in self.connection.execute(f"SELECT * FROM {table}")}

    def snapshot_columns(self, table: str, columns: tuple[str, ...]) -> dict[str, dict]:
        """Slim cousin of `snapshot_ids`: only the named columns come back.

        An incremental update compares ids, lines, hashes and symbol metadata
        -- never the parsed blobs, which until stage 7A-3 were 94.8% of the
        nodes table (appendix B.2.3) and are now in the cold store. Reading a
        column here and throwing it away is the largest over-read the update
        path can make.
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

        File nodes are excluded on purpose: what their metadata carried was the
        parse cache, which changes iff the source bytes change, which
        `source_hash` already reports. The updated-node comparison in
        incremental.py can therefore treat File nodes by hash alone. (Stage
        7A-3 moved the cache out of the column; the exclusion stands, now on
        the hash argument alone rather than on the column's size.)
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

    def iter_reference_survivors(
        self,
        repo_id: str,
        *,
        exclude_file_ids: set[str] | None = None,
        exclude_ids: set[str] | None = None,
    ) -> Iterator[sqlite3.Row]:
        """Stream the old reference records a carry path can reach, one row at a time.

        A record is carried only when its file was not changed and its id is
        not in the affected set; every other record is re-derived from the
        re-parsed file. Loading the unreusable ones is pure waste, and on this
        table -- the largest in the database -- materialising the *reusable*
        ones is what made update memory track the table's size: 328,213 rows
        and 475 MB on the llama.cpp corpus, read once and thrown away (stage
        7A-2). The rows are yielded as the cursor produces them and never
        collected.
        """
        exclude_file_ids = exclude_file_ids or set()
        exclude_ids = exclude_ids or set()
        if not exclude_file_ids and not exclude_ids:
            yield from self.connection.execute(
                "SELECT * FROM raw_references WHERE repo_id = ?", (repo_id,)
            )
            return
        pieces = ["repo_id = ?"]
        params: list[object] = [repo_id]
        # The two exclusions may each be large enough to need their own temp
        # table; id_filter nests exactly one level per table so an ExitStack
        # keeps both alive -- across every yield, since the generator holds the
        # frame until the caller stops asking.
        with ExitStack() as stack:
            if exclude_file_ids:
                sql, binds = stack.enter_context(self.id_filter(exclude_file_ids))
                pieces.append(f"file_id NOT IN ({sql})")
                params.extend(binds)
            if exclude_ids:
                sql, binds = stack.enter_context(self.id_filter(exclude_ids))
                pieces.append(f"id NOT IN ({sql})")
                params.extend(binds)
            yield from self.connection.execute(
                "SELECT * FROM raw_references WHERE " + " AND ".join(pieces),
                tuple(params),
            )

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
        reference_delete_file_ids: set[str] | None = None,
        reference_delete_ids: set[str] | None = None,
    ) -> None:
        """Write a whole snapshot, or -- when the two `reference_delete_*` sets
        are given -- everything but the reference rows they do not name.

        A full index passes neither set: the reference layer is replaced
        wholesale, exactly as before stage 7A-2. An update passes the file ids
        it re-parsed and the ids it re-derived; the rows outside both sets were
        never read into memory, are not rewritten here, and keep their content
        verbatim -- only their generation advances. That carry is what makes
        update memory stop tracking the reference table's size.
        """
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM edges WHERE COALESCE(json_extract(metadata, '$.layer'), 'code') != 'knowledge'"
            )
            connection.execute("DELETE FROM shard_edges")
            # The reference layer may live in the ATTACHed cold database; the
            # delete goes through the writable name (the TEMP VIEW is not).
            if reference_delete_file_ids is None and reference_delete_ids is None:
                connection.execute(f"DELETE FROM {self.reference_table}")
            else:
                predicates: list[str] = []
                params: list[object] = [repo_id]
                with ExitStack() as stack:
                    if reference_delete_file_ids:
                        sql, binds = stack.enter_context(
                            self.id_filter(reference_delete_file_ids)
                        )
                        predicates.append(f"file_id IN ({sql})")
                        params.extend(binds)
                    if reference_delete_ids:
                        sql, binds = stack.enter_context(self.id_filter(reference_delete_ids))
                        predicates.append(f"id IN ({sql})")
                        params.extend(binds)
                    if predicates:
                        connection.execute(
                            f"DELETE FROM {self.reference_table} WHERE repo_id = ? AND ("
                            + " OR ".join(predicates) + ")",
                            tuple(params),
                        )
                # The carried rows' content was not recomputed, but their
                # generation says which graph they belong to, and a full rebuild
                # of the same tree stamps every row with the new generation.
                # Advancing them here is what keeps the two snapshots identical
                # (7A-2 gate 2), rather than merely equal in content.
                connection.execute(
                    f"UPDATE {self.reference_table} SET generation = ? "
                    "WHERE repo_id = ? AND generation <> ?",
                    (generation, repo_id, generation),
                )
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
            # On a split layout the parse cache leaves the nodes table here
            # (stage 7A-3). The node object is not touched -- the in-memory
            # File metadata is what `build_graph` and the shard strategies read
            # -- only the row written for it drops the key, and the payload
            # becomes a row of its own in the cold store.
            cold_parse_cache = self._reference_schema == "cold"
            parse_rows: list[tuple] = []
            node_rows: list[tuple] = []
            for node in nodes:
                metadata = node.metadata
                if cold_parse_cache and node.kind == "File" and "parsed" in metadata:
                    parse_rows.append(
                        (
                            node.id, repo_id, metadata["relative_path"],
                            json.dumps(metadata["parsed"], sort_keys=True), generation,
                        )
                    )
                    metadata = {
                        key: value for key, value in metadata.items() if key != "parsed"
                    }
                node_rows.append(
                    (
                        node.id, node.kind, node.repo_id, node.shard_id, node.file_id,
                        node.name, node.qualified_name, node.language, node.start_line,
                        node.end_line, node.signature, node.source_hash, node.generation,
                        json.dumps(metadata, sort_keys=True),
                    )
                )
            connection.executemany(
                "INSERT INTO nodes VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                node_rows,
            )
            if cold_parse_cache:
                # The cache is replaced whole, like the nodes it came from: an
                # update rewrites every file's File node, changed or not, so
                # every payload is in hand and none of the old ones is wanted.
                connection.execute("DELETE FROM cold.parse_cache WHERE repo_id = ?", (repo_id,))
                connection.executemany(
                    "INSERT INTO cold.parse_cache VALUES(?, ?, ?, ?, ?)", parse_rows
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
                f"INSERT INTO {self.reference_table} "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
