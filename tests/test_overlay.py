from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from provenlattice.graph import full_index
from provenlattice.models import OverlayDelta
from provenlattice.overlay import (
    GraphView, OverlayStore, apply_repository_overlay, capture_snapshot_delta, commit_to_branch,
    detect_conflicts, materialize_view, merge_overlays, rebase_overlay,
)
from provenlattice.query import GraphQuery
from provenlattice.storage import SQLiteStorage


BASE = """from module_b.util import helper

def run(value: int) -> int:
    return helper(value)
"""

BRANCH = """from module_c.api import extra

def run(value: int) -> int:
    return extra(value)

def added(value: int) -> int:
    return value * 2
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def repository(database: Path) -> dict:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return dict(connection.execute("SELECT * FROM repositories LIMIT 1").fetchone())
    finally:
        connection.close()


def normalized(rows: list[dict], key: str) -> list[dict]:
    result = []
    for row in rows:
        item = dict(row)
        item.pop("generation", None)
        result.append(item)
    return sorted(result, key=lambda item: item[key])


class OverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root / "module_a" / "service.py", BASE)
        write(self.root / "module_b" / "util.py", "def helper(value: int) -> int:\n    return value + 1\n")
        write(self.root / "module_c" / "api.py", "def extra(value: int) -> int:\n    return value - 1\n")
        self.base = Path(self.temp.name) / "base.db"
        self.target = Path(self.temp.name) / "target.db"
        full_index(self.root, self.base)
        self.repo = repository(self.base)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def overlay(self, name: str, overlay_type: str = "BRANCH", **kwargs) -> OverlayStore:
        return OverlayStore.create(
            Path(self.temp.name) / f"{name}.db", overlay_type=overlay_type,
            repository_id=self.repo["repo_id"], base_commit="C100",
            base_generation=self.repo["current_generation"], branch_name=name,
            **kwargs,
        )

    def test_materialization_and_all_queries_match_branch_snapshot(self) -> None:
        branch_root = Path(self.temp.name) / "branch-worktree"
        write(branch_root / "module_a" / "service.py", BRANCH)
        write(branch_root / "module_b" / "util.py", "def helper(value: int) -> int:\n    return value + 1\n")
        write(branch_root / "module_c" / "api.py", "def extra(value: int) -> int:\n    return value - 1\n")
        full_index(branch_root, self.target, repository_id_override=self.repo["repo_id"])
        with self.overlay("branch-a") as branch:
            counts = capture_snapshot_delta(self.base, self.target, branch)
            self.assertGreater(sum(counts.values()), 0)
            effective = materialize_view(self.base, branch)
            for entity, (table, key) in {
                "Node": ("nodes", "id"), "Edge": ("edges", "id"),
                "RawReference": ("raw_references", "id"),
                "Shard": ("shards", "shard_id"),
                "BoundaryEdge": ("shard_edges", "edge_id"),
            }.items():
                # Through `SQLiteStorage`: the reference layer is cold on a
                # split layout and invisible to a bare connection.
                with SQLiteStorage(self.target) as storage:
                    target = [
                        dict(row)
                        for row in storage.connection.execute(f"SELECT * FROM {table}")
                    ]
                self.assertEqual(normalized(effective[entity], key), normalized(target, key))
            with GraphQuery(self.base, branch_overlay=branch.path) as query:
                self.assertEqual(len(query.find_symbol("added")["data"]), 1)
                callees = query.get_callees("module_a.service.run")["data"]
                self.assertEqual([item["name"] for item in callees], ["extra"])
                self.assertTrue(query.get_dependencies("module_a")["data"])
                self.assertTrue(query.get_boundary_references("module_c.api.extra")["data"])
                self.assertTrue(query.get_subgraph("module_a.service.run", 1, 20)["data"]["edges"])

    def test_session_priority_tombstone_recreate_commit_and_discard(self) -> None:
        with GraphQuery(self.base) as query:
            row = query.get_definition("module_b.util.helper")["data"]
        branch, session = self.overlay("branch"), self.overlay(
            "session", "SESSION", parent_overlay_id="branch"
        )
        try:
            branch.put(OverlayDelta(branch.metadata.overlay_id, "Node", row["id"], "DELETE",
                                    "base", None, 2))
            recreated = dict(row)
            recreated["name"] = "session_helper"
            session.put(OverlayDelta(session.metadata.overlay_id, "Node", row["id"], "ADD",
                                     "base", recreated, 2))
            with GraphQuery(self.base, branch_overlay=branch.path,
                            session_overlay=session.path) as query:
                self.assertEqual(query.get_definition(row["id"])["data"]["name"], "session_helper")
            committed = commit_to_branch(session, branch)
            self.assertGreater(committed["committed_deltas"], 0)
            self.assertEqual(session.metadata.status, "DISCARDED")
            with GraphQuery(self.base, branch_overlay=branch.path) as query:
                self.assertEqual(query.get_definition(row["id"])["data"]["name"], "session_helper")
            branch.discard()
            with GraphQuery(self.base, branch_overlay=branch.path) as query:
                self.assertEqual(query.get_definition(row["id"])["data"]["name"], "helper")
        finally:
            branch.close()
            session.close()

    def test_stale_overlay_requires_rebase(self) -> None:
        overlay = self.overlay("stale")
        path = overlay.path
        overlay.close()
        with self.assertRaisesRegex(RuntimeError, "REBASE_REQUIRED"):
            GraphView(self.base, path, current_base_commit="C110")
        with OverlayStore(path) as reopened:
            self.assertEqual(reopened.metadata.status, "REBASE_REQUIRED")

    def test_session_apply_uses_incremental_parse_without_mutating_base(self) -> None:
        write(self.root / "module_a" / "service.py", BRANCH)
        before = repository(self.base)["current_generation"]
        with self.overlay("incremental-session", "SESSION") as session:
            result = apply_repository_overlay(self.root, self.base, session)
            self.assertEqual(result["files_reparsed"], 1)
            self.assertGreater(result["files_reused"], 0)
            with GraphQuery(self.base, session_overlay=session.path) as query:
                self.assertEqual(len(query.find_symbol("added")["data"]), 1)
            rebased = self.overlay("reconciled")
            try:
                rebased_result = rebase_overlay(
                    self.base, self.base, session, rebased, repository=self.root
                )
                self.assertEqual(rebased_result["status"], "ACTIVE")
                self.assertEqual(rebased_result["reconciliation"]["files_reparsed"], 1)
            finally:
                rebased.close()
        self.assertEqual(repository(self.base)["current_generation"], before)

    def test_conflict_contract_merge_and_rebase(self) -> None:
        left, right, merged = self.overlay("left"), self.overlay("right"), self.overlay("merged")
        try:
            left.put(OverlayDelta(left.metadata.overlay_id, "Node", "same", "UPDATE", "v1",
                                  {"id": "same", "name": "left"}, 2))
            right.put(OverlayDelta(right.metadata.overlay_id, "Node", "same", "DELETE", "v1", None, 2))
            self.assertIn("DELETE_UPDATE_CONFLICT",
                          {item.conflict_type for item in detect_conflicts(left, right)})
            self.assertEqual(merge_overlays(left, right, merged)["status"], "CONFLICT")
        finally:
            left.close(); right.close(); merged.close()

        a, b, target = self.overlay("a"), self.overlay("b"), self.overlay("target")
        try:
            a.put(OverlayDelta(a.metadata.overlay_id, "Node", "a", "ADD", None,
                               {"id": "a", "name": "a"}, 2))
            b.put(OverlayDelta(b.metadata.overlay_id, "Node", "b", "ADD", None,
                               {"id": "b", "name": "b"}, 2))
            self.assertEqual(detect_conflicts(a, b), [])
            self.assertEqual(merge_overlays(a, b, target)["status"], "NO_CONFLICT")
        finally:
            a.close(); b.close(); target.close()

        source = self.overlay("source")
        rebased = OverlayStore.create(
            Path(self.temp.name) / "rebased.db", overlay_type="BRANCH",
            repository_id=self.repo["repo_id"], base_commit="C110",
            base_generation=self.repo["current_generation"], branch_name="rebased",
        )
        try:
            source.put(OverlayDelta(source.metadata.overlay_id, "Node", "new", "ADD", None,
                                    {"id": "new", "name": "new"}, 2))
            result = rebase_overlay(self.base, self.base, source, rebased)
            self.assertEqual(result["status"], "ACTIVE")
            self.assertEqual(len(rebased.deltas()), 1)
        finally:
            source.close(); rebased.close()

    def test_add_add_entity_and_boundary_conflicts(self) -> None:
        left, right = self.overlay("conflict-left"), self.overlay("conflict-right")
        try:
            left.put(OverlayDelta(left.metadata.overlay_id, "Node", "added", "ADD", None,
                                  {"id": "added", "name": "left"}, 2))
            right.put(OverlayDelta(right.metadata.overlay_id, "Node", "added", "ADD", None,
                                   {"id": "added", "name": "right"}, 2))
            self.assertIn("ADD_ADD_CONFLICT",
                          {item.conflict_type for item in detect_conflicts(left, right)})
            left.clear(); right.clear()
            left.put(OverlayDelta(left.metadata.overlay_id, "Node", "updated", "UPDATE", "v1",
                                  {"id": "updated", "name": "left"}, 2))
            right.put(OverlayDelta(right.metadata.overlay_id, "Node", "updated", "UPDATE", "v1",
                                   {"id": "updated", "name": "right"}, 2))
            self.assertIn("ENTITY_CONFLICT",
                          {item.conflict_type for item in detect_conflicts(left, right)})
            left.clear(); right.clear()
            left.put(OverlayDelta(left.metadata.overlay_id, "Shard", "shard-api", "UPDATE", "v1",
                                  {"shard_id": "shard-api", "boundary_dirty": 1}, 2))
            right.put(OverlayDelta(
                right.metadata.overlay_id, "BoundaryEdge", "edge-new", "ADD", None,
                {"edge_id": "edge-new", "src_shard_id": "shard-client",
                 "dst_shard_id": "shard-api", "edge_type": "CALLS",
                 "raw_reference_id": "ref"}, 2,
            ))
            self.assertIn("BOUNDARY_CONFLICT",
                          {item.conflict_type for item in detect_conflicts(left, right)})
        finally:
            left.close(); right.close()


if __name__ == "__main__":
    unittest.main()
