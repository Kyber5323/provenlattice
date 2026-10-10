from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice.analysis_c_ast import analyze_c_ast_scope
from provenlattice.analysis_c_cfg import analyze_c_cfg_scope
from provenlattice.analysis_query import query_analysis
from provenlattice.graph import full_index
from provenlattice.incremental import incremental_update
from provenlattice.query import GraphQuery


SOURCES = {
    "src/call01.c": "void foo01(void) {}\nvoid caller01(void) { foo01(); }\n",
    "include/call02.h": "void foo02(void);\n",
    "src/impl02.c": "void foo02(void) {}\n",
    "src/use02.c": "void caller02(void) { foo02(); }\n",
    "src/a03.c": "void foo03(void) {}\n",
    "src/b03.c": "void foo03(void) {}\n",
    "src/use03.c": "void caller03(void) { foo03(); }\n",
    "src/call04.c": ("typedef int (*handler04_t)(int);\n"
                     "int run04(handler04_t h04, int x) { return h04(x); }\n"),
    "src/other04.c": "void h04(int x) { }\n",
    "src/call05.c": ("int add05(int a, int b) { return a + b; }\n"
                     "void caller05(void) { int (*fp05)(int, int); "
                     "fp05 = add05; fp05(1, 2); }\n"),
    "src/call06.c": ("int add06(int a) { return a; }\n"
                     "void caller06(void) { int (*fp06)(int); fp06 = add06; "
                     "(*fp06)(3); }\n"),
    "src/call07.c": ("struct ops07 { int (*read07)(int); };\n"
                     "int impl07(int x) { return x; }\n"
                     "void caller07(void) { struct ops07 o07; o07.read07 = impl07; "
                     "struct ops07 *p07 = &o07; p07->read07(5); }\n"),
    "src/call08.c": ("struct ops08 { int (*read08)(int); };\n"
                     "void caller08(void) { struct ops08 o08; o08.read08(5); }\n"),
    "src/other08.c": "void read08(int x) { }\n",
    "src/call09.c": "void caller09(void) { unknown09(1); }\n",
    "src/call10.c": ("void target10(void) {}\n"
                     "void caller10(void) { register10(target10); }\n"),
    "src/call11.c": "void caller11(void) { get11()(7); }\n",
    "src/call12.c": ("void caller12(void) { int (*table12[2])(int); "
                     "table12[0](7); }\n"),
    "src/call13.c": ("void foo13(int x) { }\n"
                     "void caller13(void) { CALL13(foo13, 1); }\n"),
    "src/call14.c": ("static void helper14(void) {}\n"
                     "void caller14(void) { helper14(); }\n"),
    "src/impl15.c": "void util15(void) {}\n",
    "src/use15.c": "void caller15(void) { util15(); }\n",
}


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


class CCallSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        for relative, value in SOURCES.items():
            write(self.root / relative, value)
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def refs(self, raw_name: str | None = None, file_suffix: str | None = None) -> list[dict]:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute(
                "SELECT r.*, f.path AS file_path FROM raw_references r "
                "JOIN files f ON f.file_id=r.file_id WHERE r.reference_type='CALLS' "
                "ORDER BY f.path, r.start_line")]
        if raw_name is not None:
            rows = [row for row in rows if row["raw_name"] == raw_name]
        if file_suffix is not None:
            rows = [row for row in rows if row["file_path"].endswith(file_suffix)]
        for row in rows:
            row["metadata"] = json.loads(row["metadata"])
            row["candidate_symbols"] = json.loads(row["candidate_symbols"])
        return rows

    def one_ref(self, file_suffix: str, raw_name: str) -> dict:
        matches = self.refs(raw_name=raw_name, file_suffix=file_suffix)
        self.assertEqual(len(matches), 1, (file_suffix, raw_name, len(matches)))
        return matches[0]

    def edges_from(self, caller_name: str) -> set[str]:
        with GraphQuery(self.database) as query:
            callers = query.get_callers("foo")  # placeholder to open view
            del callers
            node = query._resolve(caller_name)
            if node is None:
                return set()
            edges = query.view.query(
                "Edge", "SELECT * FROM edges WHERE src_id=?", (node["id"],),
                lambda row: row.get("src_id") == node["id"])
            return {edge["dst_id"] for edge in edges if edge["type"] == "CALLS"}

    def test_call01_same_file_direct(self) -> None:
        ref = self.one_ref("call01.c", "foo01")
        self.assertEqual(ref["status"], "resolved")
        self.assertEqual(ref["metadata"]["call_kind"], "direct")
        self.assertIsNotNone(ref["resolved_symbol_id"])
        self.assertGreater(ref["confidence"], 0.9)

    def test_call02_declaration_direct(self) -> None:
        ref = self.one_ref("use02.c", "foo02")
        self.assertEqual(ref["status"], "resolved")
        self.assertEqual(ref["metadata"]["call_kind"], "direct")
        self.assertEqual(ref["resolution_strategy"], "qualified_name_resolution")

    def test_call03_ambiguous_never_bound(self) -> None:
        ref = self.one_ref("use03.c", "foo03")
        self.assertEqual(ref["status"], "ambiguous")
        self.assertEqual(ref["metadata"]["call_kind"], "direct")
        self.assertIsNone(ref["resolved_symbol_id"])
        self.assertEqual(len(ref["candidate_symbols"]), 2)
        self.assertLessEqual(len(ref["candidate_symbols"]), 8)
        caller = self.one_ref("use03.c", "foo03")
        self.assertEqual(self.edges_from("caller03"), set())
        del caller

    def test_call04_function_pointer_parameter_indirect(self) -> None:
        ref = self.one_ref("call04.c", "h04")
        self.assertEqual(ref["status"], "unresolved")
        self.assertEqual(ref["metadata"]["call_kind"], "indirect")
        self.assertEqual(ref["metadata"]["callee_shape"], "identifier")
        self.assertEqual(ref["metadata"]["callee_symbol"], "h04")
        self.assertIsNone(ref["resolved_symbol_id"])
        self.assertEqual(ref["candidate_symbols"], [])
        self.assertEqual(self.edges_from("run04"), set())

    def test_call04_negative_no_false_binding_to_global_h04(self) -> None:
        h04_symbols = 0
        with closing(sqlite3.connect(self.database)) as connection:
            h04_symbols = connection.execute(
                "SELECT COUNT(*) FROM nodes WHERE name='h04' AND kind='Function'"
            ).fetchone()[0]
        self.assertGreaterEqual(h04_symbols, 1)
        self.assertEqual(self.edges_from("run04"), set())

    def test_call05_local_function_pointer_indirect(self) -> None:
        ref = self.one_ref("call05.c", "fp05")
        self.assertEqual(ref["status"], "unresolved")
        self.assertEqual(ref["metadata"]["call_kind"], "indirect")
        self.assertEqual(ref["metadata"]["callee_symbol"], "fp05")
        self.assertEqual(self.edges_from("caller05"), set())

    def test_call06_dereference_indirect(self) -> None:
        ref = self.one_ref("call06.c", "(*fp06)")
        self.assertEqual(ref["metadata"]["call_kind"], "indirect")
        self.assertEqual(ref["metadata"]["callee_shape"], "deref")
        self.assertEqual(ref["metadata"]["callee_symbol"], "fp06")
        self.assertIsNone(ref["resolved_symbol_id"])

    def test_call07_struct_pointer_field_indirect(self) -> None:
        ref = self.one_ref("call07.c", "p07->read07")
        self.assertEqual(ref["metadata"]["call_kind"], "indirect")
        self.assertEqual(ref["metadata"]["callee_shape"], "field_access")
        self.assertEqual(ref["metadata"]["field"], "read07")
        self.assertIsNone(ref["resolved_symbol_id"])
        self.assertEqual(self.edges_from("caller07"), set())

    def test_call08_struct_dot_field_indirect_no_global_binding(self) -> None:
        ref = self.one_ref("call08.c", "o08.read08")
        self.assertEqual(ref["metadata"]["call_kind"], "indirect")
        self.assertEqual(ref["metadata"]["field"], "read08")
        self.assertEqual(ref["status"], "unresolved")
        self.assertEqual(ref["candidate_symbols"], [])
        self.assertEqual(self.edges_from("caller08"), set())

    def test_call09_unknown_unresolved_without_candidates(self) -> None:
        ref = self.one_ref("call09.c", "unknown09")
        self.assertEqual(ref["metadata"]["call_kind"], "direct")
        self.assertEqual(ref["status"], "unresolved")
        self.assertIsNone(ref["resolved_symbol_id"])
        self.assertEqual(ref["candidate_symbols"], [])
        self.assertEqual(self.edges_from("caller09"), set())

    def test_call10_callback_registration_address_taken(self) -> None:
        ref = self.one_ref("call10.c", "register10")
        self.assertEqual(ref["metadata"]["call_kind"], "direct")
        self.assertEqual(ref["status"], "unresolved")
        self.assertEqual(
            ref["metadata"].get("function_reference_args"), ["target10"])

    def test_call11_nested_call_indirect(self) -> None:
        ref = self.one_ref("call11.c", "get11()")
        self.assertEqual(ref["metadata"]["call_kind"], "indirect")
        self.assertEqual(ref["metadata"]["callee_shape"], "nested_call")
        self.assertIsNone(ref["resolved_symbol_id"])

    def test_call12_table_subscript_indirect(self) -> None:
        ref = self.one_ref("call12.c", "table12[0]")
        self.assertEqual(ref["metadata"]["call_kind"], "indirect")
        self.assertEqual(ref["metadata"]["callee_shape"], "subscript")
        self.assertIsNone(ref["resolved_symbol_id"])

    def test_call13_macro_shaped_unresolved(self) -> None:
        ref = self.one_ref("call13.c", "CALL13")
        self.assertEqual(ref["metadata"]["call_kind"], "direct")
        self.assertEqual(ref["status"], "unresolved")
        self.assertEqual(
            ref["metadata"].get("function_reference_args"), ["foo13"])
        self.assertEqual(self.edges_from("caller13"), set())

    def test_call14_same_file_static_direct(self) -> None:
        ref = self.one_ref("call14.c", "helper14")
        self.assertEqual(ref["status"], "resolved")
        self.assertEqual(ref["metadata"]["call_kind"], "direct")
        self.assertEqual(ref["resolution_strategy"], "same_file_resolution")

    def test_call15_cross_file_unique_direct(self) -> None:
        ref = self.one_ref("use15.c", "util15")
        self.assertEqual(ref["status"], "resolved")
        self.assertEqual(ref["metadata"]["call_kind"], "direct")
        self.assertIsNotNone(ref["resolved_symbol_id"])

    def test_call_semantics_query_classification(self) -> None:
        with GraphQuery(self.database) as query:
            result = query.get_call_semantics("run04")
        self.assertEqual(result["query_type"], "call_semantics")
        sites = result["data"]["sites"]
        self.assertEqual(len(sites), 1)
        site = sites[0]
        self.assertEqual(site["class"], "indirect")
        self.assertEqual(site["call_kind"], "indirect")
        self.assertEqual(site["callee"], "h04")
        self.assertIsNone(site["target"])
        self.assertEqual(site["candidates"], [])
        self.assertTrue(site["file"].endswith("call04.c"))
        self.assertGreater(site["line"], 0)
        self.assertEqual(result["data"]["counts"], {"indirect": 1})

    def test_call_semantics_counts_direct_resolved(self) -> None:
        with GraphQuery(self.database) as query:
            result = query.get_call_semantics("caller01")
        self.assertEqual(result["data"]["counts"], {"direct": 1})
        site = result["data"]["sites"][0]
        self.assertEqual(site["class"], "direct")
        self.assertEqual(site["target"], "foo01")
        self.assertTrue(site["file"].endswith("call01.c"))

    def test_call_semantics_ambiguous_candidates_bounded(self) -> None:
        with GraphQuery(self.database) as query:
            result = query.get_call_semantics("caller03")
        self.assertEqual(result["data"]["counts"], {"ambiguous": 1})
        site = result["data"]["sites"][0]
        self.assertEqual(len(site["candidates"]), 2)
        self.assertIsNone(site["target"])

    def test_explain_symbol_exposes_call_semantics(self) -> None:
        with GraphQuery(self.database) as query:
            explained = query.explain_symbol("caller01")
        section = explained["call_semantics"]
        self.assertEqual(section["counts"], {"direct": 1})
        self.assertEqual(section["sites"][0]["class"], "direct")

    def test_ast_cfg_call_identity_alignment(self) -> None:
        analyze_c_ast_scope(self.root, "src", self.database)
        analyze_c_cfg_scope(self.root, "src", self.database)
        ast = query_analysis(
            self.root, "ast", file_path="src/call01.c", database=self.database)
        cfg = query_analysis(
            self.root, "cfg", file_path="src/call01.c", database=self.database)
        self.assertEqual(ast["status"], "available")
        self.assertEqual(cfg["status"], "available")
        callsites = [node for node in cfg["nodes"] if node["kind"] == "callsite"]
        self.assertEqual(len(callsites), 1)
        callsite = callsites[0]
        ast_call = next(
            node for node in ast["nodes"]
            if node["analysis_node_id"] == callsite["payload"]["ast_node_id"])
        self.assertEqual(ast_call["kind"], "CALL")
        self.assertEqual(
            ast_call["payload"]["start_byte"],
            callsite["payload"]["start_byte"])
        ref = self.one_ref("call01.c", "foo01")
        self.assertEqual(
            ref["start_line"],
            self.text_line("src/call01.c", callsite["payload"]["start_byte"]))

    def text_line(self, relative: str, byte_offset: int) -> int:
        source = (self.root / relative).read_bytes()
        return source[:byte_offset].count(b"\n") + 1

    def test_full_index_determinism_and_incremental_parity(self) -> None:
        def snapshot(database: Path) -> list[tuple]:
            with closing(sqlite3.connect(database)) as connection:
                return sorted(connection.execute(
                    "SELECT id, status, resolved_symbol_id, resolution_strategy, "
                    "metadata FROM raw_references WHERE reference_type='CALLS'"))

        second = Path(self.temp.name) / "second.db"
        full_index(self.root, second)
        self.assertEqual(snapshot(second), snapshot(self.database))
        before = snapshot(self.database)
        incremental_update(self.root, self.database)
        self.assertEqual(snapshot(self.database), before)

    def test_real_c_smoke_callbacks_fixture(self) -> None:
        smoke_root = Path(self.temp.name) / "smoke"
        shutil.copytree(Path(__file__).parent / "fixture_cpp", smoke_root)
        database = Path(self.temp.name) / "smoke.db"
        full_index(smoke_root, database)
        with GraphQuery(database) as query:
            run_all = query.get_call_semantics("run_all")["data"]
            apply_site = query.get_call_semantics("apply_callback")["data"]
            pipeline_site = query.get_call_semantics("run_pipeline")["data"]
        self.assertEqual(run_all["counts"], {"direct": 2, "unresolved": 1})
        by_callee = {site["callee"]: site for site in run_all["sites"]}
        self.assertEqual(by_callee["apply_callback"]["class"], "direct")
        self.assertEqual(by_callee["run_pipeline"]["class"], "direct")
        self.assertEqual(by_callee["unknown_helper"]["class"], "unresolved")
        self.assertIsNone(by_callee["unknown_helper"]["target"])
        self.assertEqual(by_callee["unknown_helper"]["candidates"], [])
        self.assertEqual(apply_site["counts"], {"indirect": 1})
        cb = apply_site["sites"][0]
        self.assertEqual(cb["callee"], "cb")
        self.assertEqual(cb["call_kind"], "indirect")
        self.assertIsNone(cb["target"])
        self.assertEqual(pipeline_site["counts"], {"indirect": 1})
        step = pipeline_site["sites"][0]
        self.assertEqual(step["callee"], "pipe->step")
        self.assertEqual(step["call_kind"], "indirect")
        self.assertEqual(step["candidates"], [])
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute(
                "SELECT metadata FROM raw_references WHERE reference_type='CALLS' "
                "AND raw_name='apply_callback'")]
        metadata = json.loads(rows[0]["metadata"])
        self.assertEqual(
            sorted(metadata.get("function_reference_args", [])),
            ["negate_value"])
        # false direct bindings = 0: no indirect call gained a CALLS edge
        with closing(sqlite3.connect(database)) as connection:
            indirect_lines = [
                row[0] for row in connection.execute(
                    "SELECT start_line FROM raw_references WHERE reference_type='CALLS' "
                    "AND metadata LIKE '%\"call_kind\": \"indirect\"%'")]
            resolved_lines = [
                row[0] for row in connection.execute(
                    "SELECT start_line FROM raw_references WHERE reference_type='CALLS' "
                    "AND status='resolved'")]
        self.assertFalse(set(indirect_lines) & set(resolved_lines))


if __name__ == "__main__":
    unittest.main()
