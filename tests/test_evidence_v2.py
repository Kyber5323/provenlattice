"""SUB-03 / R0: Evidence Schema V2 five-block additive adapter (contract §5).

Freezes the adapter that assembles SUB-01/02/04/05 facts under each candidate's
``evidence_v2`` container: the five blocks are always present; sub-facts join by
exact ``access_event_id`` only; unresolved/ambiguous state is never upgraded to
``definite``; a boolean/confidence without a proven source is ``null``, never
``false``; and the SUB-05 pair set/order and the old envelope stay unchanged.
"""

import json
import tempfile
import unittest
from pathlib import Path

from provenlattice.concurrency_profile import run_concurrency_profile
from provenlattice.defect_query import query_defect_candidates
from provenlattice.evidence import (
    ACCESS_EVENT_LINKAGE_UNAVAILABLE,
    ACCESS_KIND_UNAVAILABLE,
    CONFIDENCE_UNAVAILABLE,
    IS_WRITE_UNAVAILABLE,
    LOCK_FACTS_UNAVAILABLE,
    TRACE_EVIDENCE_UNAVAILABLE,
    assemble_evidence_v2,
)
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope

FIVE_BLOCKS = {"access", "lock", "competing_access", "trace_evidence",
               "confidence_limitation"}


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


def race_candidate(**overrides) -> dict:
    """A synthetic concurrency candidate in build_race_scenarios' output shape."""
    candidate = {
        "scenario_id": "shared_state_race",
        "subject": "g",
        "identity": "global:g",
        "storage": "global",
        "exact": "true",
        "unresolved": False,
        "owners": ["a", "b"],
        "thread_entry_owners": [],
        "source_spans": [[3, 3], [4, 4]],
        "related_accesses": [
            {"event_id": "event:a:WRITE:10", "event_type": "WRITE",
             "access_kind": "WRITE", "is_write": "true", "owner": "a",
             "start_line": 3, "end_line": 3, "exact": "true", "subject_type": "int"},
            {"event_id": "event:b:READ:20", "event_type": "READ",
             "access_kind": "READ", "is_write": "false", "owner": "b",
             "start_line": 4, "end_line": 4, "exact": "true", "subject_type": "int"},
        ],
        "evidence_references": ["event:a:WRITE:10", "event:b:READ:20"],
        "missing_evidence": ["may_parallel", "lock_protection"],
        "candidate_state": "ambiguous",
        "trace_evidence": [
            {"step": 1, "step_type": "access", "from_loc": {"file": "src/a.c", "line": 3},
             "to_loc": None, "label": "WRITE fact on g",
             "refs": ["event:a:WRITE:10"], "ground": "fact"},
        ],
    }
    candidate.update(overrides)
    return candidate


def defect_candidate(**overrides) -> dict:
    """A synthetic defect candidate in ``Candidate.to_dict()`` shape."""
    candidate = {
        "defect_key": "6.2",
        "query": "resource_lifetime",
        "subject": {"name": "p"},
        "anchor": "event:alloc:0",
        "discriminator": "alloc-leak",
        "resolution_status": "resolved",
        "confidence": 0.6,
        "facts": [],
        "uncertain_facts": [],
        "paths": [],
        "source_evidence": [],
        "missing_evidence": ["control_flow"],
        "metadata": {},
        "evidence_id": "E-DEFECT-000000000000000000000000",
    }
    candidate.update(overrides)
    return candidate


def lockset_record(**overrides) -> dict:
    record = {
        "access_event_id": "event:a:WRITE:10",
        "event_type": "WRITE",
        "owner": "a",
        "owner_symbol_id": "owner:a",
        "subject": "g",
        "subject_decl": "global:g",
        "start_line": 3,
        "end_line": 3,
        "held_locks": ["m"],
        "limitation": ["cross_function_call"],
        "candidate_state": "ambiguous",
    }
    record.update(overrides)
    return record


def competing_pair(**overrides) -> dict:
    pair = {
        "subject": "g",
        "identity": "global:g",
        "same_storage": "true",
        "at_least_one_write": True,
        "may_execute_concurrently": True,
        "common_lock": False,
        "different_lock": True,
        "unprotected": True,
        "confidence": 0.5,
        "candidate_state": "ambiguous",
        "accesses": [
            {"access_event_id": "event:a:WRITE:10", "event_type": "WRITE",
             "access_kind": "WRITE", "is_write": True, "owner": "a",
             "start_line": 3, "source_span": [3, 3], "held_locks": []},
            {"access_event_id": "event:b:READ:20", "event_type": "READ",
             "access_kind": "READ", "is_write": False, "owner": "b",
             "start_line": 4, "source_span": [4, 4], "held_locks": []},
        ],
    }
    pair.update(overrides)
    return pair


