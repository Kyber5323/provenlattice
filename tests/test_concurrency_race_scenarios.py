"""S4 race scenario tests (Parent #28): non_atomic_incdec / bool_flag_race / shared_state_race.

Freezes the Candidate contract for the three race scenarios: candidates are
evidence-resolution states only, MAY_PARALLEL stays ambiguous, missing
lock/atomic/identity evidence is named, and atomic/local subjects are excluded.
"""

import json
import tempfile
import unittest
from pathlib import Path

from provenlattice.analysis_concurrency_context import build_concurrency_context
from provenlattice.analysis_query import query_analysis
from provenlattice.concurrency_race_scenarios import build_race_scenarios
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


class RaceScenarioTestBase(unittest.TestCase):
    FIXTURE = ""

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

    def result(self) -> dict:
        return build_race_scenarios(self.events())

    def candidates(self, scenario_id: str) -> list[dict]:
        return [
            candidate for candidate in self.result()["candidates"]
            if candidate["scenario_id"] == scenario_id
        ]


class NonAtomicIncdecTests(RaceScenarioTestBase):
    FIXTURE = (
        "#include <pthread.h>\n"
        "int g_count;\n"
        "void *worker(void *a) { g_count++; return 0; }\n"
        "void updater(void) { g_count += 1; }\n"
        "void spawner(void) { pthread_t t; pthread_create(&t, NULL, worker, NULL); }\n"
    )

    def test_rmw_update_is_a_candidate(self):
        incdec = self.candidates("non_atomic_incdec")
        self.assertEqual(len(incdec), 1)
        candidate = incdec[0]
        self.assertEqual(candidate["subject"], "g_count")
        self.assertEqual(candidate["identity"], "global:g_count")
        self.assertEqual(candidate["candidate_state"], "ambiguous")
        self.assertTrue(candidate["rmw_spans"], "RMW spans must be non-empty")

    def test_candidate_carries_unified_contract_fields(self):
        candidate = self.candidates("non_atomic_incdec")[0]
        for field in ("scenario_id", "source_spans", "related_accesses",
                      "candidate_state", "evidence_references", "missing_evidence"):
            self.assertIn(field, candidate)
        self.assertIn("lock_protection", candidate["missing_evidence"])


class BoolFlagRaceTests(RaceScenarioTestBase):
    FIXTURE = (
        "_Bool g_flag;\n"
        "void set(void) { g_flag = 1; }\n"
        "void get(void) { int x = g_flag; (void)x; }\n"
    )

    def test_bool_flag_is_a_candidate(self):
        flags = self.candidates("bool_flag_race")
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["subject"], "g_flag")
        self.assertEqual(flags[0]["candidate_state"], "ambiguous")

    def test_non_bool_subject_is_not_a_flag_race(self):
        # g_flag is _Bool, so it is a flag race; the same subject is also a
        # shared_state_race but must not double as non_atomic_incdec (no RMW).
        self.assertEqual(self.candidates("non_atomic_incdec"), [])


class SharedStateRaceTests(RaceScenarioTestBase):
    FIXTURE = (
        "int g_plain;\n"
        "void writer(void) { g_plain = 1; }\n"
        "void reader(void) { int x = g_plain; (void)x; }\n"
    )

    def test_shared_subject_is_a_candidate(self):
        shared = self.candidates("shared_state_race")
        self.assertEqual(len(shared), 1)
        candidate = shared[0]
        self.assertEqual(candidate["subject"], "g_plain")
        self.assertEqual(candidate["candidate_state"], "ambiguous")
        self.assertIn("lock_protection", candidate["missing_evidence"])
        self.assertIn("may_parallel", candidate["missing_evidence"])


class ExclusionTests(RaceScenarioTestBase):
    FIXTURE = (
        "#include <stdatomic.h>\n"
        "atomic_int g_atomic;\n"
        "int g_shared;\n"
        "void atom_a(void) { atomic_fetch_add(&g_atomic, 1); }\n"
        "void atom_b(void) { atomic_store(&g_atomic, 2); }\n"
        "void shared_a(void) { g_shared++; }\n"
        "void shared_b(void) { g_shared = 1; }\n"
        "void local_only(void) { int l = 0; l++; }\n"
    )

    def test_atomic_subject_is_not_a_race(self):
        subjects = {candidate["subject"] for candidate in self.result()["candidates"]}
        self.assertNotIn("g_atomic", subjects)

    def test_local_variable_is_not_a_race(self):
        subjects = {candidate["subject"] for candidate in self.result()["candidates"]}
        self.assertNotIn("l", subjects)


