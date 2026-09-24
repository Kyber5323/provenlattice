from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice import storage
from provenlattice.graph import full_index
from provenlattice.identity import repository_id
from provenlattice.incremental import incremental_update
from provenlattice.query import GraphQuery
from provenlattice.storage import SQLiteStorage


REVISION_A = """from module_b.util import helper

def run(value: int) -> int:
    return helper(value) + 1

def removed(value: int) -> int:
    return value

def changed_signature(value: int) -> int:
    return value

def becomes_resolved() -> int:
    return future()

def becomes_unresolved(value: int) -> int:
    return helper(value)
"""

REVISION_B = """from module_c.api import extra

def run(value: int) -> int:
    return extra(value) + 20

def added(value: int) -> int:
    return value * 2

def changed_signature(value: int, mode: str = "fast") -> int:
    return value

def future() -> int:
    return 42

def becomes_resolved() -> int:
    return future()

def becomes_unresolved() -> int:
    return vanished()
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def snapshot(database: Path) -> dict:
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
    result["boundary_edges"] = [
        dict(row) for row in connection.execute(
            "SELECT id, src_id, dst_id, type, metadata FROM edges "
            "WHERE json_extract(metadata, '$.scope') = 'boundary' ORDER BY id"
        )
    ]
    connection.close()
    return result


class IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root / "module_a" / "service.py", REVISION_A)
        write(
            self.root / "module_b" / "util.py",
            "def helper(value: int) -> int:\n    return value + 1\n\n"
            "def second() -> int:\n    return helper(1)\n",
        )
        write(self.root / "module_c" / "api.py", "def extra(value: int) -> int:\n    return value - 1\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_index_query_and_body_only_incremental(self) -> None:
        database = Path(self.temp.name) / "graph.db"
        indexed = full_index(self.root, database)
        self.assertGreater(indexed["metrics"]["nodes_created"], 0)
        with GraphQuery(database) as query:
            self.assertEqual(len(query.find_symbol("run")["data"]), 1)
            self.assertEqual(len(query.get_callees("module_a.service.run")["data"]), 1)
            bounded = query.get_subgraph("module_a.service.run", max_hops=1, max_nodes=2)
            self.assertLessEqual(len(bounded["data"]["nodes"]), 2)
        with closing(sqlite3.connect(database)) as connection:
            before = connection.execute(
                "SELECT api_fingerprint FROM shards WHERE path='module_a'"
            ).fetchone()[0]
        write(self.root / "module_a" / "service.py", REVISION_A.replace("+ 1", "+ 99"))
        updated = incremental_update(self.root, database)
        self.assertEqual(updated["metrics"]["files_parsed"], 1)
        with closing(sqlite3.connect(database)) as connection:
            after = connection.execute(
                "SELECT api_fingerprint FROM shards WHERE path='module_a'"
            ).fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(updated["delta"]["boundary_dirty"], [])
        self.assertEqual(len(updated["delta"]["shards_updated"]), 1)
        self.assertGreater(updated["delta"]["references_reused"], 0)

    def test_subgraph_parity_across_id_binding_strategies(self) -> None:
        """Above ID_BIND_LIMIT ids, traversal switches from bound placeholders to
        a temp-table join. No other test reaches that branch — the fixtures are
        all far below the limit — so force it and require identical results."""
        database = Path(self.temp.name) / "graph.db"
        full_index(self.root, database)
        with GraphQuery(database) as query:
            bound = query.get_subgraph("module_a.service.run", max_hops=2, max_nodes=50)
        original = storage.ID_BIND_LIMIT
        storage.ID_BIND_LIMIT = 1
        try:
            with GraphQuery(database) as query:
                joined = query.get_subgraph("module_a.service.run", max_hops=2, max_nodes=50)
        finally:
            storage.ID_BIND_LIMIT = original
        self.assertTrue(bound["data"]["nodes"])
        self.assertEqual(bound["data"], joined["data"])

    def test_full_incremental_parity_for_required_changes(self) -> None:
        incremental_db = Path(self.temp.name) / "incremental.db"
        full_db = Path(self.temp.name) / "full.db"
        full_index(self.root, incremental_db)
        write(self.root / "module_a" / "service.py", REVISION_B)
        update = incremental_update(self.root, incremental_db)
        full_index(self.root, full_db)
        self.assertTrue(update["changed"])
        self.assertGreater(update["delta"]["nodes_added"], 0)
        self.assertGreater(update["delta"]["nodes_removed"], 0)
        self.assertGreater(update["delta"]["edges_added"], 0)
        self.assertGreater(update["delta"]["edges_removed"], 0)
        self.assertTrue(update["delta"]["boundary_dirty"])
        self.assertEqual(snapshot(incremental_db), snapshot(full_db))
        with GraphQuery(incremental_db) as query:
            resolved = query.get_raw_references(raw_name="future")["data"]
            unresolved = query.get_raw_references(raw_name="vanished")["data"]
        self.assertTrue(resolved and all(item["status"] == "resolved" for item in resolved))
        self.assertTrue(unresolved and all(item["status"] == "unresolved" for item in unresolved))

    def test_add_modify_delete_files(self) -> None:
        database = Path(self.temp.name) / "changes.db"
        full_index(self.root, database)
        write(self.root / "module_a" / "new_file.py", "def new_symbol():\n    return 1\n")
        (self.root / "module_b" / "util.py").unlink()
        write(self.root / "module_c" / "api.py", "def extra(value: int) -> int:\n    return value + 10\n")
        update = incremental_update(self.root, database)
        self.assertEqual(update["changes"]["added"], ["module_a/new_file.py"])
        self.assertEqual(update["changes"]["deleted"], ["module_b/util.py"])
        self.assertEqual(update["changes"]["modified"], ["module_c/api.py"])

    def test_candidate_disappearance_rebuilds_ambiguous_reference(self) -> None:
        incremental_db = Path(self.temp.name) / "candidate-incremental.db"
        full_db = Path(self.temp.name) / "candidate-full.db"
        write(
            self.root / "module_a" / "caller.py",
            "def caller(item):\n    return item.duplicate()\n",
        )
        duplicate_b = self.root / "module_b" / "duplicate.py"
        duplicate_c = self.root / "module_c" / "duplicate.py"
        write(duplicate_b, "def duplicate():\n    return 1\n")
        write(duplicate_c, "def duplicate():\n    return 2\n")
        full_index(self.root, incremental_db)

        duplicate_c.unlink()
        update = incremental_update(self.root, incremental_db)
        full_index(self.root, full_db)

        self.assertTrue(update["changed"])
        self.assertEqual(snapshot(incremental_db), snapshot(full_db))
        with GraphQuery(incremental_db) as query:
            references = query.get_raw_references(raw_name="duplicate")["data"]
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]["status"], "resolved")
        self.assertEqual(len(references[0]["candidate_symbols"]), 1)

    def test_resolution_contract_and_raw_reference_indexes(self) -> None:
        database = Path(self.temp.name) / "resolution.db"
        write(
            self.root / "module_a" / "resolution.py",
            "def local():\n    return 1\n\n"
            "def use_local():\n    return local()\n\n"
            "def missing():\n    return unknown_target()\n\n"
            "def ambiguous():\n    return duplicate()\n",
        )
        write(self.root / "module_b" / "duplicate.py", "def duplicate():\n    return 1\n")
        write(self.root / "module_c" / "duplicate.py", "def duplicate():\n    return 2\n")
        full_index(self.root, database)
        with GraphQuery(database) as query:
            local = query.get_raw_references(raw_name="local")["data"]
            missing = query.get_raw_references(raw_name="unknown_target")["data"]
            ambiguous = query.get_raw_references(raw_name="duplicate")["data"]
        self.assertEqual(local[0]["status"], "resolved")
        self.assertEqual(local[0]["resolution_strategy"], "same_file_resolution")
        self.assertEqual(local[0]["provenance"], "same_file_resolution")
        self.assertEqual(missing[0]["status"], "unresolved")
        self.assertIsNone(missing[0]["resolved_symbol_id"])
        self.assertEqual(ambiguous[0]["status"], "ambiguous")
        self.assertEqual(len(ambiguous[0]["candidate_symbols"]), 2)
        with closing(sqlite3.connect(database)) as connection:
            indexes = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='raw_references'"
                )
            }
            ambiguous_edges = connection.execute(
                "SELECT COUNT(*) FROM edges e JOIN raw_references r "
                "ON json_extract(e.metadata, '$.raw_reference_id') = r.id "
                "WHERE r.status != 'resolved'"
            ).fetchone()[0]
        self.assertEqual(
            indexes,
            {"sqlite_autoindex_raw_references_1", "idx_raw_references_name",
             "idx_raw_references_file", "idx_raw_references_owner",
             "idx_raw_references_resolved"},
        )
        self.assertEqual(ambiguous_edges, 0)

    def test_internal_relation_change_updates_only_current_shard(self) -> None:
        database = Path(self.temp.name) / "internal.db"
        target = self.root / "module_a" / "internal.py"
        write(
            target,
            "def left():\n    return 1\n\ndef right():\n    return 2\n\n"
            "def run():\n    return left()\n",
        )
        full_index(self.root, database)
        write(
            target,
            "def left():\n    return 1\n\ndef right():\n    return 2\n\n"
            "def run():\n    return right()\n",
        )
        update = incremental_update(self.root, database)
        self.assertEqual(len(update["delta"]["shards_updated"]), 1)
        self.assertFalse(update["delta"]["boundary_dirty"])

    def test_public_signature_change_marks_boundary_dirty(self) -> None:
        database = Path(self.temp.name) / "signature.db"
        full_index(self.root, database)
        target = self.root / "module_b" / "util.py"
        write(
            target,
            "def helper(value: int, mode: str = 'fast') -> int:\n    return value + 1\n\n"
            "def second() -> int:\n    return helper(1)\n",
        )
        update = incremental_update(self.root, database)
        self.assertTrue(update["delta"]["boundary_dirty"])

    def test_reverse_boundary_index(self) -> None:
        database = Path(self.temp.name) / "reverse.db"
        full_index(self.root, database)
        with GraphQuery(database) as query:
            dependencies = query.get_dependencies("module_a")
            boundary_refs = query.get_boundary_references("module_b.util.helper")
        self.assertTrue(any(item["path"] == "module_b" for item in dependencies["data"]))
        self.assertTrue(boundary_refs["data"])


class TargetedLoaderTests(unittest.TestCase):
    """The 0.3 loaders (appendix A) must return exactly what the old full-table
    dumps returned after Python filtering. Each test pins one loader against
    that ground truth, computed from the same database."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root / "module_a" / "service.py", REVISION_A)
        write(
            self.root / "module_b" / "util.py",
            "def helper(value: int) -> int:\n    return value + 1\n\n"
            "def second() -> int:\n    return helper(1)\n",
        )
        write(self.root / "module_c" / "api.py", "def extra(value: int) -> int:\n    return value - 1\n")
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_snapshot_columns_reads_exactly_the_named_columns(self) -> None:
        with SQLiteStorage(self.database) as store:
            full = store.snapshot_ids("nodes")
            slim = store.snapshot_columns("nodes", ("id", "kind", "source_hash"))
        self.assertEqual(set(slim), set(full))
        for node_id, row in slim.items():
            self.assertEqual(set(row), {"id", "kind", "source_hash"})
            self.assertEqual(row["kind"], full[node_id]["kind"])

    def test_nodes_for_files_equals_the_full_scan_filtered(self) -> None:
        with SQLiteStorage(self.database) as store:
            full = store.snapshot_ids("nodes")
            wanted = {row["id"] for row in full.values() if row["kind"] == "File"}
            changed = set(list(wanted)[:2])  # two arbitrary files
            targeted = store.nodes_for_files(changed)
        shared = ("file_id", "shard_id", "kind", "name", "qualified_name")
        expected = {
            row["id"]: {key: row[key] for key in shared}
            for row in full.values() if row["file_id"] in changed
        }
        self.assertTrue(targeted)
        self.assertEqual(
            expected,
            {row["id"]: {key: row[key] for key in shared} for row in targeted},
        )
        # Slim: no fat metadata column tags along.
        self.assertTrue(all("metadata" not in row for row in targeted))

    def test_nodes_metadata_covers_every_non_file_node_and_no_file_node(self) -> None:
        with SQLiteStorage(self.database) as store:
            full = store.snapshot_ids("nodes")
            metadata = store.nodes_metadata()
        file_ids = {row["id"] for row in full.values() if row["kind"] == "File"}
        symbol_ids = set(full) - file_ids
        self.assertTrue(file_ids.isdisjoint(metadata))
        self.assertEqual(set(metadata), symbol_ids)
        for node_id, raw in metadata.items():
            self.assertEqual(json.loads(raw), json.loads(full[node_id]["metadata"]))

    def test_boundary_edges_equals_the_full_scan_filtered(self) -> None:
        with SQLiteStorage(self.database) as store:
            full = store.snapshot_ids("edges")
            boundary = store.boundary_edges()
        shared = ("src_id", "dst_id", "metadata")
        expected = {
            edge_id: {key: row[key] for key in shared} for edge_id, row in full.items()
            if json.loads(row["metadata"]).get("scope") == "boundary"
        }
        self.assertEqual(
            {edge_id: {key: row[key] for key in shared} for edge_id, row in boundary.items()},
            expected,
        )
        self.assertEqual(set(boundary), set(expected))


    def test_boundary_edges_matches_the_old_substring_check(self) -> None:
        with SQLiteStorage(self.database) as store:
            full = store.snapshot_ids("edges")
            boundary = store.boundary_edges()
        substring_ids = {
            edge_id for edge_id, row in full.items()
            if '"scope": "boundary"' in row.get("metadata", "")
            or '"scope":"boundary"' in row.get("metadata", "")
        }
        self.assertEqual(set(boundary), substring_ids)

    def test_raw_reference_cache_exclusions_equal_full_then_filtered(self) -> None:
        repo_id = repository_id(self.root)
        with SQLiteStorage(self.database) as store:
            full = store.raw_reference_cache(repo_id)
        all_rows = list(full.values())
        excluded_files = {all_rows[0]["file_id"]}
        excluded_ids = {all_rows[1]["id"]}
        with SQLiteStorage(self.database) as store:
            targeted = store.raw_reference_cache(
                repo_id,
                exclude_file_ids=excluded_files,
                exclude_ids=excluded_ids,
            )
        expected = {
            row["id"]: row for row in all_rows
            if row["file_id"] not in excluded_files and row["id"] not in excluded_ids
        }
        self.assertEqual(targeted, expected)