class FiveBlocksAlwaysPresentTests(unittest.TestCase):
    def test_concurrency_candidate_has_all_five_blocks(self):
        result = assemble_evidence_v2(race_candidate())
        self.assertEqual(set(result), FIVE_BLOCKS)
        self.assertEqual(result["lock"], {"access_locksets": [], "relations": []})

    def test_defect_candidate_has_all_five_blocks(self):
        result = assemble_evidence_v2(defect_candidate())
        self.assertEqual(set(result), FIVE_BLOCKS)
        self.assertEqual(result["lock"], {"access_locksets": [], "relations": []})
        self.assertEqual(result["access"], [])
        self.assertEqual(result["competing_access"], [])

    def test_blocks_are_not_omitted_when_empty(self):
        empty = assemble_evidence_v2(defect_candidate(
            missing_evidence=[], resolution_status="unresolved", confidence=None))
        self.assertEqual(set(empty), FIVE_BLOCKS)
        self.assertEqual(empty["access"], [])
        self.assertEqual(empty["lock"]["access_locksets"], [])


class AccessBlockTests(unittest.TestCase):
    def test_access_records_normalize_is_write_to_boolean(self):
        result = assemble_evidence_v2(race_candidate())
        writes = {record["access_event_id"]: record["is_write"]
                  for record in result["access"]}
        self.assertIs(writes["event:a:WRITE:10"], True)
        self.assertIs(writes["event:b:READ:20"], False)

    def test_access_records_are_sorted_by_span_then_id(self):
        candidate = race_candidate()
        candidate["related_accesses"] = list(reversed(candidate["related_accesses"]))
        result = assemble_evidence_v2(candidate)
        self.assertEqual(
            [record["access_event_id"] for record in result["access"]],
            ["event:a:WRITE:10", "event:b:READ:20"],
        )

    def test_unknown_access_kind_and_is_write_become_tokens_not_guesses(self):
        candidate = race_candidate()
        candidate["related_accesses"] = [{
            "event_id": "event:c:READ:30", "event_type": "READ",
            "access_kind": "", "is_write": "maybe", "owner": "c",
            "start_line": 5, "end_line": 5,
        }]
        result = assemble_evidence_v2(candidate)
        record = result["access"][0]
        self.assertEqual(record["access_kind"], "unknown")
        self.assertIsNone(record["is_write"])
        limitations = result["confidence_limitation"]["limitations"]
        self.assertIn(ACCESS_KIND_UNAVAILABLE, limitations)
        self.assertIn(IS_WRITE_UNAVAILABLE, limitations)

    def test_missing_access_event_id_is_dropped_and_noted(self):
        candidate = race_candidate()
        candidate["related_accesses"] = [{
            "event_id": "", "event_type": "READ", "access_kind": "READ",
            "is_write": "false", "owner": "c", "start_line": 5, "end_line": 5,
        }]
        result = assemble_evidence_v2(candidate)
        self.assertEqual(result["access"], [])
        self.assertIn(ACCESS_EVENT_LINKAGE_UNAVAILABLE,
                      result["confidence_limitation"]["limitations"])


