from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice.analysis_c_ast import analyze_c_ast_scope
from provenlattice.analysis_c_cfg import _CCfgBuilder, _index_tree, analyze_c_cfg_scope
from provenlattice.analysis_c_frontend import CFrontend, NormalizedCNode
from provenlattice.analysis_cfg import _FunctionCFG
from provenlattice.analysis_query import query_analysis
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope


SOURCES = {
    "src/scfg01.c": """int scfg01(int s1a, int (*s1b)(void)) {
    if (s1a > 0 && s1b() != 2) {
        yes01();
    }
    no01();
    return 0;
}
""",
    "src/scfg02.c": """int scfg02(int s2a, int (*s2b)(void)) {
    if (s2a > 0 || s2b() != 2) {
        yes02();
    }
    no02();
    return 0;
}
""",
    "src/scfg03.c": """int scfg03(int s3a, int s3b, int (*s3c)(void)) {
    if (s3a > 0 && s3b > 1 && s3c() != 2) {
        yes03();
    }
    no03();
    return 0;
}
""",
    "src/scfg04.c": """int scfg04(int s4a, int s4b, int (*s4c)(void)) {
    if (s4a > 0 || s4b > 1 || s4c() != 2) {
        yes04();
    }
    no04();
    return 0;
}
""",
    "src/scfg05a.c": """int scfg05a(int s5a, int s5b, int (*s5c)(void)) {
    if (s5a > 0 && (s5b > 1 || s5c() != 2)) {
        yes05();
    }
    no05();
    return 0;
}
""",
    "src/scfg05b.c": """int scfg05b(int s5a, int (*s5b)(void), int s5c) {
    if ((s5a > 0 || s5b() != 2) && s5c > 1) {
        yes05();
    }
    no05();
    return 0;
}
""",
    "src/scfg06.c": """int scfg06(int s6a, int (*f06)(void), int (*g06)(void)) {
    if (s6a > 0 ? f06() != 1 : g06() != 2) {
        yes06();
    }
    no06();
    return 0;
}
""",
    "src/scfg07.c": """int scfg07(int s7a, int (*s7b)(void)) {
    while (s7a > 0 && s7b() != 2) {
        body07();
    }
    after07();
    return 0;
}
""",
    "src/scfg08.c": """int scfg08(int s8a, int (*s8b)(void)) {
    for (; s8a > 0 || s8b() != 2; ) {
        body08();
    }
    after08();
    return 0;
}
""",
    "src/scfg09.c": """int scfg09(int s9a, int (*s9b)(void)) {
    return s9a > 0 && s9b() != 2;
}
""",
    "src/scfg10.c": """int scfg10(int s10a, int (*foo10)(void), int (*bar10)(void)) {
    s10x = s10a > 0 ? foo10() : bar10();
    after10();
    return 0;
}
""",
    "src/forfull.c": """int i;
int forfull(void) {
    for (i = 0; i < 2; i += 1) {
        bodyf();
    }
    return 0;
}
""",
    "src/fornoinit.c": """int i;
int fornoinit(void) {
    for (; i < 2; i += 1) {
        bodyn();
    }
    return 0;
}
""",
    "src/fornocond.c": """int i;
int fornocond(void) {
    for (i = 0; ; i += 1) {
        bodyc();
    }
    return 0;
}
""",
    "src/fornoupdate.c": """int i;
int fornoupdate(void) {
    for (i = 0; i < 2; ) {
        bodyu();
    }
    return 0;
}
""",
    "src/foronlybody.c": """int foronlybody(void) {
    for (;;) {
        bodyb();
    }
    return 0;
}
""",
}


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


class CScfgAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        for relative, value in SOURCES.items():
            write(self.root / relative, value)
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)
        analyze_c_ast_scope(self.root, "src", self.database)
        analyze_c_cfg_scope(self.root, "src", self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def text(self, relative: str) -> str:
        return (self.root / relative).read_bytes().decode("utf-8")

    def graph(self, relative: str):
        result = query_analysis(
            self.root, "cfg", file_path=relative, database=self.database)
        self.assertEqual(result["status"], "available", result.get("reason"))
        self.assertEqual(result["analyzer"], "cfg:c")
        nodes = {node["analysis_node_id"]: node for node in result["nodes"]}
        successors: dict[str, list[tuple[str, str]]] = {}
        for edge in result["edges"]:
            successors.setdefault(edge["src_id"], []).append(
                (edge["dst_id"], edge["kind"]))
        entry = next(node for node in nodes.values() if node["kind"] == "entry")
        exit_node = next(node for node in nodes.values() if node["kind"] == "exit")
        return {
            "result": result, "nodes": nodes, "succ": successors,
            "entry": entry["analysis_node_id"],
            "exit": exit_node["analysis_node_id"],
        }

    def by_span(self, graph, relative: str, needle: str) -> str:
        source = self.text(relative)
        index = source.index(needle)
        span = (index, index + len(needle))
        matches = [
            node for node in graph["nodes"].values()
            if (node["payload"]["start_byte"], node["payload"]["end_byte"]) == span
        ]
        self.assertEqual(len(matches), 1, (needle, len(matches)))
        return matches[0]["analysis_node_id"]

    def by_role(self, graph, role: str) -> list[str]:
        return [
            node_id for node_id, node in graph["nodes"].items()
            if node["payload"]["structural_role"] == role
        ]

    def test_scfg01_and(self) -> None:
        graph = self.graph("src/scfg01.c")
        source = "src/scfg01.c"
        a = self.by_span(graph, source, "s1a > 0")
        b = self.by_span(graph, source, "s1b() != 2")
        yes = self.by_span(graph, source, "yes01()")
        no = self.by_span(graph, source, "no01()")
        merge = self.by_role(graph, "branch_merge")[0]
        self.assertEqual(graph["succ"][graph["entry"]], [(a, "next")])
        self.assertEqual(
            sorted(graph["succ"][a]), sorted([(b, "true"), (merge, "false")]))
        self.assertEqual(
            sorted(graph["succ"][b]), sorted([(yes, "true"), (merge, "false")]))
        self.assertEqual(graph["succ"][merge], [(no, "next")])
        self.assertNotIn((yes, "true"), graph["succ"][a])

    def test_scfg02_or(self) -> None:
        graph = self.graph("src/scfg02.c")
        source = "src/scfg02.c"
        a = self.by_span(graph, source, "s2a > 0")
        b = self.by_span(graph, source, "s2b() != 2")
        yes = self.by_span(graph, source, "yes02()")
        no = self.by_span(graph, source, "no02()")
        merge = self.by_role(graph, "branch_merge")[0]
        self.assertEqual(
            sorted(graph["succ"][a]), sorted([(yes, "true"), (b, "false")]))
        self.assertEqual(
            sorted(graph["succ"][b]), sorted([(yes, "true"), (merge, "false")]))
        self.assertEqual(graph["succ"][merge], [(no, "next")])

    def test_scfg03_chained_and(self) -> None:
        graph = self.graph("src/scfg03.c")
        source = "src/scfg03.c"
        a = self.by_span(graph, source, "s3a > 0")
        b = self.by_span(graph, source, "s3b > 1")
        c = self.by_span(graph, source, "s3c() != 2")
        yes = self.by_span(graph, source, "yes03()")
        no = self.by_span(graph, source, "no03()")
        merge = self.by_role(graph, "branch_merge")[0]
        self.assertEqual(
            sorted(graph["succ"][a]), sorted([(b, "true"), (merge, "false")]))
        self.assertEqual(
            sorted(graph["succ"][b]), sorted([(c, "true"), (merge, "false")]))
        self.assertEqual(
            sorted(graph["succ"][c]), sorted([(yes, "true"), (merge, "false")]))
        self.assertEqual(graph["succ"][merge], [(no, "next")])

    def test_scfg04_chained_or(self) -> None:
        graph = self.graph("src/scfg04.c")
        source = "src/scfg04.c"
        a = self.by_span(graph, source, "s4a > 0")
        b = self.by_span(graph, source, "s4b > 1")
        c = self.by_span(graph, source, "s4c() != 2")
        yes = self.by_span(graph, source, "yes04()")
        no = self.by_span(graph, source, "no04()")
        merge = self.by_role(graph, "branch_merge")[0]
        self.assertEqual(
            sorted(graph["succ"][a]), sorted([(yes, "true"), (b, "false")]))
        self.assertEqual(
            sorted(graph["succ"][b]), sorted([(yes, "true"), (c, "false")]))
        self.assertEqual(
            sorted(graph["succ"][c]), sorted([(yes, "true"), (merge, "false")]))
        self.assertEqual(graph["succ"][merge], [(no, "next")])

    def test_scfg05a_and_nested_or(self) -> None:
        graph = self.graph("src/scfg05a.c")
        source = "src/scfg05a.c"
        a = self.by_span(graph, source, "s5a > 0")
        b = self.by_span(graph, source, "s5b > 1")
        c = self.by_span(graph, source, "s5c() != 2")
        yes = self.by_span(graph, source, "yes05()")
        no = self.by_span(graph, source, "no05()")
        merge = self.by_role(graph, "branch_merge")[0]
        self.assertEqual(
            sorted(graph["succ"][a]), sorted([(b, "true"), (merge, "false")]))
        self.assertEqual(
            sorted(graph["succ"][b]), sorted([(yes, "true"), (c, "false")]))
        self.assertEqual(
            sorted(graph["succ"][c]), sorted([(yes, "true"), (merge, "false")]))
        self.assertEqual(graph["succ"][merge], [(no, "next")])

    def test_scfg05b_or_grouped_then_and(self) -> None:
        graph = self.graph("src/scfg05b.c")
        source = "src/scfg05b.c"
        a = self.by_span(graph, source, "s5a > 0")
        b = self.by_span(graph, source, "s5b() != 2")
        c = self.by_span(graph, source, "s5c > 1")
        yes = self.by_span(graph, source, "yes05()")
        no = self.by_span(graph, source, "no05()")
        merge = self.by_role(graph, "branch_merge")[0]
        self.assertEqual(
            sorted(graph["succ"][a]), sorted([(c, "true"), (b, "false")]))
        self.assertEqual(
            sorted(graph["succ"][b]), sorted([(c, "true"), (merge, "false")]))
        self.assertEqual(
            sorted(graph["succ"][c]), sorted([(yes, "true"), (merge, "false")]))
        self.assertEqual(graph["succ"][merge], [(no, "next")])

    def test_scfg06_ternary_condition(self) -> None:
        graph = self.graph("src/scfg06.c")
        source = "src/scfg06.c"
        cond = self.by_span(graph, source, "s6a > 0")
        f = self.by_span(graph, source, "f06() != 1")
        g = self.by_span(graph, source, "g06() != 2")
        yes = self.by_span(graph, source, "yes06()")
        no = self.by_span(graph, source, "no06()")
        merge = self.by_role(graph, "branch_merge")[0]
        self.assertEqual(
            sorted(graph["succ"][cond]), sorted([(f, "true"), (g, "false")]))
        self.assertEqual(
            sorted(graph["succ"][f]), sorted([(yes, "true"), (merge, "false")]))
        self.assertEqual(
            sorted(graph["succ"][g]), sorted([(yes, "true"), (merge, "false")]))
        self.assertEqual(graph["succ"][merge], [(no, "next")])

    def test_scfg07_while_and(self) -> None:
        graph = self.graph("src/scfg07.c")
        source = "src/scfg07.c"
        a = self.by_span(graph, source, "s7a > 0")
        b = self.by_span(graph, source, "s7b() != 2")
        body = self.by_span(graph, source, "body07()")
        after = self.by_span(graph, source, "after07()")
        exit_merge = self.by_role(graph, "loop_exit")[0]
        self.assertEqual(graph["succ"][graph["entry"]], [(a, "next")])
        self.assertEqual(
            sorted(graph["succ"][a]), sorted([(b, "true"), (exit_merge, "false")]))
        self.assertEqual(
            sorted(graph["succ"][b]), sorted([(body, "true"), (exit_merge, "false")]))
        self.assertIn((a, "back"), graph["succ"][body])
        self.assertEqual(graph["succ"][exit_merge], [(after, "next")])

    def test_scfg08_for_or_skips_second_operand(self) -> None:
        graph = self.graph("src/scfg08.c")
        source = "src/scfg08.c"
        a = self.by_span(graph, source, "s8a > 0")
        b = self.by_span(graph, source, "s8b() != 2")
        body = self.by_span(graph, source, "body08()")
        after = self.by_span(graph, source, "after08()")
        exit_merge = self.by_role(graph, "loop_exit")[0]
        self.assertEqual(graph["succ"][graph["entry"]], [(a, "next")])
        self.assertEqual(
            sorted(graph["succ"][a]), sorted([(body, "true"), (b, "false")]))
        self.assertEqual(
            sorted(graph["succ"][b]), sorted([(body, "true"), (exit_merge, "false")]))
        self.assertIn((a, "back"), graph["succ"][body])
        self.assertEqual(graph["succ"][exit_merge], [(after, "next")])

    def test_scfg09_return_and(self) -> None:
        graph = self.graph("src/scfg09.c")
        source = "src/scfg09.c"
        a = self.by_span(graph, source, "s9a > 0")
        b = self.by_span(graph, source, "s9b() != 2")
        ret = self.by_span(graph, source, "return s9a > 0 && s9b() != 2;")
        self.assertEqual(
            sorted(graph["succ"][a]), sorted([(b, "true"), (ret, "false")]))
        self.assertEqual(
            sorted(graph["succ"][b]), sorted([(ret, "true"), (ret, "false")]))
        self.assertEqual(graph["succ"][ret], [(graph["exit"], "return")])

    def test_scfg10_assignment_ternary(self) -> None:
        graph = self.graph("src/scfg10.c")
        source = "src/scfg10.c"
        cond = self.by_span(graph, source, "s10a > 0")
        foo = self.by_span(graph, source, "foo10()")
        bar = self.by_span(graph, source, "bar10()")
        assign = self.by_span(graph, source, "s10x = s10a > 0 ? foo10() : bar10();")
        after = self.by_span(graph, source, "after10()")
        self.assertEqual(graph["succ"][graph["entry"]], [(cond, "next")])
        self.assertEqual(
            sorted(graph["succ"][cond]), sorted([(foo, "true"), (bar, "false")]))
        self.assertEqual(graph["succ"][foo], [(assign, "next")])
        self.assertEqual(graph["succ"][bar], [(assign, "next")])
        self.assertEqual(graph["succ"][assign], [(after, "next")])

    def test_scfg_deterministic_digest(self) -> None:
        def digest(relative: str) -> str:
            with closing(sqlite3.connect(self.database)) as connection:
                connection.row_factory = sqlite3.Row
                rows = [
                    dict(row) for row in connection.execute(
                        "SELECT n.analysis_node_id, n.kind, n.payload FROM "
                        "analysis_nodes n JOIN files f ON f.file_id=n.file_id "
                        "WHERE f.path=? AND n.feature='cfg:c' "
                        "ORDER BY n.analysis_node_id", (relative,))
                ]
                edges = [
                    dict(row) for row in connection.execute(
                        "SELECT e.analysis_edge_id, e.src_id, e.dst_id, e.kind FROM "
                        "analysis_edges e JOIN files f ON f.file_id=e.file_id "
                        "WHERE f.path=? AND e.feature='cfg:c' "
                        "ORDER BY e.analysis_edge_id", (relative,))
                ]
            return hashlib.sha256(json.dumps(
                [rows, edges], sort_keys=True).encode()).hexdigest()

        before = {relative: digest(relative) for relative in SOURCES}
        analyze_c_cfg_scope(self.root, "src", self.database)
        for relative, value in before.items():
            self.assertEqual(digest(relative), value, relative)

    def test_for_roles_normalized(self) -> None:
        ast = query_analysis(
            self.root, "ast", file_path="src/forfull.c", database=self.database)
        self.assertEqual(ast["status"], "available")
        by_span = {}
        source = self.text("src/forfull.c")
        for needle in ("i = 0", "i < 2", "i += 1", "{\n        bodyf();\n    }"):
            index = source.index(needle)
            by_span[needle] = (index, index + len(needle))
        roles = {
            node["payload"]["part_role"]: node["payload"]["parser_kind"]
            for node in ast["nodes"]
            if node["payload"].get("part_role")
        }
        self.assertEqual(roles.get("init"), "assignment_expression")
        self.assertEqual(roles.get("condition"), "binary_expression")
        self.assertEqual(roles.get("update"), "assignment_expression")
        self.assertEqual(roles.get("body"), "compound_statement")

    def _for_cfg(self, relative: str):
        graph = self.graph(relative)
        source = relative
        text = self.text(source)
        init = self.by_span(graph, source, "i = 0") if "i = 0" in text else None
        cond = self.by_span(graph, source, "i < 2") if "i < 2" in text else None
        update = self.by_span(graph, source, "i += 1") if "i += 1" in text else None
        return graph, init, cond, update

    def test_for_full_cfg_edges_unchanged(self) -> None:
        graph, init, cond, update = self._for_cfg("src/forfull.c")
        body = self.by_span(graph, "src/forfull.c", "bodyf()")
        self.assertEqual(graph["succ"][graph["entry"]], [(init, "next")])
        self.assertEqual(graph["succ"][init], [(cond, "next")])
        exit_merge = self.by_role(graph, "loop_exit")[0]
        self.assertEqual(
            sorted(graph["succ"][cond]), sorted([(body, "true"), (exit_merge, "false")]))
        self.assertEqual(graph["succ"][body], [(update, "next")])
        self.assertEqual(graph["succ"][update], [(cond, "back")])

    def test_for_no_init_starts_at_condition(self) -> None:
        graph, _, cond, update = self._for_cfg("src/fornoinit.c")
        body = self.by_span(graph, "src/fornoinit.c", "bodyn()")
        self.assertEqual(graph["succ"][graph["entry"]], [(cond, "next")])
        exit_merge = self.by_role(graph, "loop_exit")[0]
        self.assertEqual(
            sorted(graph["succ"][cond]), sorted([(body, "true"), (exit_merge, "false")]))
        self.assertEqual(graph["succ"][update], [(cond, "back")])

    def test_for_no_condition_synthetic_header(self) -> None:
        graph, init, _, update = self._for_cfg("src/fornocond.c")
        body = self.by_span(graph, "src/fornocond.c", "bodyc()")
        headers = [
            node_id for node_id, node in graph["nodes"].items()
            if node["kind"] == "condition"
            and node["payload"]["structural_role"] == "loop_header"
            and node["payload"]["start_byte"] is None
        ]
        self.assertEqual(len(headers), 1)
        header = headers[0]
        self.assertIsNone(graph["nodes"][header]["payload"]["ast_node_id"])
        self.assertEqual(graph["succ"][graph["entry"]], [(init, "next")])
        self.assertEqual(graph["succ"][init], [(header, "next")])
        exit_merge = self.by_role(graph, "loop_exit")[0]
        self.assertEqual(
            sorted(graph["succ"][header]),
            sorted([(body, "true"), (exit_merge, "false")]))
        self.assertEqual(graph["succ"][body], [(update, "next")])
        self.assertEqual(graph["succ"][update], [(header, "back")])

    def test_for_no_update_continues_to_condition(self) -> None:
        graph, init, cond, _ = self._for_cfg("src/fornoupdate.c")
        body = self.by_span(graph, "src/fornoupdate.c", "bodyu()")
        self.assertEqual(graph["succ"][init], [(cond, "next")])
        exit_merge = self.by_role(graph, "loop_exit")[0]
        self.assertEqual(
            sorted(graph["succ"][cond]), sorted([(body, "true"), (exit_merge, "false")]))
        self.assertEqual(graph["succ"][body], [(cond, "back")])

    def test_for_only_body_self_looping_header(self) -> None:
        graph = self.graph("src/foronlybody.c")
        body = self.by_span(graph, "src/foronlybody.c", "bodyb()")
        headers = [
            node_id for node_id, node in graph["nodes"].items()
            if node["kind"] == "condition"
            and node["payload"]["structural_role"] == "loop_header"
            and node["payload"]["start_byte"] is None
        ]
        self.assertEqual(len(headers), 1)
        header = headers[0]
        self.assertEqual(graph["succ"][graph["entry"]], [(header, "next")])
        exit_merge = self.by_role(graph, "loop_exit")[0]
        self.assertEqual(
            sorted(graph["succ"][header]),
            sorted([(body, "true"), (exit_merge, "false")]))
        self.assertEqual(graph["succ"][body], [(header, "back")])
        ret = self.by_span(graph, "src/foronlybody.c", "return 0;")
        self.assertEqual(graph["succ"][exit_merge], [(ret, "next")])
        self.assertEqual(graph["succ"][ret], [(graph["exit"], "return")])

    def test_builder_source_has_no_parser_api(self) -> None:
        import inspect

        from provenlattice import analysis_c_cfg
        source = inspect.getsource(analysis_c_cfg)
        for token in (
            "tree_sitter", "child_by_field_name",
            "field_name_for_child", "parser_for_language", "parse_bytes",
        ):
            self.assertNotIn(token, source)

    def test_builder_runs_on_handbuilt_normalized_tree(self) -> None:
        def node(kind, parser_kind, start, end, children=(), name=None, role=None):
            return NormalizedCNode(
                kind=kind, parser_kind=parser_kind, start_byte=start,
                end_byte=end, start_point=(0, start), end_point=(0, end),
                child_order=0, name=name, is_error=False, is_missing=False,
                children=tuple(children), part_role=role,
            )

        a = node("IDENTIFIER", "identifier", 40, 41, name="a")
        b = node("CALL", "call_expression", 45, 49, name="b")
        logical = node("LOGICAL_AND", "binary_expression", 40, 49, children=(a, b))
        if_parts = [
            node("SYNTAX", "parenthesized_expression", 39, 50,
                 children=(logical,), role="condition"),
            node("EXPRESSION", "expression_statement", 56, 63,
                 children=(node("CALL", "call_expression", 56, 62, name="yes"),),
                 role="consequence"),
        ]
        if_stmt = node(
            "CONTROL_STRUCTURE", "if_statement", 36, 63, children=if_parts)
        no = node("EXPRESSION", "expression_statement", 68, 74,
                  children=(node("CALL", "call_expression", 68, 73, name="no"),))
        body = node("BLOCK", "compound_statement", 32, 80, children=(if_stmt, no))
        function = node(
            "FUNCTION", "function_definition", 0, 80, children=(body,), name="t")
        index = _index_tree(function)
        graph = _FunctionCFG("file:test", "symbol:t")
        builder = _CCfgBuilder(graph, index, set(), {}, set())
        builder.build(function)
        node_by_span = {
            (n.payload["start_byte"], n.payload["end_byte"]): n
            for n in graph.nodes
        }
        a_id = node_by_span[(40, 41)].analysis_node_id
        b_id = node_by_span[(45, 49)].analysis_node_id
        yes_id = node_by_span[(56, 62)].analysis_node_id
        succ = {}
        for edge in graph.edges:
            succ.setdefault(edge.src_id, []).append((edge.dst_id, edge.kind))
        false_targets = [dst for dst, kind in succ[a_id] if kind == "false"]
        self.assertEqual(len(false_targets), 1)
        merge_id = false_targets[0]
        merge_node = next(
            n for n in graph.nodes if n.analysis_node_id == merge_id)
        self.assertIsNone(merge_node.payload["ast_node_id"])
        self.assertIn((b_id, "true"), succ[a_id])
        self.assertIn((yes_id, "true"), succ[b_id])
        self.assertIn((merge_id, "false"), succ[b_id])
        self.assertIn((merge_id, "next"), succ[yes_id])


if __name__ == "__main__":
    unittest.main()
