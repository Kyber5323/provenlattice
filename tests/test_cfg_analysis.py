from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice.analysis_ast import analyze_ast_scope
from provenlattice.analysis_cfg import analyze_cfg_scope
from provenlattice.graph import full_index
from provenlattice.identity import path_id
from provenlattice.incremental import incremental_update
from provenlattice.query import GraphQuery

SRC_FLOWS = """def seq():
    a()
    b()
    return 1

def branch(x):
    if x:
        a()
    else:
        b()
    c()
    return x

def loop(xs):
    for x in xs:
        work(x)
    done()

def loopctl(xs):
    for x in xs:
        if stop(x):
            break
        if skip(x):
            continue
        work(x)
    after()

def term(x):
    if x:
        return 1
    work()
    return 2
"""

SRC_UNSUPPORTED = """def risky(x):
    try:
        return load(x)
    except KeyError:
        return 0

async def ping():
    await pong()

def gen(xs):
    for x in xs:
        yield x
"""

SRC_MODLEVEL = """flag = True

if flag:
    setting = 1

def configured():
    return setting
"""

SRC_DUP = """def dup():
    return 1

def dup():
    return 2
"""

SRC_HELPERS = """def helper():
    return 7
"""

SRC_FLOWS_V2 = SRC_FLOWS + """
def appended(value):
    return extra(value)
"""


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


def query_row(database: Path, sql: str, params: tuple) -> sqlite3.Row:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(sql, params).fetchone()


def cfg_rows(database: Path, file_id: str) -> tuple[list[dict], list[dict]]:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        nodes = [
            dict(row) for row in connection.execute(
                "SELECT * FROM analysis_nodes WHERE file_id=? AND feature='cfg:python' "
                "ORDER BY analysis_node_id", (file_id,))
        ]
        edges = [
            dict(row) for row in connection.execute(
                "SELECT * FROM analysis_edges WHERE file_id=? AND feature='cfg:python' "
                "ORDER BY analysis_edge_id", (file_id,))
        ]
    for row in nodes:
        row["payload"] = json.loads(row["payload"])
    for row in edges:
        row["payload"] = json.loads(row["payload"])
    return nodes, edges


def cfg_state(database: Path, file_id: str) -> dict | None:
    row = query_row(
        database,
        "SELECT * FROM analysis_state WHERE file_id=? AND feature='cfg:python'",
        (file_id,),
    )
    return dict(row) if row else None


def base_symbol_id(database: Path, file_id: str, name: str) -> str:
    return query_row(
        database,
        "SELECT id FROM nodes WHERE file_id=? AND kind='Function' AND name=?",
        (file_id, name),
    )["id"]


class CFGAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root / "src" / "flows.py", SRC_FLOWS)
        write(self.root / "src" / "unsupported.py", SRC_UNSUPPORTED)
        write(self.root / "src" / "modlevel.py", SRC_MODLEVEL)
        write(self.root / "tests" / "dup.py", SRC_DUP)
        write(self.root / "tests" / "helpers.py", SRC_HELPERS)
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)
        self.repo_id = query_row(
            self.database, "SELECT repo_id FROM repositories LIMIT 1", ()
        )["repo_id"]
        analyze_ast_scope(self.root, "src", self.database)
        analyze_ast_scope(self.root, "tests", self.database)
        self.stores: list = []

    def tearDown(self) -> None:
        for store in self.stores:
            store.storage.close()
        self.temp.cleanup()

    def fid(self, relative_path: str) -> str:
        return path_id(self.repo_id, "File", relative_path)

    def store(self):
        from provenlattice.analysis import AnalysisStore
        from provenlattice.storage import SQLiteStorage
        store = AnalysisStore(SQLiteStorage(self.database))
        self.stores.append(store)
        return store

    def build_cfg(self, scope: str = "src") -> dict:
        return analyze_cfg_scope(self.root, scope, self.database)

    def function_cfg(self, relative_path: str, function_name: str):
        file_id = self.fid(relative_path)
        owner = base_symbol_id(self.database, file_id, function_name)
        nodes, edges = cfg_rows(self.database, file_id)
        nodes = [n for n in nodes if n["payload"]["owner_symbol_id"] == owner]
        node_ids = {n["analysis_node_id"] for n in nodes}
        edges = [e for e in edges if e["payload"]["owner_symbol_id"] == owner]
        by_id = {n["analysis_node_id"]: n for n in nodes}
        successors = {}
        for edge in edges:
            successors.setdefault(edge["src_id"], []).append(
                (edge["kind"], edge["dst_id"]))
        return by_id, successors, nodes, edges

    def unique(self, by_id, kind, role=None):
        matches = [n for n in by_id.values() if n["kind"] == kind
                   and (role is None or n["payload"]["structural_role"] == role)]
        self.assertEqual(len(matches), 1, kind)
        return matches[0]

    def test_g1_sequential_flow(self) -> None:
        self.build_cfg()
        by_id, successors, nodes, edges = self.function_cfg("src/flows.py", "seq")
        self.assertEqual(
            sorted(n["kind"] for n in nodes),
            ["callsite", "callsite", "entry", "exit", "return"],
        )
        entry = self.unique(by_id, "entry")
        exit_node = self.unique(by_id, "exit")
        return_node = self.unique(by_id, "return")
        callsites = sorted(
            (n for n in nodes if n["kind"] == "callsite"),
            key=lambda n: n["payload"]["start_byte"],
        )
        self.assertEqual(successors[entry["analysis_node_id"]],
                         [("next", callsites[0]["analysis_node_id"])])
        self.assertEqual(
            successors[callsites[0]["analysis_node_id"]],
            [("next", callsites[1]["analysis_node_id"])])
        self.assertEqual(successors[callsites[1]["analysis_node_id"]],
                         [("next", return_node["analysis_node_id"])])
        self.assertEqual(successors[return_node["analysis_node_id"]],
                         [("return", exit_node["analysis_node_id"])])
        for callsite in callsites:
            metadata = callsite["payload"]["metadata"]
            self.assertIsNone(metadata["resolved_symbol_id"])
            self.assertEqual(metadata["target_scope"], "unknown")
            self.assertIsNotNone(callsite["payload"]["ast_node_id"])
        self.assertIsNone(entry["payload"]["ast_node_id"])
        self.assertTrue(all(e["payload"]["owner_symbol_id"] ==
                            base_symbol_id(self.database, self.fid("src/flows.py"), "seq")
                            for e in edges))

    def test_g2_if_else_merge(self) -> None:
        self.build_cfg()
        by_id, successors, nodes, _ = self.function_cfg("src/flows.py", "branch")
        condition = self.unique(by_id, "condition", "if_condition")
        merge = self.unique(by_id, "merge", "branch_merge")
        exit_node = self.unique(by_id, "exit")
        return_node = self.unique(by_id, "return")
        cond_out = successors[condition["analysis_node_id"]]
        self.assertEqual(sorted(kind for kind, _ in cond_out), ["false", "true"])
        callsites = sorted(
            (n for n in nodes if n["kind"] == "callsite"),
            key=lambda n: n["payload"]["start_byte"],
        )
        self.assertEqual(len(callsites), 3)
        by_kind = {kind: dst for kind, dst in cond_out}
        true_target = by_id[by_kind["true"]]
        false_target = by_id[by_kind["false"]]
        self.assertEqual(true_target["kind"], "callsite")
        self.assertEqual(false_target["kind"], "callsite")
        self.assertEqual(true_target["payload"]["start_byte"],
                         callsites[0]["payload"]["start_byte"])
        self.assertEqual(false_target["payload"]["start_byte"],
                         callsites[1]["payload"]["start_byte"])
        _, all_edges = cfg_rows(self.database, self.fid("src/flows.py"))
        merge_in = [
            edge for edge in all_edges
            if edge["dst_id"] == merge["analysis_node_id"]
        ]
        self.assertEqual(
            {(edge["src_id"], edge["kind"]) for edge in merge_in},
            {
                (callsites[0]["analysis_node_id"], "next"),
                (callsites[1]["analysis_node_id"], "next"),
            },
        )
        self.assertEqual(len(merge_in), 2)
        self.assertEqual(successors[merge["analysis_node_id"]],
                         [("next", callsites[2]["analysis_node_id"])])
        self.assertEqual(successors[callsites[2]["analysis_node_id"]],
                         [("next", return_node["analysis_node_id"])])
        self.assertEqual(successors[return_node["analysis_node_id"]],
                         [("return", exit_node["analysis_node_id"])])

    def test_g3_loop_back_edge(self) -> None:
        self.build_cfg()
        by_id, successors, nodes, edges = self.function_cfg("src/flows.py", "loop")
        header = self.unique(by_id, "condition", "loop_header")
        exit_merge = self.unique(by_id, "merge", "loop_exit")
        header_out = successors[header["analysis_node_id"]]
        kinds = sorted(kind for kind, _ in header_out)
        self.assertEqual(kinds, ["false", "true"])
        by_kind = dict(header_out)
        body_callsite = by_id[by_kind["true"]]
        self.assertEqual(body_callsite["kind"], "callsite")
        self.assertEqual(successors[body_callsite["analysis_node_id"]],
                         [("back", header["analysis_node_id"])])
        self.assertEqual(by_id[by_kind["false"]]["analysis_node_id"],
                         exit_merge["analysis_node_id"])
        done = [n for n in nodes if n["kind"] == "callsite"
                and n["payload"]["start_byte"] > header["payload"]["end_byte"]][0]
        self.assertEqual(successors[exit_merge["analysis_node_id"]],
                         [("next", done["analysis_node_id"])])

    def test_g4_break_continue(self) -> None:
        self.build_cfg()
        by_id, successors, nodes, edges = self.function_cfg("src/flows.py", "loopctl")
        header = self.unique(by_id, "condition", "loop_header")
        exit_merge = self.unique(by_id, "merge", "loop_exit")
        break_node = self.unique(by_id, "break")
        continue_node = self.unique(by_id, "continue")
        self.assertEqual(successors[break_node["analysis_node_id"]],
                         [("break", exit_merge["analysis_node_id"])])
        self.assertEqual(successors[continue_node["analysis_node_id"]],
                         [("continue", header["analysis_node_id"])])
        work = [n for n in nodes if n["kind"] == "callsite"
                and n["payload"]["start_byte"] > continue_node["payload"]["end_byte"]
                and n["payload"]["start_byte"] < header["payload"]["end_byte"]][0]
        self.assertEqual(successors[work["analysis_node_id"]],
                         [("back", header["analysis_node_id"])])
        after = [n for n in nodes if n["kind"] == "callsite"
                 and n["payload"]["start_byte"] > header["payload"]["end_byte"]][0]
        self.assertEqual(successors[exit_merge["analysis_node_id"]],
                         [("next", after["analysis_node_id"])])
        header_out = dict(successors[header["analysis_node_id"]])
        stop_condition = by_id[header_out["true"]]
        self.assertEqual(stop_condition["kind"], "condition")
        stop_out = dict(successors[stop_condition["analysis_node_id"]])
        self.assertEqual(by_id[stop_out["true"]]["analysis_node_id"],
                         break_node["analysis_node_id"])
        self.assertNotEqual(stop_out["false"], work["analysis_node_id"])

    def test_g5_return_termination(self) -> None:
        self.build_cfg()
        by_id, successors, nodes, edges = self.function_cfg("src/flows.py", "term")
        exit_node = self.unique(by_id, "exit")
        returns = [n for n in nodes if n["kind"] == "return"]
        self.assertEqual(len(returns), 2)
        first_return = min(returns, key=lambda n: n["payload"]["start_byte"])
        self.assertEqual(successors[first_return["analysis_node_id"]],
                         [("return", exit_node["analysis_node_id"])])
        for return_node in returns:
            outs = successors[return_node["analysis_node_id"]]
            self.assertEqual(len(outs), 1)
            self.assertEqual(outs[0][0], "return")
            self.assertEqual(outs[0][1], exit_node["analysis_node_id"])
        exit_ins = [edge for edge in edges if edge["dst_id"] == exit_node["analysis_node_id"]]
        self.assertEqual(sorted(edge["kind"] for edge in exit_ins), ["return", "return"])
        work = [n for n in nodes if n["kind"] == "callsite"][0]
        self.assertNotIn(
            (first_return["analysis_node_id"], work["analysis_node_id"]),
            [(edge["src_id"], edge["dst_id"]) for edge in edges],
        )

    def test_g6_deterministic_rebuild(self) -> None:
        self.build_cfg()
        self.build_cfg("tests")

        def dump() -> list:
            rows = []
            with closing(sqlite3.connect(self.database)) as connection:
                connection.row_factory = sqlite3.Row
                for row in connection.execute(
                    "SELECT * FROM analysis_state WHERE feature LIKE 'cfg%' ORDER BY file_id"
                ):
                    item = dict(row)
                    item.pop("updated_at")
                    rows.append(("state", item))
                for row in connection.execute(
                    "SELECT * FROM analysis_nodes WHERE feature='cfg:python' "
                    "ORDER BY analysis_node_id"
                ):
                    rows.append(("node", dict(row)))
                for row in connection.execute(
                    "SELECT * FROM analysis_edges WHERE feature='cfg:python' "
                    "ORDER BY analysis_edge_id"
                ):
                    rows.append(("edge", dict(row)))
            return rows

        first = dump()
        with closing(sqlite3.connect(self.database)) as connection:
            for table in ("analysis_state", "analysis_nodes", "analysis_edges"):
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            connection.commit()
        analyze_ast_scope(self.root, "src", self.database)
        analyze_ast_scope(self.root, "tests", self.database)
        self.build_cfg()
        self.build_cfg("tests")
        self.assertEqual(dump(), first)

    def test_g7_cache_reuse(self) -> None:
        self.build_cfg()
        before = cfg_state(self.database, self.fid("src/flows.py"))["updated_at"]
        result = self.build_cfg()
        self.assertEqual(result["reused"],
                         ["src/flows.py", "src/modlevel.py", "src/unsupported.py"])
        self.assertEqual(result["materialized"], [])
        self.assertEqual(cfg_state(self.database, self.fid("src/flows.py"))["updated_at"],
                         before)

    def test_g8_ast_dependency_stale(self) -> None:
        self.build_cfg()
        file_id = self.fid("src/flows.py")
        before_rows = len(cfg_rows(self.database, file_id)[0])
        write(self.root / "src" / "flows.py", SRC_FLOWS_V2)
        incremental_update(self.root, self.database)
        store = self.store()
        self.assertFalse(store.read_current_analysis(file_id, "ast:python")["fresh"])
        self.assertFalse(store.read_current_analysis(file_id, "cfg:python")["fresh"])
        result = self.build_cfg()
        self.assertEqual(result["skipped_ast_stale"], ["src/flows.py"])
        self.assertEqual(result["materialized"], [])
        self.assertEqual(len(cfg_rows(self.database, file_id)[0]), before_rows)
        analyze_ast_scope(self.root, "src", self.database)
        rebuilt = self.build_cfg()
        self.assertIn("src/flows.py", rebuilt["materialized"])
        self.assertEqual(rebuilt["functions_built"], 6)
        self.assertTrue(store.read_current_analysis(file_id, "cfg:python")["fresh"])
        self.assertGreater(len(cfg_rows(self.database, file_id)[0]), before_rows)

    def test_g9_scope_isolation_and_module_level(self) -> None:
        self.build_cfg()
        self.assertIsNone(cfg_state(self.database, self.fid("tests/dup.py")))
        self.assertIsNone(cfg_state(self.database, self.fid("tests/helpers.py")))
        modlevel_nodes, _ = cfg_rows(self.database, self.fid("src/modlevel.py"))
        self.assertTrue(modlevel_nodes)
        configured_owner = base_symbol_id(
            self.database, self.fid("src/modlevel.py"), "configured")
        self.assertTrue(all(
            node["payload"]["owner_symbol_id"] == configured_owner
            for node in modlevel_nodes))
        self.assertNotIn("module/if_statement[1]",
                         {node["payload"]["structural_path"] for node in modlevel_nodes})

    def test_g10_unmatched_owner_skipped(self) -> None:
        result = self.build_cfg("tests")
        self.assertEqual(result["functions_skipped_unmatched_owner"], 1)
        self.assertEqual(result["functions_built"], 2)
        dup_owner = base_symbol_id(self.database, self.fid("tests/dup.py"), "dup")
        dup_nodes, dup_edges = cfg_rows(self.database, self.fid("tests/dup.py"))
        self.assertEqual(len(dup_nodes), 3)
        self.assertTrue(all(
            node["payload"]["owner_symbol_id"] == dup_owner
            and node["payload"]["structural_path"].startswith("module/function_definition[0]")
            for node in dup_nodes))
        self.assertEqual(len(dup_edges), 2)

    def test_unsupported_constructs_fail_closed(self) -> None:
        result = self.build_cfg()
        self.assertEqual(result["functions_skipped_unsupported_construct"], 3)
        self.assertEqual(result["functions_built"], 6)
        unsupported_file = self.fid("src/unsupported.py")
        nodes, edges = cfg_rows(self.database, unsupported_file)
        self.assertEqual(nodes, [])
        self.assertEqual(edges, [])
        metadata = json.loads(cfg_state(self.database, unsupported_file)["metadata"])
        self.assertEqual(metadata["functions_built"], 0)
        self.assertEqual(metadata["functions_skipped_unsupported_construct"], 3)
        self.assertEqual(metadata["contract"], "python-cfg-v1")
        self.assertEqual(metadata["analyzer"], "cfg:python")

    def test_g11_base_graph_zero_impact(self) -> None:
        before = base_snapshot(self.database)
        self.build_cfg()
        self.build_cfg("tests")
        self.build_cfg()
        self.assertEqual(base_snapshot(self.database), before)
        with GraphQuery(self.database) as query:
            self.assertEqual(len(query.find_symbol("seq")["data"]), 1)


if __name__ == "__main__":
    unittest.main()
