from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from formal_c9c1 import (NORMALIZATION_CONTRACT, extract_task_context_facts_v2,
                         normalize_trace_v2)
from formal_harness import FormalDataError, normalize_trace

FIXTURES = Path(__file__).parent / "fixtures"
P0B_RAW_FACT_IDS = {
    "symbol:symbol:09ddaa84ead587b52566294154609ba42b2196d063d4c0649305f0261eda8fd7",
    "span:3128e03be69b9d3db054a3765ae1310c7b3666f6:lib/zstd.h:18978:19084",
}


def load_fixture(status: str) -> dict:
    return json.loads((FIXTURES / f"task_context_v0_{status}_p0b.json").read_text(encoding="utf-8"))


def result_event(sequence: int, payload) -> dict:
    return {"sequence": sequence, "kind": "tool_result",
            "detail": {"result": payload}}


class NormalizeV2Tests(unittest.TestCase):
    def test_a_legacy_top_level_facts_unchanged(self):
        payload = {"version": "task-context-v0", "status": "available",
                   "facts": [{"fact_id": "symbol:x"}, {"fact_id": "span:y"}]}
        facts, states = extract_task_context_facts_v2([result_event(0, payload)])
        self.assertEqual(["symbol:x", "span:y"], [f["fact_id"] for f in facts])
        self.assertEqual({"legacy_fact"}, {f["fact_kind"] for f in facts})
        # identical to the frozen v1 behavior on the same input
        cell = {"cell_id": "synthetic"}
        v1 = normalize_trace(cell, "raw/x", [
            {"kind": "tool_result", "detail": {"result": payload}}], "", "")
        self.assertEqual([f["fact_id"] for f in v1["returned_facts"]],
                         [f["fact_id"] for f in facts])

    def test_b_real_p0b_available_payload_ingested(self):
        payload = load_fixture("available")
        facts, states = extract_task_context_facts_v2([result_event(7, payload)])
        self.assertEqual(2, len(facts))
        self.assertEqual(P0B_RAW_FACT_IDS, {f["fact_id"] for f in facts})
        kinds = {f["source_section"]: f["fact_kind"] for f in facts}
        self.assertEqual({"primary_targets": "primary_target", "source_spans": "source_span"}, kinds)
        for f in facts:
            self.assertTrue(f["evidence_ids"], "real payload items carry evidence_ids")
            self.assertEqual("available", f["payload_provenance"]["status"])
            self.assertEqual("ZSTD_decompressStream", f["payload_provenance"]["anchor"])
        self.assertEqual("available_facts_extracted", states[0]["state"])
        self.assertEqual(2, states[0]["facts_extracted"])

    def test_c_not_built_yields_zero_facts(self):
        facts, states = extract_task_context_facts_v2([result_event(0, load_fixture("not_built"))])
        self.assertEqual([], facts)
        self.assertEqual("status_not_built_no_facts", states[0]["state"])
        self.assertEqual(0, states[0]["facts_extracted"])

    def test_d_not_found_yields_zero_facts(self):
        facts, states = extract_task_context_facts_v2([result_event(0, load_fixture("not_found"))])
        self.assertEqual([], facts)
        self.assertEqual("status_not_found_no_facts", states[0]["state"])

    def test_e_unparsed_and_unknown_version_fail_closed(self):
        repr_text = "{'version': 'task-context-v0', 'status': 'available'}"  # malformed JSON (repr)
        facts, states = extract_task_context_facts_v2([result_event(0, repr_text)])
        self.assertEqual([], facts)
        self.assertEqual("unparsed_text_result_not_ingested", states[0]["state"])
        self.assertTrue(states[0]["is_task_context_shaped_text"])
        facts2, states2 = extract_task_context_facts_v2(
            [result_event(0, {"version": "task-context-v9", "status": "available"})])
        self.assertEqual([], facts2)
        self.assertEqual("unknown_version_not_ingested", states2[0]["state"])
        facts3, states3 = extract_task_context_facts_v2(
            [result_event(0, {"version": "task-context-v0", "status": "degraded"})])
        self.assertEqual([], facts3)
        self.assertEqual("status_unavailable_no_facts", states3[0]["state"])

    def test_f_duplicate_fact_id_deduplicated_stably(self):
        fid = "symbol:dup"
        payload = {"version": "task-context-v0", "status": "available",
                   "primary_targets": [{"fact_id": fid, "kind": "symbol"}],
                   "source_spans": [{"fact_id": fid, "file": "a.c"}],
                   "relations": [{"fact_id": fid, "kind": "edge"}],
                   "candidates": []}
        facts, _ = extract_task_context_facts_v2([result_event(3, payload)])
        self.assertEqual(1, len(facts))
        self.assertEqual("primary_targets", facts[0]["source_section"])
        self.assertEqual(["primary_targets", "source_spans", "relations"], facts[0]["sections"])
        again, _ = extract_task_context_facts_v2([result_event(3, payload)])
        self.assertEqual(facts, again)

    def test_g_exact_p0b_fact_ids_in_normalized_output(self):
        payload = load_fixture("available")
        cell = {"cell_id": "Multi-SWE-Bench__c__maintenance__bugfix__21d59e43::B::r1"}
        repr_text = "{'version': 'task-context-v0', 'status': 'not_built'}"  # malformed-JSON path
        trace = normalize_trace_v2(cell, "raw/raw_trace.txt",
                                   [result_event(9, payload), result_event(10, repr_text)],
                                   "patch", "answer")
        self.assertEqual(NORMALIZATION_CONTRACT, trace["normalization_contract"])
        self.assertEqual(2, trace["returned_fact_count"])
        self.assertEqual(P0B_RAW_FACT_IDS, {f["fact_id"] for f in trace["returned_facts"]})
        for fact in trace["returned_facts"]:
            self.assertIn(fact["fact_id"], {f["fact_id"] for f in trace["returned_facts"]})
            self.assertIn("returned_sequence", fact)
        # consumer compatibility: record-style fact_id projection works
        self.assertEqual(2, len([f["fact_id"] for f in trace["returned_facts"]]))
        # explicit parse states for observability (one available, one unparsed)
        self.assertEqual({"available_facts_extracted", "unparsed_text_result_not_ingested"},
                         {s["state"] for s in trace["task_context_parse_states"]})

    def test_legacy_strict_identity_check_preserved(self):
        payload = {"version": "task-context-v0", "status": "available",
                   "facts": [{"kind": "no_id"}]}
        with self.assertRaises(FormalDataError):
            extract_task_context_facts_v2([result_event(0, payload)])

    def test_non_task_context_events_ignored(self):
        facts, states = extract_task_context_facts_v2([
            {"sequence": 0, "kind": "tool_use", "tool": "Bash", "detail": {"command": "ls"}},
            result_event(1, {"some": "other payload"}),
        ])
        self.assertEqual([], facts)
        self.assertEqual([], states)


if __name__ == "__main__":
    unittest.main()
