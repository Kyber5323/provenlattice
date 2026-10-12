from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice.analysis import (
    ANALYZER_VERSIONS,
    MAX_ANALYSIS_NODE_PAYLOAD_BYTES,
    AnalysisNode,
    AnalysisStore,
    PayloadTooLargeError,
    analyzer_contract,
    analyzer_id,
    encoded_payload_size,
    register_analyzer,
)
from provenlattice.analysis_ast import ANALYZER as PYTHON_AST_ANALYZER
from provenlattice.analysis_ast import PYTHON_AST_CONTRACT, analyze_ast_scope
from provenlattice.graph import full_index
from provenlattice.incremental import incremental_update
from provenlattice.storage import SQLiteStorage

SRC_A = """def foo(x):
    if x:
        for item in x:
            bar(item)
    return x
"""

SRC_DEEP = "value = " + "(" * 150 + "1" + ")" * 150 + "\n"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def synthetic_nodes(tag: str, payload: dict | None = None) -> list[AnalysisNode]:
    base = {"tag": tag} if payload is None else payload
    return [AnalysisNode(f"anode:{tag}:1", "test_ast_node", dict(base))]


def base_snapshot(database: Path) -> dict:
    # raw_references lives in the cold store (stage 7A); only a storage-open
    # connection carries the TEMP VIEW that re-exposes it to readers.
    storage = SQLiteStorage(database)
    connection = storage.connection
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
    storage.close()
    return result


def query_row(database: Path, sql: str, params: tuple) -> sqlite3.Row:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(sql, params).fetchone()


def ast_state_row(database: Path, file_id: str) -> dict | None:
    row = query_row(
        database,
        "SELECT * FROM analysis_state WHERE file_id=? AND feature='ast:python'",
        (file_id,),
    )
    return dict(row) if row else None


def ast_node_rows(database: Path, file_id: str) -> list[dict]:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row) for row in connection.execute(
                "SELECT * FROM analysis_nodes WHERE file_id=? AND feature='ast:python' "
                "ORDER BY analysis_node_id", (file_id,))
        ]


