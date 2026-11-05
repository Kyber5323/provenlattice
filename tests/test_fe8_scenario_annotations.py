"""FE8-F1 golden tests (Issue #51): frozen pragma fact production + walker byte span.

Covers the frozen contract ``fe8_formal_feature_contract.md`` (§1 syntax, §2.1
fields, §3 deterministic id, §4/§5 nesting and fail-closed semantics, §6
determinism, §7 version bump). The relation/group layer (FE8-F2) is out of scope:
these tests assert only that ``scenario_annotations[]`` is materialised additively
and that walker points publish ``start_byte``/``end_byte``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice.analysis import ANALYZER_VERSIONS
from provenlattice.analysis_c_ast import (
    C_AST_CONTRACT,
    SCENARIO_ANNOTATION_KIND,
    SCENARIO_SYNTAX_KIND,
    analyze_c_ast_scope,
    scenario_annotation_id,
)
from provenlattice.analysis_query import query_analysis
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope


def _reference_annotation_id(
    repo: str, relative_path: str, syntax_kind: str, scenario_id: str,
    start_byte: int, end_byte: int,
) -> str:
    """Independent re-encoding of the frozen §3 template (seed order + hash).

    Mirrors ``identity._stable``: the six frozen seed fields joined with ``\\x1f``
    and SHA-256 hashed, with the ``scenario:`` namespace prefix. Deliberately
    excludes ``analyzer_version`` (content-derived identity, §3).
    """
    canonical = "\x1f".join((
        str(repo), relative_path, syntax_kind, scenario_id,
        str(start_byte), str(end_byte),
    ))
    return "scenario:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ScenarioAnnotationIdTests(unittest.TestCase):
    """§3 deterministic id: frozen seed fields, template shape, byte encoding."""

    def test_annotation_id_encoding_is_frozen(self) -> None:
        # Locked byte encoding: fixed inputs -> fixed sha256 (guards drift).
        self.assertEqual(
            scenario_annotation_id(
                "repo:abc123", "src/foo.c", "pragma_region", "race_1", 10, 20),
            "scenario:0ba77fb682b2e66a6ae4d16467a0ff6137fd00f2cb8d28c24037f4dcf749ecf4",
        )

    def test_annotation_id_handles_colon_in_relative_path(self) -> None:
        # §3: a bare ':' in a Linux filename must not make the id ambiguous.
        # The hash encoding keeps the id well-formed and distinct.
        value = scenario_annotation_id(
            "repo:abc123", "src/a:b.c", "pragma_region", "race_1", 10, 20)
        self.assertEqual(
            value,
            "scenario:742364f7010cc657fa1f94bfc06468f06b4dac4af5b9b302b8f2ae676038b6e6",
        )
        # distinct from a path that differs only by the char at the colon site
        self.assertNotEqual(
            value,
            scenario_annotation_id(
                "repo:abc123", "src/aXb.c", "pragma_region", "race_1", 10, 20),
        )

    def test_annotation_id_is_sensitive_to_each_seed_field(self) -> None:
        base = ("repo:abc123", "src/foo.c", "pragma_region", "race_1", 10, 20)
        ids = {
            scenario_annotation_id(*base),
            scenario_annotation_id("repo:abc124", "src/foo.c", "pragma_region", "race_1", 10, 20),
            scenario_annotation_id("repo:abc123", "src/bar.c", "pragma_region", "race_1", 10, 20),
            scenario_annotation_id("repo:abc123", "src/foo.c", "pragma_region", "race_2", 10, 20),
            scenario_annotation_id("repo:abc123", "src/foo.c", "pragma_region", "race_1", 11, 20),
            scenario_annotation_id("repo:abc123", "src/foo.c", "pragma_region", "race_1", 10, 21),
        }
        self.assertEqual(len(ids), 6)


class ScenarioAnnotationGoldenTests(unittest.TestCase):
    """P1/P2/P4 (positive), N1-N4 (0 annotation), U1/U3/U6 (limitation)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "graph.db"
        src = self.root / "src"
        src.mkdir(parents=True)
        fixtures = {
            "p1.c": (
                "int g;\n"
                "void f() {\n"
                "#pragma provenlattice scenario race_1 begin\n"
                "    g = 1;\n"
                "#pragma provenlattice scenario race_1 end\n"
                "}\n"
            ),
            "p2.c": (
                "int a;\n"
                "void f1() {\n"
                "#pragma provenlattice scenario shared begin\n"
                "    a = 1;\n"
                "#pragma provenlattice scenario shared end\n"
                "}\n"
                "void f2() {\n"
                "#pragma provenlattice scenario shared begin\n"
                "    a = 2;\n"
                "#pragma provenlattice scenario shared end\n"
                "}\n"
            ),
            "p4.c": (
                "int a;\n"
                "void f() {\n"
                "#pragma provenlattice scenario outer begin\n"
                "#pragma provenlattice scenario inner begin\n"
                "    a = 1;\n"
                "#pragma provenlattice scenario inner end\n"
                "#pragma provenlattice scenario outer end\n"
                "}\n"
            ),
            "p5.c": (
                "int a;\n"
                "void f() {\n"
                "#pragma provenlattice scenario race_1 begin\n"
                "    a = 1;\n"
                "#pragma provenlattice scenario race_1 end\n"
                "    a = 2;\n"
                "#pragma provenlattice scenario race_1 begin\n"
                "    a = 3;\n"
                "#pragma provenlattice scenario race_1 end\n"
                "}\n"
            ),
            "fs.c": (
                "int tail_counter = 0;\n"
                "#pragma provenlattice scenario tail begin\n"
                "int extra = 1;\n"
                "#pragma provenlattice scenario tail end\n"
            ),
            "n1.c": (
                "int a;\n"
                "void f() {\n"
                "    // scenario race_1\n"
                "    a = 1;\n"
                "}\n"
            ),
            "n2.c": (
                "int a;\n"
                "void f() {\n"
                "    // TODO: fix race\n"
                "    /* GT marker */\n"
                "    a = 1;\n"
                "}\n"
            ),
            "n3.c": (
                "int a;\n"
                "void f() {\n"
                "    a = 1;\n"
                "    a = 2;\n"
                "}\n"
            ),
            "n4.c": (
                "int a;\n"
                "void f() {\n"
                "#pragma myvendor scenario race_1 begin\n"
                "    a = 1;\n"
                "#pragma provenlattice scenario race_1 enter\n"
                "    a = 2;\n"
                "#pragma myvendor scenario race_1 end\n"
                "}\n"
            ),
            "u1.c": (
                "int a;\n"
                "void f() {\n"
                "#pragma provenlattice scenario race_1 begin\n"
                "    a = 1;\n"
                "}\n"
            ),
            "u2.c": (
                "int a;\n"
                "void f() {\n"
                "    a = 1;\n"
                "#pragma provenlattice scenario race_1 end\n"
                "}\n"
            ),
            "u3.c": (
                "int a;\n"
                "void f() {\n"
                "#pragma provenlattice scenario A begin\n"
                "#pragma provenlattice scenario B begin\n"
                "    a = 1;\n"
                "#pragma provenlattice scenario A end\n"
                "#pragma provenlattice scenario B end\n"
                "}\n"
            ),
            "u6.c": (
                "int a;\n"
                "void f( {\n"
                "#pragma provenlattice scenario race_1 begin\n"
                "    a = 1;\n"
                "#pragma provenlattice scenario race_1 end\n"
                "}\n"
            ),
        }
        for name, text in fixtures.items():
            (src / name).write_bytes(text.encode("utf-8"))
        full_index(self.root, self.database)
        self.repo_id = analyze_c_ast_scope(self.root, "src", self.database)["repo_id"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _source(self, relative: str) -> bytes:
        return (self.root / relative).read_bytes()

    def annotations(self, relative: str) -> list[dict]:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute(
                "SELECT n.* FROM analysis_nodes n JOIN files f ON f.file_id=n.file_id "
                "WHERE f.path=? AND n.feature='ast:c' ORDER BY n.analysis_node_id",
                (relative,),
            )]
        return sorted(
            (json.loads(row["payload"]) for row in rows
             if row["kind"] == SCENARIO_ANNOTATION_KIND),
            key=lambda ann: (ann["start_byte"], ann["end_byte"], ann["scenario_id"]),
        )

    def test_p1_single_function_single_region(self) -> None:
        anns = self.annotations("src/p1.c")
        self.assertEqual(len(anns), 1)
        ann = anns[0]
        self.assertEqual(ann["scenario_id"], "race_1")
        self.assertEqual(ann["syntax_kind"], SCENARIO_SYNTAX_KIND)
        self.assertEqual(ann["relative_path"], "src/p1.c")
        self.assertEqual(ann["parse_status"], "resolved")
        self.assertEqual(ann["limitations"], [])
        self.assertIsNotNone(ann["function_symbol_id"])
        self.assertLess(ann["start_byte"], ann["end_byte"])
        # region content span: begin 之后、end 之前 (§2.1)
        self.assertEqual(
            self._source("src/p1.c")[ann["start_byte"]:ann["end_byte"]],
            b"    g = 1;\n",
        )
        self.assertEqual(
            ann["annotation_id"],
            _reference_annotation_id(
                self.repo_id, "src/p1.c", SCENARIO_SYNTAX_KIND, "race_1",
                ann["start_byte"], ann["end_byte"],
            ),
        )

    def test_p2_two_functions_same_scenario_id(self) -> None:
        anns = self.annotations("src/p2.c")
        self.assertEqual(len(anns), 2)
        self.assertEqual({ann["scenario_id"] for ann in anns}, {"shared"})
        self.assertTrue(all(ann["parse_status"] == "resolved" for ann in anns))
        owners = {ann["function_symbol_id"] for ann in anns}
        self.assertEqual(len(owners), 2)
        self.assertNotIn(None, owners)

    def test_p4_nested_regions_both_resolved(self) -> None:
        anns = self.annotations("src/p4.c")
        self.assertEqual(len(anns), 2)
        by_id = {ann["scenario_id"]: ann for ann in anns}
        self.assertIn("outer", by_id)
        self.assertIn("inner", by_id)
        self.assertEqual(by_id["outer"]["parse_status"], "resolved")
        self.assertEqual(by_id["inner"]["parse_status"], "resolved")
        # outer contains inner; no crossing → both resolved
        self.assertLessEqual(
            by_id["outer"]["start_byte"], by_id["inner"]["start_byte"])
        self.assertGreaterEqual(
            by_id["outer"]["end_byte"], by_id["inner"]["end_byte"])

    def test_p5_same_function_two_disjoint_regions_not_merged(self) -> None:
        anns = self.annotations("src/p5.c")
        self.assertEqual(len(anns), 2)
        for ann in anns:
            self.assertEqual(ann["scenario_id"], "race_1")
            self.assertEqual(ann["parse_status"], "resolved")
        ids = {ann["annotation_id"] for ann in anns}
        self.assertEqual(len(ids), 2)  # distinct, no span merge
        spans = sorted((ann["start_byte"], ann["end_byte"]) for ann in anns)
        self.assertLessEqual(spans[0][1], spans[1][0])  # disjoint

    def test_file_scope_region_null_function_no_limitation(self) -> None:
        anns = self.annotations("src/fs.c")
        self.assertEqual(len(anns), 1)
        ann = anns[0]
        self.assertEqual(ann["scenario_id"], "tail")
        self.assertEqual(ann["parse_status"], "resolved")
        self.assertIsNone(ann["function_symbol_id"])
        self.assertEqual(ann["limitations"], [])

    def test_n1_plain_comment_scenario_ignored(self) -> None:
        self.assertEqual(self.annotations("src/n1.c"), [])

    def test_n2_todo_and_marker_ignored(self) -> None:
        self.assertEqual(self.annotations("src/n2.c"), [])

    def test_n3_plain_accesses_without_pragma(self) -> None:
        self.assertEqual(self.annotations("src/n3.c"), [])

    def test_n4_non_whitelist_namespace_and_action_ignored(self) -> None:
        self.assertEqual(self.annotations("src/n4.c"), [])

    def test_u1_unclosed_region_is_unknown(self) -> None:
        anns = self.annotations("src/u1.c")
        self.assertEqual(len(anns), 1)
        self.assertEqual(anns[0]["parse_status"], "unknown")
        self.assertEqual(anns[0]["limitations"], ["unclosed_scenario_region"])

    def test_u2_orphan_end_produces_no_annotation(self) -> None:
        # An end marker with no begin has no region and materialises nothing;
        # FE8-F2's relation layer reports `orphan_scenario_end`.
        self.assertEqual(self.annotations("src/u2.c"), [])

    def test_u3_crossing_regions_are_invalid(self) -> None:
        anns = self.annotations("src/u3.c")
        self.assertEqual(len(anns), 2)
        for ann in anns:
            self.assertEqual(ann["parse_status"], "invalid")
            self.assertEqual(ann["limitations"], ["crossing_scenario_regions"])

    def test_u6_parse_partial_is_unknown(self) -> None:
        anns = self.annotations("src/u6.c")
        self.assertEqual(len(anns), 1)
        self.assertEqual(anns[0]["parse_status"], "unknown")
        self.assertEqual(anns[0]["limitations"], ["annotation_parse_partial"])

    def test_determinism_byte_stable_across_runs(self) -> None:
        first = self.annotations("src/p2.c")
        analyze_c_ast_scope(self.root, "src", self.database)
        second = self.annotations("src/p2.c")
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True),
        )

    def test_default_off_old_envelope_unchanged(self) -> None:
        # A file with no pragma must materialise zero annotation nodes, leaving
        # the pre-FE8 AST node set byte-identical (default OFF = zero envelope
        # regression, §5/§7).
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM analysis_nodes n JOIN files f "
                "ON f.file_id=n.file_id WHERE f.path='src/n3.c' "
                "AND n.feature='ast:c' AND n.kind=?",
                (SCENARIO_ANNOTATION_KIND,),
            ).fetchone()
        self.assertEqual(row[0], 0)

    def test_annotations_keep_single_root_ast_integrity(self) -> None:
        # The additive annotation leaf nodes parent to the FILE root, so the
        # ast query still passes the single-root integrity check (not "corrupt").
        result = query_analysis(self.root, "ast", file_path="src/p1.c",
                                database=self.database)
        self.assertEqual(result["status"], "available")
        self.assertIsNotNone(result["root_id"])
        kinds = {node["kind"] for node in result["nodes"]}
        self.assertIn(SCENARIO_ANNOTATION_KIND, kinds)

    def test_annotation_id_stable_across_reindex(self) -> None:
        # Owner Decision V1 req: id is stable across repeated full indexes of
        # the same repository (deterministic repo_id + content-derived seed).
        first_ids = [ann["annotation_id"] for ann in self.annotations("src/p1.c")]
        db2 = Path(self.temp.name) / "graph2.db"
        full_index(self.root, db2)
        repo_id2 = analyze_c_ast_scope(self.root, "src", db2)["repo_id"]
        self.assertEqual(repo_id2, self.repo_id)
        with closing(sqlite3.connect(db2)) as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute(
                "SELECT n.* FROM analysis_nodes n JOIN files f ON f.file_id=n.file_id "
                "WHERE f.path='src/p1.c' AND n.feature='ast:c' ORDER BY n.analysis_node_id")]
        second = sorted(
            (json.loads(row["payload"]) for row in rows
             if row["kind"] == SCENARIO_ANNOTATION_KIND),
            key=lambda ann: (ann["start_byte"], ann["end_byte"], ann["scenario_id"]),
        )
        self.assertTrue(first_ids)
        self.assertEqual(first_ids, [ann["annotation_id"] for ann in second])

    def test_annotation_id_differs_across_repo_identity(self) -> None:
        # Owner Decision V1 req: same source content under a different repo
        # identity (root path -> distinct store.repo_id) yields a distinct id.
        other_root = Path(self.temp.name) / "repo2"
        other_src = other_root / "src"
        other_src.mkdir(parents=True)
        (other_src / "p1.c").write_bytes((self.root / "src" / "p1.c").read_bytes())
        db2 = Path(self.temp.name) / "graph3.db"
        full_index(other_root, db2)
        repo_id2 = analyze_c_ast_scope(other_root, "src", db2)["repo_id"]
        self.assertNotEqual(repo_id2, self.repo_id)
        with closing(sqlite3.connect(db2)) as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute(
                "SELECT n.* FROM analysis_nodes n JOIN files f ON f.file_id=n.file_id "
                "WHERE f.path='src/p1.c' AND n.feature='ast:c' ORDER BY n.analysis_node_id")]
        other = sorted(
            (json.loads(row["payload"]) for row in rows
             if row["kind"] == SCENARIO_ANNOTATION_KIND),
            key=lambda ann: (ann["start_byte"], ann["end_byte"], ann["scenario_id"]),
        )
        self.assertEqual(len(other), 1)
        self.assertNotEqual(
            self.annotations("src/p1.c")[0]["annotation_id"],
            other[0]["annotation_id"],
        )


