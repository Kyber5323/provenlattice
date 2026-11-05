"""F1 (SUB-09/R6): bounded indirect field-callee READ in C (sparse_cfg).

Freezes the F1 contract, which the Owner Implementation Gate authorized for the
C/profile path only:

* a C call whose callee is a `field_expression` (`g_timePara.procFunc()`) emits
  an additive READ of that function-pointer slot, stamped `is_indirect=true` and
  carrying an informative `field_path`;
* the READ's canonical slot (`subject_decl`, `subject_member`) is byte-identical
  to the WRITE the same field publishes, so the two join on the slot F2 uses;
* every other callee shape (a direct call, `(*fp)()`, `table[i]()`, a parameter
  receiver) and the whole C++ path are left unchanged -- no new READ;
* the READ is a plain `access_kind=READ`, so it cannot fabricate an RMW or
  boolean-flag race candidate;
* the `sparse_cfg:cpp` version bump (3 -> 4) forces a rebuild that materializes
  the new fact rather than reusing a stale v3 cache.
"""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice.analysis_c_scfg import ANALYZER
from provenlattice.analysis_query import query_analysis
from provenlattice.concurrency_race_scenarios import build_race_scenarios
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


class IndirectCalleeReadTestBase(unittest.TestCase):
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

    def events(self) -> list[dict]:
        env = query_analysis(
            self.root, "sparse_cfg", file_path=self.FILENAME,
            database=self.database)
        self.assertEqual(env["status"], "available", env.get("reason"))
        return env["events"]

    def accesses(self, owner: str | None = None,
                 event_type: str | None = None,
                 subject: str | None = None) -> list[dict]:
        return [
            event for event in self.events()
            if (owner is None or event.get("owner_qualified_name") == owner)
            and (event_type is None or event.get("event_type") == event_type)
            and (subject is None
                 or event.get("annotations", {}).get("subject") == subject)
        ]

    def indirect_reads(self) -> list[dict]:
        return [
            event for event in self.events()
            if event.get("event_type") == "READ"
            and event.get("annotations", {}).get("is_indirect") == "true"
        ]


class GoldenFieldCalleeTests(IndirectCalleeReadTestBase):
    FIXTURE = (
        "struct TimerParams { void (*procFunc)(void); };\n"
        "struct TimerParams g_timePara;\n"
        "void timer_fn(void) {}\n"
        "void setit(void) { g_timePara.procFunc = timer_fn; }\n"
        "void callit(void) { g_timePara.procFunc(); }\n"
    )

    def test_indirect_call_emits_one_read(self):
        reads = self.accesses("callit", "READ", "g_timePara")
        self.assertEqual(len(reads), 1)

    def test_read_is_stamped_indirect_with_field_path(self):
        read = self.accesses("callit", "READ", "g_timePara")[0]
        anns = read["annotations"]
        self.assertEqual(anns["is_indirect"], "true")
        self.assertEqual(anns["is_write"], "false")
        self.assertEqual(anns["access_kind"], "READ")
        self.assertEqual(anns["field_path"], "g_timePara.procFunc")

    def test_canonical_slot_matches_write_byte_for_byte(self):
        read = self.accesses("callit", "READ", "g_timePara")[0]
        write = self.accesses("setit", "WRITE", "g_timePara")[0]
        self.assertEqual(read["annotations"]["subject_decl"], "global:g_timePara")
        self.assertEqual(read["annotations"]["subject_member"], "procFunc")
        self.assertEqual(
            read["annotations"]["subject_decl"],
            write["annotations"]["subject_decl"],
        )
        self.assertEqual(
            read["annotations"]["subject_member"],
            write["annotations"]["subject_member"],
        )
        self.assertEqual(read["annotations"]["subject_storage"], "global")

    def test_write_path_is_unchanged(self):
        # The WRITE production path stays zero-change: it keeps write_source and
        # gains neither is_indirect nor field_path from the read path.
        write = self.accesses("setit", "WRITE", "g_timePara")[0]
        anns = write["annotations"]
        self.assertEqual(anns["write_source"], "timer_fn")
        self.assertNotIn("is_indirect", anns)
        self.assertNotIn("field_path", anns)


class DirectCallTests(IndirectCalleeReadTestBase):
    FIXTURE = (
        "void foo(void) {}\n"
        "void caller(void) { foo(); }\n"
    )

    def test_direct_call_adds_no_indirect_read(self):
        self.assertEqual(self.indirect_reads(), [])

    def test_direct_call_adds_no_read_of_callee(self):
        self.assertEqual(self.accesses(event_type="READ", subject="foo"), [])


class NonFieldIndirectTests(IndirectCalleeReadTestBase):
    FIXTURE = (
        "typedef int (*handler_t)(int);\n"
        "static int cb0(int x) { return x; }\n"
        "void use_ptr(handler_t fp, int v) { (*fp)(v); }\n"
        "void use_table(void) {\n"
        "    handler_t table[2] = { cb0, cb0 };\n"
        "    table[0](1);\n"
        "}\n"
    )

    def test_pointer_deref_and_subscript_callees_do_not_upgrade(self):
        # `(*fp)()` and `table[0]()` are indirect but not field_expression
        # callees: neither may be re-admitted as an is_indirect READ.
        self.assertEqual(self.indirect_reads(), [])


