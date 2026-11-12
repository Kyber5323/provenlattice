"""The analysis transaction boundary: one commit per scope, never zero.

`replace_analysis_for_file` used to open and commit its own transaction per
file, so a scope paid one fsync per file and per analyzer (240 commits, 2.0s of
a 7.5s analyze on an 80-file corpus). `AnalysisStore.defer_commit()` moves that
commit to the storage scope.

The failure mode this must never have is a *lost* commit: rows written into a
transaction that the connection never commits are silently absent, and a cache
that answers "no analysis" after a successful run is worse than a slow one. So
the tests below pin both directions -- fewer commits on success, and a rollback
(not a partial publish) on failure.
"""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice.analysis import AnalysisEdge, AnalysisNode, AnalysisStore
from provenlattice.storage import SQLiteStorage


def build_repo(root: Path, files: int = 6) -> None:
    (root / "src").mkdir(parents=True)
    for index in range(files):
        (root / "src" / f"u{index}.c").write_text(
            f"int f{index}(int n){{ return n + {index}; }}\n", encoding="utf-8")


class DeferCommitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "g.db"
        build_repo(self.root)
        from provenlattice.graph import full_index

        full_index(self.root, self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _publish(self, store: AnalysisStore, file_index: int) -> None:
        store.replace_analysis_for_file(
            f"file:{file_index}", "sparse_cfg:cpp", f"hash{file_index}", 1,
            [AnalysisNode(f"node:{file_index}", "CALL", {"i": file_index})],
            [AnalysisEdge(f"edge:{file_index}", f"node:{file_index}",
                          f"node:{file_index}", "CONTROL_REACHES", {})],
        )

    def test_deferred_rows_are_committed_when_the_scope_exits(self) -> None:
        with SQLiteStorage(self.database) as storage:
            store = AnalysisStore(storage)
            store.defer_commit()
            for index in range(4):
                self._publish(store, index)
        # a *separate* connection must see them: commit happened on close
        with closing(sqlite3.connect(self.database)) as check:
            nodes = check.execute("SELECT COUNT(*) FROM analysis_nodes").fetchone()[0]
            states = check.execute("SELECT COUNT(*) FROM analysis_state").fetchone()[0]
        self.assertEqual(nodes, 4)
        self.assertEqual(states, 4)

    def test_without_defer_commit_each_publish_is_durable_immediately(self) -> None:
        storage = SQLiteStorage(self.database)
        store = AnalysisStore(storage)
        self._publish(store, 0)
        with closing(sqlite3.connect(self.database)) as check:
            nodes = check.execute("SELECT COUNT(*) FROM analysis_nodes").fetchone()[0]
        self.assertEqual(nodes, 1)
        storage.close()

    def test_a_failure_rolls_the_whole_scope_back(self) -> None:
        # Two publishes succeed, then the scope raises. Nothing may survive:
        # a half-published generation is the state this boundary exists to rule
        # out, and "some files are current" is harder to diagnose than "none".
        with self.assertRaises(RuntimeError):
            with SQLiteStorage(self.database) as storage:
                store = AnalysisStore(storage)
                store.defer_commit()
                self._publish(store, 0)
                self._publish(store, 1)
                raise RuntimeError("boom")
        with closing(sqlite3.connect(self.database)) as check:
            nodes = check.execute("SELECT COUNT(*) FROM analysis_nodes").fetchone()[0]
            states = check.execute("SELECT COUNT(*) FROM analysis_state").fetchone()[0]
        self.assertEqual(nodes, 0)
        self.assertEqual(states, 0)

    def test_batch_context_commits_on_its_own_exit(self) -> None:
        with SQLiteStorage(self.database) as storage:
            store = AnalysisStore(storage)
            with store.batch():
                for index in range(3):
                    self._publish(store, index)
            # the batch committed; the scope has not closed yet
            with closing(sqlite3.connect(self.database)) as check:
                nodes = check.execute("SELECT COUNT(*) FROM analysis_nodes").fetchone()[0]
            self.assertEqual(nodes, 3)

    def test_batch_then_scope_close_is_not_double_counted(self) -> None:
        with SQLiteStorage(self.database) as storage:
            store = AnalysisStore(storage)
            with store.batch():
                self._publish(store, 0)
        with closing(sqlite3.connect(self.database)) as check:
            nodes = check.execute("SELECT COUNT(*) FROM analysis_nodes").fetchone()[0]
            states = check.execute("SELECT COUNT(*) FROM analysis_state").fetchone()[0]
        self.assertEqual((nodes, states), (1, 1))


if __name__ == "__main__":
    unittest.main()