class LockBlockTests(unittest.TestCase):
    def test_lockset_filtered_to_candidate_access_ids(self):
        result = assemble_evidence_v2(
            race_candidate(),
            access_locksets=[
                lockset_record(),
                lockset_record(access_event_id="event:other:99"),
            ],
            lock_relations=[{"relation": "PROTECTS", "lock": "m",
                             "access_event_id": "event:a:WRITE:10",
                             "owner": "a", "owner_symbol_id": "owner:a"},
                            {"relation": "PROTECTS", "lock": "m",
                             "access_event_id": "event:other:99",
                             "owner": "x", "owner_symbol_id": "owner:x"}],
        )
        self.assertEqual(result["lock"]["access_locksets"], [lockset_record()])
        self.assertEqual(len(result["lock"]["relations"]), 1)
        self.assertEqual(result["lock"]["relations"][0]["access_event_id"],
                         "event:a:WRITE:10")

    def test_lockset_limitation_propagates_to_limitations(self):
        result = assemble_evidence_v2(
            race_candidate(), access_locksets=[lockset_record()])
        self.assertIn("cross_function_call",
                      result["confidence_limitation"]["limitations"])

    def test_missing_lockset_input_is_empty_and_noted_not_unprotected(self):
        result = assemble_evidence_v2(race_candidate())
        self.assertEqual(result["lock"], {"access_locksets": [], "relations": []})
        self.assertIn(LOCK_FACTS_UNAVAILABLE,
                      result["confidence_limitation"]["limitations"])

    def test_same_and_different_lock_require_both_endpoints(self):
        relations = [
            {"relation": "SAME_LOCK", "subject": "global:g",
             "subject_decl": "global:g",
             "access_event_ids": ["event:a:WRITE:10", "event:b:READ:20"],
             "owners": ["a", "b"], "locks": ["m"]},
            {"relation": "DIFFERENT_LOCK", "subject": "global:g",
             "subject_decl": "global:g",
             "access_event_ids": ["event:a:WRITE:10", "event:other:99"],
             "owners": ["a", "x"], "unprotected": True},
        ]
        result = assemble_evidence_v2(race_candidate(), lock_relations=relations)
        self.assertEqual(
            [relation["relation"] for relation in result["lock"]["relations"]],
            ["SAME_LOCK"])


class CompetingAccessBlockTests(unittest.TestCase):
    def test_pair_with_both_endpoints_is_kept(self):
        result = assemble_evidence_v2(
            race_candidate(candidate_state="ambiguous"),
            competing_access=[competing_pair()],
        )
        self.assertEqual(len(result["competing_access"]), 1)
        self.assertEqual(result["competing_access"][0]["same_storage"], "true")

    def test_pair_with_foreign_endpoint_is_dropped(self):
        pair = competing_pair()
        pair["accesses"][1]["access_event_id"] = "event:other:99"
        result = assemble_evidence_v2(race_candidate(), competing_access=[pair])
        self.assertEqual(result["competing_access"], [])

    def test_producer_order_and_set_are_preserved_not_re_ranked(self):
        first = competing_pair(subject="g1", confidence=0.3)
        second = competing_pair(subject="g2", confidence=0.5)
        result = assemble_evidence_v2(
            race_candidate(), competing_access=[first, second])
        self.assertEqual(
            [pair["subject"] for pair in result["competing_access"]],
            ["g1", "g2"],
        )


class ConfidenceLimitationTests(unittest.TestCase):
    def test_ambiguous_concurrency_is_may_not_definite(self):
        result = assemble_evidence_v2(race_candidate())
        self.assertEqual(result["confidence_limitation"]["analysis_kind"], "may")
        self.assertTrue(result["confidence_limitation"]["may_be_false_positive"])

    def test_unresolved_concurrency_is_unknown_not_definite(self):
        result = assemble_evidence_v2(
            race_candidate(candidate_state="unresolved", unresolved=True))
        self.assertEqual(result["confidence_limitation"]["analysis_kind"], "unknown")
        self.assertTrue(result["confidence_limitation"]["may_be_false_positive"])

    def test_resolved_defect_is_may_not_definite(self):
        result = assemble_evidence_v2(defect_candidate())
        self.assertEqual(result["confidence_limitation"]["analysis_kind"], "may")
        self.assertTrue(result["confidence_limitation"]["may_be_false_positive"])

    def test_concurrency_confidence_is_null_without_source(self):
        result = assemble_evidence_v2(race_candidate())
        self.assertIsNone(result["confidence_limitation"]["confidence"])
        self.assertIn(CONFIDENCE_UNAVAILABLE,
                      result["confidence_limitation"]["limitations"])

    def test_defect_confidence_is_copied_verbatim(self):
        result = assemble_evidence_v2(defect_candidate(confidence=0.5))
        self.assertEqual(result["confidence_limitation"]["confidence"], 0.5)
        self.assertNotIn(CONFIDENCE_UNAVAILABLE,
                         result["confidence_limitation"]["limitations"])

    def test_limitations_are_deduplicated_and_sorted(self):
        result = assemble_evidence_v2(
            race_candidate(missing_evidence=["may_parallel", "may_parallel", "alpha"]))
        limitations = result["confidence_limitation"]["limitations"]
        self.assertEqual(limitations, sorted(set(limitations)))
        self.assertEqual(limitations.count("may_parallel"), 1)


