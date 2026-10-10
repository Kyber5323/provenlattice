from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice.graph import full_index
from provenlattice.query import GraphQuery


SOURCES = {
    "include/api36.h": "void foo36(void);\nvoid multi36(void);\n",
    "src/impl36.c": "void foo36(void) {}\nvoid caller36(void) { foo36(); }\n",
    "src/a36.c": "void multi36(void) {}\n",
    "src/b36.c": "void multi36(void) {}\n",
    "include/stat36.h": "static void helper36(void);\n",
    "src/stat36.c": ("static void helper36(void) {}\n"
                     "void caller36b(void) { helper36(); }\n"),
    "include/ext36.h": "void ext36(void);\n",
    "src/useext36.c": "void useext36(void) { ext36(); }\n",
    "src/sw36.c": ("int sw36(int x) {\n"
                   "    switch (x) {\n"
                   "    case 1:\n"
                   "        a36();\n"
                   "    case 2:\n"
                   "        b36();\n"
                   "        break;\n"
                   "    default:\n"
                   "        c36();\n"
                   "    }\n"
                   "    return 0;\n"
                   "}\n"),
    "src/call36.c": ("typedef int (*handler36_t)(int);\n"
                     "int run36(handler36_t h36, int x) { return h36(x); }\n"),
    "src/ind36.c": "void caller36c(void) { ghost36(1); }\n",
    "src/mod36.py": ("def alpha36():\n    return 1\n\n\n"
                     "class A36:\n    def alpha36(self):\n        return 2\n"),
}


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


class CContextRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        for relative, value in SOURCES.items():
            write(self.root / relative, value)
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)
        from provenlattice.analysis_c_ast import analyze_c_ast_scope
        from provenlattice.analysis_c_cfg import analyze_c_cfg_scope
        analyze_c_ast_scope(self.root, "src", self.database)
        analyze_c_cfg_scope(self.root, "src", self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_ret_c01_definition_preferred_over_declaration(self) -> None:
        with GraphQuery(self.database) as query:
            result = query.get_definition("foo36")
        data = result["data"]
        self.assertIsNotNone(data)
        self.assertTrue((data["metadata"] or {}).get("definition"))
        self.assertTrue(data["metadata"]["relative_path"].endswith("impl36.c"))
        declarations = result.get("related_declarations") or []
        self.assertEqual(len(declarations), 1)
        self.assertTrue(declarations[0]["metadata"]["relative_path"]
                        .endswith("api36.h"))
        self.assertNotIn("definition_ambiguous", result)

    def test_ret_c02_declaration_relationship_preserved(self) -> None:
        with GraphQuery(self.database) as query:
            result = query.get_definition("foo36")
            declaration_id = result["related_declarations"][0]["id"]
            direct = query.get_definition(declaration_id)
        self.assertEqual(direct["data"]["id"], declaration_id)
        self.assertFalse((direct["data"]["metadata"] or {}).get("definition"))

    def test_ret_c03_two_definitions_stay_ambiguous(self) -> None:
        with GraphQuery(self.database) as query:
            first = query.get_definition("multi36")
            second = query.get_definition("multi36")
        self.assertTrue(first.get("definition_ambiguous"))
        candidates = first.get("definition_candidates") or []
        self.assertEqual(len(candidates), 2)
        # The primary stays deterministic (never random) while the ambiguity
        # is surfaced through definition_candidates instead of being silently
        # resolved to one definition.
        self.assertEqual(json.dumps(first["data"], sort_keys=True),
                         json.dumps(second["data"], sort_keys=True))
        self.assertEqual(
            json.dumps({k: v for k, v in first.items() if k != "query_time_ms"},
                       sort_keys=True),
            json.dumps({k: v for k, v in second.items() if k != "query_time_ms"},
                       sort_keys=True))

    def test_ret_c04_same_file_definition_preferred(self) -> None:
        with GraphQuery(self.database) as query:
            result = query.get_definition("helper36")
        data = result["data"]
        self.assertTrue((data["metadata"] or {}).get("definition"))
        self.assertTrue(data["metadata"]["relative_path"].endswith("stat36.c"))
        self.assertEqual(
            [item["metadata"]["relative_path"]
             for item in result.get("related_declarations") or []],
            ["include/stat36.h"])

    def test_ret_c05_declaration_only_remains_valid_target(self) -> None:
        with GraphQuery(self.database) as query:
            result = query.get_definition("ext36")
        data = result["data"]
        self.assertIsNotNone(data)
        self.assertFalse((data["metadata"] or {}).get("definition"))
        self.assertTrue(data["metadata"]["relative_path"].endswith("ext36.h"))
        self.assertNotIn("definition_ambiguous", result)
        self.assertNotIn("related_declarations", result)

    def test_ret_c06_symbol_scoped_ast_not_whole_file(self) -> None:
        with GraphQuery(self.database) as query:
            summary = query.get_analysis_summary("caller36")["data"]
            file_ast = query_analysis_file_nodes("caller36", self.root, self.database)
        self.assertEqual(summary["ast"]["status"], "available")
        self.assertLess(summary["ast"]["node_count"], file_ast)
        self.assertTrue(summary["file"].endswith("impl36.c"))
        self.assertEqual(summary["lines"][0], 2)

    def test_ret_c07_cfg_compact_summary_counts(self) -> None:
        with GraphQuery(self.database) as query:
            summary = query.get_analysis_summary("sw36")["data"]
        cfg = summary["cfg"]
        self.assertEqual(cfg["status"], "available")
        self.assertEqual(cfg["switch_cases"], 3)
        self.assertEqual(cfg["fallthroughs"], 1)
        self.assertEqual(cfg["branches"], 0)
        self.assertEqual(cfg["returns"], 1)
        self.assertEqual(cfg["calls"], 3)
        self.assertEqual(cfg["node_count"], 12)
        self.assertGreater(cfg["edge_count"], 0)

    def test_ret_c08_call_summary_direct_indirect_unresolved(self) -> None:
        with GraphQuery(self.database) as query:
            direct = query.get_analysis_summary("caller36")["data"]["calls"]
            indirect = query.get_analysis_summary("run36")["data"]["calls"]
            unresolved = query.get_analysis_summary("caller36c")["data"]["calls"]
        self.assertEqual(direct["direct"], ["foo36"])
        self.assertEqual(direct["indirect"], [])
        self.assertEqual(indirect["indirect"], ["h36"])
        self.assertEqual(indirect["direct"], [])
        self.assertEqual(unresolved["unresolved"], ["ghost36"])

    def test_ret_c09_deterministic_output(self) -> None:
        def stable(value: dict) -> str:
            return json.dumps(
                {k: v for k, v in value.items() if k != "query_time_ms"},
                sort_keys=True)

        with GraphQuery(self.database) as query:
            first = query.get_analysis_summary("sw36")
            second = query.get_analysis_summary("sw36")
            explain_a = query.explain_symbol("sw36")
            explain_b = query.explain_symbol("sw36")
        self.assertEqual(stable(first), stable(second))
        self.assertEqual(stable(explain_a), stable(explain_b))

    def test_ret_c10_python_behavior_unchanged(self) -> None:
        with GraphQuery(self.database) as query:
            result = query.get_definition("alpha36")
            legacy = query._resolve_candidates("alpha36")
        expected = min(
            legacy[1], key=lambda row: (
                0 if row["id"] == "alpha36"
                else 1 if row["qualified_name"] == "alpha36" else 2,
                row["qualified_name"], row["id"]))
        self.assertEqual(result["data"]["id"], expected["id"])
        self.assertNotIn("related_declarations", result)
        self.assertNotIn("definition_candidates", result)

    def test_analysis_summary_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as fresh:
            root = Path(fresh) / "repo"
            write(root / "src" / "solo36.c",
                  "int solo36(int v) { return v; }\n")
            database = Path(fresh) / "fresh.db"
            full_index(root, database)
            with GraphQuery(database) as query:
                summary = query.get_analysis_summary("solo36")["data"]
                self.assertEqual(summary["ast"]["status"], "not_built")
                self.assertEqual(summary["cfg"]["status"], "not_built")
            with closing(sqlite3.connect(database)) as connection:
                exists = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='analysis_state'").fetchone()
                count = connection.execute(
                    "SELECT COUNT(*) FROM analysis_state WHERE feature='ast:c'"
                ).fetchone()[0] if exists else 0
            self.assertEqual(count, 0)

    def test_callers_reach_definition_after_preference(self) -> None:
        with GraphQuery(self.database) as query:
            callers = query.get_callers("foo36")["data"]
        self.assertEqual(len(callers), 1)
        self.assertEqual(callers[0]["name"], "caller36")


def query_analysis_file_nodes(caller: str, root: Path, database: Path) -> int:
    del caller
    from provenlattice.analysis_query import query_analysis
    result = query_analysis(root, "ast", file_path="src/impl36.c", database=database)
    return len(result["nodes"])


if __name__ == "__main__":
    unittest.main()