class AnalyzerIdentityTests(unittest.TestCase):
    def test_analyzer_helpers_and_registry(self) -> None:
        self.assertEqual(analyzer_id("ast", "python"), "ast:python")
        self.assertEqual(
            analyzer_contract("ast", "python", 1), "python-ast-v1"
        )
        self.assertEqual(ANALYZER_VERSIONS["ast:python"], 1)
        register_analyzer("ast:zlang", 3)
        self.assertEqual(ANALYZER_VERSIONS["ast:zlang"], 3)
        register_analyzer("ast:zlang", 3)
        with self.assertRaises(ValueError):
            register_analyzer("ast:zlang", 4)
        with self.assertRaises(ValueError):
            register_analyzer("dfg:python", 1)
        with self.assertRaises(ValueError):
            analyzer_id("ast", "")
        del ANALYZER_VERSIONS["ast:zlang"]

    def test_analyze_result_carries_analyzer_identity(self) -> None:
        result = analyze_ast_scope(self.root, "src", self.database)
        self.assertEqual(result["analyzer"], PYTHON_AST_ANALYZER)
        self.assertEqual(result["contract"], PYTHON_AST_CONTRACT)
        self.assertEqual(result["contract"], "python-ast-v1")
        state = ast_state_row(self.database, self.fid("src/a.py"))
        metadata = json.loads(state["metadata"])
        self.assertEqual(metadata["analyzer"], "ast:python")
        self.assertEqual(metadata["contract"], "python-ast-v1")
        self.assertEqual(metadata["language"], "python")

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root / "src" / "a.py", SRC_A)
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)
        self.repo_id = query_row(
            self.database, "SELECT repo_id FROM repositories LIMIT 1", ()
        )["repo_id"]
        row = query_row(
            self.database,
            "SELECT file_id, source_hash FROM files WHERE path='src/a.py'",
            (),
        )
        self.file_id = row["file_id"]
        self.hash_a = row["source_hash"]
        self.stores: list[AnalysisStore] = []

    def tearDown(self) -> None:
        for store in self.stores:
            store.storage.close()
        self.temp.cleanup()

    def fid(self, relative_path: str) -> str:
        from provenlattice.identity import path_id
        return path_id(self.repo_id, "File", relative_path)

    def store(self) -> AnalysisStore:
        store = AnalysisStore(SQLiteStorage(self.database))
        self.stores.append(store)
        return store

    def test_g2_same_analyzer_version_upgrade_invalidates(self) -> None:
        version = ANALYZER_VERSIONS["ast:python"]
        self.store().replace_analysis_for_file(
            self.file_id, "ast:python", self.hash_a, version,
            synthetic_nodes("v1"), [],
        )
        self.assertTrue(
            self.store().is_analysis_fresh(self.file_id, "ast:python", self.hash_a, version)
        )
        self.store().replace_analysis_for_file(
            self.file_id, "ast:python", self.hash_a, version + 1,
            synthetic_nodes("v2"), [],
        )
        self.assertTrue(
            self.store().is_analysis_fresh(
                self.file_id, "ast:python", self.hash_a, version + 1)
        )
        self.assertFalse(
            self.store().is_analysis_fresh(
                self.file_id, "ast:python", self.hash_a, version)
        )
        old_read = self.store().read_analysis_for_file(
            self.file_id, "ast:python", source_hash=self.hash_a, feature_version=version
        )
        self.assertFalse(old_read["fresh"])
        self.assertEqual(old_read["nodes"], [])
        rows = ast_node_rows(self.database, self.file_id)
        self.assertEqual(
            {row["analysis_node_id"] for row in rows}, {"anode:v2:1"}
        )

    def test_g2_cross_analyzer_isolation(self) -> None:
        version = ANALYZER_VERSIONS["ast:python"]
        self.store().replace_analysis_for_file(
            self.file_id, "ast:python", self.hash_a, version,
            synthetic_nodes("py"), [],
        )
        self.assertFalse(
            self.store().is_analysis_fresh(
                self.file_id, "ast:zlang", self.hash_a, version)
        )
        self.store().replace_analysis_for_file(
            self.file_id, "ast:zlang", self.hash_a, version,
            synthetic_nodes("z"), [],
        )
        py_read = self.store().read_analysis_for_file(
            self.file_id, "ast:python", source_hash=self.hash_a, feature_version=version
        )
        z_read = self.store().read_analysis_for_file(
            self.file_id, "ast:zlang", source_hash=self.hash_a, feature_version=version
        )
        self.assertEqual(py_read["nodes"][0]["analysis_node_id"], "anode:py:1")
        self.assertEqual(z_read["nodes"][0]["analysis_node_id"], "anode:z:1")
        self.store().delete_analysis_for_file(self.file_id, "ast:zlang")
        self.assertTrue(
            self.store().is_analysis_fresh(
                self.file_id, "ast:python", self.hash_a, version)
        )
        self.assertIsNotNone(ast_state_row(self.database, self.file_id))

    def test_g3_unknown_analyzer_fail_closed(self) -> None:
        version = ANALYZER_VERSIONS["ast:python"]
        with self.assertRaises(ValueError):
            self.store().read_current_analysis(self.file_id, "ast:python-v999")
        with self.assertRaises(ValueError):
            self.store().freshness_against_base(self.file_id, "ast:python-v999")
        self.store().replace_analysis_for_file(
            self.file_id, "ast:python", self.hash_a, 999,
            synthetic_nodes("future"), [],
        )
        self.assertFalse(
            self.store().is_analysis_fresh(
                self.file_id, "ast:python", self.hash_a, version)
        )
        result = self.store().read_current_analysis(self.file_id, "ast:python")
        self.assertFalse(result["fresh"])
        self.assertIn("feature_version_changed", result["reason"])
        self.assertEqual(result["nodes"], [])
        self.store().replace_analysis_for_file(
            self.file_id, "ast:python", self.hash_a, version,
            synthetic_nodes("v1"), [],
        )
        self.assertTrue(
            self.store().read_current_analysis(self.file_id, "ast:python")["fresh"]
        )

    def test_g4_payload_boundary_exact(self) -> None:
        store = self.store()
        version = ANALYZER_VERSIONS["ast:python"]
        base_size = encoded_payload_size({"pad": ""})
        exact = {"pad": "x" * (MAX_ANALYSIS_NODE_PAYLOAD_BYTES - base_size)}
        self.assertEqual(encoded_payload_size(exact), MAX_ANALYSIS_NODE_PAYLOAD_BYTES)
        store.replace_analysis_for_file(
            self.file_id, "ast:python", self.hash_a, version,
            [AnalysisNode("anode:exact:1", "test_ast_node", exact)], [],
        )
        self.assertIsNotNone(ast_state_row(self.database, self.file_id))
        over = {"pad": "x" * (MAX_ANALYSIS_NODE_PAYLOAD_BYTES - base_size + 1)}
        with self.assertRaises(PayloadTooLargeError) as caught:
            store.replace_analysis_for_file(
                self.file_id, "ast:python", self.hash_a, version,
                [AnalysisNode("anode:over:1", "test_ast_node", over)], [],
            )
        self.assertEqual(caught.exception.size, MAX_ANALYSIS_NODE_PAYLOAD_BYTES + 1)
        self.assertEqual(caught.exception.limit, MAX_ANALYSIS_NODE_PAYLOAD_BYTES)
        self.assertEqual(caught.exception.item_id, "anode:over:1")

    def test_g5_store_level_oversize_is_atomic(self) -> None:
        store = self.store()
        version = ANALYZER_VERSIONS["ast:python"]
        store.replace_analysis_for_file(
            self.file_id, "ast:python", self.hash_a, version,
            synthetic_nodes("keep"), [],
        )
        before_state = ast_state_row(self.database, self.file_id)
        before_rows = ast_node_rows(self.database, self.file_id)
        oversized = {"pad": "x" * (MAX_ANALYSIS_NODE_PAYLOAD_BYTES + 1)}
        with self.assertRaises(PayloadTooLargeError):
            store.replace_analysis_for_file(
                self.file_id, "ast:python", self.hash_a, version,
                [AnalysisNode("anode:new:1", "test_ast_node", oversized)], [],
            )
        self.assertEqual(ast_state_row(self.database, self.file_id), before_state)
        self.assertEqual(ast_node_rows(self.database, self.file_id), before_rows)

    def test_g5_scope_level_oversize_file_fails_closed_and_recovers(self) -> None:
        analyze_ast_scope(self.root, "src", self.database)
        fresh = self.store().read_current_analysis(self.file_id, "ast:python")
        self.assertTrue(fresh["fresh"])
        original_rows = ast_node_rows(self.database, self.file_id)
        write(self.root / "src" / "a.py", SRC_DEEP)
        incremental_update(self.root, self.database)
        state_before_failure = ast_state_row(self.database, self.file_id)
        result = analyze_ast_scope(self.root, "src", self.database)
        self.assertEqual(result["skipped_payload_too_large"], ["src/a.py"])
        self.assertEqual(result["materialized"], [])
        self.assertEqual(ast_state_row(self.database, self.file_id), state_before_failure)
        self.assertEqual(len(ast_node_rows(self.database, self.file_id)),
                         len(original_rows))
        stale = self.store().read_current_analysis(self.file_id, "ast:python")
        self.assertFalse(stale["fresh"])
        write(self.root / "src" / "a.py", SRC_A)
        incremental_update(self.root, self.database)
        recovered = analyze_ast_scope(self.root, "src", self.database)
        self.assertEqual(recovered["reused"], ["src/a.py"])
        self.assertEqual(recovered["materialized"], [])
        self.assertTrue(
            self.store().read_current_analysis(self.file_id, "ast:python")["fresh"]
        )
        self.assertEqual(
            [row["analysis_node_id"] for row in ast_node_rows(self.database, self.file_id)],
            [row["analysis_node_id"] for row in original_rows],
        )

    def test_g6_base_graph_zero_impact(self) -> None:
        before = base_snapshot(self.database)
        analyze_ast_scope(self.root, "src", self.database)
        store = self.store()
        store.replace_analysis_for_file(
            self.file_id, "ast:zlang", self.hash_a, 1,
            synthetic_nodes("z"), [],
        )
        oversized = {"pad": "x" * (MAX_ANALYSIS_NODE_PAYLOAD_BYTES + 1)}
        with self.assertRaises(PayloadTooLargeError):
            store.replace_analysis_for_file(
                self.file_id, "ast:zlang", self.hash_a, 1,
                [AnalysisNode("anode:over:1", "test_ast_node", oversized)], [],
            )
        self.assertEqual(base_snapshot(self.database), before)


if __name__ == "__main__":
    unittest.main()