class UnresolvedIdentityTests(RaceScenarioTestBase):
    FIXTURE = (
        "void a(void) { g_undeclared++; }\n"
        "void b(void) { g_undeclared = 2; }\n"
    )

    def test_unresolved_identity_is_marked(self):
        candidates = [
            candidate for candidate in self.result()["candidates"]
            if candidate["subject"] == "g_undeclared"
        ]
        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertTrue(candidate["unresolved"])
            self.assertIsNone(candidate["identity"])
            self.assertIn("identity_unresolved", candidate["missing_evidence"])


class DeterminismTests(RaceScenarioTestBase):
    FIXTURE = (
        "int g_count;\n"
        "void a(void) { g_count++; }\n"
        "void b(void) { g_count += 1; }\n"
    )

    def test_deterministic_output(self):
        events = self.events()
        first = build_race_scenarios(events)
        second = build_race_scenarios(list(events))
        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(first, sort_keys=True, default=str),
            json.dumps(second, sort_keys=True, default=str),
        )

    def test_consumes_s2_context(self):
        events = self.events()
        context = build_concurrency_context(events)
        with_context = build_race_scenarios(events, context=context)
        without_context = build_race_scenarios(events)
        self.assertEqual(with_context, without_context)

    def test_competing_access_is_empty_without_lock_facts(self):
        # No held-lockset can be derived without SUB-04's inputs, so the
        # access-level pairs must stay empty rather than fabricate "unprotected".
        self.assertEqual(self.result()["competing_access"], [])


class CompetingAccessBase(unittest.TestCase):
    """Base for SUB-05 (R5) access-level competing-access pair tests.

    Analyses both ``events`` (lock facts) and ``sparse_cfg`` (access facts +
    control-flow edges) so ``build_race_scenarios`` can consume SUB-04's
    held-lockset projection.
    """

    FIXTURE = ""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root, "src/a.c", self.FIXTURE)
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("events", "sparse_cfg"),
                      database=self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def result(self, top_k=3) -> dict:
        sparse = query_analysis(
            self.root, "sparse_cfg", file_path="src/a.c", database=self.database)
        self.assertEqual(sparse["status"], "available", sparse.get("reason"))
        locks = query_analysis(
            self.root, "events", file_path="src/a.c", database=self.database)
        self.assertEqual(locks["status"], "available", locks.get("reason"))
        return build_race_scenarios(
            sparse["events"], edges=sparse["edges"], lock_facts=locks["events"],
            top_k=top_k)

    def competing(self, top_k=3) -> list[dict]:
        return self.result(top_k)["competing_access"]


class ReadReadNotCompetingTests(CompetingAccessBase):
    FIXTURE = (
        "int g;\n"
        "int a(void) {\n    return g;\n}\n"
        "int b(void) {\n    return g;\n}\n"
    )

    def test_read_read_is_zero(self):
        self.assertEqual(self.competing(), [])


class ReadWriteCompetingTests(CompetingAccessBase):
    FIXTURE = (
        "int g;\n"
        "void a(void) {\n    g = 1;\n}\n"
        "int b(void) {\n    return g;\n}\n"
    )

    def test_read_write_is_one_pair(self):
        pairs = self.competing()
        self.assertEqual(len(pairs), 1)

    def test_pair_carries_frozen_fields(self):
        pair = self.competing()[0]
        for field in ("same_storage", "at_least_one_write",
                      "may_execute_concurrently", "common_lock",
                      "different_lock", "unprotected", "confidence"):
            self.assertIn(field, pair)
        self.assertEqual(pair["subject"], "g")
        self.assertEqual(pair["identity"], "global:g")
        self.assertEqual(pair["same_storage"], "true")
        self.assertTrue(pair["at_least_one_write"])
        self.assertTrue(pair["may_execute_concurrently"])
        self.assertFalse(pair["common_lock"])
        self.assertTrue(pair["different_lock"])
        self.assertTrue(pair["unprotected"])
        self.assertEqual(pair["confidence"], 0.5)
        self.assertEqual(pair["candidate_state"], "ambiguous")


class SameLockExcludedTests(CompetingAccessBase):
    FIXTURE = (
        "#include <pthread.h>\n"
        "pthread_mutex_t m;\n"
        "int g;\n"
        "void a(void) {\n"
        "    pthread_mutex_lock(&m);\n"
        "    g = 1;\n"
        "    pthread_mutex_unlock(&m);\n"
        "}\n"
        "void b(void) {\n"
        "    pthread_mutex_lock(&m);\n"
        "    g = 2;\n"
        "    pthread_mutex_unlock(&m);\n"
        "}\n"
    )

    def test_same_lock_is_excluded(self):
        self.assertEqual(self.competing(), [])


