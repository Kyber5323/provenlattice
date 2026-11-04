"""I0R1 regression tests: collapsed-line functions keep their C CFG.

Root cause (R1): the base graph records every C declaration as a
Function/Method-kind symbol (``definition: false``). When a function's span
collapses onto a single physical line -- e.g. ``for (int i = 0; ...)`` in a
one-line function -- the for-initializer symbol shares the function's
(start_line, end_line) span, the ast:c line-span alignment saw two candidates
and stayed unmatched, and the cfg:c layer skipped the whole file
(``no_functions_built``).

The fix disambiguates alignment by name, then by definition kind
(analysis_c_ast._resolve_alignment) and bumps the ast:c analyzer version so
existing caches invalidate. These tests assert the *control flow* of collapsed
functions, not merely that the skip reason disappeared.
"""

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import sqlite3

from provenlattice.analysis_c_ast import C_AST_CONTRACT
from provenlattice.analysis_query import query_analysis
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope

SINGLE_LINE_FOR = (
    "int f(int n) { for (int i = 0; i < n; i += 1) { b_f(); } a_f(); return 0; }\n"
)
MULTI_LINE_FOR = """int f(int n) {
    for (int i = 0; i < n; i += 1) {
        b_f();
    }
    a_f();
    return 0;
}
"""


