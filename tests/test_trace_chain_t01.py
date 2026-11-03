"""T01 candidate trace_evidence assembly tests (Parent #36 / SUB-01).

Freezes the pure chain assembler and the additive wiring in the concurrency
profile: every candidate carries its own ``evidence_id`` and an ordered
``trace_evidence`` chain whose refs only cite the candidate's existing event ids
and its own ``E-DEFECT-*`` id. Old envelope keys must not regress.
"""

import json
import tempfile
import unittest
from pathlib import Path

from provenlattice.concurrency_profile import run_concurrency_profile
from provenlattice.graph import full_index
from provenlattice.trace_chain import (
    GROUND_FACT,
    GROUND_LIMITATION,
    STEP_ACCESS,
    STEP_LIMITATION,
    assemble_trace_chain,
)


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


class AssembleTraceChainTests(unittest.TestCase):
    CANDIDATE = {
        "subject": "g_count",
        "identity": "g_count",
        "storage": "global",
        "exact": True,
        "scenario_id": "shared_state_race",
        "candidate_state": "ambiguous",
        "unresolved": False,
        "related_accesses": [
            {"event_id": "ev-write", "event_type": "WRITE", "owner": "worker",
             "start_line": 4, "end_line": 4, "exact": "true", "subject_type": "int"},
            {"event_id": "ev-read", "event_type": "READ", "owner": "worker",
             "start_line": 4, "end_line": 4, "exact": "true", "subject_type": "int"},
        ],
        "related_locks": [],
        "evidence_references": ["ev-read", "ev-write"],
        "missing_evidence": ["may_parallel", "lock_protection"],
    }

    def test_chain_opens_with_access_and_closes_with_limitation(self):
        chain = assemble_trace_chain(self.CANDIDATE, "E-DEFECT-abc")
        self.assertGreaterEqual(len(chain), 1)
        self.assertEqual(chain[0]["step_type"], STEP_ACCESS)
        self.assertEqual(chain[-1]["step_type"], STEP_LIMITATION)

    def test_access_steps_are_ordered_by_line_then_event_id(self):
        chain = assemble_trace_chain(self.CANDIDATE, "E-DEFECT-abc")
        access_steps = [s for s in chain if s["step_type"] == STEP_ACCESS]
        self.assertEqual(
            [s["refs"][0] for s in access_steps], ["ev-read", "ev-write"])

    def test_refs_only_cite_candidate_events_and_evidence_id(self):
        chain = assemble_trace_chain(self.CANDIDATE, "E-DEFECT-abc")
        allowed = set(self.CANDIDATE["evidence_references"]) | {"E-DEFECT-abc"}
        for step in chain:
            for ref in step["refs"]:
                self.assertIn(ref, allowed)

    def test_first_step_cites_the_candidate_evidence_id(self):
        chain = assemble_trace_chain(self.CANDIDATE, "E-DEFECT-abc")
        self.assertIn("E-DEFECT-abc", chain[0]["refs"])

    def test_limitation_steps_are_lexicographic_and_grounded_as_limitations(self):
        chain = assemble_trace_chain(self.CANDIDATE, "E-DEFECT-abc")
        limitations = [s for s in chain if s["step_type"] == STEP_LIMITATION]
        self.assertEqual(
            [s["label"] for s in limitations],
            ["lock_protection not proven", "may_parallel not proven"])
        self.assertTrue(all(s["ground"] == GROUND_LIMITATION for s in limitations))
        self.assertTrue(all(s["refs"] == [] for s in limitations))

    def test_access_steps_are_grounded_as_facts(self):
        chain = assemble_trace_chain(self.CANDIDATE, "E-DEFECT-abc")
        access_steps = [s for s in chain if s["step_type"] == STEP_ACCESS]
        self.assertTrue(access_steps)
        self.assertTrue(all(s["ground"] == GROUND_FACT for s in access_steps))

    def test_steps_are_numbered_contiguously(self):
        chain = assemble_trace_chain(self.CANDIDATE, "E-DEFECT-abc")
        self.assertEqual([s["step"] for s in chain], list(range(1, len(chain) + 1)))

    def test_legacy_lock_candidate_with_string_locks(self):
        candidate = {
            "scenario_id": "lock_discipline",
            "subject": "a <-> b",
            "identity": None,
            "related_locks": ["a", "b"],
            "evidence_references": [],
            "missing_evidence": ["control_flow"],
        }
        chain = assemble_trace_chain(candidate, "E-DEFECT-xyz")
        self.assertGreaterEqual(len(chain), 1)
        self.assertEqual(chain[-1]["step_type"], STEP_LIMITATION)

    def test_empty_candidate_produces_single_limitation(self):
        chain = assemble_trace_chain({}, "E-DEFECT-empty")
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0]["step_type"], STEP_LIMITATION)
        self.assertEqual(chain[0]["ground"], GROUND_LIMITATION)


class TraceChainProfileTests(unittest.TestCase):
    FIXTURE = (
        "#include <pthread.h>\n"
        "int g_count;\n"
        "_Bool g_flag;\n"
        "void *worker(void *a) { g_count++; return 0; }\n"
        "void updater(void) { g_count += 1; }\n"
        "void set(void) { g_flag = 1; }\n"
        "void get(void) { int x = g_flag; (void)x; }\n"
    )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root, "src/a.c", self.FIXTURE)
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def result(self) -> dict:
        return run_concurrency_profile(self.root, database=self.database, scope="src")

    def test_each_race_candidate_carries_evidence_id_and_trace_chain(self):
        scenarios = self.result()["scenarios"]
        candidates = scenarios["shared_state_race"]["candidates"]
        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertIn("evidence_id", candidate)
            self.assertTrue(candidate["evidence_id"].startswith("E-DEFECT-"))
            self.assertIn("trace_evidence", candidate)
            self.assertTrue(candidate["trace_evidence"])
            self.assertEqual(candidate["trace_evidence"][0]["step_type"], STEP_ACCESS)
            self.assertEqual(candidate["trace_evidence"][-1]["step_type"], STEP_LIMITATION)

    def test_chain_refs_are_traceable_within_the_candidate(self):
        scenarios = self.result()["scenarios"]
        for scenario_id, scenario in scenarios.items():
            for candidate in scenario["candidates"]:
                allowed = set(candidate.get("evidence_references", [])) | {
                    candidate["evidence_id"]}
                for step in candidate["trace_evidence"]:
                    for ref in step["refs"]:
                        self.assertIn(ref, allowed)

    def test_output_is_byte_stable_across_runs(self):
        first = self.result()
        second = self.result()
        self.assertEqual(
            json.dumps(first["scenarios"], sort_keys=True),
            json.dumps(second["scenarios"], sort_keys=True))

    def test_old_envelope_keys_do_not_regress(self):
        result = self.result()
        self.assertEqual(result["status"], "complete")
        self.assertIn("profile", result)
        self.assertIn("plan", result)
        self.assertIn("scenarios", result)
        self.assertIn("evidence", result)
        self.assertIn("coverage", result)
        self.assertTrue(result["evidence"])
        for item in result["evidence"]:
            self.assertEqual(item["kind"], "DEFECT_CANDIDATE")


if __name__ == "__main__":
    unittest.main()