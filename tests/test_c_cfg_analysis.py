from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice.analysis_c_ast import analyze_c_ast_scope
from provenlattice.analysis_c_cfg import analyze_c_cfg_scope
from provenlattice.analysis_query import query_analysis
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope
from provenlattice.query import GraphQuery


def fix(number: int, body: str) -> str:
    return f"int ccfg{number:02d}(int x) {{\n{body}}}\n"


SOURCES = {
    "src/ccfg01.c": """int seq01(void) {
    a01();
    b01();
    return 0;
}
""",
    "src/ccfg02.c": """int if02(int x) {
    if (x) {
        a02();
    }
    b02();
    return 0;
}
""",
    "src/ccfg03.c": """int ifelse03(int x) {
    if (x) {
        a03();
    } else {
        b03();
    }
    c03();
    return 0;
}
""",
    "src/ccfg04.c": """int while04(int x) {
    while (x > 0) {
        a04();
        x -= 1;
    }
    b04();
    return 0;
}
""",
    "src/ccfg05.c": """int dowhile05(int x) {
    do {
        a05();
    } while (x > 0);
    b05();
    return 0;
}
""",
    "src/ccfg06.c": """int for06(int n) {
    for (int i = 0; i < n; i += 1) {
        if (i == 2) {
            continue;
        }
        a06();
    }
    b06();
    return 0;
}
""",
    "src/ccfg07.c": """int nested07(int n) {
    while (n > 0) {
        for (int i = 0; i < n; i += 1) {
            if (i == 1) {
                continue;
            }
            if (i == 2) {
                break;
            }
            a07();
        }
        n -= 1;
    }
    return 0;
}
""",
    "src/ccfg08.c": """int swbreak08(int x) {
    switch (x) {
    case 1:
        a08();
        break;
    case 2:
        b08();
        break;
    }
    c08();
    return 0;
}
""",
    "src/ccfg09.c": """int swfall09(int x) {
    switch (x) {
    case 1:
        a09();
    case 2:
        b09();
        break;
    }
    c09();
    return 0;
}
""",
    "src/ccfg10.c": """int swdef10(int x) {
    switch (x) {
    case 1:
        a10();
        break;
    default:
        b10();
    }
    c10();
    return 0;
}
""",
    "src/ccfg11.c": """int gfwd11(int x) {
    if (x) {
        goto cleanup;
    }
    work11();
cleanup:
    release11();
    return 0;
}
""",
    "src/ccfg12.c": """int gback12(int x) {
retry:
    work12();
    if (x) {
        goto retry;
    }
    return 0;
}
""",
    "src/ccfg13.c": """int multi13(int x) {
    if (x) {
        return 1;
    }
    return 2;
}
""",
    "src/ccfg14.c": """void implicit14(int x) {
    a14();
}
""",
    "src/ccfg15.c": """int badlabel15(int x) {
    goto nowhere15;
    return 0;
}
""",
    "src/combine.c": """int combine(int x) {
    if (x) {
        return 1;
    }
    x += 1;
    return x;
}
""",
    "src/hdrbody.h": """static inline int hdr16(int v) {
    if (v) {
        return 1;
    }
    return 0;
}
""",
    "src/malformed.c": """int broken17(int value) {
    if (value > 0) {
        return value
""",
}


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


