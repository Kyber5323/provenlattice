from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice.analysis_ast import analyze_ast_scope
from provenlattice.graph import full_index
from provenlattice.identity import path_id
from provenlattice.incremental import incremental_update
from provenlattice.query import GraphQuery
from provenlattice.storage import SQLiteStorage

SRC_A = """def foo(x):
    if x:
        for item in x:
            bar(item)
    return x
"""

SRC_A_V2 = """def foo(x):
    if x:
        for item in x:
            bar(item)
    return x

def extra(value):
    return zip(value)
"""

SRC_B = """class Widget:
    def render(self, value):
        return format(value)
"""

SRC_C = """def test_foo():
    assert foo(1) == 1
"""

SRC_DUP = """def dup():
    return 1

def dup():
    return 1
"""

SRC_ESCAPE = """def escape():
    return 1
"""

SRC_C_FILE = "int f(void) {\n    return 0;\n}\n"

PAYLOAD_KEYS = {
    "parent_id", "structural_path", "start_byte", "end_byte",
    "start_row", "start_col", "end_row", "end_col",
    "owner_symbol_id", "alignment",
}


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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
    result["boundary_edges"] = [
        dict(row) for row in connection.execute(
            "SELECT id, src_id, dst_id, type, metadata FROM edges "
            "WHERE json_extract(metadata, '$.scope') = 'boundary' ORDER BY id"
        )
    ]
    storage.close()
    return result


def query_row(database: Path, sql: str, params: tuple) -> sqlite3.Row:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(sql, params).fetchone()


def ast_nodes(database: Path, file_id: str) -> list[dict]:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        rows = [
            dict(row) for row in connection.execute(
                "SELECT * FROM analysis_nodes WHERE file_id=? AND feature='ast:python' "
                "ORDER BY analysis_node_id", (file_id,))
        ]
    for row in rows:
        row["payload"] = json.loads(row["payload"])
    return rows


def ast_state_row(database: Path, file_id: str) -> dict | None:
    row = query_row(
        database,
        "SELECT * FROM analysis_state WHERE file_id=? AND feature='ast:python'",
        (file_id,),
    )
    return dict(row) if row else None


class AstAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root / "src" / "a.py", SRC_A)
        write(self.root / "src" / "sub" / "b.py", SRC_B)
        write(self.root / "src" / "extension.c", SRC_C_FILE)
        write(self.root / "src" / "notes.md", "# notes\n")
        write(self.root / "srcfoo" / "escape.py", SRC_ESCAPE)
        write(self.root / "tests" / "c.py", SRC_C)
        write(self.root / "tests" / "dup.py", SRC_DUP)
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)
        self.repo_id = query_row(
            self.database, "SELECT repo_id FROM repositories LIMIT 1", ()
        )["repo_id"]
        self.stores: list = []

    def tearDown(self) -> None:
        for store in self.stores:
            store.storage.close()
        self.temp.cleanup()

    def file_id(self, relative_path: str) -> str:
        return path_id(self.repo_id, "File", relative_path)

    def store(self):
        from provenlattice.analysis import AnalysisStore
        store = AnalysisStore(SQLiteStorage(self.database))
        self.stores.append(store)
        return store

    def test_real_ast_covers_function_body_hierarchy(self) -> None:
        result = analyze_ast_scope(self.root, "src", self.database)
        self.assertEqual(result["materialized"], ["src/a.py", "src/sub/b.py"])
        file_id = self.file_id("src/a.py")
        nodes = ast_nodes(self.database, file_id)
        kinds = {node["kind"] for node in nodes}
        self.assertLessEqual(
            {"function_definition", "if_statement", "for_statement", "call",
             "return_statement", "identifier", "block", "parameters"},
            kinds,
        )
        by_id = {node["analysis_node_id"]: node for node in nodes}
        calls = [node for node in nodes if node["kind"] == "call"]
        self.assertEqual(len(calls), 1)
        ancestor_kinds = set()
        cursor = calls[0]
        while cursor["payload"]["parent_id"] is not None:
            cursor = by_id[cursor["payload"]["parent_id"]]
            ancestor_kinds.add(cursor["kind"])
        self.assertLessEqual(
            {"for_statement", "if_statement", "function_definition"}, ancestor_kinds
        )
        returns = [node for node in nodes if node["kind"] == "return_statement"]
        self.assertEqual(len(returns), 1)
        return_parent = by_id[returns[0]["payload"]["parent_id"]]
        self.assertEqual(return_parent["kind"], "block")
        function_def = [node for node in nodes
                        if node["kind"] == "function_definition"][0]
        self.assertEqual(return_parent["payload"]["parent_id"],
                         function_def["analysis_node_id"])
        self.assertEqual(returns[0]["payload"]["structural_path"],
                         "module/function_definition[0]/block[2]/return_statement[1]")
        root = [node for node in nodes if node["payload"]["parent_id"] is None]
        self.assertEqual(len(root), 1)
        self.assertEqual(root[0]["kind"], "module")

    def test_payload_contract_no_source_text(self) -> None:
        analyze_ast_scope(self.root, "src", self.database)
        for relative in ("src/a.py", "src/sub/b.py"):
            for node in ast_nodes(self.database, self.file_id(relative)):
                self.assertEqual(set(node["payload"]), PAYLOAD_KEYS)
                self.assertEqual(node["feature"], "ast:python")
                self.assertNotIn("text", node["payload"])
        dumped = json.dumps(
            [node["payload"] for node in ast_nodes(self.database, self.file_id("src/a.py"))]
        )
        self.assertNotIn("def foo", dumped)
        self.assertNotIn("bar(", dumped)
        self.assertNotIn("return x", dumped)

    def test_analyze_is_deterministic(self) -> None:
        analyze_ast_scope(self.root, "src", self.database)

        def dump() -> list:
            rows = []
            with closing(sqlite3.connect(self.database)) as connection:
                connection.row_factory = sqlite3.Row
                for row in connection.execute(
                    "SELECT * FROM analysis_state ORDER BY file_id"
                ):
                    item = dict(row)
                    item.pop("updated_at")
                    rows.append(("state", item))
                for row in connection.execute(
                    "SELECT * FROM analysis_nodes ORDER BY analysis_node_id"
                ):
                    rows.append(("node", dict(row)))
            return rows

        first = dump()
        with closing(sqlite3.connect(self.database)) as connection:
            for table in ("analysis_state", "analysis_nodes", "analysis_edges"):
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            connection.commit()
        analyze_ast_scope(self.root, "src", self.database)
        self.assertEqual(dump(), first)

    def test_scope_isolation_and_prefix_trap(self) -> None:
        result = analyze_ast_scope(self.root, "src", self.database)
        self.assertEqual(result["materialized"], ["src/a.py", "src/sub/b.py"])
        self.assertEqual(result["skipped_unsupported"], ["src/extension.c"])
        for outside in ("tests/c.py", "tests/dup.py", "srcfoo/escape.py"):
            self.assertIsNone(ast_state_row(self.database, self.file_id(outside)))
        self.assertEqual(ast_nodes(self.database, self.file_id("srcfoo/escape.py")), [])
        tests_result = analyze_ast_scope(self.root, "tests", self.database)
        self.assertEqual(tests_result["materialized"], ["tests/c.py", "tests/dup.py"])

    def test_invalid_scope_rejected(self) -> None:
        for bad_scope in ("", ".", "  ", "../outside", "/abs", "src/a.py",
                          "src/missing_dir", "C:/windows"):
            with self.assertRaises(ValueError, msg=bad_scope):
                analyze_ast_scope(self.root, bad_scope, self.database)

    def test_cache_reuse_does_not_replace(self) -> None:
        analyze_ast_scope(self.root, "src", self.database)
        before = {relative: ast_state_row(self.database, self.file_id(relative))["updated_at"]
                  for relative in ("src/a.py", "src/sub/b.py")}
        result = analyze_ast_scope(self.root, "src", self.database)
        self.assertEqual(result["reused"], ["src/a.py", "src/sub/b.py"])
        self.assertEqual(result["materialized"], [])
        after = {relative: ast_state_row(self.database, self.file_id(relative))["updated_at"]
                 for relative in ("src/a.py", "src/sub/b.py")}
        self.assertEqual(after, before)

    def test_stale_then_refresh(self) -> None:
        analyze_ast_scope(self.root, "src", self.database)
        file_id = self.file_id("src/a.py")
        old_count = len(ast_nodes(self.database, file_id))
        write(self.root / "src" / "a.py", SRC_A_V2)
        incremental_update(self.root, self.database)
        stale = self.store().read_current_analysis(file_id, "ast:python")
        self.assertFalse(stale["fresh"])
        self.assertIn("source_hash_changed", stale["reason"])
        result = analyze_ast_scope(self.root, "src", self.database)
        self.assertEqual(result["materialized"], ["src/a.py"])
        self.assertEqual(result["reused"], ["src/sub/b.py"])
        refreshed = self.store().read_current_analysis(file_id, "ast:python")
        self.assertTrue(refreshed["fresh"])
        new_hash = query_row(
            self.database,
            "SELECT source_hash FROM file_state WHERE file_id=?", (file_id,),
        )["source_hash"]
        self.assertEqual(refreshed["state"]["source_hash"], new_hash)
        nodes = ast_nodes(self.database, file_id)
        self.assertGreater(len(nodes), old_count)
        paths = {node["payload"]["structural_path"] for node in nodes}
        self.assertIn("module/function_definition[1]", paths)

    def test_zero_impact_on_base_snapshot(self) -> None:
        before = base_snapshot(self.database)
        analyze_ast_scope(self.root, "src", self.database)
        analyze_ast_scope(self.root, "tests", self.database)
        self.assertEqual(base_snapshot(self.database), before)
        with GraphQuery(self.database) as query:
            self.assertEqual(len(query.find_symbol("foo")["data"]), 1)

    def test_unsupported_language_skipped(self) -> None:
        result = analyze_ast_scope(self.root, "src", self.database)
        self.assertEqual(result["skipped_unsupported"], ["src/extension.c"])
        self.assertNotIn("src/extension.c", result["materialized"])
        self.assertIsNone(ast_state_row(self.database, self.file_id("src/extension.c")))
        self.assertEqual(
            ast_nodes(self.database, self.file_id("src/extension.c")), []
        )

    def test_alignment_fail_closed(self) -> None:
        analyze_ast_scope(self.root, "tests", self.database)
        analyze_ast_scope(self.root, "src", self.database)
        dup_id = self.file_id("tests/dup.py")
        base_foo = query_row(
            self.database,
            "SELECT id FROM nodes WHERE file_id=? AND kind='Function' AND name='foo'",
            (self.file_id("src/a.py"),),
        )["id"]
        foo_nodes = ast_nodes(self.database, self.file_id("src/a.py"))
        foo_def = [node for node in foo_nodes
                   if node["kind"] == "function_definition"][0]
        self.assertEqual(foo_def["payload"]["alignment"], "resolved")
        self.assertEqual(foo_def["payload"]["owner_symbol_id"], base_foo)
        render_def = [node for node in ast_nodes(self.database, self.file_id("src/sub/b.py"))
                      if node["kind"] == "function_definition"][0]
        self.assertEqual(render_def["payload"]["alignment"], "resolved")
        self.assertIsNotNone(render_def["payload"]["owner_symbol_id"])
        dup_defs = [node for node in ast_nodes(self.database, dup_id)
                    if node["kind"] == "function_definition"]
        self.assertEqual(len(dup_defs), 2)
        alignments = sorted(
            (node["payload"]["alignment"], node["payload"]["owner_symbol_id"] is None)
            for node in dup_defs
        )
        self.assertEqual(
            alignments, [("resolved", False), ("unmatched", True)]
        )
        non_defs = [node for node in foo_nodes
                    if node["kind"] not in {"function_definition"}]
        self.assertTrue(
            all(node["payload"]["alignment"] == "unmatched"
                and node["payload"]["owner_symbol_id"] is None
                for node in non_defs)
        )

    def test_file_not_in_base_graph_skipped(self) -> None:
        write(self.root / "src" / "later.py", "def later():\n    return 5\n")
        result = analyze_ast_scope(self.root, "src", self.database)
        self.assertEqual(result["skipped_not_in_base"], ["src/later.py"])
        self.assertIsNone(ast_state_row(self.database, self.file_id("src/later.py")))


if __name__ == "__main__":
    unittest.main()
