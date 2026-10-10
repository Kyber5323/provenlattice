from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice.analysis_c_ast import analyze_c_ast_scope
from provenlattice.analysis_query import query_analysis
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope
from provenlattice.query import GraphQuery


SOURCES = {
    "src/add.c": """int add(int a, int b) {
    return a + b;
}
""",
    "src/branch.c": """int classify(int x) {
    int total = 0;
    if (x > 0) { total = x; } else { total = -x; }
    while (total > 10) { total--; }
    for (int i = 0; i < 2; i++) { total += i; }
    return total;
}
""",
    "src/switch.c": """int choose(int x) {
    switch (x) { case 1: x++; case 2: return x; default: return 0; }
}
""",
    "src/jump.c": """int jump(int x) {
    if (x) goto done;
    x = 3;
done:
    return x;
}
""",
    "src/types.c": """typedef struct Pair { int left; int right; } Pair;
union Value { int integer; char byte; };
enum Mode { MODE_A, MODE_B };
""",
    "src/calls.c": """typedef int (*handler_t)(int);
int run(handler_t h, int x) {
    return h(x);
}
""",
    "src/malformed.c": """int broken(int value) {
    if (value > 0) {
        return value
""",
    "src/api.h": """typedef struct Api { int version; } Api;
int api_run(const Api *api, int value);
""",
}


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


class CAstAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        for relative, value in SOURCES.items():
            write(self.root / relative, value)
        write(self.root / "src" / "keep.py", "def keep(value):\n    return value\n")
        write(self.root / "src" / "not_c.cpp", "int cpp_only() { return 1; }\n")
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def rows(self, relative: str) -> list[dict]:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute(
                "SELECT n.* FROM analysis_nodes n JOIN files f ON f.file_id=n.file_id "
                "WHERE f.path=? AND n.feature='ast:c' ORDER BY n.analysis_node_id",
                (relative,),
            )]
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def state(self, relative: str) -> dict:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT s.* FROM analysis_state s JOIN files f ON f.file_id=s.file_id "
                "WHERE f.path=? AND s.feature='ast:c'", (relative,),
            ).fetchone()
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        return result

    def test_t1_function_parameters_return_and_normalized_boundary(self) -> None:
        analyze_c_ast_scope(self.root, "src", self.database)
        rows = self.rows("src/add.c")
        kinds = {row["kind"] for row in rows}
        self.assertLessEqual({"FILE", "FUNCTION", "PARAMETER", "RETURN", "OPERATOR"}, kinds)
        self.assertNotIn("function_definition", kinds)
        function = next(row for row in rows if row["kind"] == "FUNCTION")
        self.assertEqual(function["payload"]["parser_kind"], "function_definition")
        self.assertEqual(function["payload"]["language"], "c")
        self.assertEqual(function["payload"]["name"], "add")

    def test_t2_local_if_else(self) -> None:
        analyze_c_ast_scope(self.root, "src", self.database)
        rows = self.rows("src/branch.c")
        self.assertTrue(any(row["kind"] == "LOCAL" for row in rows))
        controls = {row["payload"]["parser_kind"] for row in rows
                    if row["kind"] == "CONTROL_STRUCTURE"}
        self.assertIn("if_statement", controls)

    def test_t3_while_and_for(self) -> None:
        analyze_c_ast_scope(self.root, "src", self.database)
        controls = {row["payload"]["parser_kind"] for row in self.rows("src/branch.c")
                    if row["kind"] == "CONTROL_STRUCTURE"}
        self.assertLessEqual({"while_statement", "for_statement"}, controls)

    def test_t4_switch_case_default_and_fallthrough_syntax(self) -> None:
        analyze_c_ast_scope(self.root, "src", self.database)
        controls = [row["payload"]["parser_kind"] for row in self.rows("src/switch.c")
                    if row["kind"] == "CONTROL_STRUCTURE"]
        self.assertIn("switch_statement", controls)
        self.assertGreaterEqual(controls.count("case_statement"), 3)

    def test_t5_goto_and_label(self) -> None:
        analyze_c_ast_scope(self.root, "src", self.database)
        rows = self.rows("src/jump.c")
        self.assertTrue(any(row["payload"]["parser_kind"] == "goto_statement"
                            for row in rows))
        label = next(row for row in rows if row["kind"] == "LABEL")
        self.assertEqual(label["payload"]["name"], "done")

    def test_t6_struct_union_enum_typedef(self) -> None:
        analyze_c_ast_scope(self.root, "src", self.database)
        parser_kinds = {row["payload"]["parser_kind"] for row in self.rows("src/types.c")
                        if row["kind"] == "TYPE_DECL"}
        self.assertLessEqual(
            {"type_definition", "struct_specifier", "union_specifier", "enum_specifier"},
            parser_kinds,
        )

    def test_t7_direct_call(self) -> None:
        analyze_c_ast_scope(self.root, "src", self.database)
        calls = [row for row in self.rows("src/calls.c") if row["kind"] == "CALL"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["payload"]["name"], "h")

    def test_t8_function_pointer_structure_without_target_inference(self) -> None:
        analyze_c_ast_scope(self.root, "src", self.database)
        rows = self.rows("src/calls.c")
        typedef = next(row for row in rows
                       if row["payload"]["parser_kind"] == "type_definition")
        self.assertEqual(typedef["kind"], "TYPE_DECL")
        parameters = {row["payload"]["name"] for row in rows
                      if row["kind"] == "PARAMETER" and row["payload"]["name"]}
        self.assertEqual(parameters, {"h", "x"})
        self.assertNotIn("resolved_target_id", next(
            row for row in rows if row["kind"] == "CALL"
        )["payload"])

    def test_t9_malformed_source_is_degraded_not_failed(self) -> None:
        result = analyze_c_ast_scope(self.root, "src", self.database)
        self.assertIn("src/malformed.c", result["materialized"])
        metadata = self.state("src/malformed.c")["metadata"]
        self.assertEqual(metadata["analysis_status"], "degraded")
        self.assertTrue(metadata["partial"])
        self.assertGreater(metadata["error_node_count"] + metadata["missing_node_count"], 0)
        queried = query_analysis(
            self.root, "ast", file_path="src/malformed.c", database=self.database
        )
        self.assertEqual(queried["status"], "available")
        self.assertTrue(queried["coverage"]["partial"])

    def test_t10_header_and_prototype(self) -> None:
        result = analyze_scope(self.root, "src", "ast", self.database)
        self.assertIn("src/api.h", result["features"]["ast"]["materialized"])
        rows = self.rows("src/api.h")
        prototype = next(row for row in rows
                         if row["kind"] == "FUNCTION"
                         and row["payload"]["parser_kind"] == "declaration")
        self.assertEqual(prototype["payload"]["name"], "api_run")
        queried = query_analysis(
            self.root, "ast", file_path="src/api.h", database=self.database
        )
        self.assertEqual(queried["analyzer"], "ast:c")
        self.assertEqual(queried["status"], "available")

    def test_parent_child_order_spans_and_deterministic_digest(self) -> None:
        analyze_c_ast_scope(self.root, "src", self.database)
        first = self.rows("src/add.c")
        by_id = {row["analysis_node_id"]: row for row in first}
        root = [row for row in first if row["payload"]["parent_id"] is None]
        self.assertEqual(len(root), 1)
        for row in first:
            parent = row["payload"]["parent_id"]
            if parent is not None:
                self.assertIn(parent, by_id)
            self.assertLessEqual(row["payload"]["start_byte"], row["payload"]["end_byte"])
        canonical = json.dumps(first, sort_keys=True)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        analyze_c_ast_scope(self.root, "src", self.database)
        self.assertEqual(
            hashlib.sha256(json.dumps(self.rows("src/add.c"), sort_keys=True).encode()).hexdigest(),
            digest,
        )

    def test_c_cfg_requires_explicit_request_then_available(self) -> None:
        not_built = query_analysis(
            self.root, "cfg", file_path="src/add.c", database=self.database
        )
        self.assertEqual(not_built["status"], "not_built")
        self.assertEqual(not_built["reason"], "analysis_not_built")
        result = analyze_scope(self.root, "src", "cfg", self.database)
        self.assertEqual(result["status"], "complete")
        self.assertIn("src/add.c", result["features"]["cfg"]["materialized"])
        available = query_analysis(
            self.root, "cfg", file_path="src/add.c", database=self.database
        )
        self.assertEqual(available["status"], "available")
        self.assertEqual(available["analyzer"], "cfg:c")
        with closing(sqlite3.connect(self.database)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM analysis_state WHERE feature='cfg:c'"
            ).fetchone()[0]
        self.assertEqual(count, 5)

    def test_explain_symbol_uses_shared_optional_analysis_surface(self) -> None:
        analyze_scope(self.root, "src", "ast", self.database)
        with GraphQuery(self.database) as query:
            explained = query.explain_symbol("add", analysis="ast")
        ast = explained["analysis"]["ast"]
        self.assertEqual(ast["status"], "available")
        self.assertEqual(ast["root"]["kind"], "FUNCTION")
        self.assertEqual(ast["root"]["parser_kind"], "function_definition")


if __name__ == "__main__":
    unittest.main()