class ParameterReceiverTests(IndirectCalleeReadTestBase):
    FIXTURE = (
        "struct Driver { void (*cb)(void); };\n"
        "void run(struct Driver *d) { d->cb(); }\n"
    )

    def test_parameter_receiver_is_not_published_as_shared_read(self):
        # `d->cb()` is a field_expression callee, but its receiver is a
        # parameter (frame-scoped): the read must not surface as shared state.
        self.assertEqual(self.indirect_reads(), [])
        self.assertEqual(self.accesses(event_type="READ", subject="d"), [])


class CppPathUntouchedTests(IndirectCalleeReadTestBase):
    FILENAME = "src/t.cpp"
    FIXTURE = (
        "struct T { void (*cb)(void); };\n"
        "struct T g_t;\n"
        "void f(void) {}\n"
        "void callit(void) { g_t.cb(); }\n"
    )

    def test_cpp_field_callee_emits_no_indirect_read(self):
        # F1 is a bounded C-only change; the C++ profile is left untouched.
        self.assertEqual(self.indirect_reads(), [])


class CacheRebuildTests(unittest.TestCase):
    FIXTURE = (
        "struct TimerParams { void (*procFunc)(void); };\n"
        "struct TimerParams g_timePara;\n"
        "void timer_fn(void) {}\n"
        "void callit(void) { g_timePara.procFunc(); }\n"
    )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root, "src/a.c", self.FIXTURE)
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("sparse_cfg",), database=self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _indirect_reads(self) -> list[dict]:
        env = query_analysis(
            self.root, "sparse_cfg", file_path="src/a.c",
            database=self.database)
        self.assertEqual(env["status"], "available", env.get("reason"))
        return [
            event for event in env["events"]
            if event.get("event_type") == "READ"
            and event.get("annotations", {}).get("is_indirect") == "true"
        ]

    def test_stale_v3_cache_is_rebuilt_and_materializes_the_read(self):
        self.assertEqual(len(self._indirect_reads()), 1)
        # Simulate a cache built under the previous analyzer version: the new
        # fact was never materialized there.
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE analysis_state SET feature_version=3 "
                "WHERE feature=? AND file_id IN "
                "(SELECT file_id FROM files WHERE path='src/a.c')",
                (ANALYZER,),
            )
            connection.commit()

        result = analyze_scope(
            self.root, "src", ("sparse_cfg",), database=self.database)
        self.assertEqual(
            result["features"]["sparse_cfg"]["materialized"], ["src/a.c"])
        self.assertEqual(len(self._indirect_reads()), 1)


class RaceCandidateGuardTests(unittest.TestCase):
    FIXTURE = (
        "struct TimerParams { void (*procFunc)(void); };\n"
        "struct TimerParams g_timePara;\n"
        "void timer_fn(void) {}\n"
        "void setit(void) { g_timePara.procFunc = timer_fn; }\n"
        "void callit(void) { g_timePara.procFunc(); }\n"
    )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root, "src/a.c", self.FIXTURE)
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("sparse_cfg",), database=self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _events(self) -> list[dict]:
        env = query_analysis(
            self.root, "sparse_cfg", file_path="src/a.c",
            database=self.database)
        self.assertEqual(env["status"], "available", env.get("reason"))
        return env["events"]

    def test_indirect_read_is_not_an_rmw_candidate(self):
        # The new READ is a plain READ: it must not push the slot into
        # non_atomic_incdec, and it must not turn a struct field into a
        # boolean-flag race.
        scenario_ids = {
            candidate["scenario_id"]
            for candidate in build_race_scenarios(self._events())["candidates"]
        }
        self.assertEqual(scenario_ids, {"shared_state_race"})


class SingleOwnerRaceGuardTests(unittest.TestCase):
    FIXTURE = (
        "struct TimerParams { void (*procFunc)(void); };\n"
        "struct TimerParams g_timePara;\n"
        "void timer_fn(void) {}\n"
        "void callit(void) { g_timePara.procFunc(); }\n"
    )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root, "src/a.c", self.FIXTURE)
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("sparse_cfg",), database=self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_single_owner_read_fabricates_no_candidate(self):
        # A field-callee READ alone (one owner) cannot self-race.
        env = query_analysis(
            self.root, "sparse_cfg", file_path="src/a.c",
            database=self.database)
        self.assertEqual(env["status"], "available", env.get("reason"))
        result = build_race_scenarios(env["events"])
        self.assertEqual(result["candidates"], [])


class DeterminismTests(IndirectCalleeReadTestBase):
    FIXTURE = (
        "struct TimerParams { void (*procFunc)(void); };\n"
        "struct TimerParams g_timePara;\n"
        "void timer_fn(void) {}\n"
        "void setit(void) { g_timePara.procFunc = timer_fn; }\n"
        "void callit(void) { g_timePara.procFunc(); }\n"
    )

    def test_analysis_is_reused_and_deterministic(self):
        first = self.events()
        result = analyze_scope(
            self.root, "src", ("sparse_cfg",), database=self.database)
        self.assertEqual(result["features"]["sparse_cfg"]["materialized"], [])
        self.assertEqual(result["features"]["sparse_cfg"]["reused"], ["src/a.c"])
        second = self.events()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()