from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice.analysis_query import query_analysis
from provenlattice.cli import build_parser, run
from provenlattice.graph import full_index
from provenlattice.identity import path_id
from provenlattice.incremental import incremental_update
from provenlattice.orchestrator import AnalysisRequestError, analyze_scope

SRC_MAIN = """def foo(x):
    if x:
        bar(x)
    return x

def other():
    return 2
"""

SRC_MAIN_V2 = """def foo(x):
    while x:
        x = x - 1
    return x

def other():
    return 2
"""

SRC_DUP = """def foo():
    return 1

def foo():
    return 2
"""

SRC_TWIN = """def foo():
    return 9
"""

SRC_C_FILE = "int f(void) {\n    return 0;\n}\n"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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
    connection.close()
    return result


def analysis_snapshot(database: Path) -> dict:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='analysis_state'"
        ).fetchone()
        if exists is None:
            return {"state": [], "nodes": [], "edges": [], "tables": False}
        return {
            "tables": True,
            "state": [dict(row) for row in connection.execute(
                "SELECT * FROM analysis_state ORDER BY 1")],
            "nodes": [dict(row) for row in connection.execute(
                "SELECT * FROM analysis_nodes ORDER BY 1")],
            "edges": [dict(row) for row in connection.execute(
                "SELECT * FROM analysis_edges ORDER BY 1")],
        }


def query_row(database: Path, sql: str, params: tuple) -> sqlite3.Row:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(sql, params).fetchone()


def base_symbol_id(database: Path, file_id: str, name: str) -> str:
    return query_row(
        database,
        "SELECT id FROM nodes WHERE file_id=? AND kind='Function' AND name=?",
        (file_id, name),
    )["id"]


class QueryAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root / "src" / "main.py", SRC_MAIN)
        write(self.root / "src" / "dup.py", SRC_DUP)
        write(self.root / "src" / "twin.py", SRC_TWIN)
        write(self.root / "src" / "b.c", SRC_C_FILE)
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)
        self.repo_id = query_row(
            self.database, "SELECT repo_id FROM repositories LIMIT 1", ()
        )["repo_id"]
        self.fids = {
            relative: path_id(self.repo_id, "File", relative)
            for relative in ("src/main.py", "src/dup.py", "src/twin.py")
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self) -> dict:
        return analyze_scope(self.root, "src", "ast,cfg", self.database)

    def symbol(self, relative: str, name: str = "foo") -> str:
        return base_symbol_id(self.database, self.fids[relative], name)

    def test_g1_query_real_ast_from_store(self) -> None:
        self.build()
        result = query_analysis(
            self.root, "ast", symbol_id=self.symbol("src/main.py"),
            database=self.database,
        )
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["root"]["kind"], "function_definition")
        kinds = self.tree_kinds(result["root"])
        self.assertIn("if_statement", kinds)
        self.assertIn("block", kinds)
        self.assertIn("call", kinds)
        write(self.root / "src" / "main.py", SRC_MAIN_V2)
        after_edit = query_analysis(
            self.root, "ast", symbol_id=self.symbol("src/main.py"),
            database=self.database,
        )
        self.assertEqual(after_edit["status"], "available")
        kinds_after = self.tree_kinds(after_edit["root"])
        self.assertIn("if_statement", kinds_after)
        self.assertNotIn("while_statement", kinds_after)

    def tree_kinds(self, tree: dict) -> set:
        kinds = {tree["kind"]}
        for child in tree["children"]:
            kinds |= self.tree_kinds(child)
        return kinds

    def test_g2_query_real_cfg_matches_store(self) -> None:
        self.build()
        result = query_analysis(
            self.root, "cfg", symbol_id=self.symbol("src/main.py"),
            database=self.database,
        )
        self.assertEqual(result["status"], "available")
        self.assertIsNotNone(result["entry_id"])
        self.assertIsNotNone(result["exit_id"])
        kinds = {node["kind"] for node in result["nodes"]}
        self.assertEqual(
            kinds, {"entry", "exit", "condition", "callsite", "return", "merge"})
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            stored_nodes = {
                row["analysis_node_id"]
                for row in connection.execute(
                    "SELECT * FROM analysis_nodes WHERE feature='cfg:python' "
                    "AND json_extract(payload, '$.owner_symbol_id')=?",
                    (self.symbol("src/main.py"),))
            }
        self.assertEqual({node["analysis_node_id"] for node in result["nodes"]},
                         stored_nodes)
        callsites = [node for node in result["nodes"] if node["kind"] == "callsite"]
        self.assertTrue(all(
            node["payload"]["metadata"]["target_scope"] in {"internal", "external", "unknown"}
            for node in callsites))

    def test_g3_missing_analysis_not_built(self) -> None:
        before = base_snapshot(self.database)
        result = query_analysis(
            self.root, "cfg", symbol_id=self.symbol("src/main.py"),
            database=self.database,
        )
        self.assertEqual(result["status"], "not_built")
        self.assertEqual(result["reason"], "analysis_not_built")
        self.assertEqual(result["nodes"], [])
        self.assertEqual(base_snapshot(self.database), before)
        with closing(sqlite3.connect(self.database)) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("analysis_state", tables)

    def test_g4_stale_fail_closed(self) -> None:
        self.build()
        rows_before = analysis_snapshot(self.database)
        write(self.root / "src" / "main.py", SRC_MAIN_V2)
        incremental_update(self.root, self.database)
        result = query_analysis(
            self.root, "cfg", symbol_id=self.symbol("src/main.py"),
            database=self.database,
        )
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["nodes"], [])
        self.assertEqual(result["edges"], [])
        after = analysis_snapshot(self.database)
        self.assertEqual(after, rows_before)

    def test_g5_refresh_restores_available(self) -> None:
        self.build()
        before_count = len(analysis_snapshot(self.database)["nodes"])
        write(self.root / "src" / "main.py", SRC_MAIN_V2)
        incremental_update(self.root, self.database)
        analyze_scope(self.root, "src", "cfg", self.database)
        result = query_analysis(
            self.root, "cfg", symbol_id=self.symbol("src/main.py"),
            database=self.database,
        )
        self.assertEqual(result["status"], "available")
        after_count = len(analysis_snapshot(self.database)["nodes"])
        self.assertGreater(after_count, before_count)

    def test_g6_exact_symbol_identity(self) -> None:
        self.build()
        main_result = query_analysis(
            self.root, "cfg", symbol_id=self.symbol("src/main.py"),
            database=self.database,
        )
        self.assertTrue(all(
            node["payload"]["structural_path"].startswith("module/function_definition[0]")
            for node in main_result["nodes"]))
        twin_result = query_analysis(
            self.root, "cfg", symbol_id=self.symbol("src/twin.py"),
            database=self.database,
        )
        self.assertEqual(twin_result["status"], "available")
        self.assertEqual(twin_result["file_id"], self.fids["src/twin.py"])
        dup_result = query_analysis(
            self.root, "cfg", symbol_id=self.symbol("src/dup.py"),
            database=self.database,
        )
        self.assertEqual(dup_result["status"], "available")
        self.assertEqual(len(dup_result["nodes"]), 3)
        self.assertTrue(all(
            node["payload"]["structural_path"].startswith("module/function_definition[0]")
            for node in dup_result["nodes"]))
        missing = query_analysis(
            self.root, "cfg", symbol_id="symbol:00000000000000000000000000000000",
            database=self.database,
        )
        self.assertEqual(missing["status"], "not_available")
        self.assertEqual(missing["reason"], "symbol_not_found")

    def test_g7_file_isolation(self) -> None:
        self.build()
        result = query_analysis(
            self.root, "cfg", file_path="src/main.py", database=self.database,
        )
        self.assertEqual(result["status"], "available")
        self.assertTrue(all(node["file_id"] == self.fids["src/main.py"]
                            for node in result["nodes"]))
        self.assertTrue(all(edge["file_id"] == self.fids["src/main.py"]
                            for edge in result["edges"]))

    def test_g8_ast_tree_deterministic(self) -> None:
        self.build()
        first = query_analysis(
            self.root, "ast", symbol_id=self.symbol("src/main.py"),
            database=self.database,
        )
        second = query_analysis(
            self.root, "ast", symbol_id=self.symbol("src/main.py"),
            database=self.database,
        )
        self.assertEqual(json.dumps(first["root"], sort_keys=True),
                         json.dumps(second["root"], sort_keys=True))

    def test_g9_cfg_integrity_corrupt(self) -> None:
        self.build()
        foo_id = self.symbol("src/main.py")
        entry_row = query_row(
            self.database,
            "SELECT * FROM analysis_nodes WHERE feature='cfg:python' "
            "AND kind='entry' AND json_extract(payload, '$.owner_symbol_id')=?",
            (foo_id,),
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM analysis_nodes WHERE analysis_node_id=?",
                               (entry_row["analysis_node_id"],))
            connection.commit()
        result = query_analysis(self.root, "cfg", symbol_id=foo_id,
                                database=self.database)
        self.assertEqual(result["status"], "corrupt")
        self.assertEqual(result["reason"], "entry_missing")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("INSERT INTO analysis_nodes VALUES(?,?,?,?,?)",
                               tuple(entry_row))
            connection.commit()
        callsite_row = query_row(
            self.database,
            "SELECT * FROM analysis_nodes WHERE feature='cfg:python' "
            "AND kind='callsite' AND json_extract(payload, '$.owner_symbol_id')=?",
            (foo_id,),
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM analysis_nodes WHERE analysis_node_id=?",
                               (callsite_row["analysis_node_id"],))
            connection.commit()
        result = query_analysis(self.root, "cfg", symbol_id=foo_id,
                                database=self.database)
        self.assertEqual(result["status"], "corrupt")
        self.assertEqual(result["reason"], "edge_endpoint_missing")

    def test_g10_read_only_across_statuses(self) -> None:
        self.build()
        base_before = base_snapshot(self.database)
        analysis_before = analysis_snapshot(self.database)
        available = query_analysis(
            self.root, "cfg", file_path="src/main.py", database=self.database)
        self.assertEqual(available["status"], "available")
        c_ast = query_analysis(
            self.root, "ast", file_path="src/b.c", database=self.database)
        self.assertEqual(c_ast["status"], "available")
        self.assertEqual(c_ast["analyzer"], "ast:c")
        self.assertEqual(base_snapshot(self.database), base_before)
        self.assertEqual(analysis_snapshot(self.database), analysis_before)

    def test_g11_invalid_inputs_fail_closed(self) -> None:
        base_before = base_snapshot(self.database)
        analysis_before = analysis_snapshot(self.database)
        result = query_analysis(self.root, "dfg", symbol_id=self.symbol("src/main.py"),
                                database=self.database)
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["reason"], "unsupported_feature")
        missing = query_analysis(
            self.root, "cfg", symbol_id="symbol:deadbeef", database=self.database)
        self.assertEqual(missing["status"], "not_available")
        self.assertEqual(missing["reason"], "symbol_not_found")
        with self.assertRaises(AnalysisRequestError):
            query_analysis(self.root, "cfg", database=self.database)
        with self.assertRaises(AnalysisRequestError):
            query_analysis(self.root, "cfg", file_path="src/main.py",
                           symbol_id=self.symbol("src/main.py"),
                           database=self.database)
        self.assertEqual(base_snapshot(self.database), base_before)
        self.assertEqual(analysis_snapshot(self.database), analysis_before)

    def test_cli_analysis_command(self) -> None:
        self.build()
        args = build_parser().parse_args([
            "analysis", str(self.root), "--feature", "cfg",
            "--symbol-id", self.symbol("src/main.py"), "--database", str(self.database),
        ])
        result = run(args)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["coverage"],
                         {"type": "scoped", "scope": "src"})


if __name__ == "__main__":
    unittest.main()
