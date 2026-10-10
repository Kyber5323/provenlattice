from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

FH = Path(__file__).parent
D10 = FH / "d9-c10"
PROTOCOL_SHA = "066d06160eb4a04d90ba6e45329ad167fae4c1289bc259b6622bdf52729a0359"

sys.path.insert(0, str(FH))
from formal_c9c1 import extract_task_context_facts_v2


def mcp_conversation(adapter: Path, env: dict, calls: list[dict]) -> dict:
    payload_lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2024-11-05", "capabilities": {}}}),
                     json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                     json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})]
    for i, call in enumerate(calls, start=3):
        payload_lines.append(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                         "params": {"arguments": call}}))
    proc = subprocess.run([sys.executable, str(adapter), str(FH / "d9-c8" /
                           "d9-c8-a-native-tool-exposure-contract.json")],
                          input="\n".join(payload_lines) + "\n", capture_output=True,
                          text=True, encoding="utf-8", timeout=600, env=env)
    assert proc.returncode == 0, proc.stderr[-500:]
    replies = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("id") is not None:
            replies[msg["id"]] = msg
    return replies


def adapter_env(source: str, pythonpath: str) -> dict:
    return {**__import__("os").environ,
            "PL_C10_RESPONSE_SOURCE": source,
            "PL_C10_CONTROL_PAYLOAD": str(D10 / "control-payload.json"),
            "PL_MCP_CONTRACT": str(FH / "d9-c8" / "d9-c8-a-native-tool-exposure-contract.json"),
            "PYTHONPATH": pythonpath}


class C10ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.protocol = json.loads((D10 / "controlled-capability-protocol.json").read_text(encoding="utf-8"))
        stored = self.protocol.pop("protocol_sha256")
        from formal_harness import digest
        self.assertEqual(PROTOCOL_SHA, stored)
        self.assertEqual(digest(self.protocol), PROTOCOL_SHA)
        self.protocol["protocol_sha256"] = stored

    def test_t1_control_payload_schema_and_digest(self):
        doc = json.loads((D10 / "control-payload.json").read_text(encoding="utf-8"))
        payload = doc["payload"]
        self.assertEqual("task-context-v0", payload["version"])
        self.assertEqual("control_no_facts", payload["status"])
        self.assertIs(True, payload["control"])
        for forbidden in ("primary_targets", "source_spans", "relations", "candidates", "fact_id"):
            self.assertNotIn(forbidden, payload)
        serialized = json.dumps(payload, sort_keys=True)
        self.assertEqual(self.protocol["treatment_contract"]["control_payload"]["serialized_sha256_anchor_null"],
                         hashlib.sha256(serialized.encode("utf-8")).hexdigest())

    def test_t2_normalizer_control_state(self):
        payload = json.loads(json.dumps(
            json.loads((D10 / "control-payload.json").read_text(encoding="utf-8"))["payload"]))
        payload["anchor"] = "ZSTD_decompressStream"
        facts, states = extract_task_context_facts_v2(
            [{"sequence": 5, "kind": "tool_result", "detail": {"result": payload}}])
        self.assertEqual([], facts)
        self.assertEqual("control_no_facts", states[0]["state"])
        self.assertEqual(0, states[0]["facts_extracted"])

    def test_t3_treatment_path_unchanged(self):
        fixture = json.loads((FH / "fixtures" / "task_context_v0_available_p0b.json")
                             .read_text(encoding="utf-8"))
        facts, states = extract_task_context_facts_v2(
            [{"sequence": 9, "kind": "tool_result", "detail": {"result": fixture}}])
        self.assertEqual(2, len(facts))
        self.assertEqual("available_facts_extracted", states[0]["state"])

    def test_t4_adapter_dual_mode(self):
        adapter = D10 / "provenlattice_control_adapter.py"
        arm_a = self.protocol["treatment_contract"]["adapter"]["arm_env"]["A"]
        arm_b = self.protocol["treatment_contract"]["adapter"]["arm_env"]["B"]
        control_env = adapter_env("control", arm_a["PYTHONPATH"])
        bridge_env = adapter_env("bridge", arm_b["PYTHONPATH"])
        tool_entry = json.loads((FH / "d9-c8" / "d9-c8-a-native-tool-exposure-contract.json")
                                .read_text(encoding="utf-8"))["mechanism"]["tool_entry_frozen_bytes"]

        replies = mcp_conversation(adapter, control_env, [
            {"command": "task-context", "args": ["--json", "ZSTD_decompressStream"]},
            {"command": "--help"}])
        tools = replies[2]["result"]["tools"]
        self.assertEqual(1, len(tools))
        self.assertEqual(tool_entry, tools[0])  # tool schema identical to frozen C8-A entry
        control_result = replies[3]["result"]
        payload = json.loads(control_result["content"][0]["text"])
        self.assertTrue(payload["control"])
        self.assertEqual("control_no_facts", payload["status"])
        self.assertEqual("ZSTD_decompressStream", payload["anchor"])
        self.assertNotIn("primary_targets", payload)
        self.assertIs(False, control_result["isError"])
        help_result = replies[4]["result"]  # non-task-context command is bridged in control mode
        self.assertIn("usage: provenlattice", help_result["content"][0]["text"])
        self.assertIs(False, help_result["isError"])

        bridge_replies = mcp_conversation(adapter, bridge_env, [
            {"command": "task-context", "args": ["--help"]}])
        bridge_result = bridge_replies[3]["result"]
        self.assertIn("task-context", bridge_result["content"][0]["text"])
        self.assertIs(False, bridge_result["isError"])

    def test_t5_assignment_sealed(self):
        assignment = json.loads((D10 / "assignment-sealed.json").read_text(encoding="utf-8"))
        stored = assignment.pop("assignment_sha256")
        from formal_harness import digest
        self.assertEqual(stored, digest(assignment))
        self.assertEqual({"A": "control", "B": "bridge"}, assignment["arm_assignment"])
        self.assertTrue(assignment["reviewer_visibility"].startswith("NEVER"))

    def test_t6_instruction_byte_identity(self):
        contract = self.protocol["required_use_contract"]
        self.assertEqual(contract["instruction_sha256"],
                         hashlib.sha256(contract["instruction_text"].encode("utf-8")).hexdigest())
        self.assertTrue(contract["byte_identical_arms"])
        for forbidden in ("arm A", "arm B", "treatment", "better", "control payload"):
            self.assertNotIn(forbidden, contract["instruction_text"].lower())

    def test_t7_no_case_specific_branch(self):
        case_tokens = ["21d59e43", "2b5e5e23", "5adf8efc", "616889c2", "9a7be8ba", "a47dfbbf"]
        for name, blob in (("adapter", (D10 / "provenlattice_control_adapter.py").read_text(encoding="utf-8")),
                           ("control_payload", json.dumps(self.protocol["treatment_contract"]["control_payload"]["payload"])),
                           ("instruction", self.protocol["required_use_contract"]["instruction_text"])):
            for token in case_tokens:
                self.assertNotIn(token, blob.lower(), f"{name} contains case token {token}")


if __name__ == "__main__":
    unittest.main()
