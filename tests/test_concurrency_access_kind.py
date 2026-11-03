"""R1 first-class access_kind at scenario level (SUB-02).

A first-class ``access_kind == READ_MODIFY_WRITE`` drives ``non_atomic_incdec``:
an update/compound assignment is an RMW, while ``x = x + 1`` reads its right-hand
``x`` but is a plain write -- it must not surface as a non-atomic increment.
"""

import tempfile
import unittest
from pathlib import Path

from provenlattice.analysis_query import query_analysis
from provenlattice.concurrency_race_scenarios import build_race_scenarios
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


class RmwScenarioTestBase(unittest.TestCase):
    FIXTURE: str = ""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root, "src/a.c", self.FIXTURE)
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("sparse_cfg",), database=self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def events(self) -> list[dict]:
        env = query_analysis(
            self.root, "sparse_cfg", file_path="src/a.c", database=self.database)
        self.assertEqual(env["status"], "available", env.get("reason"))
        return env["events"]

    def incdec_subjects(self) -> set[str]:
        return {
            candidate["subject"]
            for candidate in build_race_scenarios(self.events())["candidates"]
            if candidate["scenario_id"] == "non_atomic_incdec"
        }


class UpdateIsRmwTests(RmwScenarioTestBase):
    FIXTURE = (
        "int g_count;\n"
        "void a(void) { g_count++; }\n"
        "void b(void) { g_count += 1; }\n"
    )

    def test_update_is_incdec(self):
        self.assertEqual(self.incdec_subjects(), {"g_count"})


class SelfReadIsNotRmwTests(RmwScenarioTestBase):
    FIXTURE = (
        "int g_count;\n"
        "void a(void) { g_count = g_count + 1; }\n"
        "void b(void) { g_count = 0; }\n"
    )

    def test_self_read_assignment_is_not_incdec(self):
        self.assertNotIn("g_count", self.incdec_subjects())


if __name__ == "__main__":
    unittest.main()