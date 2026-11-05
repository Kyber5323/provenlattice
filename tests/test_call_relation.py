"""F2 (SUB-09/R6): query-time MAY_CALL derivation (call relation).

Freezes the F2 contract, which the Owner Implementation Gate authorized as a
query-time, default-OFF, same-file reader over the facts F1 materialised:

* an assigned function-pointer field (``g_timePara.procFunc = timer_fn; ...
  g_timePara.procFunc()``) derives ``MAY_CALL=[timer_fn]`` with
  ``resolution="unique_same_file_assignment"``;
* an unassigned field derives ``MAY_CALL=[]`` with ``resolution="unknown"`` and
  ``may_be_false_positive=true`` -- the F1 READ is still published;
* two assignments (``p.fn = a; p.fn = b;``) derive ``MAY_CALL=[]`` with
  ``resolution="ambiguous_multiple_assignments"`` -- never a "last assignment"
  pick of ``b``;
* a typedef'd field and a prototype+definition of one target both resolve to a
  single ``MAY_CALL`` target;
* an indirect call never upgrades to a definite ``CALLS``: the reader is pure,
  writes no edge, and reuses ``test_c_call_semantics.py`` for the resolver's
  ``resolved_symbol_id is None`` invariant.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice.analysis_call_relation import (
    RESOLUTION_AMBIGUOUS,
    RESOLUTION_UNIQUE,
    RESOLUTION_UNKNOWN,
    query_call_relation,
)
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


class CallRelationTestBase(unittest.TestCase):
    FIXTURE: str = ""
    FILENAME: str = "src/a.c"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root, self.FILENAME, self.FIXTURE)
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("sparse_cfg",), database=self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def result(self) -> dict:
        return query_call_relation(
            self.root, self.FILENAME, database=self.database)

    def relations(self) -> list[dict]:
        result = self.result()
        self.assertEqual(result["status"], "available", result.get("reason"))
        return result["call_relations"]


class GoldenAssignedTests(CallRelationTestBase):
    FIXTURE = (
        "struct TimerParams { void (*procFunc)(void); };\n"
        "struct TimerParams g_timePara;\n"
        "void timer_fn(void) {}\n"
        "void setit(void) { g_timePara.procFunc = timer_fn; }\n"
        "void callit(void) { g_timePara.procFunc(); }\n"
    )

    def test_may_call_unique_same_file_assignment(self):
        relations = self.relations()
        self.assertEqual(len(relations), 1)
        relation = relations[0]
        self.assertEqual(relation["subject_decl"], "global:g_timePara")
        self.assertEqual(relation["subject_member"], "procFunc")
        self.assertEqual(relation["may_call"], ["timer_fn"])
        self.assertEqual(relation["resolution"], RESOLUTION_UNIQUE)
        self.assertFalse(relation["may_be_false_positive"])

    def test_read_and_write_join_on_the_canonical_slot(self):
        relation = self.relations()[0]
        self.assertEqual(len(relation["read_event_ids"]), 1)
        self.assertEqual(len(relation["write_event_ids"]), 1)


class GoldenUnassignedTests(CallRelationTestBase):
    FIXTURE = (
        "struct TimerParams { void (*procFunc)(void); };\n"
        "struct TimerParams g_timePara;\n"
        "void callit(void) { g_timePara.procFunc(); }\n"
    )

    def test_unassigned_is_unknown_and_false_positive(self):
        relation = self.relations()[0]
        self.assertEqual(relation["may_call"], [])
        self.assertEqual(relation["resolution"], RESOLUTION_UNKNOWN)
        self.assertTrue(relation["may_be_false_positive"])
        # F1 still publishes the READ, so the call site is visible.
        self.assertEqual(len(relation["read_event_ids"]), 1)
        self.assertEqual(relation["write_event_ids"], [])


class GoldenAmbiguousTests(CallRelationTestBase):
    # NIT #1: the receiver must be global/file-static so the WRITE/READ facts
    # are published (local `struct S s;` emits neither).
    FIXTURE = (
        "struct S { void (*fn)(void); };\n"
        "static struct S p;\n"
        "void a(void) {}\n"
        "void b(void) {}\n"
        "void setit(void) { p.fn = a; p.fn = b; }\n"
        "void callit(void) { p.fn(); }\n"
    )

    def test_multiple_assignments_are_ambiguous_not_last_wins(self):
        relation = self.relations()[0]
        self.assertEqual(relation["may_call"], [])
        self.assertEqual(relation["resolution"], RESOLUTION_AMBIGUOUS)
        self.assertFalse(relation["may_be_false_positive"])
        self.assertEqual(len(relation["write_event_ids"]), 2)


class GoldenTypedefFieldTests(CallRelationTestBase):
    # The typedef is never parsed for F2: the call-site READ + the write_source
    # naturally cover the assignment, with no fp declaration / type analysis.
    FIXTURE = (
        "typedef void (*cb_t)(int);\n"
        "struct S { cb_t procFunc; };\n"
        "struct S g_s;\n"
        "void f(int x) {}\n"
        "void setit(void) { g_s.procFunc = f; }\n"
        "void callit(void) { g_s.procFunc(); }\n"
    )

    def test_typedef_field_resolves_one_target(self):
        relation = self.relations()[0]
        self.assertEqual(relation["subject_decl"], "global:g_s")
        self.assertEqual(relation["subject_member"], "procFunc")
        self.assertEqual(relation["may_call"], ["f"])
        self.assertEqual(relation["resolution"], RESOLUTION_UNIQUE)


class GoldenLocalPointerTests(CallRelationTestBase):
    # `fp(1)` is an identifier callee, not a field_expression: F1 emits no
    # indirect READ, so F2 has no slot and derives nothing (documented, §8.1 #5).
    FIXTURE = (
        "int foo(int x) { return x; }\n"
        "void use(void) {\n"
        "    int (*fp)(int);\n"
        "    fp = &foo;\n"
        "    fp(1);\n"
        "}\n"
    )

    def test_local_pointer_call_derives_nothing(self):
        self.assertEqual(self.relations(), [])


class GoldenPrototypeDefinitionTests(CallRelationTestBase):
    # NIT #3: a prototype and a definition of the same function are two nodes
    # (their signatures differ: `(int)` vs `(int x)`) but one logical target.
    FIXTURE = (
        "struct TimerParams { void (*procFunc)(void); };\n"
        "struct TimerParams g_timePara;\n"
        "void timer_fn(int);\n"
        "void timer_fn(int x) {}\n"
        "void setit(void) { g_timePara.procFunc = timer_fn; }\n"
        "void callit(void) { g_timePara.procFunc(); }\n"
    )

    def test_prototype_and_definition_dedupe_to_one_target(self):
        relation = self.relations()[0]
        self.assertEqual(relation["may_call"], ["timer_fn"])
        self.assertEqual(relation["resolution"], RESOLUTION_UNIQUE)


class DeterminismTests(CallRelationTestBase):
    FIXTURE = (
        "struct TimerParams { void (*procFunc)(void); };\n"
        "struct TimerParams g_timePara;\n"
        "void timer_fn(void) {}\n"
        "void setit(void) { g_timePara.procFunc = timer_fn; }\n"
        "void callit(void) { g_timePara.procFunc(); }\n"
    )

    def test_deterministic_output(self):
        first = json.dumps(self.result(), sort_keys=True)
        second = json.dumps(self.result(), sort_keys=True)
        self.assertEqual(first, second)


class PureReaderTests(CallRelationTestBase):
    FIXTURE = (
        "struct TimerParams { void (*procFunc)(void); };\n"
        "struct TimerParams g_timePara;\n"
        "void timer_fn(void) {}\n"
        "void setit(void) { g_timePara.procFunc = timer_fn; }\n"
        "void callit(void) { g_timePara.procFunc(); }\n"
    )

    def _edge_count(self) -> int:
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute("SELECT COUNT(*) FROM edges").fetchone()
            return int(row[0])

    def test_query_writes_no_edge_and_never_asserts_a_definite_call(self):
        before = self._edge_count()
        relation = self.relations()[0]
        # a definite CALLS would be a `resolved_symbol_id`, which this reader
        # never carries; resolution stays within the three frozen MAY_CALL
        # statuses.
        self.assertIn(
            relation["resolution"],
            {RESOLUTION_UNKNOWN, RESOLUTION_UNIQUE, RESOLUTION_AMBIGUOUS},
        )
        self.assertNotIn("resolved_symbol_id", relation)
        self.assertEqual(self._edge_count(), before)


class FailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root, "src/a.c",
              "struct S { void (*fn)(void); };\n"
              "struct S g_s;\n"
              "void callit(void) { g_s.fn(); }\n")
        self.database = Path(self.temp.name) / "graph.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_not_built_preserved(self):
        full_index(self.root, self.database)
        result = query_call_relation(
            self.root, "src/a.c", database=self.database)
        self.assertEqual(result["status"], "not_built")
        self.assertEqual(result["call_relations"], [])

    def test_stale_source_hash_fails_closed(self):
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("sparse_cfg",), database=self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE file_state SET source_hash='deadbeef' WHERE path='src/a.c'")
            connection.commit()
        result = query_call_relation(
            self.root, "src/a.c", database=self.database)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["call_relations"], [])


class UnsupportedLanguageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root, "src/a.py", "def a():\n    return 1\n")
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_python_reports_unsupported(self):
        result = query_call_relation(
            self.root, "src/a.py", database=self.database)
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["call_relations"], [])


if __name__ == "__main__":
    unittest.main()