class DifferentLockCompetingTests(CompetingAccessBase):
    FIXTURE = (
        "#include <pthread.h>\n"
        "pthread_mutex_t m1, m2;\n"
        "int g;\n"
        "void a(void) {\n"
        "    pthread_mutex_lock(&m1);\n"
        "    g = 1;\n"
        "    pthread_mutex_unlock(&m1);\n"
        "}\n"
        "void b(void) {\n"
        "    pthread_mutex_lock(&m2);\n"
        "    g = 2;\n"
        "    pthread_mutex_unlock(&m2);\n"
        "}\n"
    )

    def test_different_lock_is_one_pair_not_unprotected(self):
        pairs = self.competing()
        self.assertEqual(len(pairs), 1)
        self.assertFalse(pairs[0]["unprotected"])
        self.assertTrue(pairs[0]["different_lock"])
        self.assertFalse(pairs[0]["common_lock"])
        # Both sides hold a lock, just not the same one -> the lowest band.
        self.assertEqual(pairs[0]["confidence"], 0.3)
        self.assertEqual(
            [side["held_locks"] for side in pairs[0]["accesses"]],
            [["m1"], ["m2"]],
        )


class CompetingConfidenceOrderingTests(CompetingAccessBase):
    FIXTURE = (
        "#include <pthread.h>\n"
        "pthread_mutex_t m1, m2;\n"
        "int g_unprot;\n"
        "int g_half;\n"
        "int g_diff;\n"
        "void a(void) {\n"
        "    g_unprot = 1;\n"
        "    pthread_mutex_lock(&m1);\n"
        "    g_half = 1;\n"
        "    g_diff = 1;\n"
        "    pthread_mutex_unlock(&m1);\n"
        "}\n"
        "void b(void) {\n"
        "    g_unprot = 2;\n"
        "    g_half = 2;\n"
        "    pthread_mutex_lock(&m2);\n"
        "    g_diff = 2;\n"
        "    pthread_mutex_unlock(&m2);\n"
        "}\n"
    )

    def test_ranked_by_confidence_descending(self):
        pairs = self.competing()
        confidences = [pair["confidence"] for pair in pairs]
        self.assertEqual(confidences, sorted(confidences, reverse=True))
        self.assertEqual(
            [pair["subject"] for pair in pairs],
            ["g_unprot", "g_half", "g_diff"],
        )
        by_subject = {pair["subject"]: pair for pair in pairs}
        self.assertEqual(by_subject["g_unprot"]["confidence"], 0.5)
        self.assertEqual(by_subject["g_half"]["confidence"], 0.4)
        self.assertEqual(by_subject["g_diff"]["confidence"], 0.3)

    def test_top_k_is_configurable_and_does_not_drop_ties(self):
        result = self.result(top_k=2)
        coverage = result["coverage"]["competing_access"]
        self.assertEqual(coverage["total"], 3)
        self.assertEqual(coverage["returned"], 2)
        self.assertTrue(coverage["truncated"])
        self.assertEqual(
            [pair["subject"] for pair in result["competing_access"]],
            ["g_unprot", "g_half"],
        )


class UnresolvedSameStorageTests(CompetingAccessBase):
    FIXTURE = (
        "void a(void) { g_undeclared++; }\n"
        "void b(void) { g_undeclared = 2; }\n"
    )

    def test_unresolved_identity_is_unknown_not_same(self):
        pairs = self.competing()
        self.assertTrue(pairs)
        for pair in pairs:
            self.assertEqual(pair["same_storage"], "unknown")
            self.assertIsNone(pair["identity"])


class CompetingDeterminismTests(CompetingAccessBase):
    FIXTURE = (
        "int g;\n"
        "void a(void) {\n    g = 1;\n}\n"
        "int b(void) {\n    return g;\n}\n"
    )

    def test_competing_access_is_deterministic(self):
        sparse = query_analysis(
            self.root, "sparse_cfg", file_path="src/a.c", database=self.database)
        locks = query_analysis(
            self.root, "events", file_path="src/a.c", database=self.database)
        first = build_race_scenarios(
            sparse["events"], edges=sparse["edges"], lock_facts=locks["events"])
        second = build_race_scenarios(
            list(sparse["events"]), edges=list(sparse["edges"]),
            lock_facts=list(locks["events"]))
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()