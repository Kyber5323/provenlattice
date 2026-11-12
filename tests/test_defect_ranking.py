"""P0 selection-layer tests: rank order, global cut, family floor, gate order.

These pin the three behaviours the benchmark exposed and the P0 change fixes.
Each test names the failure it prevents, because the regression they guard is
silent: a window that collapses to one family still *looks* like a working
query, it just answers a different question than the reader thinks.

Nothing here asserts on the matrix contents' semantic correctness -- that is
``test_defect_i2a.py``'s job, and those 591 lines are left untouched.
"""

import json
import tempfile
import unittest
from pathlib import Path

from provenlattice.defect import (
    AMBIGUOUS,
    GRADE_RANK,
    RESOLVED,
    UNRESOLVED,
    Candidate,
    _family_grade_rank,
)
from provenlattice.defect_query import _select_window, query_defect_candidates
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope


def make(
    defect_key: str,
    confidence: float,
    *,
    status: str = RESOLVED,
    anchor: str = "event:0000",
    direct: bool = True,
    discriminator: str = "d",
) -> Candidate:
    """A candidate that exists only to be ordered, never to be adjudicated."""
    return Candidate(
        defect_key=defect_key,
        query=defect_key,
        subject={"name": "p", "kind": "dereference"},
        anchor=anchor,
        discriminator=discriminator,
        resolution_status=status,
        confidence=confidence,
        metadata={"direct": direct},
    )


class RankKeyTests(unittest.TestCase):
    """``rank_key`` is the quality order; ``sort_key`` stays the identity order."""

    def test_grade_outranks_a_larger_family(self) -> None:
        # 20 grade-A 1.1 candidates at the lowest band, one grade-A 9.1 above it.
        many = [make("1.1", 0.5, anchor=f"event:1{i:03d}") for i in range(20)]
        one = [make("9.1", 0.7, anchor="event:9999")]
        ordered = sorted(many + one, key=lambda c: c.rank_key())
        self.assertEqual(ordered[0].defect_key, "9.1")

    def test_confidence_breaks_ties_inside_a_family(self) -> None:
        low = make("1.1", 0.3, anchor="event:0000")
        high = make("1.1", 0.5, anchor="event:9999")
        ordered = sorted([low, high], key=lambda c: c.rank_key())
        self.assertEqual(ordered[0].confidence, 0.5)

    def test_resolution_is_ordered_when_score_ties(self) -> None:
        resolved = make("3.1", 0.5, status=RESOLVED)
        ambiguous = make("3.1", 0.5, status=AMBIGUOUS)
        unresolved = make("3.1", 0.5, status=UNRESOLVED)
        ordered = sorted([unresolved, ambiguous, resolved], key=lambda c: c.rank_key())
        self.assertEqual([c.resolution_status for c in ordered],
                         [RESOLVED, AMBIGUOUS, UNRESOLVED])

    def test_sort_key_is_still_lexical_identity(self) -> None:
        # Pinned deliberately: identity order must not become quality order,
        # or every candidate id would move when a confidence band is retuned.
        a = make("1.1", 0.5, anchor="event:0000")
        b = make("9.1", 0.9, anchor="event:0000")
        self.assertEqual(sorted([b, a], key=lambda c: c.sort_key())[0].defect_key, "1.1")

    def test_grade_table_matches_the_matrix(self) -> None:
        self.assertEqual(GRADE_RANK, {"A": 3, "B": 2, "C": 1})
        self.assertEqual(_family_grade_rank("1.1"), 3)   # A
        self.assertEqual(_family_grade_rank("3.1"), 2)   # B
        self.assertEqual(_family_grade_rank("taint"), 0)  # no matrix row, by design

    def test_rank_key_is_total_and_deterministic(self) -> None:
        a = make("1.1", 0.5, anchor="event:0001")
        b = make("1.1", 0.5, anchor="event:0002")
        first = [c.anchor for c in sorted([b, a], key=lambda c: c.rank_key())]
        second = [c.anchor for c in sorted([a, b], key=lambda c: c.rank_key())]
        self.assertEqual(first, second)


