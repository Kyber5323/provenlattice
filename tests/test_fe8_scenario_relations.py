"""FE8-F2 golden tests (Issue #52): query-time scenario relation derivation.

Covers the frozen contract ``fe8_formal_feature_contract.md`` (§2.2 relations,
§2.3 groups, §2.4 namespace isolation, §4 six-condition containment, §5
unknown/missing fail-closed semantics, §6 determinism/sort). The producer is a
pure query-time reader over the FE8-F1 ``scenario_annotations[]`` materialised
by ``ast:c`` and the READ/WRITE access events materialised by ``sparse_cfg:cpp``;
these tests assert the two derived arrays and the fail-closed freshness gate,
not the annotation materialisation (covered by ``test_fe8_scenario_annotations``).
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice.analysis_c_ast import analyze_c_ast_scope
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope
from provenlattice.scenario_relations import (
    BYTE_RANGE_EXACT,
    CONTAINS_ACCESS,
    derive_scenario_relations,
)

_RELATION_FIELDS = frozenset({
    "annotation_id", "scenario_id", "access_event_id", "relation", "join_kind",
    "function_symbol_id", "limitations",
    # denormalised sort/containment keys (§6.2, §4.4)
    "relative_path", "start_byte", "end_byte",
})
_GROUP_FIELDS = frozenset({
    "scenario_id", "function_symbol_id", "annotation_id", "access_event_ids",
})


class ScenarioRelationGoldenTests(unittest.TestCase):
    """P4/P5/X1/X2/U2/U3, namespace isolation, determinism, fail-closed."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "graph.db"
        src = self.root / "src"
        src.mkdir(parents=True)
        fixtures = {
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
            "x1.c": (
                "int a;\n"
                "void f() {\n"
                "#pragma provenlattice scenario cross begin\n"
                "}\n"
                "void g() {\n"
                "    a = 1;\n"
                "#pragma provenlattice scenario cross end\n"
                "}\n"
            ),
            "wrap.c": (
                "int a;\n"
                "#pragma provenlattice scenario s begin\n"
                "void f() {\n"
                "    a = 1;\n"
                "}\n"
                "#pragma provenlattice scenario s end\n"
            ),
            "multi.c": (
                "int a, b;\n"
                "void f() {\n"
                "#pragma provenlattice scenario m begin\n"
                "    a = 1;\n"
                "    b = 2;\n"
                "#pragma provenlattice scenario m end\n"
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
            "ns.c": (
                "int a;\n"
                "void f() {\n"
                "#pragma provenlattice scenario non_atomic_incdec begin\n"
                "    a = 1;\n"
                "#pragma provenlattice scenario non_atomic_incdec end\n"
                "}\n"
            ),
            "none.c": (
                "int a;\n"
                "void f() {\n"
                "    a = 1;\n"
                "}\n"
            ),
        }
        for name, text in fixtures.items():
            (src / name).write_text(text)
        full_index(self.root, self.database)
        analyze_c_ast_scope(self.root, "src", self.database)
        analyze_scope(self.root, "src", "sparse_cfg", self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _result(self) -> dict:
        return derive_scenario_relations(self.root, database=self.database)

    def _relations(self, path: str) -> list[dict]:
        result = self._result()
        self.assertEqual(result["status"], "available", result.get("reason"))
        return [rel for rel in result["scenario_access_relations"]
                if rel["relative_path"] == path]

    def _groups_for(self, scenario_id: str) -> list[dict]:
        result = self._result()
        return [group for group in result["function_scenario_groups"]
                if group["scenario_id"] == scenario_id]

    def test_p4_nested_access_builds_two_relations_and_two_groups(self) -> None:
        relations = self._relations("src/p4.c")
        self.assertEqual(len(relations), 2)
        by_scenario = {rel["scenario_id"]: rel for rel in relations}
        self.assertIn("outer", by_scenario)
        self.assertIn("inner", by_scenario)
        # one access event joins to both nesting annotations (P4)
        self.assertEqual(by_scenario["outer"]["access_event_id"],
                         by_scenario["inner"]["access_event_id"])
        for rel in relations:
            self.assertEqual(rel["relation"], CONTAINS_ACCESS)
            self.assertEqual(rel["join_kind"], BYTE_RANGE_EXACT)
            self.assertEqual(rel["limitations"], [])
            self.assertIsNotNone(rel["function_symbol_id"])
            self.assertEqual(set(rel), _RELATION_FIELDS)
        # outer region contains inner region (containment, §4.4)
        self.assertLessEqual(by_scenario["outer"]["start_byte"],
                             by_scenario["inner"]["start_byte"])
        # the shared access appears in two groups (§2.3 P4)
        outer_group = self._groups_for("outer")
        inner_group = self._groups_for("inner")
        self.assertEqual([g["access_event_ids"] for g in outer_group],
                         [g["access_event_ids"] for g in inner_group])
        self.assertEqual(outer_group[0]["access_event_ids"],
                         [by_scenario["outer"]["access_event_id"]])

    def test_p5_disjoint_regions_not_merged(self) -> None:
        relations = self._relations("src/p5.c")
        self.assertEqual(len(relations), 2)
        # two distinct annotation_ids, same scenario_id (X2: no span merge)
        self.assertEqual({rel["scenario_id"] for rel in relations}, {"race_1"})
        self.assertEqual(len({rel["annotation_id"] for rel in relations}), 2)
        # the access in the gap between the two regions is not joined
        spans = sorted((rel["start_byte"], rel["end_byte"]) for rel in relations)
        self.assertLessEqual(spans[0][1], spans[1][0])
        self.assertEqual(len(self._groups_for("race_1")), 2)

    def test_x1_cross_function_boundary_no_relation(self) -> None:
        # region begins in f() and ends in g(); its access belongs to a
        # different function owner than the region -> §4 condition 5 fails.
        self.assertEqual(self._relations("src/x1.c"), [])

    def test_u2_orphan_end_no_relation(self) -> None:
        # end marker with no begin -> no annotation -> no relation (§5 U2)
        self.assertEqual(self._relations("src/u2.c"), [])

    def test_u3_crossing_regions_no_relation(self) -> None:
        # crossing overlap invalidates both regions -> 0 relation (§5 U3)
        self.assertEqual(self._relations("src/u3.c"), [])

    def test_file_scope_region_wrapping_function_owner_unresolved(self) -> None:
        # A file-scope region (function_symbol_id None) whose byte span wraps a
        # function's access: single-side owner -> relation builds with the
        # `function_owner_unresolved` limitation (§4.5 / §5).
        relations = self._relations("src/wrap.c")
        self.assertEqual(len(relations), 1)
        rel = relations[0]
        self.assertIsNone(rel["function_symbol_id"])
        self.assertEqual(rel["limitations"], ["function_owner_unresolved"])
        self.assertEqual(rel["relation"], CONTAINS_ACCESS)

    def test_multi_access_group_stable_sorted_access_ids(self) -> None:
        # One region containing two writes -> one group with both access ids,
        # stable-sorted by access_event_id (§2.3 / §6).
        relations = self._relations("src/multi.c")
        self.assertEqual(len(relations), 2)
        groups = self._groups_for("m")
        self.assertEqual(len(groups), 1)
        access_ids = groups[0]["access_event_ids"]
        self.assertEqual(access_ids, sorted(access_ids))
        self.assertEqual(set(access_ids),
                         {rel["access_event_id"] for rel in relations})
        self.assertEqual(set(groups[0]), _GROUP_FIELDS)

    def test_namespace_isolation_builtin_scenario_id(self) -> None:
        # §2.4: a source scenario_id equal to a candidate-layer built-in id
        # (`non_atomic_incdec`) stays a plain FE-8 scenario_id; it is read from
        # the annotation arrays only and never rewrites candidate semantics.
        relations = self._relations("src/ns.c")
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]["scenario_id"], "non_atomic_incdec")
        self.assertEqual(relations[0]["relation"], CONTAINS_ACCESS)
        groups = self._groups_for("non_atomic_incdec")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["scenario_id"], "non_atomic_incdec")

    def test_no_pragma_relation_arrays_empty(self) -> None:
        # default OFF (§5/§7): a file with no pragma yields no relation/group.
        self.assertEqual(self._relations("src/none.c"), [])

    def test_relation_sort_order_frozen(self) -> None:
        result = self._result()
        relations = result["scenario_access_relations"]
        self.assertEqual(
            relations,
            sorted(relations, key=lambda rel: (
                rel["relative_path"], rel["start_byte"], rel["end_byte"],
                rel["scenario_id"], rel["access_event_id"],
            )),
        )

    def test_determinism_byte_stable_across_runs(self) -> None:
        first = json.dumps(self._result(), sort_keys=True)
        second = json.dumps(self._result(), sort_keys=True)
        self.assertEqual(first, second)

    def test_stale_source_hash_fails_closed(self) -> None:
        # §5 source_hash_stale: a source-hash change makes both upstream
        # analyzers stale -> the whole query answers empty, not partial.
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE file_state SET source_hash='deadbeef' WHERE path='src/p4.c'")
            connection.commit()
        result = self._result()
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["scenario_access_relations"], [])
        self.assertEqual(result["function_scenario_groups"], [])


class ScenarioRelationFailClosedTests(unittest.TestCase):
    """Upstream not built -> ``not_built`` (no partial relations)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "graph.db"
        src = self.root / "src"
        src.mkdir(parents=True)
        (src / "p1.c").write_text(
            "int g;\n"
            "void f() {\n"
            "#pragma provenlattice scenario race_1 begin\n"
            "    g = 1;\n"
            "#pragma provenlattice scenario race_1 end\n"
            "}\n"
        )
        full_index(self.root, self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_upstream_not_built_answers_not_built(self) -> None:
        result = derive_scenario_relations(self.root, database=self.database)
        self.assertEqual(result["status"], "not_built")
        self.assertEqual(result["scenario_access_relations"], [])
        self.assertEqual(result["function_scenario_groups"], [])
        self.assertEqual(
            set(result["missing_analyzers"]), {"ast:c", "sparse_cfg:cpp"})


if __name__ == "__main__":
    unittest.main()