class CCfgAnalysisTests(unittest.TestCase):
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

    def by_role(self, graph, role: str, kind: str | None = None) -> list[str]:
        return [
            node_id for node_id, node in graph["nodes"].items()
            if node["payload"]["structural_role"] == role
            and (kind is None or node["kind"] == kind)
        ]

    def by_role_ordered(self, graph, role: str) -> list[str]:
        return sorted(
            self.by_role(graph, role),
            key=lambda node_id: graph["nodes"][node_id]["payload"]["start_byte"])

    def state_metadata(self, relative: str, feature: str = "cfg:c") -> dict:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT s.metadata FROM analysis_state s JOIN files f "
                "ON f.file_id=s.file_id WHERE f.path=? AND s.feature=?",
                (relative, feature),
            ).fetchone()
        self.assertIsNotNone(row, (relative, feature))
        return json.loads(row["metadata"])

    def test_ccfg01_sequence(self) -> None:
        graph = self.graph("src/ccfg01.c")
        a = self.by_span(graph, "src/ccfg01.c", "a01()")
        b = self.by_span(graph, "src/ccfg01.c", "b01()")
        ret = self.by_span(graph, "src/ccfg01.c", "return 0;")
        self.assertEqual(graph["succ"][graph["entry"]], [(a, "next")])
        self.assertEqual(graph["succ"][a], [(b, "next")])
        self.assertEqual(graph["succ"][b], [(ret, "next")])
        self.assertEqual(graph["succ"][ret], [(graph["exit"], "return")])

    def test_ccfg02_if(self) -> None:
        graph = self.graph("src/ccfg02.c")
        source = "src/ccfg02.c"
        cond = self.by_span(graph, source, "(x)")
        a = self.by_span(graph, source, "a02()")
        b = self.by_span(graph, source, "b02()")
        merge = self.by_role(graph, "branch_merge")[0]
        self.assertEqual(graph["succ"][graph["entry"]], [(cond, "next")])
        self.assertIn((a, "true"), graph["succ"][cond])
        self.assertIn((merge, "false"), graph["succ"][cond])
        self.assertIn((merge, "next"), graph["succ"][a])
        self.assertEqual(graph["succ"][merge], [(b, "next")])

    def test_ccfg03_if_else(self) -> None:
        graph = self.graph("src/ccfg03.c")
        source = "src/ccfg03.c"
        cond = self.by_span(graph, source, "(x)")
        a = self.by_span(graph, source, "a03()")
        b = self.by_span(graph, source, "b03()")
        c = self.by_span(graph, source, "c03()")
        merge = self.by_role(graph, "branch_merge")[0]
        self.assertIn((a, "true"), graph["succ"][cond])
        self.assertIn((b, "false"), graph["succ"][cond])
        self.assertIn((merge, "next"), graph["succ"][a])
        self.assertIn((merge, "next"), graph["succ"][b])
        self.assertEqual(graph["succ"][merge], [(c, "next")])

    def test_ccfg04_while(self) -> None:
        graph = self.graph("src/ccfg04.c")
        source = "src/ccfg04.c"
        cond = self.by_span(graph, source, "(x > 0)")
        a = self.by_span(graph, source, "a04()")
        dec = self.by_span(graph, source, "x -= 1;")
        b = self.by_span(graph, source, "b04()")
        self.assertIn((a, "true"), graph["succ"][cond])
        self.assertIn((dec, "next"), graph["succ"][a])
        self.assertIn((cond, "back"), graph["succ"][dec])
        exit_merge = self.by_role(graph, "loop_exit")[0]
        self.assertIn((exit_merge, "false"), graph["succ"][cond])
        self.assertEqual(graph["succ"][exit_merge], [(b, "next")])

    def test_ccfg05_do_while_body_runs_first(self) -> None:
        graph = self.graph("src/ccfg05.c")
        source = "src/ccfg05.c"
        cond = self.by_span(graph, source, "(x > 0)")
        a = self.by_span(graph, source, "a05()")
        b = self.by_span(graph, source, "b05()")
        self.assertEqual(graph["succ"][graph["entry"]], [(a, "next")])
        self.assertIn((cond, "back"), graph["succ"][a])
        self.assertIn((a, "true"), graph["succ"][cond])
        exit_merge = self.by_role(graph, "loop_exit")[0]
        self.assertIn((exit_merge, "false"), graph["succ"][cond])
        self.assertEqual(graph["succ"][exit_merge], [(b, "next")])

    def test_ccfg06_for_and_continue_to_update(self) -> None:
        graph = self.graph("src/ccfg06.c")
        source = "src/ccfg06.c"
        init = self.by_span(graph, source, "int i = 0;")
        cond = self.by_span(graph, source, "i < n")
        update = self.by_span(graph, source, "i += 1")
        continue_node = self.by_span(graph, source, "continue;")
        a = self.by_span(graph, source, "a06()")
        self.assertEqual(
            graph["nodes"][init]["payload"]["structural_role"], "for_init")
        self.assertEqual(
            graph["nodes"][update]["payload"]["structural_role"], "for_update")
        self.assertEqual(graph["succ"][graph["entry"]], [(init, "next")])
        self.assertIn((cond, "next"), graph["succ"][init])
        self.assertIn((update, "continue"), graph["succ"][continue_node])
        self.assertIn((cond, "back"), graph["succ"][update])
        self.assertIn((update, "next"), graph["succ"][a])
        exit_merge = self.by_role(graph, "loop_exit")[0]
        self.assertIn((exit_merge, "false"), graph["succ"][cond])
        self.assertEqual(graph["succ"][exit_merge], [(self.by_span(graph, source, "b06()"), "next")])

    def test_ccfg07_nested_break_and_continue_scoping(self) -> None:
        graph = self.graph("src/ccfg07.c")
        source = "src/ccfg07.c"
        while_cond = self.by_span(graph, source, "(n > 0)")
        for_cond = self.by_span(graph, source, "i < n")
        update = self.by_span(graph, source, "i += 1")
        continue_node = self.by_span(graph, source, "continue;")
        break_node = self.by_span(graph, source, "break;")
        for_exit = next(
            dst for dst, kind in graph["succ"][for_cond] if kind == "false")
        while_exit = next(
            dst for dst, kind in graph["succ"][while_cond] if kind == "false")
        self.assertEqual(
            graph["nodes"][update]["payload"]["structural_role"], "for_update")
        self.assertIn((update, "continue"), graph["succ"][continue_node])
        self.assertIn((for_exit, "break"), graph["succ"][break_node])
        self.assertNotIn((while_exit, "break"), graph["succ"][break_node])
        self.assertEqual(
            graph["nodes"][break_node]["payload"]["structural_role"], "statement")

    def test_ccfg08_switch_break(self) -> None:
        graph = self.graph("src/ccfg08.c")
        source = "src/ccfg08.c"
        cond = self.by_span(graph, source, "(x)")
        case1, case2 = self.by_role_ordered(graph, "switch_case")
        a = self.by_span(graph, source, "a08()")
        b = self.by_span(graph, source, "b08()")
        brk1 = self.by_span(graph, source, "break;")
        c = self.by_span(graph, source, "c08()")
        switch_exit = self.by_role(graph, "switch_exit")[0]
        self.assertIn((case1, "case"), graph["succ"][cond])
        self.assertIn((case2, "case"), graph["succ"][cond])
        self.assertIn((a, "next"), graph["succ"][case1])
        self.assertIn((switch_exit, "break"), graph["succ"][brk1])
        self.assertIn((b, "next"), graph["succ"][case2])
        self.assertNotIn((case2, "fallthrough"), graph["succ"][a])
        self.assertEqual(graph["succ"][switch_exit], [(c, "next")])

    def test_ccfg09_switch_fallthrough(self) -> None:
        graph = self.graph("src/ccfg09.c")
        source = "src/ccfg09.c"
        cond = self.by_span(graph, source, "(x)")
        case1, case2 = self.by_role_ordered(graph, "switch_case")
        a = self.by_span(graph, source, "a09()")
        b = self.by_span(graph, source, "b09()")
        brk = self.by_span(graph, source, "break;")
        self.assertIn((case1, "case"), graph["succ"][cond])
        self.assertIn((case2, "case"), graph["succ"][cond])
        self.assertIn((a, "next"), graph["succ"][case1])
        self.assertIn((case2, "fallthrough"), graph["succ"][a])
        self.assertIn((b, "next"), graph["succ"][case2])
        switch_exit = self.by_role(graph, "switch_exit")[0]
        self.assertIn((switch_exit, "break"), graph["succ"][brk])

    def test_ccfg10_default(self) -> None:
        graph = self.graph("src/ccfg10.c")
        source = "src/ccfg10.c"
        cond = self.by_span(graph, source, "(x)")
        cases = self.by_role(graph, "switch_case")
        defaults = self.by_role(graph, "switch_default")
        self.assertEqual(len(cases), 1)
        self.assertEqual(len(defaults), 1)
        self.assertIn((cases[0], "case"), graph["succ"][cond])
        self.assertIn((defaults[0], "default"), graph["succ"][cond])
        self.assertEqual(
            graph["nodes"][defaults[0]]["payload"]["metadata"], {"default": True})
        b = self.by_span(graph, source, "b10()")
        switch_exit = self.by_role(graph, "switch_exit")[0]
        self.assertIn((switch_exit, "next"), graph["succ"][b])

    def test_ccfg11_goto_forward(self) -> None:
        graph = self.graph("src/ccfg11.c")
        source = "src/ccfg11.c"
        goto_node = self.by_span(graph, source, "goto cleanup;")
        label = self.by_role(graph, "label")[0]
        work = self.by_span(graph, source, "work11()")
        release = self.by_span(graph, source, "release11()")
        self.assertIn((label, "goto"), graph["succ"][goto_node])
        self.assertNotIn((work, "goto"), graph["succ"][goto_node])
        self.assertEqual(
            graph["nodes"][label]["payload"]["metadata"], {"name": "cleanup"})
        self.assertIn((label, "next"), graph["succ"][work])
        self.assertIn((release, "next"), graph["succ"][label])

    def test_ccfg12_goto_backward(self) -> None:
        graph = self.graph("src/ccfg12.c")
        source = "src/ccfg12.c"
        goto_node = self.by_span(graph, source, "goto retry;")
        label = self.by_role(graph, "label")[0]
        work = self.by_span(graph, source, "work12()")
        self.assertIn((label, "goto"), graph["succ"][goto_node])
        self.assertLess(
            graph["nodes"][label]["payload"]["start_byte"],
            graph["nodes"][goto_node]["payload"]["start_byte"])
        self.assertIn((work, "next"), graph["succ"][label])

    def test_ccfg13_multiple_returns(self) -> None:
        graph = self.graph("src/ccfg13.c")
        source = "src/ccfg13.c"
        ret1 = self.by_span(graph, source, "return 1;")
        ret2 = self.by_span(graph, source, "return 2;")
        merge = self.by_role(graph, "branch_merge")[0]
        self.assertIn((graph["exit"], "return"), graph["succ"][ret1])
        self.assertIn((graph["exit"], "return"), graph["succ"][ret2])
        self.assertIn((ret2, "next"), graph["succ"][merge])
        self.assertEqual(graph["succ"][ret2], [(graph["exit"], "return")])

    def test_ccfg14_implicit_exit(self) -> None:
        graph = self.graph("src/ccfg14.c")
        a = self.by_span(graph, "src/ccfg14.c", "a14()")
        self.assertEqual(graph["succ"][graph["entry"]], [(a, "next")])
        self.assertEqual(graph["succ"][a], [(graph["exit"], "next")])
        self.assertNotIn("return", {edge["kind"] for edge in graph["result"]["edges"]})

    def test_ccfg15_unresolved_label_degraded(self) -> None:
        graph = self.graph("src/ccfg15.c")
        goto_node = self.by_span(graph, "src/ccfg15.c", "goto nowhere15;")
        self.assertEqual(graph["succ"].get(goto_node, []), [])
        ret = self.by_span(graph, "src/ccfg15.c", "return 0;")
        self.assertIn((graph["exit"], "return"), graph["succ"][ret])
        metadata = self.state_metadata("src/ccfg15.c")
        self.assertEqual(metadata["unresolved_gotos"], 1)
        self.assertEqual(metadata["analysis_status"], "degraded")

    def test_cfg_deterministic_digest(self) -> None:
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
            canonical = json.dumps([rows, edges], sort_keys=True)
            return hashlib.sha256(canonical.encode()).hexdigest()

        before = {relative: digest(relative) for relative in SOURCES}
        analyze_c_cfg_scope(self.root, "src", self.database)
        for relative, value in before.items():
            self.assertEqual(digest(relative), value, relative)

    def test_ast_cfg_coexistence_and_shared_identity(self) -> None:
        analyze_scope(self.root, "src", "ast,cfg", self.database)
        ast = query_analysis(
            self.root, "ast", file_path="src/combine.c", database=self.database)
        cfg = query_analysis(
            self.root, "cfg", file_path="src/combine.c", database=self.database)
        self.assertEqual(ast["status"], "available")
        self.assertEqual(cfg["status"], "available")
        ast_ids = {node["analysis_node_id"] for node in ast["nodes"]}
        ast_by_id = {node["analysis_node_id"]: node for node in ast["nodes"]}
        for node in cfg["nodes"]:
            link = node["payload"].get("ast_node_id")
            if node["kind"] in {"entry", "exit", "merge"}:
                self.assertIsNone(link)
                continue
            self.assertIn(link, ast_ids, node["kind"])
            self.assertEqual(
                node["payload"]["start_byte"],
                ast_by_id[link]["payload"]["start_byte"])
            self.assertEqual(
                node["payload"]["end_byte"],
                ast_by_id[link]["payload"]["end_byte"])

    def test_ast_only_request_builds_no_cfg(self) -> None:
        with tempfile.TemporaryDirectory() as fresh:
            root = Path(fresh) / "repo"
            write(root / "src" / "solo.c", SOURCES["src/ccfg01.c"])
            database = Path(fresh) / "fresh.db"
            full_index(root, database)
            analyze_scope(root, "src", "ast", database)
            with closing(sqlite3.connect(database)) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM analysis_state WHERE feature='cfg:c'"
                ).fetchone()[0]
            self.assertEqual(count, 0)
            not_built = query_analysis(
                root, "cfg", file_path="src/solo.c", database=database)
            self.assertEqual(not_built["status"], "not_built")
            combined = analyze_scope(root, "src", "ast,cfg", database)
            self.assertEqual(combined["status"], "complete")
            available = query_analysis(
                root, "cfg", file_path="src/solo.c", database=database)
            self.assertEqual(available["status"], "available")
            self.assertEqual(available["analyzer"], "cfg:c")

    def test_explain_symbol_cfg(self) -> None:
        analyze_scope(self.root, "src", "cfg", self.database)
        with GraphQuery(self.database) as query:
            explained = query.explain_symbol("seq01", analysis="cfg")
        section = explained["analysis"]["cfg"]
        self.assertEqual(section["status"], "available")
        self.assertIn("entry_id", section)
        self.assertIn("exit_id", section)
        kinds = {node["kind"] for node in section["nodes"]}
        self.assertLessEqual({"entry", "exit", "callsite"}, kinds)

    def test_header_function_cfg(self) -> None:
        graph = self.graph("src/hdrbody.h")
        ret1 = self.by_span(graph, "src/hdrbody.h", "return 1;")
        self.assertIn((graph["exit"], "return"), graph["succ"][ret1])
        cond = self.by_span(graph, "src/hdrbody.h", "(v)")
        merge = self.by_role(graph, "branch_merge")[0]
        self.assertIn((merge, "false"), graph["succ"][cond])

    def test_malformed_source_fail_closed_not_crash(self) -> None:
        result = analyze_c_cfg_scope(self.root, "src", self.database)
        self.assertIn(
            "src/malformed.c",
            result.get("skipped_no_functions_built", []))
        not_built = query_analysis(
            self.root, "cfg", file_path="src/malformed.c", database=self.database)
        self.assertEqual(not_built["status"], "not_built")
        self.assertEqual(not_built["reason"], "analysis_not_built")


if __name__ == "__main__":
    unittest.main()