class ScenarioWalkerByteSpanTests(unittest.TestCase):
    """Walker point additive ``start_byte``/``end_byte`` publication (§7)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "graph.db"
        src = self.root / "src"
        src.mkdir(parents=True)
        (src / "p.c").write_bytes(
            b"int g;\n"
            b"void f(int *p) {\n"
            b"    if (p != 0) {\n"
            b"        *p = g;\n"
            b"    }\n"
            b"}\n"
        )
        full_index(self.root, self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_walker_points_publish_byte_span(self) -> None:
        analyze_scope(self.root, "src", "sparse_cfg", self.database)
        result = query_analysis(self.root, "sparse_cfg", file_path="src/p.c",
                                database=self.database)
        self.assertEqual(result["status"], "available")
        events = {event["event_id"]: event for event in result["events"]}
        kinds = {event["event_type"] for event in events.values()}
        # structural points and access events must all carry byte spans
        self.assertLessEqual({"BRANCH", "DEREFERENCE", "WRITE", "READ"}, kinds)
        for event in events.values():
            start = event.get("start_byte")
            end = event.get("end_byte")
            self.assertIsNotNone(start)
            self.assertIsNotNone(end)
            self.assertLessEqual(start, end)
            # existing line fields remain (additive, no payload regression)
            self.assertIn("start_line", event)
            self.assertIn("end_line", event)

    def test_ast_contract_and_sparse_cfg_version_bumped(self) -> None:
        self.assertEqual(C_AST_CONTRACT, "c-ast-v4")
        self.assertEqual(ANALYZER_VERSIONS["ast:c"], 4)
        # sparse_cfg:cpp advanced 1->2 for FE8-F1 (byte spans), 2->3 for
        # FE-5 (THREAD_JOIN handle annotations), and 3->4 for SUB-09 F1
        # (indirect field-callee READ). Each advance invalidates the
        # sparse_cfg cache so the new facts are rematerialized.
        self.assertEqual(ANALYZER_VERSIONS["sparse_cfg:cpp"], 4)

    def test_sparse_cfg_version_bump_restalifies_cache(self) -> None:
        # Owner Decision V1 (sparse_cfg:cpp 1->2): the version genuinely
        # participates in cache invalidation, not a dead constant. A cached
        # result recorded under the old version is stale and rematerialized.
        analyze_scope(self.root, "src", "sparse_cfg", self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE analysis_state SET feature_version = 1 "
                "WHERE feature='sparse_cfg:cpp'")
            connection.commit()
        result = analyze_scope(self.root, "src", "sparse_cfg", self.database)
        self.assertEqual(
            result["features"]["sparse_cfg"]["materialized"], ["src/p.c"])
        env = query_analysis(self.root, "sparse_cfg", file_path="src/p.c",
                             database=self.database)
        self.assertEqual(env["status"], "available")


if __name__ == "__main__":
    unittest.main()