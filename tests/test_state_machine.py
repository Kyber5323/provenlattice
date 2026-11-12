"""`state_machine`: opt-in, keyless, and honest about its blind spot.

The matrix has no state-machine row, so the query is addressed by name (the same
asymmetry `taint_path` documents) and carries no matrix key.

It is also deliberately **not** part of `all`: the walker records assignments but
not declaration initialisers, so `static state_t g = ST_IDLE;` followed by
`if (g == ST_IDLE)` is reported as a never-assigned state. That is a false
positive, and `test_initializer_blind_spot_is_what_keeps_this_opt_in` pins it --
so the limitation is a recorded fact rather than folklore, and whoever adds
initialiser points can see exactly which behaviour changes.
"""

import tempfile
import unittest
from pathlib import Path

from provenlattice.defect import QUERY_NAMES, STATE_MACHINE
from provenlattice.defect_query import query_defect_candidates
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope

# ST_RUNNING is compared and never assigned; ST_IDLE/ST_STOPPED are assigned in
# start()/stop(). The state is initialised in a function rather than in its
# declaration, so this fixture does not hit the blind spot.
BODY = """#include <stdio.h>

typedef enum { ST_IDLE = 0, ST_RUNNING = 1, ST_STOPPED = 2 } state_t;

static state_t g_state;

int handle(int x) {
    if (g_state == ST_RUNNING) { return 1; }
    if (g_state == ST_STOPPED) { return 2; }
    if (g_state == ST_IDLE) { return 0; }
    if (x == 0) { return -1; }
    return x;
}

void start(void) { g_state = ST_IDLE; }
void stop(void) { g_state = ST_STOPPED; }
"""

INITIALISER_BODY = """typedef enum { A = 0, B = 1 } st_t;
static st_t g = A;
int f(void) { if (g == A) { return 0; } if (g == B) { return 1; } return 2; }
void go(void) { g = B; }
"""


class StateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "g.db"
        (self.root / "src").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self, body: str) -> None:
        (self.root / "src" / "t.c").write_text(body, encoding="utf-8")
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("defect",), database=self.database)

    def query(self, defect_type: str = STATE_MACHINE, **kwargs) -> dict:
        return query_defect_candidates(
            self.root, defect_type=defect_type, database=self.database, **kwargs)

    def test_the_never_assigned_state_is_reported(self) -> None:
        self.build(BODY)
        env = self.query(max_candidates=10)
        self.assertEqual(env["status"], "available")
        pairs = {(c["subject"]["name"], c["subject"]["value"])
                 for c in env["candidates"]}
        self.assertIn(("g_state", "ST_RUNNING"), pairs)

    def test_assigned_states_are_not_reported(self) -> None:
        self.build(BODY)
        env = self.query(max_candidates=10)
        values = {c["subject"]["value"] for c in env["candidates"]}
        self.assertNotIn("ST_IDLE", values)
        self.assertNotIn("ST_STOPPED", values)

    def test_numeric_comparisons_are_not_state_comparisons(self) -> None:
        self.build(BODY)
        env = self.query(max_candidates=10)
        self.assertTrue(all(not c["subject"]["value"].isdigit()
                            for c in env["candidates"]))

    def test_the_candidate_is_ambiguous_and_names_what_is_missing(self) -> None:
        self.build(BODY)
        cand = self.query(max_candidates=10)["candidates"][0]
        self.assertEqual(cand["resolution_status"], "ambiguous")
        self.assertEqual(cand["defect_key"], "")
        self.assertIn("memcpy", " ".join(cand["missing_evidence"]))

    def test_the_query_is_opt_in(self) -> None:
        # Addressable by name, absent from `all`, and with no matrix row: three
        # separate statements that the same test keeps together.
        self.assertNotIn(STATE_MACHINE, QUERY_NAMES)
        self.build(BODY)
        self.assertEqual(
            query_defect_candidates(
                self.root, defect_type=STATE_MACHINE, database=self.database)["status"],
            "available")
        everything = self.query("all", max_candidates=20)
        self.assertNotIn(STATE_MACHINE, everything["provenance"]["queries"])
        self.assertEqual(
            [c for c in everything["candidates"] if c["query"] == STATE_MACHINE], [])

    def test_initializer_blind_spot_is_what_keeps_this_opt_in(self) -> None:
        # Documented limitation, pinned so it cannot regress silently into a
        # surprise: the declaration initialiser is invisible, so the state looks
        # never-assigned. When initialisers become WRITE points this assertion
        # is expected to flip, and that flip is the fix.
        self.build(INITIALISER_BODY)
        env = self.query(max_candidates=10)
        pairs = {(c["subject"]["name"], c["subject"]["value"])
                 for c in env["candidates"]}
        self.assertIn(("g", "A"), pairs,
                      "the initialiser blind spot changed: if initialisers are "
                      "now recorded as writes, remove this assertion and the "
                      "opt-in note in QUERY_NAMES")


if __name__ == "__main__":
    unittest.main()
