from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice.cli import build_parser, run
from provenlattice.evidence import evidence_id
from provenlattice.graph import full_index
from provenlattice.query import GraphQuery

HUB_SRC = (
    "def hub(x):\n"
    + "".join(f"    c{index:02d}(x)\n" for index in range(14))
    + "\n"
    + "".join(f"def c{index:02d}(x):\n    return x\n\n" for index in range(14))
    + "".join(f"def caller_{index:02d}(x):\n    return hub(x)\n\n" for index in range(15))
)

EXTRA_SRC = """def uses_ghost(x):
    return ghost(x) + phantom(x)

def long_one(a, b):
    total = a
    """ + "\n    ".join(f"total = total + {index} * a + b" for index in range(30)) + """
    return total
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def serialize(bundle: dict) -> str:
    return json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))


def symbol_row(database: Path, name: str) -> sqlite3.Row:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM nodes WHERE kind='Function' AND name=? ORDER BY 1", (name,)
        ).fetchone()


class TaskContextContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root / "src" / "hub.py", HUB_SRC)
        write(self.root / "src" / "extra.py", EXTRA_SRC)
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def bundle(self, symbol: str) -> dict:
        with GraphQuery(self.database) as query:
            return query.task_context(symbol)

    def cli(self, *args: str) -> dict:
        return run(build_parser().parse_args(["task-context", *args, "--json"]))

    def test_happy_path_available_with_frozen_envelope(self) -> None:
        bundle = self.bundle("hub")
        self.assertEqual(bundle["version"], "task-context-v0")
        self.assertEqual(bundle["status"], "available")
        self.assertTrue(bundle["anchor"])
        self.assertIn("base_commit", bundle)
        self.assertEqual(bundle["truncation"]["max_serialized_chars"], 6000)
        for section in ("primary_targets", "relations", "candidates",
                        "source_spans", "expansion_handles"):
            self.assertIn(section, bundle)

    def test_repeated_calls_are_byte_identical(self) -> None:
        self.assertEqual(serialize(self.bundle("hub")), serialize(self.bundle("hub")))
        self.assertEqual(serialize(self.bundle("uses_ghost")),
                         serialize(self.bundle("uses_ghost")))

    def test_rebuild_at_same_path_is_byte_identical(self) -> None:
        first = serialize(self.bundle("hub"))
        full_index(self.root, self.database)
        self.assertEqual(first, serialize(self.bundle("hub")))

    def test_stable_fact_identity_formats_without_ordinals(self) -> None:
        bundle = self.bundle("hub")
        for fact in bundle["primary_targets"]:
            self.assertRegex(fact["fact_id"], r"^symbol:symbol:[0-9a-f]{64}$")
            self.assertEqual(fact["fact_id"], f"symbol:{fact['symbol_id']}")
        for fact in bundle["relations"]:
            self.assertRegex(fact["fact_id"],
                             r"^relation:CALLS:symbol:[0-9a-f]{64}:symbol:[0-9a-f]{64}$")
            self.assertEqual(fact["fact_id"],
                             f"relation:{fact['relation']}:{fact['source_symbol_id']}:"
                             f"{fact['target_symbol_id']}")
        for fact in bundle["candidates"]:
            if fact["kind"] == "unresolved":
                self.assertRegex(fact["fact_id"], r"^unresolved:reference:[0-9a-f]{64}$")
                self.assertEqual(fact["fact_id"], f"unresolved:{fact['reference_id']}")
            else:
                self.assertRegex(fact["fact_id"],
                                 r"^candidate:reference:[0-9a-f]{64}:symbol:symbol:[0-9a-f]{64}$")
                self.assertEqual(fact["fact_id"],
                                 f"candidate:{fact['reference_id']}:{fact['symbol_id']}")
        for fact in bundle["source_spans"]:
            self.assertRegex(fact["fact_id"], r"^span:[^:]+:[^:]+:\d+:\d+$")
            self.assertEqual(fact["fact_id"],
                             f"span:{bundle['base_commit']}:{fact['file']}:"
                             f"{fact['start_byte']}:{fact['end_byte']}")

    def test_anchor_evidence_back_reference_is_content_addressed(self) -> None:
        bundle = self.bundle("hub")
        anchor = bundle["primary_targets"][0]
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            repo_id = connection.execute(
                "SELECT repo_id FROM repositories LIMIT 1").fetchone()["repo_id"]
            node = connection.execute(
                "SELECT id, file_id FROM nodes WHERE id=?",
                (anchor["symbol_id"],)).fetchone()
        expected = evidence_id("CODE_DEFINITION", repo_id, node["file_id"],
                               "DEFINES", node["id"], node["id"])
        self.assertEqual(anchor["evidence_ids"], [expected])
        for section in ("relations", "candidates", "source_spans"):
            for fact in bundle[section]:
                self.assertTrue(fact["evidence_ids"])
                for item in fact["evidence_ids"]:
                    self.assertRegex(item, r"^E-[A-Z]+-[0-9a-f]{24}$")

    def test_ordering_follows_existing_api_order(self) -> None:
        bundle = self.bundle("hub")
        with GraphQuery(self.database) as query:
            callers = query.get_callers("hub")
            callees = query.get_callees("hub")
            references = query.get_references("hub")
        combined = [*callers["evidence"], *callees["evidence"], *references["evidence"]]
        expected = [f"relation:{item['relation']}:{item['source_id']}:{item['target_id']}"
                    for item in combined][:12]
        self.assertEqual([fact["fact_id"] for fact in bundle["relations"]][:len(expected)],
                         expected[:len(bundle["relations"])])
        related = [*callers["data"], *callees["data"], *references["data"]]
        if related:
            self.assertEqual(bundle["primary_targets"][1]["symbol_id"], related[0]["id"])
        with GraphQuery(self.database) as query:
            raw = query.get_raw_references(
                owner_symbol_id=query.get_definition("hub")["data"]["id"])
        expected_candidates = []
        for row in raw["data"]:
            if row.get("status") == "resolved":
                continue
            if row.get("candidate_symbols"):
                expected_candidates.extend(
                    f"candidate:{row['id']}:{candidate}"
                    for candidate in row["candidate_symbols"])
            else:
                expected_candidates.append(f"unresolved:{row['id']}")
        self.assertEqual([fact["fact_id"] for fact in bundle["candidates"]],
                         expected_candidates[:len(bundle["candidates"])])

    def test_per_section_hard_limits(self) -> None:
        for symbol in ("hub", "uses_ghost", "long_one"):
            bundle = self.bundle(symbol)
            self.assertLessEqual(len(bundle["primary_targets"]), 2)
            self.assertLessEqual(len(bundle["relations"]), 12)
            self.assertLessEqual(len(bundle["candidates"]), 6)
            self.assertLessEqual(len(bundle["source_spans"]), 4)
            total_lines = 0
            for span in bundle["source_spans"]:
                lines = span["content"].splitlines()
                self.assertLessEqual(len(lines), 20)
                self.assertEqual(len(lines), span["end_line"] - span["start_line"] + 1)
                total_lines += len(lines)
            self.assertLessEqual(total_lines, 80)

    def test_source_span_clip_and_target_line_preserved(self) -> None:
        bundle = self.bundle("long_one")
        self.assertEqual(len(bundle["source_spans"]), 1)
        span = bundle["source_spans"][0]
        self.assertEqual(span["file"], "src/extra.py")
        self.assertEqual(len(span["content"].splitlines()), 20)
        self.assertTrue(span["content"].splitlines()[0].startswith("def long_one"))
        self.assertLessEqual(span["end_line"] - span["start_line"] + 1, 20)

    def test_unresolved_references_surfaced_with_existing_order(self) -> None:
        bundle = self.bundle("uses_ghost")
        self.assertTrue(bundle["candidates"])
        for fact in bundle["candidates"]:
            self.assertEqual(fact["kind"], "unresolved")
            self.assertEqual(fact["status"], "unresolved")
            self.assertTrue(fact["reference_id"])
            self.assertTrue(fact["evidence_ids"])

    def test_global_hard_limit_and_deterministic_truncation(self) -> None:
        first = self.bundle("hub")
        second = self.bundle("hub")
        self.assertLessEqual(len(serialize(first).encode("utf-8")), 6000)
        self.assertTrue(first["truncation"]["truncated"])
        self.assertEqual(serialize(first), serialize(second))
        with GraphQuery(self.database) as query:
            callers = query.get_callers("hub")
            callees = query.get_callees("hub")
            references = query.get_references("hub")
            anchor_id = query.get_definition("hub")["data"]["id"]
            raw = query.get_raw_references(owner_symbol_id=anchor_id)
        related = [*callers["data"], *callees["data"], *references["data"]]
        expected_sizes = {
            "primary_targets": min(2, 1 + (1 if related else 0)),
            "relations": min(12, len([*callers["evidence"], *callees["evidence"],
                                      *references["evidence"]])),
            "candidates": 0,
            "source_spans": min(4, 1 + min(3, len(related))),
        }
        for row in raw["data"]:
            if row.get("status") == "resolved":
                continue
            expected_sizes["candidates"] += (len(row["candidate_symbols"])
                                             if row.get("candidate_symbols") else 1)
        expected_sizes["candidates"] = min(6, expected_sizes["candidates"])
        for section, pre in expected_sizes.items():
            omitted = first["truncation"]["omitted"].get(section, 0)
            self.assertEqual(len(first[section]), pre - omitted, section)
        self.assertEqual(first["truncation"]["omitted"].get("primary_targets", 0), 0)
        handles = {item["symbol_id"] for item in first["expansion_handles"]}
        targets = {item["symbol_id"] for item in first["primary_targets"]}
        self.assertEqual(handles, targets)

    def test_truncated_output_is_prefix_of_untruncated_order(self) -> None:
        bundle = self.bundle("hub")
        with GraphQuery(self.database) as query:
            callers = query.get_callers("hub")
            callees = query.get_callees("hub")
            references = query.get_references("hub")
        combined = [*callers["evidence"], *callees["evidence"], *references["evidence"]]
        expected = [f"relation:{item['relation']}:{item['source_id']}:{item['target_id']}"
                    for item in combined]
        actual = [fact["fact_id"] for fact in bundle["relations"]]
        self.assertTrue(expected[:len(actual)] == actual or len(actual) < 12)
        if len(actual) == 12:
            self.assertEqual(actual, expected[:12])

    def test_no_result_not_found_is_clean(self) -> None:
        bundle = self.bundle("no_such_symbol_here")
        self.assertEqual(bundle["status"], "not_found")
        for section in ("primary_targets", "relations", "candidates",
                        "source_spans", "expansion_handles"):
            self.assertEqual(bundle[section], [])
        self.assertFalse(bundle["truncation"]["truncated"])

    def test_cli_not_built_stale_corrupt_statuses(self) -> None:
        missing = self.cli("hub", "--database", str(Path(self.temp.name) / "none.db"))
        self.assertEqual(missing["status"], "not_built")
        corrupt_path = Path(self.temp.name) / "corrupt.db"
        corrupt_path.write_text("not sqlite", encoding="utf-8")
        corrupt = self.cli("hub", "--database", str(corrupt_path))
        self.assertEqual(corrupt["status"], "corrupt")
        available = self.cli("hub", "--database", str(self.database))
        self.assertEqual(available["status"], "available")
        stale = self.cli("hub", "--database", str(self.database),
                         "--expected-base-commit", "wrong")
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["expected_base_commit"], "wrong")

    def test_cli_malformed_and_invalid_symbols_do_not_crash(self) -> None:
        for symbol in ("", "   ", "../../escape", "a b c", "x" * 300):
            result = self.cli(symbol, "--database", str(self.database))
            self.assertEqual(result["status"], "not_found", symbol)

    def test_cli_json_matches_python_bundle(self) -> None:
        self.assertEqual(serialize(self.cli("hub", "--database", str(self.database))),
                         serialize(self.bundle("hub")))

    def test_existing_commands_unchanged_and_uncontaminated(self) -> None:
        with GraphQuery(self.database) as query:
            status = query.status()
            self.assertIn("graph_generation", status)
            explained = query.explain_symbol("hub")
        self.assertNotIn("task_context", explained)
        self.assertNotIn("task-context-v0", json.dumps(explained))
        plain = run(build_parser().parse_args(
            ["explain-symbol", "hub", "--database", str(self.database), "--json"]))
        self.assertNotIn("task-context-v0", json.dumps(plain))
        symbol = run(build_parser().parse_args(
            ["symbol", "hub", "--database", str(self.database), "--json"]))
        self.assertTrue(symbol["data"])


if __name__ == "__main__":
    unittest.main()