class SelectWindowTests(unittest.TestCase):
    """The cut: gate first, one per-family floor, then rank order fills."""

    def test_limit_is_global(self) -> None:
        cands = [make("1.1", 0.5, anchor=f"event:1{i:03d}") for i in range(20)]
        cands += [make("9.1", 0.7, anchor="event:9000")]
        selected, _, meta = _select_window(
            cands, limit=20, gate=0.3, suppress_below=True)
        self.assertEqual(len(selected), 20)
        self.assertEqual(meta["population"], 21)
        self.assertTrue(meta["window_truncated"])

    def test_one_family_cannot_own_a_small_window(self) -> None:
        # The benchmark's exact symptom: 1.1 everywhere, 4.1 and 9.1 never seen.
        cands = [make("1.1", 0.5, anchor=f"event:1{i:03d}") for i in range(50)]
        cands += [make("4.1", 0.6, anchor="event:4000"),
                  make("9.1", 0.7, anchor="event:9001")]
        selected, _, meta = _select_window(
            cands, limit=3, gate=0.3, suppress_below=True)
        self.assertEqual(len(selected), 3)
        self.assertEqual(sorted({c.defect_key for c in selected}),
                         ["1.1", "4.1", "9.1"])
        self.assertEqual(meta["family_floor"], 1)

    def test_gate_runs_before_the_cut_so_the_window_is_filled(self) -> None:
        # Five low-band 1.1 candidates would have filled a k=3 window on rank
        # alone, then been suppressed, leaving one survivor and two empty slots.
        low = [make("1.1", 0.2, anchor=f"event:1{i:03d}") for i in range(5)]
        keep = [make("4.1", 0.6, anchor="event:4000"),
                make("9.1", 0.7, anchor="event:9001")]
        selected, suppressed, meta = _select_window(
            low + keep, limit=3, gate=0.3, suppress_below=True)
        self.assertEqual(len(selected), 2)
        self.assertEqual(len(suppressed), 5)
        self.assertEqual(sorted({c.defect_key for c in selected}), ["4.1", "9.1"])
        self.assertEqual(meta["after_gate"], 2)
        self.assertFalse(meta["window_truncated"])

    def test_gate_disabled_keeps_everything(self) -> None:
        cands = [make("1.1", 0.1), make("4.1", 0.9)]
        selected, suppressed, _ = _select_window(
            cands, limit=2, gate=None, suppress_below=False)
        self.assertEqual(len(selected), 2)
        self.assertEqual(suppressed, [])

    def test_zero_limit_is_an_empty_window_not_an_error(self) -> None:
        cands = [make("1.1", 0.5)]
        selected, _, meta = _select_window(
            cands, limit=0, gate=0.3, suppress_below=True)
        self.assertEqual(selected, [])
        self.assertEqual(meta["population"], 1)

    def test_window_preserves_rank_order(self) -> None:
        cands = [make("9.1", 0.7, anchor="event:9"),
                 make("1.1", 0.5, anchor="event:1"),
                 make("4.1", 0.6, anchor="event:4")]
        selected, _, _ = _select_window(
            cands, limit=3, gate=0.3, suppress_below=True)
        self.assertEqual([c.confidence for c in selected], [0.7, 0.6, 0.5])

    def test_selection_is_stable_across_runs(self) -> None:
        cands = [make("1.1", 0.5, anchor=f"event:{i:04d}") for i in range(10)]
        first = [c.anchor for c in _select_window(
            cands, limit=4, gate=0.3, suppress_below=True)[0]]
        second = [c.anchor for c in _select_window(
            list(reversed(cands)), limit=4, gate=0.3, suppress_below=True)[0]]
        self.assertEqual(first, second)


class QueryWindowIntegrationTests(unittest.TestCase):
    """End to end through the real pipeline: the response reports its own cut."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "graph.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self, body: str) -> None:
        path = self.root / "src" / "t.c"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body.encode("utf-8"))
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("defect",), database=self.database)

    def test_response_carries_the_window_and_family_audit(self) -> None:
        self.build(
            "#include <stdlib.h>\n"
            "int f01(int n) {\n"
            "    char *p = malloc(64);\n"
            "    if (!p) return -1;\n"
            "    free(p);\n"
            "    free(p);\n"
            "    return n;\n"
            "}\n")
        env = query_defect_candidates(
            self.root, defect_type="all", max_candidates=5, database=self.database)
        self.assertEqual(env["status"], "available")
        self.assertIn("ranking", env)
        self.assertIn("family_distribution", env)
        self.assertIn("family_population", env)
        self.assertLessEqual(len(env["candidates"]), 5)
        # the audit block must describe the same window the caller receives
        self.assertEqual(
            env["family_distribution"],
            {c["defect_key"]: sum(1 for d in env["candidates"]
                                  if d["defect_key"] == c["defect_key"])
             for c in env["candidates"]})
        self.assertEqual(env["ranking"]["limit"], 5)
        self.assertEqual(
            env["ranking"]["population"],
            sum(env["family_population"].values()))

    def test_max_candidates_one_returns_at_most_one(self) -> None:
        self.build(
            "#include <stdlib.h>\n"
            "int f01(int n) {\n"
            "    char *p = malloc(64);\n"
            "    if (!p) return -1;\n"
            "    free(p);\n"
            "    free(p);\n"
            "    return n;\n"
            "}\n")
        env = query_defect_candidates(
            self.root, defect_type="double_free", max_candidates=1,
            database=self.database)
        self.assertEqual(env["status"], "available")
        self.assertLessEqual(len(env["candidates"]), 1)

    def test_output_is_json_serializable(self) -> None:
        self.build(
            "#include <stdlib.h>\n"
            "int f01(int n) {\n"
            "    char *p = malloc(64);\n"
            "    if (!p) return -1;\n"
            "    free(p);\n"
            "    return n;\n"
            "}\n")
        env = query_defect_candidates(
            self.root, defect_type="all", max_candidates=3, database=self.database)
        json.dumps(env, ensure_ascii=False)  # must not raise


if __name__ == "__main__":
    unittest.main()
