from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice.analysis_ast import analyze_ast_scope
from provenlattice.cli import build_parser, run
from provenlattice.graph import full_index
from provenlattice.incremental import incremental_update
from provenlattice.orchestrator import AnalysisRequestError, analyze_scope

SRC_A_V1 = """def alpha():
    return 1
"""

SRC_A_V2 = """def alpha():
    return 1

def alpha_extra(value):
    return extra(value)
"""

SRC_B = """class Widget:
    def render(self):
        return 2
"""

SRC_C = """def gamma():
    return 3
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


def analysis_state_rows(database: Path) -> list[dict]:
    with closing(sqlite3.connect(database)) as connection:
        exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='analysis_state'"
        ).fetchone()
        if exists is None:
            return []
        connection.row_factory = sqlite3.Row
        return [
            dict(row) for row in connection.execute("SELECT * FROM analysis_state")
        ]


def state_updated_at(database: Path, file_id: str, feature: str) -> str | None:
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            "SELECT updated_at FROM analysis_state WHERE file_id=? AND feature=?",
            (file_id, feature),
        ).fetchone()
    return row[0] if row else None


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root / "src" / "a.py", SRC_A_V1)
        write(self.root / "src" / "sub" / "b.py", SRC_B)
        write(self.root / "tests" / "c.py", SRC_C)
        write(self.root / "src" / "b.c", SRC_C_FILE)
        write(self.root / "src" / "README.md", "# notes\n")
        write(self.root / "src" / "x.js", "function f() {}\n")
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)
        self.stores: list = []

    def tearDown(self) -> None:
        for store in self.stores:
            store.storage.close()
        self.temp.cleanup()

    def store(self):
        from provenlattice.analysis import AnalysisStore
        from provenlattice.storage import SQLiteStorage
        store = AnalysisStore(SQLiteStorage(self.database))
        self.stores.append(store)
        return store

    def cli_analyze(self, scope: str, features: str, repo=None, database=None) -> dict:
        args = build_parser().parse_args([
            "analyze", str(repo or self.root), "--scope", scope,
            "--features", features, "--database", str(database or self.database),
        ])
        return run(args)

    def per_file(self, result: dict, path: str) -> dict:
        matches = [item for item in result["per_file"] if item["path"] == path]
        self.assertEqual(len(matches), 1, path)
        return matches[0]

    def test_g1_ast_only(self) -> None:
        result = analyze_scope(self.root, "src", "ast", self.database)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["requested_features"], ["ast"])
        self.assertEqual(set(result["features"]), {"ast"})
        self.assertEqual(result["features"]["ast"]["materialized"],
                         ["src/a.py", "src/b.c", "src/sub/b.py"])
        self.assertEqual(result["features"]["ast"]["skipped"]["unsupported_language"],
                         ["src/README.md", "src/x.js"])
        cfg_rows = [row for row in analysis_state_rows(self.database)
                    if row["feature"].startswith("cfg")]
        self.assertEqual(cfg_rows, [])
        record = self.per_file(result, "src/a.py")
        self.assertEqual(record["ast_status"], "materialized")
        self.assertEqual(record["cfg_status"], "not_requested")

    def test_g2_ast_and_cfg(self) -> None:
        result = analyze_scope(self.root, "src", "ast,cfg", self.database)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(set(result["features"]), {"ast", "cfg"})
        self.assertEqual(result["features"]["ast"]["materialized"],
                         ["src/a.py", "src/b.c", "src/sub/b.py"])
        self.assertEqual(result["features"]["cfg"]["materialized"],
                         ["src/a.py", "src/sub/b.py"])
        self.assertEqual(result["features"]["cfg"]["skipped"]["not_implemented"],
                         ["src/b.c"])
        self.assertEqual(result["files"],
                         {"seen": 5, "analyzed": 2, "reused": 0, "skipped": 3})
        self.assertEqual(len(analysis_state_rows(self.database)), 5)

    def test_g3_cfg_only_materializes_ast_dependency(self) -> None:
        result = analyze_scope(self.root, "src", "cfg", self.database)
        self.assertEqual(result["requested_features"], ["cfg"])
        self.assertEqual(result["executed_features"], ["ast", "cfg"])
        self.assertEqual(result["dependencies"]["ast"]["reason"], "required_by_cfg")
        self.assertEqual(result["dependencies"]["ast"]["action"], "materialized")
        self.assertEqual(result["features"]["cfg"]["materialized"],
                         ["src/a.py", "src/sub/b.py"])
        self.assertEqual(set(result["features"]["cfg"]["skipped"]),
                         {"not_implemented", "unsupported_language"})
        features = [row["feature"] for row in analysis_state_rows(self.database)]
        self.assertEqual(sorted(features), ["ast:python", "ast:python", "cfg:python", "cfg:python"])
        self.assertEqual(self.per_file(result, "src/a.py")["cfg_status"], "materialized")

    def test_g4_cfg_only_reuses_fresh_ast(self) -> None:
        analyze_ast_scope(self.root, "src", self.database)
        before = state_updated_at(self.database, self.fid(), "ast:python")
        result = analyze_scope(self.root, "src", "cfg", self.database)
        self.assertEqual(result["dependencies"]["ast"]["action"], "reused")
        self.assertEqual(result["features"]["cfg"]["materialized"],
                         ["src/a.py", "src/sub/b.py"])
        self.assertEqual(state_updated_at(self.database, self.fid(), "ast:python"), before)

    def fid(self) -> str:
        from provenlattice.identity import path_id
        from provenlattice.storage import SQLiteStorage
        with SQLiteStorage(self.database) as storage:
            repo_id = storage.repository()["repo_id"]
        return path_id(repo_id, "File", "src/a.py")

    def test_g5_incremental_change_rebuilds_both(self) -> None:
        analyze_scope(self.root, "src", "ast,cfg", self.database)
        write(self.root / "src" / "a.py", SRC_A_V2)
        incremental = incremental_update(self.root, self.database)
        self.assertTrue(incremental["changed"])
        result = analyze_scope(self.root, "src", "cfg", self.database)
        self.assertEqual(result["dependencies"]["ast"]["action"], "materialized")
        self.assertIn("src/a.py", result["features"]["cfg"]["materialized"])
        store = self.store()
        self.assertTrue(store.read_current_analysis(self.fid(), "ast:python")["fresh"])
        self.assertTrue(store.read_current_analysis(self.fid(), "cfg:python")["fresh"])

    def test_g6_scope_isolation(self) -> None:
        result = analyze_scope(self.root, "src", "ast,cfg", self.database)
        self.assertTrue(all(item["path"].startswith("src/")
                            for item in result["per_file"]))
        file_id = self.fid()
        from provenlattice.identity import path_id
        from provenlattice.storage import SQLiteStorage
        with SQLiteStorage(self.database) as storage:
            repo_id = storage.repository()["repo_id"]
        tests_id = path_id(repo_id, "File", "tests/c.py")
        self.assertEqual([
            row for row in analysis_state_rows(self.database) if row["file_id"] == tests_id
        ], [])

    def test_g7_unsupported_language_accounted(self) -> None:
        result = analyze_scope(self.root, "src", "ast", self.database)
        readme = self.per_file(result, "src/README.md")
        self.assertIsNone(readme["language"])
        self.assertEqual(readme["ast_status"], "skipped")
        self.assertIn("ast:unsupported_language", readme["reasons"])
        c_file = self.per_file(result, "src/b.c")
        self.assertEqual(c_file["language"], "c")
        self.assertEqual(c_file["ast_status"], "materialized")
        self.assertNotIn("ast:unsupported_language", c_file["reasons"])
        self.assertEqual(result["files"],
                         {"seen": 5, "analyzed": 3, "reused": 0, "skipped": 2})
        self.assertEqual(result["status"], "complete")

    def test_g8_unknown_feature_fail_closed(self) -> None:
        with self.assertRaises(AnalysisRequestError) as raised:
            analyze_scope(self.root, "src", "ast,dfg", self.database)
        self.assertEqual(raised.exception.code, "unknown_feature")
        with self.assertRaises(AnalysisRequestError):
            analyze_scope(self.root, "src", "ast,foo", self.database)
        result = self.cli_analyze("src", "ast,dfg")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "unknown_feature")
        self.assertEqual(analysis_state_rows(self.database), [])

    def test_g9_invalid_scope_fail_closed(self) -> None:
        before = base_snapshot(self.database)
        for bad_scope in ("../outside", "src/a.py", "", "/abs"):
            with self.assertRaises(AnalysisRequestError) as raised:
                analyze_scope(self.root, bad_scope, "ast", self.database)
            self.assertEqual(raised.exception.code, "invalid_scope", bad_scope)
        result = self.cli_analyze("../outside", "ast")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "invalid_scope")
        self.assertEqual(base_snapshot(self.database), before)
        self.assertEqual(analysis_state_rows(self.database), [])

    def test_g10_missing_base_graph(self) -> None:
        fresh = Path(self.temp.name) / "fresh-repo"
        write(fresh / "pkg" / "m.py", "def solo():\n    return 1\n")
        fresh_db = Path(self.temp.name) / "fresh.db"
        with self.assertRaises(AnalysisRequestError) as raised:
            analyze_scope(fresh, "pkg", "ast", fresh_db)
        self.assertEqual(raised.exception.code, "base_graph_required")
        result = self.cli_analyze("pkg", "ast", repo=fresh, database=fresh_db)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "base_graph_required")
        self.assertEqual(analysis_state_rows(fresh_db), [])

    def test_g11_repeat_execution_cache(self) -> None:
        analyze_scope(self.root, "src", "ast,cfg", self.database)
        before = {
            feature: state_updated_at(self.database, self.fid(), feature)
            for feature in ("ast:python", "cfg:python")
        }
        result = analyze_scope(self.root, "src", "ast,cfg", self.database)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["files"],
                         {"seen": 5, "analyzed": 0, "reused": 2, "skipped": 3})
        self.assertEqual(result["features"]["ast"]["reused"],
                         ["src/a.py", "src/b.c", "src/sub/b.py"])
        self.assertEqual(result["features"]["cfg"]["reused"],
                         ["src/a.py", "src/sub/b.py"])
        for feature in ("ast", "cfg"):
            self.assertEqual(result["features"][feature]["materialized"], [])
        after = {
            feature: state_updated_at(self.database, self.fid(), feature)
            for feature in ("ast:python", "cfg:python")
        }
        self.assertEqual(after, before)

    def test_g12_base_graph_zero_impact(self) -> None:
        before = base_snapshot(self.database)
        analyze_scope(self.root, "src", "ast,cfg", self.database)
        analyze_scope(self.root, "src", "cfg", self.database)
        self.assertEqual(base_snapshot(self.database), before)

    def test_cli_analyze_command(self) -> None:
        result = self.cli_analyze("src", "ast,cfg")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["scope"], "src")
        self.assertEqual(result["requested_features"], ["ast", "cfg"])


if __name__ == "__main__":
    unittest.main()