class CollapsedForCfgTests(unittest.TestCase):
    """Single-line vs multi-line for must produce the same control flow."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "graph.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self, body: str, name: str = "t.c") -> None:
        (self.root / "src").mkdir(parents=True, exist_ok=True)
        (self.root / "src" / name).write_bytes(body.encode("utf-8"))
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("ast", "cfg"), database=self.database)

    def graph(self, relative: str = "src/t.c") -> dict:
        result = query_analysis(self.root, "cfg", file_path=relative,
                                database=self.database)
        self.assertEqual(result["status"], "available", result.get("reason"))
        self.assertEqual(result["analyzer"], "cfg:c")
        nodes = {node["analysis_node_id"]: node for node in result["nodes"]}
        succ: dict[str, list[tuple[str, str]]] = {}
        for edge in result["edges"]:
            succ.setdefault(edge["src_id"], []).append((edge["dst_id"], edge["kind"]))
        entry = next(n for n in nodes.values() if n["kind"] == "entry")
        exit_node = next(n for n in nodes.values() if n["kind"] == "exit")
        return {"nodes": nodes, "succ": succ,
                "entry": entry["analysis_node_id"], "exit": exit_node["analysis_node_id"]}

    def text(self, relative: str = "src/t.c") -> str:
        return (self.root / relative).read_bytes().decode("utf-8")

    def by_span(self, graph: dict, needle: str, relative: str = "src/t.c") -> str:
        source = self.text(relative)
        index = source.index(needle)
        span = (index, index + len(needle))
        matches = [
            node for node in graph["nodes"].values()
            if (node["payload"]["start_byte"], node["payload"]["end_byte"]) == span
        ]
        self.assertEqual(len(matches), 1, (needle, len(matches)))
        return matches[0]["analysis_node_id"]

    def by_role(self, graph: dict, role: str, kind: str | None = None) -> list[str]:
        return [
            node_id for node_id, node in graph["nodes"].items()
            if node["payload"].get("structural_role") == role
            and (kind is None or node["kind"] == kind)
        ]

    def assert_for_shape(self, graph: dict) -> None:
        """The canonical for-loop control flow, independent of formatting."""
        init = self.by_role(graph, "for_init", "statement")
        self.assertEqual(len(init), 1)
        headers = self.by_role(graph, "loop_header", "condition")
        self.assertEqual(len(headers), 1)
        header = headers[0]
        exits = self.by_role(graph, "loop_exit", "merge")
        self.assertEqual(len(exits), 1)
        exit_merge = exits[0]
        body_call = self.by_span(graph, "b_f()")
        update = self.by_role(graph, "for_update", "statement")
        self.assertEqual(len(update), 1)
        after_call = self.by_span(graph, "a_f()")
        ret = self.by_span(graph, "return 0;")

        self.assertEqual(graph["succ"][graph["entry"]], [(init[0], "next")])
        self.assertEqual(graph["succ"][init[0]], [(header, "next")])
        self.assertEqual(
            sorted(graph["succ"][header]),
            sorted([(body_call, "true"), (exit_merge, "false")]))
        self.assertEqual(graph["succ"][body_call], [(update[0], "next")])
        self.assertEqual(graph["succ"][update[0]], [(header, "back")])
        self.assertEqual(graph["succ"][exit_merge], [(after_call, "next")])
        self.assertEqual(graph["succ"][after_call], [(ret, "next")])
        self.assertEqual(graph["succ"][ret], [(graph["exit"], "return")])

    def test_single_line_for_builds_canonical_control_flow(self) -> None:
        self.build(SINGLE_LINE_FOR)
        self.assert_for_shape(self.graph())

    def test_multi_line_for_keeps_canonical_control_flow(self) -> None:
        self.build(MULTI_LINE_FOR)
        self.assert_for_shape(self.graph())

    def test_single_line_for_is_not_skipped(self) -> None:
        self.build(SINGLE_LINE_FOR)
        result = query_analysis(self.root, "cfg", file_path="src/t.c",
                                database=self.database)
        self.assertEqual(result["status"], "available")

    def test_nested_single_line_for(self) -> None:
        self.build(
            "int h(int n) { for (int i = 0; i < n; i += 1) "
            "{ for (int j = 0; j < i; j += 1) { b_h(); } } a_h(); return 0; }\n")
        graph = self.graph()
        headers = sorted(
            self.by_role(graph, "loop_header", "condition"),
            key=lambda node_id: graph["nodes"][node_id]["payload"]["start_byte"])
        self.assertEqual(len(headers), 2)
        outer_header, inner_header = headers
        inits = self.by_role(graph, "for_init", "statement")
        self.assertEqual(len(inits), 2)
        # byte order: the outer `i += 1` precedes the inner `j += 1`
        updates = sorted(
            self.by_role(graph, "for_update", "statement"),
            key=lambda node_id: graph["nodes"][node_id]["payload"]["start_byte"])
        self.assertEqual(len(updates), 2)
        outer_update, inner_update = updates
        inner_exit = [dst for dst, kind in graph["succ"][inner_header]
                      if kind == "false"]
        self.assertEqual(len(inner_exit), 1)
        # the inner loop's exit continues into the outer loop's update
        self.assertEqual(graph["succ"][inner_exit[0]], [(outer_update, "next")])
        # the inner body call flows through the inner update back to the header
        body_call = self.by_span(graph, "b_h()")
        self.assertEqual(graph["succ"][body_call], [(inner_update, "next")])
        self.assertIn((inner_header, "back"), graph["succ"][inner_update])
        # the outer loop's update returns to the outer header
        self.assertIn((outer_header, "back"), graph["succ"][outer_update])

    def test_for_break_and_continue(self) -> None:
        self.build(
            "int g(int n) { for (int i = 0; i < n; i += 1) "
            "{ if (i == 2) { continue; } if (i == 3) { break; } b_g(); } "
            "a_g(); return 0; }\n")
        graph = self.graph()
        update = self.by_role(graph, "for_update", "statement")
        exit_merge = self.by_role(graph, "loop_exit", "merge")
        self.assertEqual(len(update), 1)
        self.assertEqual(len(exit_merge), 1)
        continue_targets = [dst for edges in graph["succ"].values()
                            for dst, kind in edges if kind == "continue"]
        self.assertEqual(set(continue_targets), {update[0]})
        break_targets = [dst for edges in graph["succ"].values()
                         for dst, kind in edges if kind == "break"]
        self.assertEqual(break_targets, exit_merge)
        body_call = self.by_span(graph, "b_g()")
        # no condition branch jumps straight into the body past the guards
        for cond in self.by_role(graph, "if_condition", "condition"):
            self.assertNotIn((body_call, "true"), graph["succ"][cond])

    def test_for_with_call_in_condition(self) -> None:
        self.build(
            "int k(int n) { for (int i = 0; i < limit_k(); i += step_k()) "
            "{ b_k(); } a_k(); return 0; }\n")
        graph = self.graph()
        header = self.by_role(graph, "loop_header", "condition")
        self.assertEqual(len(header), 1)
        # the condition's call is evaluated on the header path
        self.assertIn((self.by_span(graph, "b_k()"), "true"), graph["succ"][header[0]])

    def test_two_functions_one_collapsed_both_build(self) -> None:
        self.build(
            "int m1(int n) { for (int i = 0; i < n; i += 1) { b_m1(); } "
            "a_m1(); return 0; }\n"
            "int m2(int x) { if (x) { c_m2(); } return x; }\n")
        for relative in ("src/t.c",):
            result = query_analysis(self.root, "cfg", file_path=relative,
                                    database=self.database)
            self.assertEqual(result["status"], "available")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT s.metadata FROM analysis_state s JOIN files f "
                "ON f.file_id=s.file_id WHERE f.path='src/t.c' AND s.feature='cfg:c'"
            ).fetchone()
        metadata = json.loads(row["metadata"])
        self.assertEqual(metadata["functions_built"], 2)

    def test_duplicate_prototype_line_stays_conservative(self) -> None:
        # Two same-name prototypes share one collapsed span; neither may claim
        # the other's identity, but the real definition must still build.
        self.build("int dup(int x);int dup(int x);\n"
                   "int dup(int x) { return x; }\n")
        result = query_analysis(self.root, "cfg", file_path="src/t.c",
                                database=self.database)
        self.assertEqual(result["status"], "available")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            ast_rows = connection.execute(
                "SELECT n.payload FROM analysis_nodes n JOIN files f "
                "ON f.file_id=n.file_id WHERE f.path='src/t.c' "
                "AND n.feature='ast:c' AND n.kind='FUNCTION'").fetchall()
        alignments = sorted(
            json.loads(r["payload"])["alignment"] for r in ast_rows)
        # 2 prototype nodes unmatched (ambiguous on the collapsed line) and the
        # definition resolved.
        self.assertEqual(alignments, ["resolved", "unmatched", "unmatched"])

    def test_single_line_switch_goto_shortcircuit_no_regression(self) -> None:
        self.build(
            "int w(int x) { switch (x) { case 1: c1_w(); break; default: d_w(); } "
            "x_w(); return 0; }\n"
            "int t(int a) { if (a) goto out; g_t(); out: x_t(); return 0; }\n"
            "int s(int a, int (*fp_s)(void)) "
            "{ if (a > 0 && fp_s() != 2) { yes_s(); } no_s(); return 0; }\n")
        graph = self.graph()

        # switch: case/default edges off the condition, break to switch_exit
        switch_cond = self.by_span(graph, "(x)")
        case_node = self.by_role(graph, "switch_case", "case")
        default_node = self.by_role(graph, "switch_default", "case")
        switch_exit = self.by_role(graph, "switch_exit", "merge")
        self.assertEqual(len(case_node), 1)
        self.assertEqual(len(default_node), 1)
        self.assertEqual(len(switch_exit), 1)
        self.assertIn((case_node[0], "case"), graph["succ"][switch_cond])
        self.assertIn((default_node[0], "default"), graph["succ"][switch_cond])
        break_targets = [dst for edges in graph["succ"].values()
                         for dst, kind in edges if kind == "break"]
        self.assertEqual(break_targets, switch_exit)

        # goto: condition true -> goto node -> label
        cond_t = self.by_span(graph, "(a)")
        goto_node = self.by_role(graph, "statement", "goto")
        label_node = self.by_role(graph, "label", "label")
        self.assertEqual(len(goto_node), 1)
        self.assertEqual(len(label_node), 1)
        self.assertIn((goto_node[0], "true"), graph["succ"][cond_t])
        self.assertIn((label_node[0], "goto"), graph["succ"][goto_node[0]])

        # short-circuit: s() lowers && into a two-node condition chain
        # (byte order: t()'s plain `if (a)` first, then s()'s op1, op2)
        conds = sorted(
            self.by_role(graph, "if_condition", "condition"),
            key=lambda node_id: graph["nodes"][node_id]["payload"]["start_byte"])
        self.assertEqual(len(conds), 3)
        self.assertIn((conds[2], "true"), graph["succ"][conds[1]])
        yes_call = self.by_span(graph, "yes_s()")
        self.assertIn((yes_call, "true"), graph["succ"][conds[2]])


class AnalyzerVersionInvalidationTests(unittest.TestCase):
    """The ast:c version bump must invalidate pre-fix caches (fail-closed)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "graph.db"
        (self.root / "src").mkdir(parents=True)
        (self.root / "src" / "t.c").write_bytes(
            SINGLE_LINE_FOR.encode("utf-8"))
        full_index(self.root, self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_downgraded_cache_version_is_rematerialized(self) -> None:
        analyze_scope(self.root, "src", ("ast", "cfg"), database=self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE analysis_state SET feature_version = 1 WHERE feature='ast:c'")
            connection.commit()
        result = analyze_scope(self.root, "src", ("ast", "cfg"),
                               database=self.database)
        # the stale ast layer is re-materialized, and cfg follows
        self.assertEqual(result["features"]["ast"]["materialized"], ["src/t.c"])
        env = query_analysis(self.root, "cfg", file_path="src/t.c",
                             database=self.database)
        self.assertEqual(env["status"], "available")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT s.metadata FROM analysis_state s JOIN files f "
                "ON f.file_id=s.file_id WHERE f.path='src/t.c' AND s.feature='cfg:c'"
            ).fetchone()
        self.assertEqual(json.loads(row["metadata"])["ast_contract"], C_AST_CONTRACT)


if __name__ == "__main__":
    unittest.main()
