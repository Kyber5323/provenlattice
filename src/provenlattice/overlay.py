from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import time
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Callable, Iterable

from .models import OverlayConflict, OverlayDelta, OverlayMetadata
from .incremental import incremental_update
from .shard import ShardStrategy
from .storage import SQLiteStorage


ENTITY_TABLES = {
    "Node": ("nodes", "id"),
    "Edge": ("edges", "id"),
    "RawReference": ("raw_references", "id"),
    "RawEvidenceLink": ("raw_evidence_links", "id"),
    "Shard": ("shards", "shard_id"),
    "BoundaryEdge": ("shard_edges", "edge_id"),
}
OPERATIONS = {"ADD", "UPDATE", "DELETE"}
OVERLAY_TYPES = {"BRANCH", "SESSION"}
CONFLICT_TYPES = {
    "NO_CONFLICT", "ENTITY_CONFLICT", "DELETE_UPDATE_CONFLICT",
    "ADD_ADD_CONFLICT", "BOUNDARY_CONFLICT", "REBASE_REQUIRED",
}


OVERLAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS overlay_metadata (
    overlay_id TEXT PRIMARY KEY, overlay_type TEXT NOT NULL,
    repository_id TEXT NOT NULL, base_commit TEXT NOT NULL,
    base_generation INTEGER NOT NULL, branch_name TEXT,
    parent_overlay_id TEXT, status TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS overlay_deltas (
    overlay_id TEXT NOT NULL, entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL, operation TEXT NOT NULL,
    base_version TEXT, new_value TEXT, generation INTEGER NOT NULL,
    PRIMARY KEY(overlay_id, entity_type, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_overlay_delta_entity
    ON overlay_deltas(overlay_id, entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_overlay_delta_operation
    ON overlay_deltas(overlay_id, operation);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _canonical(row: dict | None) -> str | None:
    if row is None:
        return None
    value = dict(row)
    value.pop("generation", None)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entity_version(row: dict | None) -> str | None:
    canonical = _canonical(row)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest() if canonical is not None else None


class OverlayStore:
    """Small SQLite store containing only graph entity deltas."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(OVERLAY_SCHEMA)

    @classmethod
    def create(
        cls, path: str | Path, *, overlay_type: str, repository_id: str,
        base_commit: str, base_generation: int, branch_name: str | None = None,
        parent_overlay_id: str | None = None, overlay_id: str | None = None,
    ) -> "OverlayStore":
        overlay_type = overlay_type.upper()
        if overlay_type not in OVERLAY_TYPES:
            raise ValueError(f"unsupported overlay type: {overlay_type}")
        if not base_commit:
            raise ValueError("base_commit is required")
        store = cls(path)
        identity = overlay_id or f"overlay:{uuid.uuid4().hex}"
        now = _now()
        store.connection.execute(
            "INSERT INTO overlay_metadata VALUES(?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)",
            (identity, overlay_type, repository_id, base_commit, base_generation,
             branch_name, parent_overlay_id, now, now),
        )
        store.connection.commit()
        return store

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "OverlayStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @property
    def metadata(self) -> OverlayMetadata:
        row = self.connection.execute("SELECT * FROM overlay_metadata LIMIT 1").fetchone()
        if row is None:
            raise RuntimeError("overlay metadata is missing")
        return OverlayMetadata(**dict(row))

    @property
    def database_size(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    def mark_base(self, current_commit: str, current_generation: int) -> str:
        metadata = self.metadata
        status = "ACTIVE" if (
            metadata.base_commit == current_commit
            and metadata.base_generation == current_generation
        ) else "REBASE_REQUIRED"
        self.set_status(status)
        return status

    def set_status(self, status: str) -> None:
        self.connection.execute(
            "UPDATE overlay_metadata SET status=?, updated_at=?", (status, _now())
        )
        self.connection.commit()

    def put(self, delta: OverlayDelta) -> None:
        if delta.operation not in OPERATIONS:
            raise ValueError(f"unsupported operation: {delta.operation}")
        if delta.entity_type not in ENTITY_TABLES:
            raise ValueError(f"unsupported entity type: {delta.entity_type}")
        if delta.overlay_id != self.metadata.overlay_id:
            raise ValueError("delta belongs to a different overlay")
        if delta.operation == "DELETE" and delta.new_value is not None:
            raise ValueError("DELETE must be represented as a tombstone")
        self.connection.execute(
            """INSERT INTO overlay_deltas VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(overlay_id, entity_type, entity_id) DO UPDATE SET
               operation=excluded.operation, base_version=excluded.base_version,
               new_value=excluded.new_value, generation=excluded.generation""",
            (delta.overlay_id, delta.entity_type, delta.entity_id, delta.operation,
             delta.base_version,
             json.dumps(delta.new_value, sort_keys=True) if delta.new_value is not None else None,
             delta.generation),
        )
        self.connection.execute("UPDATE overlay_metadata SET updated_at=?", (_now(),))
        self.connection.commit()

    def apply(self, deltas: Iterable[OverlayDelta]) -> int:
        count = 0
        for delta in deltas:
            self.put(delta)
            count += 1
        self.set_status("ACTIVE")
        return count

    def deltas(self, entity_type: str | None = None) -> list[OverlayDelta]:
        sql = "SELECT * FROM overlay_deltas"
        params: tuple = ()
        if entity_type is not None:
            sql += " WHERE entity_type=?"
            params = (entity_type,)
        sql += " ORDER BY entity_type, entity_id"
        result = []
        for row in self.connection.execute(sql, params):
            item = dict(row)
            item["new_value"] = json.loads(item["new_value"]) if item["new_value"] else None
            result.append(OverlayDelta(**item))
        return result

    def discard(self) -> None:
        self.connection.execute("DELETE FROM overlay_deltas")
        self.connection.execute("UPDATE overlay_metadata SET status='DISCARDED', updated_at=?", (_now(),))
        self.connection.commit()

    def clear(self) -> None:
        self.connection.execute("DELETE FROM overlay_deltas")
        self.connection.execute("UPDATE overlay_metadata SET status='ACTIVE', updated_at=?", (_now(),))
        self.connection.commit()

    def counts(self) -> dict[str, int]:
        result = {f"{entity.lower()}_{operation.lower()}": 0
                  for entity in ENTITY_TABLES for operation in OPERATIONS}
        for row in self.connection.execute(
            "SELECT entity_type, operation, COUNT(*) count FROM overlay_deltas GROUP BY entity_type, operation"
        ):
            result[f"{row['entity_type'].lower()}_{row['operation'].lower()}"] = row["count"]
        return result


def _snapshot(database: str | Path, entity_type: str) -> dict[str, dict]:
    table, key = ENTITY_TABLES[entity_type]
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return {row[key]: dict(row) for row in connection.execute(f"SELECT * FROM {table}")}
    finally:
        connection.close()


def capture_snapshot_delta(
    base_database: str | Path, target_database: str | Path, overlay: OverlayStore,
) -> dict[str, int]:
    """Persist only the entity differences between two compatible graph snapshots."""
    with SQLiteStorage(base_database) as base_storage, SQLiteStorage(target_database) as target_storage:
        base_repository, target_repository = base_storage.repository(), target_storage.repository()
        if not base_repository or not target_repository:
            raise RuntimeError("base and target graphs must be indexed")
        if base_repository["repo_id"] != target_repository["repo_id"]:
            raise ValueError("target graph must use the base repository_id")
        if overlay.metadata.repository_id != base_repository["repo_id"]:
            raise ValueError("overlay repository_id does not match snapshots")
    overlay.clear()
    generation = overlay.metadata.base_generation + 1
    for entity_type in ENTITY_TABLES:
        base = _snapshot(base_database, entity_type)
        target = _snapshot(target_database, entity_type)
        for entity_id in sorted(set(base) | set(target)):
            old, new = base.get(entity_id), target.get(entity_id)
            if _canonical(old) == _canonical(new):
                continue
            operation = "ADD" if old is None else "DELETE" if new is None else "UPDATE"
            overlay.put(OverlayDelta(
                overlay.metadata.overlay_id, entity_type, entity_id, operation,
                entity_version(old), None if operation == "DELETE" else new, generation,
            ))
    return overlay.counts()


def overlay_metrics(base_database: str | Path, overlay: OverlayStore) -> dict:
    base_size = Path(base_database).resolve().stat().st_size
    overlay_size = overlay.database_size
    counts = overlay.counts()
    touched_shards = {
        value
        for delta in overlay.deltas()
        for value in (
            (delta.new_value or {}).get("shard_id"),
            delta.entity_id if delta.entity_type == "Shard" else None,
            (delta.new_value or {}).get("src_shard_id") if delta.entity_type == "BoundaryEdge" else None,
            (delta.new_value or {}).get("dst_shard_id") if delta.entity_type == "BoundaryEdge" else None,
        )
        if value
    }


def apply_repository_overlay(
    repository: str | Path, base_database: str | Path, overlay: OverlayStore,
    *, strategy: ShardStrategy | None = None,
) -> dict:
    """Incrementally parse a worktree while publishing only its delta.

    The mutable snapshot is transient and removed before return; no branch/session
    keeps a full graph copy. The shared Base database is never passed to a writer.
    """
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="pl-overlay-apply-") as temp:
        working_database = Path(temp) / "working.db"
        shutil.copy2(Path(base_database).resolve(), working_database)
        update = incremental_update(
            repository, working_database, strategy=strategy,
            repository_id_override=overlay.metadata.repository_id,
        )
        counts = capture_snapshot_delta(base_database, working_database, overlay)
    return {
        "delta_counts": counts,
        "files_reparsed": update["metrics"]["files_reparsed"],
        "files_reused": update["metrics"]["files_reused"],
        "references_reprocessed": update["metrics"]["references_reprocessed"],
        "references_reused": update["metrics"]["references_reused"],
        "shards_updated": update["metrics"]["shards_updated"],
        "boundary_edges_reprocessed": update["metrics"]["boundary_edges_reprocessed"],
        "overlay_apply_time_ms": (time.perf_counter() - started) * 1000,
    }
    return {
        "base_db_size": base_size,
        "overlay_db_size": overlay_size,
        "overlay_to_base_ratio": overlay_size / base_size if base_size else 0.0,
        "delta_count": len(overlay.deltas()),
        "shards_touched": len(touched_shards),
        **counts,
    }


class GraphView:
    """Base + small delta lookups. Merge logic is shared by every query."""

    def __init__(
        self, base_database: str | Path,
        branch_overlay: str | Path | OverlayStore | None = None,
        session_overlay: str | Path | OverlayStore | None = None,
        *, current_base_commit: str | None = None,
    ) -> None:
        self.base = SQLiteStorage(base_database)
        self._owned: list[OverlayStore] = []
        self.branch = self._open(branch_overlay)
        self.session = self._open(session_overlay)
        repository = self.base.repository()
        if repository is None:
            raise RuntimeError("base graph is empty")
        for overlay in self.layers:
            metadata = overlay.metadata
            if metadata.repository_id != repository["repo_id"]:
                raise ValueError("overlay repository_id does not match base graph")
            if metadata.base_generation != int(repository["current_generation"]):
                overlay.set_status("REBASE_REQUIRED")
                self.close()
                raise RuntimeError("REBASE_REQUIRED: base generation changed")
            if current_base_commit is not None and metadata.base_commit != current_base_commit:
                overlay.set_status("REBASE_REQUIRED")
                self.close()
                raise RuntimeError("REBASE_REQUIRED: base commit changed")
        self._delta_cache: dict[tuple[str, str], list[OverlayDelta]] = {}

    def _open(self, value: str | Path | OverlayStore | None) -> OverlayStore | None:
        if value is None:
            return None
        if isinstance(value, OverlayStore):
            return value
        store = OverlayStore(value)
        self._owned.append(store)
        return store

    @property
    def layers(self) -> list[OverlayStore]:
        return [item for item in (self.branch, self.session) if item is not None]

    @property
    def graph_generation(self) -> int:
        repository = self.base.repository()
        return int(repository["current_generation"]) if repository else 0

    def close(self) -> None:
        for store in self._owned:
            store.close()
        self.base.close()

    def _deltas(self, store: OverlayStore, entity_type: str) -> list[OverlayDelta]:
        key = (store.metadata.overlay_id, entity_type)
        if key not in self._delta_cache:
            self._delta_cache[key] = store.deltas(entity_type)
        return self._delta_cache[key]

    def query(
        self, entity_type: str, sql: str, params: tuple = (),
        predicate: Callable[[dict], bool] | None = None,
    ) -> list[dict]:
        _, key = ENTITY_TABLES[entity_type]
        predicate = predicate or (lambda row: True)
        effective = {row[key]: row for row in self.base.rows(sql, params)}
        for store in self.layers:
            for delta in self._deltas(store, entity_type):
                if delta.operation == "DELETE" or not predicate(delta.new_value or {}):
                    effective.pop(delta.entity_id, None)
                else:
                    effective[delta.entity_id] = dict(delta.new_value or {})
        return list(effective.values())

    def id_filter(self, values: Iterable[str]) -> AbstractContextManager[tuple[str, tuple]]:
        return self.base.id_filter(values)

    def by_ids(self, entity_type: str, ids: Iterable[str]) -> list[dict]:
        values = sorted(set(ids))
        if not values:
            return []
        table, key = ENTITY_TABLES[entity_type]
        wanted = set(values)
        with self.id_filter(values) as (sql, params):
            return self.query(
                entity_type, f"SELECT * FROM {table} WHERE {key} IN ({sql})", params,
                lambda row: row.get(key) in wanted,
            )

    def all(self, entity_type: str) -> list[dict]:
        table, _ = ENTITY_TABLES[entity_type]
        return self.query(entity_type, f"SELECT * FROM {table}")


def materialize_view(
    base_database: str | Path, branch_overlay: str | Path | OverlayStore | None = None,
    session_overlay: str | Path | OverlayStore | None = None,
) -> dict[str, list[dict]]:
    view = GraphView(base_database, branch_overlay, session_overlay)
    try:
        return {entity: sorted(view.all(entity), key=lambda row: row[ENTITY_TABLES[entity][1]])
                for entity in ENTITY_TABLES}
    finally:
        view.close()


def materialization_parity(
    base_database: str | Path, target_database: str | Path,
    branch_overlay: str | Path | OverlayStore | None = None,
    session_overlay: str | Path | OverlayStore | None = None,
) -> bool:
    """Compare effective entities to a full snapshot without retaining the whole graph twice."""
    view = GraphView(base_database, branch_overlay, session_overlay)
    try:
        for entity_type, (table, key) in ENTITY_TABLES.items():
            effective = sorted((_canonical(row) for row in view.all(entity_type)))
            target = sorted((_canonical(row) for row in _snapshot(target_database, entity_type).values()))
            if effective != target:
                return False
        return True
    finally:
        view.close()


def detect_conflicts(left: OverlayStore, right: OverlayStore) -> list[OverlayConflict]:
    a, b = left.metadata, right.metadata
    if (a.repository_id, a.base_commit, a.base_generation) != (
        b.repository_id, b.base_commit, b.base_generation
    ):
        return [OverlayConflict("REBASE_REQUIRED", details={
            "left_base": a.base_commit, "right_base": b.base_commit,
        })]
    conflicts: list[OverlayConflict] = []
    left_map = {(item.entity_type, item.entity_id): item for item in left.deltas()}
    right_map = {(item.entity_type, item.entity_id): item for item in right.deltas()}
    for key in sorted(set(left_map) & set(right_map)):
        first, second = left_map[key], right_map[key]
        if first.operation == second.operation and first.new_value == second.new_value:
            continue
        operations = {first.operation, second.operation}
        kind = (
            "DELETE_UPDATE_CONFLICT" if "DELETE" in operations
            else "ADD_ADD_CONFLICT" if operations == {"ADD"}
            else "ENTITY_CONFLICT"
        )
        conflicts.append(OverlayConflict(kind, key[0], key[1]))

    def changed_api_shards(store: OverlayStore) -> set[str]:
        result = set()
        for delta in store.deltas("Shard"):
            if delta.operation != "UPDATE" or delta.new_value is None:
                continue
            if delta.base_version and delta.new_value.get("boundary_dirty"):
                result.add(delta.entity_id)
        return result

    def boundary_shards(store: OverlayStore) -> set[str]:
        return {
            value
            for delta in store.deltas("BoundaryEdge") if delta.new_value
            for value in (delta.new_value.get("src_shard_id"), delta.new_value.get("dst_shard_id"))
            if value
        }

    boundary = (
        (changed_api_shards(left) & boundary_shards(right))
        | (changed_api_shards(right) & boundary_shards(left))
    )
    for shard_id in sorted(boundary):
        conflicts.append(OverlayConflict("BOUNDARY_CONFLICT", "Shard", shard_id))
    return conflicts


def commit_to_branch(session: OverlayStore, branch: OverlayStore) -> dict:
    if session.metadata.overlay_type != "SESSION" or branch.metadata.overlay_type != "BRANCH":
        raise ValueError("commit_to_branch requires SESSION then BRANCH")
    for delta in session.deltas():
        branch.put(OverlayDelta(
            branch.metadata.overlay_id, delta.entity_type, delta.entity_id,
            delta.operation, delta.base_version, delta.new_value, delta.generation,
        ))
    count = len(session.deltas())
    session.discard()
    return {"committed_deltas": count, "session_status": session.metadata.status}


def merge_overlays(left: OverlayStore, right: OverlayStore, target: OverlayStore) -> dict:
    conflicts = detect_conflicts(left, right)
    if conflicts:
        target.set_status("CONFLICT")
        return {"status": "CONFLICT", "conflicts": [item.to_dict() for item in conflicts]}
    for source in (left, right):
        for delta in source.deltas():
            target.put(OverlayDelta(
                target.metadata.overlay_id, delta.entity_type, delta.entity_id,
                delta.operation, delta.base_version, delta.new_value, delta.generation,
            ))
    return {"status": "NO_CONFLICT", "conflicts": [], "delta_count": len(target.deltas())}


def rebase_overlay(
    old_base: str | Path, new_base: str | Path, source: OverlayStore,
    target: OverlayStore, *, repository: str | Path | None = None,
    strategy: ShardStrategy | None = None,
) -> dict:
    conflicts: list[OverlayConflict] = []
    touched_shards: set[str] = set()
    boundary_recomputed = 0
    source_deltas = source.deltas()
    entity_types = {delta.entity_type for delta in source_deltas}
    old_snapshots = {entity: _snapshot(old_base, entity) for entity in entity_types}
    new_snapshots = {entity: _snapshot(new_base, entity) for entity in entity_types}
    for delta in source_deltas:
        old = old_snapshots[delta.entity_type].get(delta.entity_id)
        new = new_snapshots[delta.entity_type].get(delta.entity_id)
        if entity_version(old) != entity_version(new):
            conflicts.append(OverlayConflict(
                "DELETE_UPDATE_CONFLICT" if new is None else "ENTITY_CONFLICT",
                delta.entity_type, delta.entity_id,
            ))
            continue
        if repository is None:
            target.put(OverlayDelta(
                target.metadata.overlay_id, delta.entity_type, delta.entity_id,
                delta.operation, entity_version(new), delta.new_value, delta.generation,
            ))
        value = delta.new_value or old or {}
        if value.get("shard_id"):
            touched_shards.add(value["shard_id"])
        if delta.entity_type == "Shard":
            touched_shards.add(delta.entity_id)
        if delta.entity_type == "BoundaryEdge":
            boundary_recomputed += 1
    reconciliation = None
    if not conflicts and repository is not None:
        reconciliation = apply_repository_overlay(
            repository, new_base, target, strategy=strategy
        )
    status = "CONFLICT" if conflicts else "ACTIVE"
    target.set_status(status)
    return {
        "status": status,
        "conflicts": [item.to_dict() for item in conflicts],
        "shards_recomputed": len(touched_shards),
        "boundary_edges_recomputed": boundary_recomputed,
        "reconciliation": reconciliation,
    }


def reconcile_merged_source(
    merged_repository: str | Path, base_database: str | Path,
    target_overlay: OverlayStore, *, strategy: ShardStrategy | None = None,
) -> dict:
    """Reconcile Git-authoritative merged source into a fresh graph delta."""
    return apply_repository_overlay(
        merged_repository, base_database, target_overlay, strategy=strategy
    )
