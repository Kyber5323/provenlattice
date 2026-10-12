from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from provenlattice.analysis_query import query_analysis as real_query_analysis
from provenlattice.graph import full_index
from provenlattice.identity import path_id
from provenlattice.incremental import incremental_update
from provenlattice.knowledge import index_knowledge
from provenlattice.orchestrator import analyze_scope
from provenlattice.query import GraphQuery
from provenlattice.storage import SQLiteStorage

SRC_MAIN = """def dispatch(x):
    if x.retry:
        retry(x)
    else:
        commit(x)

def retry(x):
    return x

def commit(x):
    return x

def foo(x):
    if x:
        bar(x)
    return x
"""

SRC_MAIN_V2 = SRC_MAIN + """
def appended(value):
    return value
"""

SRC_DUP = """def foo():
    return 1

def foo():
    return 2
"""

SRC_TWIN = """def foo():
    return 9
"""

SRC_BIG = "def big():\n" + "".join(f"    call_{index}()\n" for index in range(500))


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
    storage.close()
    return result


def analysis_snapshot(database: Path) -> dict:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='analysis_state'"
        ).fetchone()
        if exists is None:
            return {"tables": False, "rows": []}
        rows = []
        for table in ("analysis_state", "analysis_nodes", "analysis_edges"):
            rows.extend(dict(row) for row in connection.execute(
                f"SELECT * FROM {table} ORDER BY 1"))
        return {"tables": True, "rows": rows}


def query_row(database: Path, sql: str, params: tuple) -> sqlite3.Row:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(sql, params).fetchone()


def strip_timing(result: dict) -> dict:
    return {key: value for key, value in result.items() if key != "query_time_ms"}


class ToolIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root / "src" / "main.py", SRC_MAIN)
        write(self.root / "src" / "dup.py", SRC_DUP)
        write(self.root / "src" / "twin.py", SRC_TWIN)
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

    def build(self, features: str = "ast,cfg") -> dict:
        return analyze_scope(self.root, "src", features, self.database)

    def symbol(self, relative: str, name: str) -> str:
        return query_row(
            self.database,
            "SELECT id FROM nodes WHERE file_id=? AND kind='Function' AND name=?",
            (self.fids[relative], name),
        )["id"]

    def explain(self, symbol: str, **kwargs) -> dict:
        with GraphQuery(self.database) as query:
            return query.explain_symbol(symbol, **kwargs)

    def find_related_documents(self, symbol: str, **kwargs) -> dict:
        with GraphQuery(self.database) as query:
            return query.find_related_documents(symbol, **kwargs)

    def find_related_code(self, document: str, **kwargs) -> dict:
        with GraphQuery(self.database) as query:
            return query.find_related_code(document, **kwargs)

    def task_context(self, symbol: str) -> dict:
        with GraphQuery(self.database) as query:
            return query.task_context(symbol)

    def multi_target_document(self) -> str:
        write(self.root / "docs" / "multi.md", """# Multi Target

[symbol:src.main.dispatch]
[symbol:src.main.retry]
[symbol:src.main.commit]
""")
        index_knowledge(self.root, self.database)
        return "Multi Target"

    def test_g1_default_behavior_unchanged(self) -> None:
        before = strip_timing(self.explain("src.main.foo"))
        self.assertNotIn("analysis", before)
        self.build()
        after = strip_timing(self.explain("src.main.foo"))
        self.assertNotIn("analysis", after)
        self.assertEqual(before, after)
        explicit_none = strip_timing(self.explain("src.main.foo", analysis=None))
        self.assertEqual(explicit_none, before)

    def test_g2_ast_explicit_consumption(self) -> None:
        self.build("ast")
        calls = []

        def _record(*args, **kwargs):
            calls.append((args, kwargs))
            return real_query_analysis(*args, **kwargs)

        with patch("provenlattice.query.query_analysis", side_effect=_record):
            result = self.explain("src.main.foo", analysis="ast")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][1], "ast")
        self.assertEqual(calls[0][1]["symbol_id"], self.symbol("src/main.py", "foo"))
        section = result["analysis"]["ast"]
        self.assertEqual(section["status"], "available")
        self.assertEqual(section["root"]["kind"], "function_definition")
        self.assertEqual(section["root"]["structural_path"],
                         "module/function_definition[3]")
        self.assertNotIn("analysis", self.explain("src.main.foo"))

    def test_g3_cfg_explicit_consumption(self) -> None:
        self.build()
        foo_id = self.symbol("src/main.py", "foo")
        result = self.explain("src.main.foo", analysis="cfg")
        section = result["analysis"]["cfg"]
        self.assertEqual(section["status"], "available")
        self.assertIsNotNone(section["entry_id"])
        self.assertIsNotNone(section["exit_id"])
        self.assertTrue(all(node["payload"]["owner_symbol_id"] == foo_id
                            for node in section["nodes"]))
        self.assertTrue(all(edge["payload"]["owner_symbol_id"] == foo_id
                            for edge in section["edges"]))

    def test_g4_not_built_keeps_base_result(self) -> None:
        result = self.explain("src.main.foo", analysis="cfg")
        section = result["analysis"]["cfg"]
        self.assertEqual(section["status"], "not_built")
        self.assertEqual(section["nodes"], [])
        self.assertGreater(result["returned_evidence_count"], 0)

    def test_g5_stale_passthrough(self) -> None:
        self.build()
        write(self.root / "src" / "main.py", SRC_MAIN_V2)
        incremental_update(self.root, self.database)
        result = self.explain("src.main.foo", analysis="cfg")
        section = result["analysis"]["cfg"]
        self.assertEqual(section["status"], "stale")
        self.assertEqual(section["nodes"], [])
        self.assertEqual(section["edges"], [])
        self.assertGreater(result["returned_evidence_count"], 0)

    def test_g6_corrupt_isolation(self) -> None:
        self.build()
        foo_id = self.symbol("src/main.py", "foo")
        entry_id = query_row(
            self.database,
            "SELECT analysis_node_id FROM analysis_nodes WHERE feature='cfg:python' "
            "AND kind='entry' AND json_extract(payload, '$.owner_symbol_id')=?",
            (foo_id,),
        )["analysis_node_id"]
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM analysis_nodes WHERE analysis_node_id=?",
                               (entry_id,))
            connection.commit()
        result = self.explain("src.main.foo", analysis="cfg")
        self.assertEqual(result["analysis"]["cfg"]["status"], "corrupt")
        self.assertGreater(result["returned_evidence_count"], 0)

    def test_g7_exact_symbol_isolation(self) -> None:
        self.build()
        for relative in ("src/main.py", "src/dup.py", "src/twin.py"):
            qualified = relative[:-3].replace("/", ".") + ".foo"
            result = self.explain(qualified, analysis="cfg")
            section = result["analysis"]["cfg"]
            self.assertEqual(section["status"], "available",
                             msg=(qualified, section["status"], section["reason"]))
            self.assertTrue(all(node["file_id"] == self.fids[relative]
                                for node in section["nodes"]))
            self.assertTrue(all(node["payload"]["owner_symbol_id"]
                                == self.symbol(relative, "foo")
                                for node in section["nodes"]))

    def test_g8_ast_and_cfg_independent(self) -> None:
        self.build()
        foo_id = self.symbol("src/main.py", "foo")
        entry_id = query_row(
            self.database,
            "SELECT analysis_node_id FROM analysis_nodes WHERE feature='cfg:python' "
            "AND kind='entry' AND json_extract(payload, '$.owner_symbol_id')=?",
            (foo_id,),
        )["analysis_node_id"]
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM analysis_nodes WHERE analysis_node_id=?",
                               (entry_id,))
            connection.commit()
        result = self.explain("src.main.foo", analysis="ast,cfg")
        self.assertEqual(result["analysis"]["ast"]["status"], "available")
        self.assertEqual(result["analysis"]["cfg"]["status"], "corrupt")

    def test_g9_response_size_guard(self) -> None:
        write(self.root / "src" / "big.py", SRC_BIG)
        incremental_update(self.root, self.database)
        self.build()
        result = self.explain("src.big.big", analysis="cfg")
        section = result["analysis"]["cfg"]
        self.assertTrue(section["truncated"])
        self.assertEqual(section["returned_nodes"], 400)
        self.assertEqual(section["total_nodes"], 502)
        kept = {node["analysis_node_id"] for node in section["nodes"]}
        self.assertTrue(all(edge["src_id"] in kept and edge["dst_id"] in kept
                            for edge in section["edges"]))
        json.dumps(result)

    def test_g10_zero_write(self) -> None:
        self.build()
        write(self.root / "src" / "main.py", SRC_MAIN_V2)
        incremental_update(self.root, self.database)
        foo_id = self.symbol("src/main.py", "foo")
        entry_id = query_row(
            self.database,
            "SELECT analysis_node_id FROM analysis_nodes WHERE feature='cfg:python' "
            "AND kind='entry' AND json_extract(payload, '$.owner_symbol_id')=?",
            (self.symbol("src/dup.py", "foo"),),
        )["analysis_node_id"]
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM analysis_nodes WHERE analysis_node_id=?",
                               (entry_id,))
            connection.commit()
        base_before = base_snapshot(self.database)
        analysis_before = analysis_snapshot(self.database)
        self.explain("src/dup.py".replace(".py", "").replace("/", ".") + ".foo",
                     analysis="cfg")
        self.explain("src.main.foo", analysis="cfg")
        self.explain("src.twin.foo", analysis="cfg")
        self.explain("src.main.foo", analysis="dfg")
        self.explain("src.missing.nope", analysis="cfg")
        self.assertEqual(base_snapshot(self.database), base_before)
        self.assertEqual(analysis_snapshot(self.database), analysis_before)

    def test_g11_tool_compatibility(self) -> None:
        self.build()
        default = strip_timing(self.explain("src.main.foo"))
        explicit = strip_timing(self.explain("src.main.foo", analysis=None))
        self.assertEqual(default, explicit)
        args = build_parser_args()
        self.assertIsNone(args)

    def test_g12_dispatch_branch_exclusivity(self) -> None:
        self.build()
        dispatch_id = self.symbol("src/main.py", "dispatch")
        retry_id = self.symbol("src/main.py", "retry")
        commit_id = self.symbol("src/main.py", "commit")
        with GraphQuery(self.database) as query:
            callees = {item["id"] for item in query.get_callees(dispatch_id)["data"]}
        self.assertEqual(callees, {retry_id, commit_id})
        result = self.explain("src.main.dispatch", analysis="cfg")
        section = result["analysis"]["cfg"]
        self.assertEqual(section["status"], "available")
        by_id = {node["analysis_node_id"]: node for node in section["nodes"]}
        condition = next(node for node in section["nodes"]
                         if node["kind"] == "condition")
        branches = {}
        for kind, target in _successors(section["edges"], condition["analysis_node_id"]):
            branches[kind] = target
        retry_callsite = by_id[branches["true"]]
        commit_callsite = by_id[branches["false"]]
        self.assertEqual(retry_callsite["payload"]["metadata"]["resolved_symbol_id"],
                         retry_id)
        self.assertEqual(commit_callsite["payload"]["metadata"]["resolved_symbol_id"],
                         commit_id)
        edge_pairs = {(edge["src_id"], edge["dst_id"]) for edge in section["edges"]}
        self.assertNotIn((retry_callsite["analysis_node_id"],
                          commit_callsite["analysis_node_id"]), edge_pairs)
        self.assertNotIn((commit_callsite["analysis_node_id"],
                          retry_callsite["analysis_node_id"]), edge_pairs)

    def test_cli_explain_symbol_analysis(self) -> None:
        self.build()
        from provenlattice.cli import build_parser, run
        args = build_parser().parse_args([
            "explain-symbol", "src.main.foo", "--database", str(self.database),
            "--analysis", "cfg", "--json",
        ])
        result = run(args)
        self.assertEqual(result["analysis"]["cfg"]["status"], "available")

    def test_d9_b1_find_related_documents_default_unchanged(self) -> None:
        before = strip_timing(self.find_related_documents("src.main.foo"))
        self.assertNotIn("analysis", before)
        self.build()
        after = strip_timing(self.find_related_documents("src.main.foo"))
        self.assertNotIn("analysis", after)
        self.assertEqual(before, after)

    def test_d9_b1_find_related_documents_cfg_analysis_v1(self) -> None:
        self.build()
        foo_id = self.symbol("src/main.py", "foo")
        result = self.find_related_documents("src.main.foo", analysis="cfg")
        analysis = result["analysis"]
        self.assertEqual(analysis["version"], "analysis-v1")
        self.assertEqual(analysis["requested_features"], ["cfg"])
        target = analysis["targets"]
        self.assertEqual(len(target), 1)
        self.assertEqual(target[0]["symbol_id"], foo_id)
        self.assertEqual(target[0]["qualified_name"], "src.main.foo")
        self.assertEqual(target[0]["features"]["cfg"]["status"], "available")
        direct = self.explain("src.main.foo", analysis="cfg")["analysis"]["cfg"]
        self.assertEqual(target[0]["features"]["cfg"], direct)

    def test_d9_b1_find_related_documents_ast_and_not_built_propagate(self) -> None:
        missing = self.find_related_documents("src.main.foo", analysis="cfg")
        self.assertEqual(missing["analysis"]["targets"][0]["features"]["cfg"]["status"],
                         "not_built")
        self.build("ast")
        result = self.find_related_documents("src.main.foo", analysis="ast")
        feature = result["analysis"]["targets"][0]["features"]["ast"]
        self.assertEqual(feature["status"], "available")
        self.assertEqual(feature["root"]["kind"], "function_definition")

    def test_d9_b1_cli_find_related_documents_analysis(self) -> None:
        self.build()
        from provenlattice.cli import build_parser, run
        args = build_parser().parse_args([
            "find-related-documents", "src.main.foo", "--database", str(self.database),
            "--analysis", "cfg", "--json",
        ])
        result = run(args)
        self.assertEqual(result["analysis"]["targets"][0]["features"]["cfg"]["status"],
                         "available")

    def test_d9_b2_default_multi_target_bundle_unchanged(self) -> None:
        document = self.multi_target_document()
        before = strip_timing(self.find_related_code(document))
        self.build()
        after = strip_timing(self.find_related_code(document))
        self.assertNotIn("analysis", before)
        self.assertNotIn("analysis", after)
        self.assertEqual(before, after)

    def test_d9_b2_valid_selector_uses_one_target_without_fanout(self) -> None:
        document = self.multi_target_document()
        self.build()
        base = self.find_related_code(document)
        symbols = [item for item in base["bundle"]["related_entities"]
                   if item["kind"] in {"Function", "Method"}]
        self.assertEqual(len(symbols), 3)
        chosen = symbols[1]
        calls = []

        def _record(*args, **kwargs):
            calls.append((args, kwargs))
            return real_query_analysis(*args, **kwargs)

        with patch("provenlattice.query.query_analysis", side_effect=_record):
            result = self.find_related_code(
                document, analysis="cfg", analysis_symbol_id=chosen["id"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["symbol_id"], chosen["id"])
        target = result["analysis"]["targets"]
        self.assertEqual(len(target), 1)
        self.assertEqual(target[0]["symbol_id"], chosen["id"])
        self.assertEqual(target[0]["features"]["cfg"]["status"], "available")
        self.assertNotIn(symbols[0]["id"], [item["symbol_id"] for item in target])
        self.assertNotIn(symbols[2]["id"], [item["symbol_id"] for item in target])

    def test_d9_b2_missing_invalid_and_zero_selectors_fail_closed(self) -> None:
        document = self.multi_target_document()
        self.build()
        missing = self.find_related_code(document, analysis="cfg")
        self.assertEqual(missing["analysis"]["error"]["code"],
                         "ANALYSIS_SYMBOL_ID_REQUIRED")
        invalid = self.find_related_code(
            document, analysis="cfg", analysis_symbol_id="symbol:not-returned")
        self.assertEqual(invalid["analysis"]["error"]["code"],
                         "ANALYSIS_SYMBOL_ID_NOT_RETURNED")
        empty = self.find_related_code("missing-document", analysis="cfg")
        self.assertNotIn("analysis", empty)
        write(self.root / "docs" / "empty.md", "# Empty Target\n")
        index_knowledge(self.root, self.database)
        zero = self.find_related_code("Empty Target", analysis="cfg")
        self.assertEqual(zero["analysis"]["error"]["code"],
                         "ANALYSIS_NO_SELECTABLE_SYMBOL")

    def test_d9_b2_cli_selector_and_ast(self) -> None:
        document = self.multi_target_document()
        self.build("ast")
        target = next(item for item in self.find_related_code(document)["bundle"]["related_entities"]
                      if item["kind"] == "Function")
        from provenlattice.cli import build_parser, run
        args = build_parser().parse_args([
            "find-related-code", document, "--database", str(self.database),
            "--analysis", "ast", "--analysis-symbol-id", target["id"], "--json",
        ])
        result = run(args)
        feature = result["analysis"]["targets"][0]["features"]["ast"]
        self.assertEqual(feature["status"], "available")
        self.assertEqual(feature["root"]["kind"], "function_definition")
        direct = self.explain(target["qualified_name"], analysis="ast")["analysis"]["ast"]
        self.assertEqual(feature, direct)


def _successors(edges, node_id: str) -> list[tuple[str, str]]:
    return [(edge["kind"], edge["dst_id"]) for edge in edges if edge["src_id"] == node_id]


def build_parser_args():
    from provenlattice.cli import build_parser
    args = build_parser().parse_args(["explain-symbol", "x", "--database", "g.db"])
    return getattr(args, "analysis", None)


def command_help(command: str) -> str:
    from provenlattice.cli import build_parser
    output = StringIO()
    with redirect_stdout(output), unittest.TestCase().assertRaises(SystemExit):
        build_parser().parse_args([command, "--help"])
    return output.getvalue()


class DiscoverySurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        ToolIntegrationTests.setUp(self)

    def tearDown(self) -> None:
        ToolIntegrationTests.tearDown(self)

    def task_context(self, symbol: str) -> dict:
        with GraphQuery(self.database) as query:
            return query.task_context(symbol)

    def test_d9_b3_analysis_help_is_shared_and_generic(self) -> None:
        help_by_command = {
            command: command_help(command)
            for command in ("explain-symbol", "find-related-documents", "find-related-code")
        }
        for text in help_by_command.values():
            normalized = " ".join(text.split())
            self.assertIn("--analysis FEATURES", text)
            self.assertIn("ast", normalized)
            self.assertIn("cfg", normalized)
            self.assertIn("Optional derived function analysis", normalized)
            self.assertIn("Omit to keep base evidence unchanged", normalized)
        multi = help_by_command["find-related-code"]
        normalized_multi = " ".join(multi.split())
        self.assertIn("--analysis-symbol-id SYMBOL_ID", multi)
        self.assertIn("exact symbol_id from this command's returned bundle", normalized_multi)
        self.assertIn("never analyzed automatically", normalized_multi)

    def test_d9_b3_help_has_no_fixture_or_case_specific_prompting(self) -> None:
        combined = "\n".join(command_help(command) for command in (
            "explain-symbol", "find-related-documents", "find-related-code"))
        for forbidden in ("D8", "A1", "A2", "B1", "B2", "C1", "C2", "pipeline"):
            self.assertNotIn(forbidden, combined)

    def test_d9_c3_task_context_is_deterministic_and_evidence_backed(self) -> None:
        first = self.task_context("src.main.dispatch")
        second = self.task_context("src.main.dispatch")
        self.assertEqual(first, second)
        self.assertEqual(first["version"], "task-context-v0")
        self.assertEqual(first["status"], "available")
        self.assertLessEqual(len(first["primary_targets"]), 2)
        self.assertLessEqual(len(first["relations"]), 12)
        self.assertLessEqual(len(first["candidates"]), 6)
        self.assertLessEqual(len(first["source_spans"]), 4)
        for section in ("primary_targets", "relations", "candidates", "source_spans"):
            for fact in first[section]:
                self.assertTrue(fact["fact_id"])
                self.assertTrue(fact["evidence_ids"])

    def test_d9_c3_task_context_limit_and_cli_statuses(self) -> None:
        value = {"primary_targets": [{"fact_id": "symbol:x"}, {"fact_id": "symbol:y"}],
                 "relations": [{"x": "z" * 3000}], "candidates": [],
                 "source_spans": [{"content": "z" * 3000}]}
        trimmed = GraphQuery._task_context_trim(value)
        self.assertLessEqual(len(json.dumps(trimmed, ensure_ascii=False,
                                           separators=(",", ":")).encode("utf-8")), 6000)
        self.assertTrue(trimmed["truncation"]["truncated"])
        from provenlattice.cli import build_parser, run
        missing = run(build_parser().parse_args([
            "task-context", "missing", "--database", str(self.root / "none.db"), "--json"]))
        self.assertEqual(missing["status"], "not_built")
        corrupt_path = self.root / "corrupt.db"
        corrupt_path.write_text("not sqlite", encoding="utf-8")
        corrupt = run(build_parser().parse_args([
            "task-context", "missing", "--database", str(corrupt_path), "--json"]))
        self.assertEqual(corrupt["status"], "corrupt")
        available = run(build_parser().parse_args([
            "task-context", "src.main.dispatch", "--database", str(self.database), "--json"]))
        stale = run(build_parser().parse_args([
            "task-context", "src.main.dispatch", "--database", str(self.database), "--expected-base-commit", "wrong", "--json"]))
        self.assertEqual(available["status"], "available")
        self.assertEqual(stale["status"], "stale")


if __name__ == "__main__":
    unittest.main()