class ExpressionIndexTests(unittest.TestCase):
    """Stage 0.4 (appendix A): `layer` lives inside the metadata JSON but is
    queried as a column, so the SCHEMA carries expression indexes. These tests
    pin the planner, not just the DDL: the knowledge layer's exact spellings
    must be answered by the indexes without reading the fat metadata blobs.
    Every EXPLAIN opens a fresh connection -- a cached statement keeps the
    plan it was compiled with even after the schema changed."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root / "module_a" / "service.py", REVISION_A)
        write(
            self.root / "module_b" / "util.py",
            "def helper(value: int) -> int:\n    return value + 1\n\n"
            "def second() -> int:\n    return helper(1)\n",
        )
        write(self.root / "module_c" / "api.py", "def extra(value: int) -> int:\n    return value - 1\n")
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def plans(self, sql: str) -> list[str]:
        with closing(sqlite3.connect(self.database)) as connection:
            return [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql)]

    def test_bare_layer_predicate_searches_instead_of_scanning(self) -> None:
        # knowledge.py spells the predicate without whitespace; the planner
        # matches on the parsed expression, so this must share the index.
        plan = self.plans(
            "SELECT id FROM nodes WHERE json_extract(metadata,'$.layer')='knowledge'"
        )
        self.assertTrue(any(
            p.startswith("SEARCH nodes USING INDEX idx_nodes_layer (") for p in plan
        ), plan)
        spaced = self.plans(
            "SELECT id FROM nodes WHERE json_extract(metadata, '$.layer') = 'knowledge'"
        )
        self.assertTrue(any(
            p.startswith("SEARCH nodes USING INDEX idx_nodes_layer (") for p in spaced
        ), spaced)

    def test_edges_layer_predicate_searches_instead_of_scanning(self) -> None:
        plan = self.plans(
            "SELECT id FROM edges WHERE json_extract(metadata,'$.layer')='knowledge'"
        )
        self.assertTrue(any(
            p.startswith("SEARCH edges USING INDEX idx_edges_layer (") for p in plan
        ), plan)

    def test_coalesced_count_is_answered_by_the_index_alone(self) -> None:
        # The != predicate cannot do an equality lookup, but the COALESCE
        # index covers the expression, so the COUNT must still be answered
        # from the index -- never by scanning the ~95%-of-the-table metadata.
        plan = self.plans(
            "SELECT COUNT(*) FROM nodes "
            "WHERE COALESCE(json_extract(metadata, '$.layer'), 'code') != 'knowledge'"
        )
        self.assertTrue(any(
            "USING COVERING INDEX idx_nodes_layer_coalesced" in p for p in plan
        ), plan)


if __name__ == "__main__":
    unittest.main()