class TraceEvidenceBlockTests(unittest.TestCase):
    def test_trace_is_consumed_as_is(self):
        result = assemble_evidence_v2(race_candidate())
        self.assertEqual(result["trace_evidence"], race_candidate()["trace_evidence"])

    def test_missing_trace_is_empty_and_noted(self):
        result = assemble_evidence_v2(defect_candidate())
        self.assertEqual(result["trace_evidence"], [])
        self.assertIn(TRACE_EVIDENCE_UNAVAILABLE,
                      result["confidence_limitation"]["limitations"])


class UnknownNeverFalseTests(unittest.TestCase):
    def test_unknown_is_not_serialized_as_false(self):
        # same_storage is the three-state string "unknown" and stays a string;
        # boolean "false" is reserved for a provably-false producer answer.
        result = assemble_evidence_v2(
            race_candidate(), competing_access=[competing_pair(same_storage="unknown")])
        self.assertEqual(result["competing_access"][0]["same_storage"], "unknown")


class IntegrationTests(unittest.TestCase):
    RACE_FIXTURE = (
        "int g;\n"
        "void a(void) { g = 1; }\n"
        "int b(void) { return g; }\n"
    )

    LIFETIME_FIXTURE = (
        "#include <stdlib.h>\n"
        "int leak_pos(int n) {\n"
        "    char *p = malloc(64);\n"
        "    if (!p) return -1;\n"
        "    if (n > 0) { return n; }\n"
        "    free(p);\n"
        "    return 0;\n"
        "}\n"
    )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "graph.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_profile_candidate_carries_evidence_v2_with_competing_pair(self):
        write(self.root, "src/a.c", self.RACE_FIXTURE)
        full_index(self.root, self.database)
        result = run_concurrency_profile(self.root, database=self.database, scope="src")
        self.assertEqual(result["status"], "complete", result.get("reason"))
        candidates = result["scenarios"]["shared_state_race"]["candidates"]
        self.assertTrue(candidates, "expected a shared_state_race candidate")
        candidate = candidates[0]
        evidence_v2 = candidate["evidence_v2"]
        self.assertEqual(set(evidence_v2), FIVE_BLOCKS)
        # SUB-05 wiring reaches the candidate: the read/write pair is visible.
        self.assertEqual(len(evidence_v2["competing_access"]), 1)
        pair = evidence_v2["competing_access"][0]
        self.assertEqual(pair["same_storage"], "true")
        # confidence for a concurrency candidate stays null (no source).
        self.assertIsNone(evidence_v2["confidence_limitation"]["confidence"])

    def test_defect_candidate_carries_evidence_v2_and_keeps_mechanism_independent(self):
        write(self.root, "src/t.c", self.LIFETIME_FIXTURE)
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("defect",), database=self.database)
        env = query_defect_candidates(self.root, defect_type="all", database=self.database)
        self.assertEqual(env["status"], "available", env.get("reason"))
        candidate = env["candidates"][0]
        self.assertEqual(set(candidate["evidence_v2"]), FIVE_BLOCKS)
        # Defect candidates have no lockset/trace producers:
        self.assertEqual(candidate["evidence_v2"]["lock"]["access_locksets"], [])
        limitations = candidate["evidence_v2"]["confidence_limitation"]["limitations"]
        self.assertIn(LOCK_FACTS_UNAVAILABLE, limitations)
        self.assertIn(TRACE_EVIDENCE_UNAVAILABLE, limitations)
        # mechanism_facts stays an independent block without evidence_v2 (§5.8).
        entry = env["mechanism_facts"]["resource_lifetime"][0]
        self.assertNotIn("evidence_v2", entry)

    def test_profile_output_is_deterministic(self):
        write(self.root, "src/a.c", self.RACE_FIXTURE)
        full_index(self.root, self.database)
        first = run_concurrency_profile(self.root, database=self.database, scope="src")
        second = run_concurrency_profile(self.root, database=self.database, scope="src")
        first_scenarios = first["scenarios"]["shared_state_race"]["candidates"]
        second_scenarios = second["scenarios"]["shared_state_race"]["candidates"]
        self.assertEqual(
            json.dumps([candidate["evidence_v2"] for candidate in first_scenarios],
                       sort_keys=True, default=str),
            json.dumps([candidate["evidence_v2"] for candidate in second_scenarios],
                       sort_keys=True, default=str),
        )


if __name__ == "__main__":
    unittest.main()
