"""Matrix 2.1 (integer overflow) is expanded, and stays candidate-grade.

The row was parked first -- registered with no query, naming `ARITH` as the
event the pipeline could not produce -- so the gap was visible instead of the
key simply being absent. The event is now produced (the sparse CFG walker emits
one ARITH point per multiplication/shift inside an allocation's size argument),
so the row carries a query and these tests pin what that query does and, just
as importantly, what it refuses to claim.
"""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice.defect import (
    INTEGER_OVERFLOW_KEY,
    query_for,
)
from provenlattice.defect_query import query_defect_candidates
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope
from provenlattice.semantics.events import LEVEL_0_EVENTS, STRUCTURAL_EVENTS
from provenlattice.semantics.vocabulary import PRODUCIBLE_EVENTS, load_defect_patterns

SIZE_BODY = """#include <stdlib.h>

int copy_n(void **dst, int count, int elem) {
    void *p = malloc(count * elem);
    if (!p) { return -1; }
    *dst = p;
    return 0;
}

int plain(int n) {
    char *r = malloc(n);
    if (!r) { return -1; }
    free(r);
    return 0;
}

int folded(void) {
    char *s = malloc(4 * 8);
    if (!s) { return -1; }
    free(s);
    return 0;
}
"""


class IntegerOverflowMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.patterns = {pattern.key: pattern for pattern in load_defect_patterns()}

    def test_the_row_is_expanded_with_its_grade(self) -> None:
        self.assertIn(INTEGER_OVERFLOW_KEY, self.patterns)
        pattern = self.patterns[INTEGER_OVERFLOW_KEY]
        self.assertEqual(pattern.grade, "B")
        self.assertTrue(pattern.expanded)
        self.assertEqual(pattern.query, "integer_overflow")

    def test_the_event_it_needs_is_now_producible(self) -> None:
        # The row declared ARITH as a gap; the walker now emits it, and the gap
        # report must agree or it would keep reporting a solved problem.
        self.assertIn("ARITH", STRUCTURAL_EVENTS)
        self.assertIn("ARITH", PRODUCIBLE_EVENTS)
        self.assertNotIn("ARITH", LEVEL_0_EVENTS)
        self.assertEqual(self.patterns[INTEGER_OVERFLOW_KEY].missing_events(), ())

    def test_the_key_resolves_to_the_query(self) -> None:
        self.assertEqual(query_for(INTEGER_OVERFLOW_KEY), "integer_overflow")


class IntegerOverflowQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "g.db"
        (self.root / "src").mkdir(parents=True)
        (self.root / "src" / "t.c").write_text(SIZE_BODY, encoding="utf-8")
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("defect",), database=self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def query(self, **kwargs) -> dict:
        return query_defect_candidates(
            self.root, defect_type="integer_overflow",
            database=self.database, **kwargs)

    def test_a_runtime_product_feeding_an_allocation_is_a_candidate(self) -> None:
        env = self.query(max_candidates=10)
        self.assertEqual(env["status"], "available")
        names = {cand["subject"]["name"] for cand in env["candidates"]}
        self.assertIn("count", names)

    def test_the_candidate_is_ambiguous_and_says_what_is_missing(self) -> None:
        env = self.query(max_candidates=10)
        self.assertTrue(env["candidates"])
        cand = env["candidates"][0]
        self.assertEqual(cand["defect_key"], INTEGER_OVERFLOW_KEY)
        self.assertEqual(cand["resolution_status"], "ambiguous")
        # The three facts that decide the defect are outside the graph; the
        # candidate must name them rather than imply the arithmetic settled it.
        missing = " ".join(cand["missing_evidence"])
        self.assertIn("width", missing)
        self.assertIn("range", missing.lower())
        self.assertIn("attacker", missing)

    def test_a_constant_product_is_declined_and_counted(self) -> None:
        # `malloc(4 * 8)` cannot wrap; it must not become a candidate, and the
        # decline must be visible in coverage rather than silently dropped.
        env = self.query(max_candidates=10)
        expressions = {cand["metadata"]["size_expression"] for cand in env["candidates"]}
        self.assertNotIn("4 * 8", expressions)
        population = env["coverage"]["population"]
        self.assertGreaterEqual(population["constant_products"], 1)

    def test_a_plain_allocation_size_is_not_a_candidate(self) -> None:
        # `malloc(n)` has no arithmetic at all: no ARITH point, no candidate.
        env = self.query(max_candidates=10)
        names = {cand["subject"]["name"] for cand in env["candidates"]}
        self.assertNotIn("n", names)

    def test_the_query_is_part_of_all(self) -> None:
        env = query_defect_candidates(
            self.root, defect_type="all", max_candidates=20, database=self.database)
        self.assertEqual(env["status"], "available")
        self.assertIn(INTEGER_OVERFLOW_KEY, env["matrix_keys"])
        self.assertIn("integer_overflow", env["provenance"]["queries"])


if __name__ == "__main__":
    unittest.main()
