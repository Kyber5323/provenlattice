from __future__ import annotations

import tempfile
import unittest
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from provenlattice.graph import full_index
from provenlattice.identity import repository_id, symbol_id
from provenlattice.incremental import incremental_update
from provenlattice.parsing.cpp import CppTreeSitterParser, tree_sitter_c, tree_sitter_cpp
from provenlattice.scanner import scan_repository
from provenlattice.storage import SQLiteStorage


HAS_CPP_GRAMMARS = tree_sitter_c is not None and tree_sitter_cpp is not None


@unittest.skipUnless(HAS_CPP_GRAMMARS, "tree-sitter-c and tree-sitter-cpp are required")
class CppParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent / "fixture_cpp"

    def test_extracts_cpp_contract(self) -> None:
        parsed = CppTreeSitterParser("cpp").parse(self.root / "src/main.cpp", "src/main.cpp")
        names = {symbol.qualified_name for symbol in parsed.symbols}
        self.assertIn("app", names)
        self.assertIn("app.execute", names)
        self.assertTrue(any(item.type == "CALLS" and item.target == "service.run" for item in parsed.references))
        self.assertEqual(parsed.imports[0].target, "api.hpp")

    def test_header_declarations_and_overloads_have_stable_distinct_identity(self) -> None:
        parsed = CppTreeSitterParser("cpp").parse(self.root / "include/api.hpp", "include/api.hpp")
        methods = [item for item in parsed.symbols if item.kind == "Method" and item.name == "run"]
        self.assertEqual(len(methods), 2)
        self.assertNotEqual(methods[0].signature, methods[1].signature)
        self.assertTrue(any(item.kind == "Type" and item.name == "DemoService" for item in parsed.symbols))

        definitions = CppTreeSitterParser("cpp").parse(self.root / "src/api.cpp", "src/api.cpp")
        defined = [item for item in definitions.symbols if item.kind == "Method" and item.name == "run"]
        self.assertEqual({item.qualified_name for item in methods}, {"demo.Service.run"})
        self.assertEqual({item.qualified_name for item in defined}, {"demo.Service.run"})
        self.assertEqual({item.signature for item in methods}, {item.signature for item in defined})
        repo_id = repository_id(self.root)
        header_id = symbol_id(repo_id, "include/api.hpp", methods[0].kind,
                              methods[0].qualified_name, methods[0].signature)
        source_match = next(item for item in defined if item.signature == methods[0].signature)
        source_id = symbol_id(repo_id, "src/api.cpp", source_match.kind,
                              source_match.qualified_name, source_match.signature)
        self.assertNotEqual(header_id, source_id)

    def test_c_extension_is_scanned_and_indexed(self) -> None:
        source_names = {item.relative_path for item in scan_repository(self.root)}
        self.assertIn("src/plain.c", source_names)
        parsed_c = CppTreeSitterParser("c").parse(self.root / "src/plain.c", "src/plain.c")
        self.assertTrue(any(item.kind == "Type" and item.name == "score_t" for item in parsed_c.symbols))
        add = [item for item in parsed_c.symbols if item.kind == "Function" and item.name == "add"]
        self.assertEqual(len(add), 1)
        self.assertTrue(add[0].metadata["definition"])
        with tempfile.TemporaryDirectory() as temp:
            result = full_index(self.root, Path(temp) / "graph.db")
            self.assertGreater(result["metrics"]["files_parsed"], 0)
            with SQLiteStorage(Path(temp) / "graph.db") as storage:
                connection = storage.connection
                include_statuses = {
                    row[0] for row in connection.execute(
                        "SELECT status FROM raw_references WHERE raw_name='api.hpp'"
                    )
                }
                member_status = connection.execute(
                    "SELECT status FROM raw_references WHERE raw_name='service.run' LIMIT 1"
                ).fetchone()[0]
            self.assertEqual(include_statuses, {"resolved"})
            self.assertEqual(member_status, "ambiguous")

    def test_cpp_full_incremental_parity_and_cross_shard_edge(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "fixture"
            shutil.copytree(self.root, root)
            incremental_db = Path(temp) / "incremental.db"
            full_db = Path(temp) / "full.db"
            full_index(root, incremental_db)
            caller = root / "module_a/caller.cpp"
            caller.write_text(caller.read_text(encoding="utf-8").replace("helper()", "other()"), encoding="utf-8")
            update = incremental_update(root, incremental_db)
            full_index(root, full_db)

            def snapshot(path: Path) -> dict[str, list[dict]]:
                # Through `SQLiteStorage`: a split layout keeps the references
                # in the cold store, which a bare connection cannot see.
                with SQLiteStorage(path) as storage:
                    return {
                        table: [
                            {key: value for key, value in dict(row).items()
                             if key not in {"generation", "boundary_dirty"}}
                            for row in storage.connection.execute(
                                f"SELECT * FROM {table} ORDER BY 1")
                        ]
                        for table in ("nodes", "edges", "shards", "raw_references")
                    }

            self.assertEqual(snapshot(incremental_db), snapshot(full_db))
            self.assertEqual(update["metrics"]["files_parsed"], 1)
            with closing(sqlite3.connect(full_db)) as connection:
                boundary_calls = connection.execute(
                    "SELECT COUNT(*) FROM edges WHERE type='CALLS' "
                    "AND json_extract(metadata, '$.scope')='boundary'"
                ).fetchone()[0]
            self.assertGreater(boundary_calls, 0)


if __name__ == "__main__":
    unittest.main()
