"""Synthetic-only D9-C5-C qualification tests; no ContextBench case is executed."""
from __future__ import annotations
import tempfile
import unittest
from copy import deepcopy
import json
import subprocess
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))
import formal_harness as h


def config():
    return {"case_prompts": {c: "synthetic prompt " + c for c in h.FROZEN_CASES},
            "model": "synthetic-model", "agent_runtime": "synthetic-agent", "timeout_s": 3600,
            "inventories": {"A": sorted(h.FROZEN_INVENTORY_A), "B": sorted(h.FROZEN_INVENTORY_B)},
            "environment_fingerprint": "synthetic-env", "evaluator_sha256": "evaluator", "metric_pipeline_sha256": "metrics", "review_protocol_sha256": "protocol"}


def review(role, verdict, review_id="blind-x", fact_id="symbol:x"):
    reason = "EXPLICIT_REASONING_USE" if verdict == "YES" else "NO_OBSERVABLE_USE"
    return {"blinded_review_id": review_id, "fact_id": fact_id, "reviewer_role": role, "verdict": verdict,
            "reasons": [reason], "schema_version": h.PROTOCOL_VERSION, "submitted_at": "t", "evidence_note": "synthetic", "trace_references": [1]}


class FormalHarnessTests(unittest.TestCase):
    def setUp(self): self.matrix = h.generate_matrix(config())
    def synthetic_repo(self, root):
        repo=root/"source"; repo.mkdir()
        for args in (["init"], ["config","user.email","synthetic@example.test"], ["config","user.name","Synthetic"]):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        (repo/"base.txt").write_text("base", encoding="utf-8")
        subprocess.run(["git","add","base.txt"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git","commit","-m","base"], cwd=repo, check=True, capture_output=True)
        commit=subprocess.run(["git","rev-parse","HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
        return repo, commit
    def invocation(self, cell, repo, commit):
        bound=dict(cell, base_commit=commit)
        return {"dispatcher_version":h.DISPATCHER_VERSION, "cell":bound, "runtime_root":"synthetic", "target_repository":str(repo), "agent_argv":["fake"], "prompt":"synthetic", "environment":{}, "timeout_s":1}
    def test_matrix_has_stable_36_cells(self):
        self.assertEqual(36, len(self.matrix)); self.assertEqual(36, len({c["cell_id"] for c in self.matrix}))
    def test_reject_matrix_errors(self):
        for bad in (self.matrix[:-1], self.matrix + [self.matrix[0]]):
            with self.assertRaises(h.FormalDataError): h.validate_matrix(bad)
        bad = [dict(c) for c in self.matrix]; bad[0]["runtime_sha"] = "wrong"
        with self.assertRaises(h.FormalDataError): h.validate_matrix(bad)
        bad = [dict(c) for c in self.matrix]; bad[0]["base_commit"] = "wrong"
        with self.assertRaises(h.FormalDataError): h.validate_matrix(bad)
    def test_reject_non_frozen_and_inventory_drift(self):
        bad = config(); bad["case_prompts"] = dict(bad["case_prompts"], other="x")
        with self.assertRaises(h.FormalDataError): h.generate_matrix(bad)
        bad = config(); bad["inventories"]["B"].remove("status")
        with self.assertRaises(h.FormalDataError): h.generate_matrix(bad)
    def test_manifest_and_normalized_trace_preserve_raw_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = h.write_manifest(Path(tmp) / "formal", self.matrix, config()); self.assertTrue(path.exists())
        cell = next(c for c in self.matrix if c["arm"] == "B")
        trace = h.normalize_trace(cell, "raw/trace.jsonl", [{"kind":"tool_use","tool":"Grep","detail":{}}, {"kind":"tool_result","tool":"Bash","detail":{"result":{"version":"task-context-v0","facts":[{"fact_id":"symbol:x"}]}}}], "patch", "answer")
        self.assertEqual("raw/trace.jsonl", trace["raw_trace_pointer"]); self.assertEqual("symbol:x", trace["returned_facts"][0]["fact_id"])
        with self.assertRaises(h.FormalDataError): h.normalize_trace(cell, "PILOT_ONLY/x", [], "", "")
    def test_runner_execution_preflight_has_zero_agent_invocations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); cfg=root/"config.json"; cfg.write_text(json.dumps(config()), encoding="utf-8")
            runner=Path(__file__).parent/"formal_runner.py"
            result=subprocess.run([sys.executable, str(runner), "--config", str(cfg), "--output", str(root/"out")], capture_output=True, text=True)
            self.assertEqual(0, result.returncode); self.assertIn('"planned_cells": 36', result.stdout)
            self.assertFalse((root/"out"/"cells").exists())
    def test_packet_is_blind(self):
        cell = next(c for c in self.matrix if c["arm"] == "B")
        trace = h.normalize_trace(cell, "raw/x", [], "patch", "answer")
        packet = h.make_packet(cell, trace, {"fact_id":"symbol:x", "payload":{}}, "synthetic")
        self.assertNotIn("runtime_sha", packet); self.assertNotIn("arm", packet)
        leak = dict(packet); leak["runtime_sha"] = h.ARM_SHA["B"]
        with self.assertRaises(h.FormalDataError): h.assert_packet_blind(leak)
    def test_review_agreement_disagreement_and_adjudication(self):
        self.assertEqual("YES", h.finalize_review([review("R1","YES"), review("R2","YES")])["final_verdict"])
        self.assertEqual("NO", h.finalize_review([review("R1","NO"), review("R2","NO")])["final_verdict"])
        pair = [review("R1","YES"), review("R2","NO")]
        self.assertEqual("ADJUDICATION_REQUIRED", h.finalize_review(pair)["state"])
        adjudication = {"blinded_review_id":"blind-x", "fact_id":"symbol:x", "reviewer_role":"R3", "r1_verdict":"YES", "r2_verdict":"NO", "final_verdict":"YES", "reasons":[h.R3_REASON], "schema_version":h.PROTOCOL_VERSION, "submitted_at":"t", "evidence_note":"synthetic", "trace_references":[1]}
        self.assertEqual("YES", h.finalize_review(pair, adjudication)["final_verdict"])
        with self.assertRaises(h.FormalDataError): h.finalize_review([review("R1","YES"), review("R2","YES")], adjudication)
    def test_review_validation_and_incomplete(self):
        self.assertEqual("INCOMPLETE", h.finalize_review([review("R1","YES")])["state"])
        self.assertEqual("INCOMPLETE", h.finalize_review([review("R1","YES"), review("R1","YES")])["state"])
        bad = review("R1","YES"); bad["reasons"] = ["NEW_CATEGORY"]
        with self.assertRaises(h.FormalDataError): h.validate_review(bad)
    def test_gate_and_machine_human_independence(self):
        records=[]
        for c in self.matrix:
            records.append({"cell_id":c["cell_id"], "provenance_sha256":c["provenance_sha256"], "dispatcher_version":h.DISPATCHER_VERSION, "attempt_count":1, "trace_parse_pass":True, "evaluator":{}, "returned_fact_ids":[], "final_reviews":[], "machine_metrics":{"retrieval_replacement_events":1, "redundant_read_events":2, "grep_calls":3, "read_calls":4}})
        self.assertEqual("PASS", h.completeness_gate(self.matrix, records)["state"])
        result=h.aggregate(self.matrix, records); self.assertEqual(0, result["human"]["consumed_fact_count"]); self.assertEqual(36, result["machine"]["retrieval_replacement_events"])
        bad=records[:-1]; self.assertEqual("FORMAL_DATA_INCOMPLETE", h.completeness_gate(self.matrix,bad)["state"])
        with self.assertRaises(h.FormalDataError): h.aggregate(self.matrix,bad)
        pilot=deepcopy(records); pilot[0]["marking"]="PILOT_ONLY"; self.assertEqual("FORMAL_DATA_INCOMPLETE", h.completeness_gate(self.matrix,pilot)["state"])
        bypass=deepcopy(records); bypass[0].pop("dispatcher_version"); self.assertEqual("FORMAL_DATA_INCOMPLETE", h.completeness_gate(self.matrix,bypass)["state"])
        uncovered=deepcopy(records); next(r for r in uncovered if "::B::" in r["cell_id"])["returned_fact_ids"]=["symbol:x"]; self.assertEqual("FORMAL_DATA_INCOMPLETE", h.completeness_gate(self.matrix,uncovered)["state"])
    def test_execution_preflight_rejects_conflict_and_schema_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"formal"; h.write_manifest(root, self.matrix, config())
            self.assertEqual(0, h.preflight_execution(self.matrix, root)["agent_invocation_count"])
            changed=config(); changed["case_prompts"][next(iter(h.FROZEN_CASES))]="different"
            with self.assertRaises(h.FormalDataError): h.preflight_execution(h.generate_matrix(changed), root)
            existing=root/"cells"/h.artifact_key(self.matrix[0]["cell_id"]); existing.mkdir(parents=True); (existing/"cell-record.json").write_text("{}")
            with self.assertRaises(h.FormalDataError): h.preflight_execution(self.matrix, root, self.matrix[0]["cell_id"])
        bad=[dict(c) for c in self.matrix]; bad[0]["review_protocol_version"]="drift"
        with self.assertRaises(h.FormalDataError): h.preflight_execution(bad, Path("does-not-exist"))
    def test_dispatcher_lifecycle_is_atomic_and_never_retries(self):
        cell=next(c for c in self.matrix if c["arm"] == "B")
        calls=[]
        def fake(_):
            calls.append(1); return {"stdout":"out", "stderr":"err", "raw_trace":"trace", "events":[], "exit_code":0, "completion_state":"completed"}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); repo,commit=self.synthetic_repo(root); invocation=self.invocation(cell,repo,commit)
            result=h.dispatch_cell(root, invocation, fake); self.assertEqual("completed", result["state"]); self.assertEqual(1,len(calls))
            self.assertTrue((Path(tmp)/"cells"/h.artifact_key(cell["cell_id"])/"raw"/"stdout.txt").exists())
            with self.assertRaises(h.FormalDataError): h.dispatch_cell(root, invocation, fake)
    def test_dispatcher_preserves_raw_on_failure(self):
        cell=next(c for c in self.matrix if c["arm"] == "A")
        cases=[
            {"stdout":"o", "stderr":"e", "raw_trace":"t", "events":None},
            {"stdout":"o", "stderr":"e", "raw_trace":"t", "events":[{"kind":"tool_result", "detail":{"result":{"version":"task-context-v0", "facts":[{}]}}}]},
            {"stdout":"o", "stderr":"e", "raw_trace":None, "events":[]},
        ]
        for outcome in cases:
            with tempfile.TemporaryDirectory() as tmp:
                artifact=Path(tmp); repo,commit=self.synthetic_repo(artifact); invocation=self.invocation(cell,repo,commit)
                result=h.dispatch_cell(artifact, invocation, lambda _: outcome); self.assertEqual("incomplete",result["state"])
                root=artifact/"cells"/h.artifact_key(cell["cell_id"]); self.assertTrue((root/"incomplete.json").exists()); self.assertFalse((root/"cell-record.json").exists())
    def test_dispatcher_records_task_failure_but_stops_infrastructure_failure(self):
        cell=next(c for c in self.matrix if c["arm"] == "A")
        task_failure={"stdout":"o", "stderr":"e", "raw_trace":"t", "events":[], "exit_code":9, "completion_state":"completed"}
        timeout={"stdout":"o", "stderr":"e", "raw_trace":"t", "events":[], "completion_state":"timeout", "infrastructure_failure":True}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); repo,commit=self.synthetic_repo(root); invocation=self.invocation(cell,repo,commit)
            self.assertEqual("completed", h.dispatch_cell(root, invocation, lambda _: task_failure)["state"])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); repo,commit=self.synthetic_repo(root); invocation=self.invocation(cell,repo,commit)
            self.assertEqual("incomplete", h.dispatch_cell(root, invocation, lambda _: timeout)["state"])
        bad=deepcopy(invocation); bad["cell"]=dict(cell, runtime_sha="wrong")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(h.FormalDataError): h.dispatch_cell(Path(tmp), bad, lambda _: task_failure)
    def test_sequential_cells_get_clean_distinct_workspaces(self):
        cells=[self.matrix[0], self.matrix[1], self.matrix[3]]
        seen=[]
        def fake(inv):
            workspace=Path(inv["target_repository"]); self.assertEqual("", subprocess.run(["git","status","--porcelain"],cwd=workspace,capture_output=True,text=True).stdout)
            (workspace/"mutation.txt").write_text(inv["cell"]["cell_id"], encoding="utf-8"); seen.append(workspace)
            return {"stdout":"o","stderr":"e","raw_trace":"t","events":[],"exit_code":0,"completion_state":"completed"}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); repo,commit=self.synthetic_repo(root)
            for cell in cells: self.assertEqual("completed", h.dispatch_cell(root,self.invocation(cell,repo,commit),fake)["state"])
            self.assertEqual(3,len(set(seen))); self.assertTrue(all((p/"mutation.txt").exists() for p in seen))
    def test_workspace_dirty_or_prebound_rejects_before_executor(self):
        cell=self.matrix[0]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); repo,commit=self.synthetic_repo(root); invocation=self.invocation(cell,repo,commit)
            (repo/"dirty.txt").write_text("dirty")
            with self.assertRaises(h.FormalDataError): h.dispatch_cell(root,invocation,lambda _: self.fail("executor called"))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); repo,commit=self.synthetic_repo(root); invocation=self.invocation(cell,repo,commit)
            (root/"worktrees"/h.artifact_key(cell["cell_id"])).mkdir(parents=True)
            with self.assertRaises(h.FormalDataError): h.dispatch_cell(root,invocation,lambda _: self.fail("executor called"))
    def test_production_adapter_checks_revision_before_agent(self):
        cell=next(c for c in self.matrix if c["arm"] == "A")
        invocation={"cell":cell, "runtime_root":"missing", "target_repository":"missing", "agent_argv":["must-not-run"], "prompt":"synthetic", "environment":{}, "timeout_s":1}
        result=h.subprocess_executor(invocation)
        self.assertTrue(result["infrastructure_failure"]); self.assertEqual("preflight_failed", result["completion_state"])

if __name__ == "__main__": unittest.main()
