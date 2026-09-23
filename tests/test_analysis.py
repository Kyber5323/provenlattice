from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice.analysis import (
    ANALYZER_VERSIONS,
    AnalysisEdge,
    AnalysisNode,
    AnalysisStore,
)
from provenlattice.graph import full_index
from provenlattice.incremental import incremental_update
from provenlattice.overlay import ENTITY_TABLES
from provenlattice.query import GraphQuery
from provenlattice.storage import SQLiteStorage

REVISION_A = """def alpha(value: int) -> int:
    return value + 1
"""

REVISION_B = """def alpha(value: int) -> int:
    return value + 100

def beta(value: int) -> int:
    return value * 2
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def synthetic_nodes(tag: str) -> list[AnalysisNode]:
    return [
        AnalysisNode(f"anode:{tag}:1", "test_ast_node", {"tag": tag}),
        AnalysisNode(f"anode:{tag}:2", "test_ast_node",
                     {"tag": tag, "parent": f"anode:{tag}:1"}),
    ]


def synthetic_edges(tag: str) -> list[AnalysisEdge]:
    return [
        AnalysisEdge(f"aedge:{tag}:1", f"anode:{tag}:1", f"anode:{tag}:2",
                     "test_ast_edge", {"tag": tag}),
    ]


def base_snapshot(database: Path) -> dict:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    result = {}
    for table in ("nodes", "edges", "shards", "raw_references"):
        rows = []
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1"):
            item = dict(row)
            item.pop("generation", None)
            if "boundary_dirty" in item:
                item.pop("boundary_dirty")
            rows.append(item)
        result[table] = rows
    result["boundary_edges"] = [
        dict(row) for row in connection.execute(
            "SELECT id, src_id, dst_id, type, metadata FROM edges "
            "WHERE json_extract(metadata, '$.scope') = 'boundary' ORDER BY id"
        )
    ]
    connection.close()
    return result


def analysis_rows(database: Path, file_id: str, analyzer: str) -> dict:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    result = {
        "state": [
            dict(row) for row in connection.execute(
                "SELECT * FROM analysis_state WHERE file_id=? AND feature=? ORDER BY 1",
                (file_id, analyzer))
        ],
        "nodes": [
            dict(row) for row in connection.execute(
                "SELECT * FROM analysis_nodes WHERE file_id=? AND feature=? ORDER BY 1",
                (file_id, analyzer))
        ],
        "edges": [
            dict(row) for row in connection.execute(
                "SELECT * FROM analysis_edges WHERE file_id=? AND feature=? ORDER BY 1",
                (file_id, analyzer))
        ],
    }
    connection.close()
    return result


def query_row(database: Path, sql: str, params: tuple) -> sqlite3.Row:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(sql, params).fetchone()


class AnalysisStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root / "pkg" / "service.py", REVISION_A)
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)
        row = query_row(
            self.database,
            "SELECT file_id, source_hash FROM files WHERE path='pkg/service.py'",
            (),
        )
        self.file_id = row["file_id"]
        self.hash_a = row["source_hash"]
        self.stores: list[AnalysisStore] = []

    def tearDown(self) -> None:
        for store in self.stores:
            store.storage.close()
        self.temp.cleanup()

    def store(self) -> AnalysisStore:
        store = AnalysisStore(SQLiteStorage(self.database))
        self.stores.append(store)
        return store

    def test_analysis_tables_are_lazy_and_absent_from_base_infrastructure(self) -> None:
        def table_names() -> set[str]:
            with closing(sqlite3.connect(self.database)) as connection:
                return {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")
                }
        self.assertFalse(
            {"analysis_state", "analysis_nodes", "analysis_edges"} & table_names()
        )
        self.store()
        self.assertTrue(
            {"analysis_state", "analysis_nodes", "analysis_edges"} <= table_names()
        )
        analysis_tables = {"analysis_state", "analysis_nodes", "analysis_edges"}
        self.assertFalse(analysis_tables & {table for table, _ in ENTITY_TABLES.values()})

    def test_writing_synthetic_analysis_does_not_change_base_snapshot(self) -> None:
        before = base_snapshot(self.database)
        self.store().replace_analysis_for_file(
            self.file_id, "ast:python", self.hash_a, ANALYZER_VERSIONS["ast:python"],
            synthetic_nodes("a"), synthetic_edges("a"),
        )
        self.assertEqual(base_snapshot(self.database), before)
        with GraphQuery(self.database) as query:
            self.assertEqual(len(query.find_symbol("alpha")["data"]), 1)

    def test_dropping_analysis_tables_restores_base_equivalence(self) -> None:
        self.store().replace_analysis_for_file(
            self.file_id, "ast:python", self.hash_a, ANALYZER_VERSIONS["ast:python"],
            synthetic_nodes("a"), synthetic_edges("a"),
        )
        expected = base_snapshot(self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            for table in ("analysis_state", "analysis_nodes", "analysis_edges"):
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            connection.commit()
        self.assertEqual(base_snapshot(self.database), expected)
        with GraphQuery(self.database) as query:
            latest = query.status()["data"]["latest_generation"]
            self.assertGreaterEqual(latest["generation"], 1)

    def test_incremental_update_preserves_rows_but_fails_freshness(self) -> None:
        self.store().replace_analysis_for_file(
            self.file_id, "ast:python", self.hash_a, ANALYZER_VERSIONS["ast:python"],
            synthetic_nodes("a"), synthetic_edges("a"),
        )
        current = self.store().read_current_analysis(self.file_id, "ast:python")
        self.assertTrue(current["fresh"])
        self.assertEqual(
            {node["analysis_node_id"] for node in current["nodes"]},
            {"anode:a:1", "anode:a:2"},
        )
        write(self.root / "pkg" / "service.py", REVISION_B)
        update = incremental_update(self.root, self.database)
        self.assertTrue(update["changed"])
        with GraphQuery(self.database) as query:
            self.assertEqual(len(query.find_symbol("beta")["data"]), 1)
        kept = query_row(
            self.database,
            "SELECT COUNT(*) AS count FROM analysis_nodes "
            "WHERE file_id=? AND feature='ast:python'",
            (self.file_id,),
        )["count"]
        kept_state = query_row(
            self.database,
            "SELECT source_hash FROM analysis_state WHERE file_id=? AND feature='ast:python'",
            (self.file_id,),
        )["source_hash"]
        new_hash = query_row(
            self.database,
            "SELECT source_hash FROM file_state WHERE file_id=?",
            (self.file_id,),
        )["source_hash"]
        self.assertEqual(kept, 2)
        self.assertEqual(kept_state, self.hash_a)
        self.assertNotEqual(new_hash, self.hash_a)
        verdict = self.store().freshness_against_base(self.file_id, "ast:python")
        self.assertFalse(verdict["fresh"])
        self.assertIn("source_hash_changed", verdict["reason"])
        result = self.store().read_current_analysis(self.file_id, "ast:python")
        self.assertFalse(result["fresh"])
        self.assertEqual(result["nodes"], [])
        self.assertEqual(result["edges"], [])
        forensic = self.store().read_current_analysis(
            self.file_id, "ast:python", allow_stale=True
        )
        self.assertFalse(forensic["fresh"])
        self.assertEqual(
            {node["analysis_node_id"] for node in forensic["nodes"]},
            {"anode:a:1", "anode:a:2"},
        )
        self.store().replace_analysis_for_file(
            self.file_id, "ast:python", new_hash, ANALYZER_VERSIONS["ast:python"],
            synthetic_nodes("b"), synthetic_edges("b"),
        )
        rebuilt = self.store().read_current_analysis(self.file_id, "ast:python")
        self.assertTrue(rebuilt["fresh"])
        self.assertEqual(
            {node["analysis_node_id"] for node in rebuilt["nodes"]},
            {"anode:b:1", "anode:b:2"},
        )

    def test_cache_reuse_and_invalidation_triggers(self) -> None:
        version = ANALYZER_VERSIONS["ast:python"]
        self.assertFalse(
            self.store().is_analysis_fresh(
                self.file_id, "ast:python", self.hash_a, version)
        )
        self.store().replace_analysis_for_file(
            self.file_id, "ast:python", self.hash_a, version,
            synthetic_nodes("a"), synthetic_edges("a"),
        )
        self.assertTrue(
            self.store().is_analysis_fresh(
                self.file_id, "ast:python", self.hash_a, version)
        )
        self.assertFalse(
            self.store().is_analysis_fresh(
                self.file_id, "ast:python", "hash-other", version)
        )
        self.assertFalse(
            self.store().is_analysis_fresh(
                self.file_id, "ast:python", self.hash_a, version + 1)
        )
        self.store().replace_analysis_for_file(
            self.file_id, "ast:python", "hash-other", version,
            synthetic_nodes("b"), synthetic_edges("b"),
        )
        self.assertTrue(
            self.store().is_analysis_fresh(
                self.file_id, "ast:python", "hash-other", version)
        )
        self.assertFalse(
            self.store().is_analysis_fresh(
                self.file_id, "ast:python", self.hash_a, version)
        )

    def test_feature_isolation_on_same_file(self) -> None:
        version = ANALYZER_VERSIONS["cfg:python"]
        self.store().replace_analysis_for_file(
            self.file_id, "ast:python", self.hash_a, ANALYZER_VERSIONS["ast:python"],
            synthetic_nodes("a"), synthetic_edges("a"),
        )
        self.store().replace_analysis_for_file(
            self.file_id, "cfg:python", self.hash_a, version,
            synthetic_nodes("c"), synthetic_edges("c"),
        )
        cfg_before = analysis_rows(self.database, self.file_id, "cfg:python")
        self.store().replace_analysis_for_file(
            self.file_id, "ast:python", "hash-other", ANALYZER_VERSIONS["ast:python"] + 1,
            synthetic_nodes("a2"), synthetic_edges("a2"),
        )
        self.assertEqual(analysis_rows(self.database, self.file_id, "cfg:python"), cfg_before)
        ast_state = self.store().get_analysis_state(self.file_id, "ast:python")
        self.assertEqual(ast_state["source_hash"], "hash-other")
        self.assertEqual(ast_state["feature_version"], ANALYZER_VERSIONS["ast:python"] + 1)

    def test_file_isolation_across_files(self) -> None:
        write(self.root / "pkg" / "other.py", "def gamma() -> int:\n    return 3\n")
        incremental_update(self.root, self.database)
        row = query_row(
            self.database,
            "SELECT file_id, source_hash FROM files WHERE path='pkg/other.py'",
            (),
        )
        other_id, other_hash = row["file_id"], row["source_hash"]
        self.store().replace_analysis_for_file(
            self.file_id, "ast:python", self.hash_a, ANALYZER_VERSIONS["ast:python"],
            synthetic_nodes("a"), synthetic_edges("a"),
        )
        self.store().replace_analysis_for_file(
            other_id, "ast:python", other_hash, ANALYZER_VERSIONS["ast:python"],
            synthetic_nodes("g"), synthetic_edges("g"),
        )
        other_before = analysis_rows(self.database, other_id, "ast:python")
        self.store().replace_analysis_for_file(
            self.file_id, "ast:python", "hash-other", ANALYZER_VERSIONS["ast:python"],
            synthetic_nodes("a2"), synthetic_edges("a2"),
        )
        self.assertEqual(analysis_rows(self.database, other_id, "ast:python"), other_before)
        self.assertEqual(
            {node["analysis_node_id"] for node in analysis_rows(
                self.database, self.file_id, "ast:python")["nodes"]},
            {"anode:a2:1", "anode:a2:2"},
        )

    def test_replacement_is_atomic_and_scoped(self) -> None:
        self.store().replace_analysis_for_file(
            self.file_id, "ast:python", self.hash_a, ANALYZER_VERSIONS["ast:python"],
            synthetic_nodes("a"), synthetic_edges("a"),
        )
        self.store().replace_analysis_for_file(
            self.file_id, "cfg:python", self.hash_a, ANALYZER_VERSIONS["cfg:python"],
            synthetic_nodes("c"), synthetic_edges("c"),
        )
        duplicated = [
            AnalysisNode("anode:dup:1", "test_ast_node", {}),
            AnalysisNode("anode:dup:1", "test_ast_node", {}),
        ]
        with self.assertRaises(sqlite3.IntegrityError):
            self.store().replace_analysis_for_file(
                self.file_id, "ast:python", "hash-other", ANALYZER_VERSIONS["ast:python"],
                duplicated, [],
            )
        state = self.store().get_analysis_state(self.file_id, "ast:python")
        self.assertEqual(state["source_hash"], self.hash_a)
        self.assertEqual(
            {node["analysis_node_id"] for node in analysis_rows(
                self.database, self.file_id, "ast:python")["nodes"]},
            {"anode:a:1", "anode:a:2"},
        )
        self.assertEqual(
            {node["analysis_node_id"] for node in analysis_rows(
                self.database, self.file_id, "cfg:python")["nodes"]},
            {"anode:c:1", "anode:c:2"},
        )
        self.store().replace_analysis_for_file(
            self.file_id, "ast:python", "hash-other", ANALYZER_VERSIONS["ast:python"],
            synthetic_nodes("a2"), synthetic_edges("a2"),
        )
        self.assertTrue(
            self.store().is_analysis_fresh(
                self.file_id, "ast:python", "hash-other", ANALYZER_VERSIONS["ast:python"]
            )
        )

    def test_delete_analysis_for_file_removes_all_rows(self) -> None:
        self.store().replace_analysis_for_file(
            self.file_id, "ast:python", self.hash_a, ANALYZER_VERSIONS["ast:python"],
            synthetic_nodes("a"), synthetic_edges("a"),
        )
        self.assertTrue(self.store().delete_analysis_for_file(self.file_id, "ast:python"))
        self.assertIsNone(self.store().get_analysis_state(self.file_id, "ast:python"))
        for table in ("analysis_state", "analysis_nodes", "analysis_edges"):
            count = query_row(
                self.database,
                f"SELECT COUNT(*) AS count FROM {table} WHERE file_id=?",
                (self.file_id,),
            )["count"]
            self.assertEqual(count, 0)
        self.assertFalse(self.store().delete_analysis_for_file(self.file_id, "ast:python"))
        self.assertFalse(
            self.store().is_analysis_fresh(
                self.file_id, "ast:python", self.hash_a, ANALYZER_VERSIONS["ast:python"]
            )
        )

    def test_unknown_analyzer_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store().get_analysis_state(self.file_id, "dfg")
        with self.assertRaises(ValueError):
            self.store().replace_analysis_for_file(
                self.file_id, "dfg", self.hash_a, 1, [], []
            )
        with self.assertRaises(ValueError):
            self.store().read_current_analysis(self.file_id, "dfg")
        with self.assertRaises(ValueError):
            self.store().get_analysis_state(self.file_id, "ast")
        with self.assertRaises(ValueError):
            self.store().get_analysis_state(self.file_id, "ast:Python")

    def test_missing_base_file_is_stale(self) -> None:
        write(self.root / "pkg" / "temp.py", "def delta() -> int:\n    return 4\n")
        incremental_update(self.root, self.database)
        row = query_row(
            self.database,
            "SELECT file_id, source_hash FROM files WHERE path='pkg/temp.py'",
            (),
        )
        temp_id, temp_hash = row["file_id"], row["source_hash"]
        self.store().replace_analysis_for_file(
            temp_id, "ast:python", temp_hash, ANALYZER_VERSIONS["ast:python"],
            synthetic_nodes("d"), synthetic_edges("d"),
        )
        self.assertTrue(
            self.store().freshness_against_base(temp_id, "ast:python")["fresh"]
        )
        (self.root / "pkg" / "temp.py").unlink()
        incremental_update(self.root, self.database)
        verdict = self.store().freshness_against_base(temp_id, "ast:python")
        self.assertFalse(verdict["fresh"])
        self.assertEqual(verdict["reason"], "file_missing_from_base")
        result = self.store().read_current_analysis(temp_id, "ast:python")
        self.assertFalse(result["fresh"])
        self.assertEqual(result["nodes"], [])


if __name__ == "__main__":
    unittest.main()
