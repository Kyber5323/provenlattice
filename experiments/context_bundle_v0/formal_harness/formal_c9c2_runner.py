"""D9-C9-C2 future-experiment runner with normalization v2 wired in.

The normalization path is EXCLUSIVELY ``formal_c9c1.normalize_trace_v2``
(contract ``d9-c9-c1-normalization-v2``); the frozen v1 ``normalize_trace`` is
never invoked here and no silent fallback exists — if v2 cannot load or fails,
the runner fails closed.  The contract identity is recorded in every normalized
trace and every cell record, making the v2-vs-v1 question auditable.

Modes:
  --self-test        wiring proof without any Agent run (synthetic fixture)
  --execute          one live diagnostic closure run (DIAGNOSTIC_ONLY)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from formal_c9c1 import NORMALIZATION_CONTRACT, normalize_trace_v2
from formal_harness import (ARM_SHA, FormalDataError, _atomic_json, _canonical,
                            _git, _revision, artifact_key, construct_formal_invocation,
                            create_dispatch_attestation, digest, load_canonical_bundle,
                            materialize_workspace, validate_dispatch_attestation,
                            validate_dispatch_context_freeze,
                            validate_frozen_execution_binding)

from formal_c6b import extract_final_patch, formal_stream_events
from formal_c8b import (FROZEN_ADAPTER_TOKENS, TOOL_ENTRY_SHA256, VISIBLE_TOOL,
                        load_contract, write_mcp_config)

if NORMALIZATION_CONTRACT != "d9-c9-c1-normalization-v2":
    raise FormalDataError("unexpected normalization contract identity", {
        "schema": "d9-c9-c2-gate-v1", "gate": "wiring_identity"})

PROBE_ID = "D9-C9-C2"
CASE_CELL = "Multi-SWE-Bench__c__maintenance__bugfix__21d59e43::B::r1"
FORMAL = Path(__file__).parent.parent / "formal"
BINDINGS = FORMAL.parent / "bindings"

FORCED_PREAMBLE = (
    "MANDATORY TOOL REQUIREMENTS for this session:\n"
    "1. Before you make ANY code change, you MUST call the mcp__provenlattice__query tool.\n"
    "2. Use the tool's task-context command to obtain repository-analysis facts for this task.\n"
    "3. Every task-context request MUST include the `--json` flag.\n"
    "4. Only after you have received the facts may you continue with the bugfix described below.\n")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize_wired(cell, pointer, events, patch, result):
    """The ONLY normalization entry of this runner — v2, no fallback."""
    return normalize_trace_v2(cell, pointer, events, patch, result)


def mint_attestation(bundle, environment, context_freeze_path):
    attestation_path = Path(_canonical(Path(json.loads(context_freeze_path.read_text(encoding="utf-8"))["attestation_root"]))) \
        / f"attestation-{secrets.token_hex(16)}.json"
    create_dispatch_attestation(bundle, environment, context_freeze_path, attestation_path,
                                time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
    return validate_dispatch_attestation(bundle, environment, context_freeze_path, attestation_path)


def self_test(output: Path) -> None:
    """Wiring proof: the wired path ingests a real fixture and stamps v2 identity."""
    fixture = json.loads((Path(__file__).parent / "fixtures" /
                          "task_context_v0_available_p0b.json").read_text(encoding="utf-8"))
    events = [{"sequence": 0, "kind": "tool_use", "tool": VISIBLE_TOOL,
               "detail": {"command": "task-context"}},
              {"sequence": 1, "kind": "tool_result",
               "detail": {"result": fixture}}]
    trace = normalize_wired({"cell_id": "wiring-self-test"}, "raw/self-test.txt",
                            events, "patch", "answer")
    checks = {
        "v2_contract_stamped": trace.get("normalization_contract") == NORMALIZATION_CONTRACT,
        "facts_ingested": trace.get("returned_fact_count", 0) > 0,
        "available_state_present": any(s.get("state") == "available_facts_extracted"
                                       for s in trace.get("task_context_parse_states", [])),
        "no_silent_v1": "task_context_parse_states" in trace,
    }
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        raise FormalDataError("normalization v2 wiring self-test failed", {
            "schema": "d9-c9-c2-gate-v1", "gate": "wiring_self_test", "failed": failed})
    output.mkdir(parents=True, exist_ok=True)
    artifact = {"schema": "d9-c9-c2-wiring-self-test-v1", "frozen_at": now_iso(),
                "normalization_contract": NORMALIZATION_CONTRACT, "checks": checks,
                "proof": "synthetic real fixture ingested through the wired path with v2 identity stamped"}
    _atomic_json(output / "wiring-self-test.json", artifact)
    print(json.dumps({"mode": "C9C2_WIRING_SELF_TEST_PASS",
                      "normalization_contract": NORMALIZATION_CONTRACT, "checks": checks}))


def do_execute(args) -> None:
    contract = load_contract()
    if digest(contract["mechanism"]["tool_entry_frozen_bytes"]) != TOOL_ENTRY_SHA256:
        raise FormalDataError("tool entry digest mismatch", {"schema": "d9-c9-c2-gate-v1"})
    bundle = load_canonical_bundle(args.formal_root)
    environment = validate_frozen_execution_binding(
        bundle, args.binding, args.binding_freeze)
    context_freeze = validate_dispatch_context_freeze(
        bundle, environment, args.dispatch_context_freeze)
    attestation = mint_attestation(bundle, environment, args.dispatch_context_freeze)
    invocation = construct_formal_invocation(bundle, environment, attestation, args.cell_id)
    cell = invocation["cell"]
    if cell["arm"] != "B":
        raise FormalDataError("live closure requires the Arm B runtime", {"schema": "d9-c9-c2-gate-v1"})
    if _revision(Path(invocation["runtime_root"]), Path(invocation["runtime_root"])) != ARM_SHA["B"]:
        raise FormalDataError("arm B runtime drift", {"schema": "d9-c9-c2-gate-v1"})
    probe_prompt = FORCED_PREAMBLE + "\n" + invocation["prompt"]
    cfg = write_mcp_config(args.output, "B", contract["mechanism"]["arm_env_bindings"]["B"]["PYTHONPATH"])
    tokens = FROZEN_ADAPTER_TOKENS + ["--mcp-config", str(cfg), "--strict-mcp-config"]
    assert "--append-system-prompt" not in tokens

    workspace = materialize_workspace(args.output, invocation)
    launcher = shutil.which(tokens[0])
    if not launcher:
        raise FormalDataError("agent launcher not resolvable", {"schema": "d9-c9-c2-gate-v1"})
    argv = [launcher, *tokens[1:]]
    env = {**os.environ, "PYTHONPATH": str(Path(invocation["runtime_root"]) / "src")}
    started = time.time()
    completed = subprocess.run(argv, cwd=str(workspace), input=probe_prompt,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=3600, env=env)
    ended = time.time()
    key = artifact_key(cell["cell_id"])
    raw_root = args.output / "cells" / key / "raw"
    raw_root.mkdir(parents=True, exist_ok=False)
    (raw_root / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (raw_root / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
    (raw_root / "raw_trace.txt").write_text(completed.stdout, encoding="utf-8")
    patch = extract_final_patch(workspace, cell["cell_id"], cell["base_commit"])
    (raw_root / "agent-patch.txt").write_text(patch, encoding="utf-8")
    _atomic_json(raw_root / "metadata.json", {
        "probe_id": PROBE_ID,
        "label": "DIAGNOSTIC_ONLY / NORMALIZATION_V2_LIVE_CLOSURE / NOT_FORMAL_EVIDENCE",
        "normalization_contract": NORMALIZATION_CONTRACT,
        "cell_id": cell["cell_id"], "case_id": cell["case_id"], "arm": cell["arm"],
        "base_commit": cell["base_commit"], "runtime_sha": cell["runtime_sha"],
        "workspace": str(workspace), "started_at": started, "ended_at": ended,
        "wall_seconds": round(ended - started, 1), "exit_code": completed.returncode,
        "forced_preamble_sha256": hashlib.sha256(FORCED_PREAMBLE.encode("utf-8")).hexdigest(),
        "prompt_sha256": digest(probe_prompt),
        "mcp_config_sha256": hashlib.sha256(Path(cfg).read_bytes()).hexdigest(),
        "agent_argv": tokens, "invocation_sha256": digest(invocation)})

    events, final_result = formal_stream_events(completed.stdout)
    for idx, e in enumerate(events):
        e["sequence"] = idx

    # normalization: v2 only, fail-closed, no fallback
    try:
        normalized = normalize_wired(cell, str(raw_root / "raw_trace.txt"), events, patch, final_result)
    except FormalDataError as exc:
        cell_root = args.output / "cells" / key
        _atomic_json(cell_root / "incomplete.json", {
            "state": "normalization_v2_failure", "attempt_count": 1,
            "cell_id": cell["cell_id"], "message": str(exc),
            "normalization_contract": NORMALIZATION_CONTRACT})
        print(json.dumps({"mode": "C9C2_NORMALIZATION_V2_FAILURE", "cell_id": cell["cell_id"]}))
        raise SystemExit(3)
    (args.output / "cells" / key / "normalized-trace.json").write_text(
        json.dumps(normalized, indent=2, ensure_ascii=False), encoding="utf-8")

    # L0
    server_entry, in_enum = None, False
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            first = json.loads(line)
        except json.JSONDecodeError:
            break
        if isinstance(first, dict) and first.get("type") == "system":
            server_entry = [s for s in first.get("mcp_servers", []) if s.get("name") == "provenlattice"]
            in_enum = VISIBLE_TOOL in (first.get("tools") or [])
        break
    l0 = bool(server_entry and server_entry[0].get("status") == "connected" and in_enum)

    # funnel from wired v2 events
    queries = []
    pending = {}
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") == "assistant":
            for c in (e.get("message") or {}).get("content") or []:
                if c.get("type") == "tool_use" and c.get("name") == VISIBLE_TOOL:
                    entry = {"input": c.get("input"), "result": None, "is_error": None}
                    queries.append(entry)
                    pending[str(c.get("id"))] = entry
        if e.get("type") == "user":
            for c in (e.get("message") or {}).get("content") or []:
                if c.get("type") == "tool_result" and str(c.get("tool_use_id")) in pending:
                    entry = pending.pop(str(c.get("tool_use_id")))
                    entry["is_error"] = bool(c.get("is_error"))
                    t = c.get("content")
                    entry["result"] = t if isinstance(t, str) else json.dumps(t, ensure_ascii=False)

    commands = [((e.get("detail") or {}).get("command") or "").strip()
                for e in events if e.get("kind") == "tool_use" and e.get("tool") == VISIBLE_TOOL]
    all_tool_uses = [e for e in events if e.get("kind") == "tool_use"]
    pl_seqs = [e["sequence"] for e in all_tool_uses if e.get("tool") == VISIBLE_TOOL]
    first_index = (sum(1 for e in all_tool_uses if e["sequence"] <= min(pl_seqs)) if pl_seqs else None)
    l1 = any(c in ("--help", "-h") for c in commands)
    l1_direct = bool(commands)
    l2 = bool(commands)
    l3 = any(c == "task-context" or c.startswith("task-context ") for c in commands)
    json_flags_any = any("--json" in json.dumps(q.get("input") or {}) for q in queries
                         if (q.get("input") or {}).get("command") == "task-context")

    # L4 raw layer
    raw_payloads, raw_fact_ids = [], []
    for entry in queries:
        result_text = entry.get("result") or ""
        try:
            arr = json.loads(result_text)
            inner = "".join(x.get("text", "") for x in arr
                            if isinstance(x, dict) and x.get("type") == "text") if isinstance(arr, list) else result_text
        except Exception:
            inner = result_text
        try:
            payload = json.loads(inner)
        except Exception:
            continue
        if isinstance(payload, dict) and payload.get("version") == "task-context-v0":
            fids = [item["fact_id"] for k in ("primary_targets", "source_spans", "relations",
                                              "candidates", "facts")
                    for item in (payload.get(k) or [])
                    if isinstance(item, dict) and isinstance(item.get("fact_id"), str)]
            raw_payloads.append({"input": entry.get("input"), "status": payload.get("status"),
                                 "fact_ids": fids})
            raw_fact_ids.extend(fids)
    l4_raw = len(raw_fact_ids) > 0
    byte_locatable = {f: (f in completed.stdout) for f in sorted(set(raw_fact_ids))}

    # L4 normalized layer (v2 identity + ingestion + state)
    contract_stamped = normalized.get("normalization_contract") == NORMALIZATION_CONTRACT
    normalized_fact_ids = [f["fact_id"] for f in normalized.get("returned_facts", [])]
    available_state = any(s.get("state") == "available_facts_extracted"
                          for s in normalized.get("task_context_parse_states", []))
    l4_norm = contract_stamped and len(normalized_fact_ids) > 0 and available_state

    correspondence = {
        "raw_distinct": sorted(set(raw_fact_ids)),
        "normalized_distinct": sorted(set(normalized_fact_ids)),
        "normalized_subset_of_raw": set(normalized_fact_ids) <= set(raw_fact_ids),
        "raw_subset_of_normalized": set(raw_fact_ids) <= set(normalized_fact_ids),
        "sections_ingested": sorted({f.get("source_section")
                                     for f in normalized.get("returned_facts", [])}),
    }

    first_tc_seq = next((e["sequence"] for e in events if e.get("kind") == "tool_result"
                         and isinstance((e.get("detail") or {}).get("result"), dict)
                         and (e["detail"]["result"] or {}).get("version") == "task-context-v0"), None)
    l5 = False
    if normalized_fact_ids and first_tc_seq is not None:
        later_blob = patch + "\n" + "\n".join(
            json.dumps(e.get("detail", {})) for e in events
            if e.get("kind") == "tool_use" and e["sequence"] > first_tc_seq)
        l5 = any(str(f).split(":")[-1] in later_blob for f in normalized_fact_ids)

    if not contract_stamped:
        verdict = "NORMALIZATION_V2_WIRING_FAIL"
    elif not l0:
        verdict = "NORMALIZATION_V2_FAIL_AT_L0"
    elif not (l1 or l1_direct):
        verdict = "NORMALIZATION_V2_FAIL_AT_L1"
    elif not l2:
        verdict = "NORMALIZATION_V2_FAIL_AT_L2"
    elif not l3:
        verdict = "NORMALIZATION_V2_FAIL_AT_L3"
    elif not l4_raw:
        verdict = "NORMALIZATION_V2_RAW_FACTS_MISSING"
    elif not l4_norm:
        verdict = "NORMALIZATION_V2_NORMALIZED_FACTS_MISSING"
    elif not (correspondence["normalized_subset_of_raw"] and correspondence["raw_subset_of_normalized"]):
        verdict = "NORMALIZATION_V2_FACT_ID_MISMATCH"
    else:
        verdict = "NORMALIZATION_V2_LIVE_CLOSURE_PASS"

    funnel = {"L0_native_tool_visible": l0,
              "L1_discovery_action": bool(l1 or l1_direct), "L1_strict_help_only": l1,
              "L2_tool_invoked": l2, "L3_task_context_requested": l3,
              "L4_raw_facts_returned": l4_raw, "L4_normalized_facts_returned": l4_norm,
              "L5_facts_used_proxy": l5}
    record = {"schema": "d9-c9-c2-probe-result-v1", "probe_id": PROBE_ID,
              "label": "DIAGNOSTIC_ONLY / NORMALIZATION_V2_LIVE_CLOSURE / NOT_FORMAL_EVIDENCE",
              "generated_at": now_iso(), "cell_id": cell["cell_id"],
              "base_commit": cell["base_commit"], "runtime_sha": cell["runtime_sha"],
              "exit_code": completed.returncode, "wall_seconds": round(ended - started, 1),
              "normalization_contract": NORMALIZATION_CONTRACT,
              "v2_wiring_evidence": {"contract_stamped_in_normalized_trace": contract_stamped,
                                     "available_facts_extracted_state": available_state,
                                     "no_fallback": True},
              "funnel": funnel,
              "native_registration": {"mcp_server_entry": server_entry,
                                      "tool_in_enumeration": in_enum,
                                      "visible_tool": VISIBLE_TOOL},
              "task_context_calls_all_json": json_flags_any,
              "mcp_query_calls": queries,
              "first_provenlattice_tool_call_index": first_index,
              "first_query_command": commands[0] if commands else None,
              "raw_layer": {"payloads": raw_payloads,
                            "distinct_raw_fact_ids": sorted(set(raw_fact_ids)),
                            "fact_id_byte_locatable_in_raw_stdout": byte_locatable},
              "normalized_layer": {"facts_returned": len(normalized_fact_ids),
                                   "fact_ids": sorted(set(normalized_fact_ids)),
                                   "normalized_trace_excerpt": {
                                       "tool_counts": normalized.get("tool_counts"),
                                       "normalization_contract": normalized.get("normalization_contract"),
                                       "parse_states": normalized.get("task_context_parse_states")}},
              "raw_to_normalized_correspondence": correspondence,
              "final_result_head": final_result[:1500],
              "final_patch_bytes": len(patch),
              "verdict": verdict}
    _atomic_json(args.output / "probe-result.json", record)
    print(json.dumps({k: record[k] for k in ("funnel", "normalization_contract",
                                             "v2_wiring_evidence", "raw_layer",
                                             "normalized_layer",
                                             "raw_to_normalized_correspondence",
                                             "verdict")}, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D9-C9-C2 v2-wired runner")
    parser.add_argument("--formal-root", type=Path, default=FORMAL)
    parser.add_argument("--binding", type=Path, default=BINDINGS / "execution-binding.local.json")
    parser.add_argument("--binding-freeze", type=Path, default=BINDINGS / "execution-binding.freeze.local.json")
    parser.add_argument("--dispatch-context-freeze", type=Path,
                        default=BINDINGS / "dispatch-context.freeze.local.json")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cell-id", default=CASE_CELL)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--self-test", action="store_true")
    modes.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.self_test:
        self_test(args.output)
    else:
        do_execute(args)


if __name__ == "__main__":
    try:
        main()
    except FormalDataError as exc:
        print(json.dumps({"state": "D9_C9_C2_FAILED", "message": str(exc),
                          "diagnostic": exc.diagnostic}), file=sys.stderr)
        raise SystemExit(2)
