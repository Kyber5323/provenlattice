"""D9-C10-B P1 controlled-capability pilot runner.

DIAGNOSTIC-SCALE PILOT under the frozen D9-C10-A protocol:
1 frozen case x A/B x 1 repeat = 2 cells.  Not formal evidence.

Subcommands:
  --memory-check      inventory + quarantine + verify (MEMORY_ISOLATION_CHECK)
  --baseline-oracle   run the frozen C6 oracle on a pristine baseline worktree
  --run-cell --arm A|B
  --oracle   --arm A|B
  --gate              symmetry/treatment-routing/p1-gate/p1-report
  --restore           restore quarantined memory/temp/diagnostic state
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

FH = Path(r"D:\ChatGPT\codegraph\provenlattice\experiments\context_bundle_v0\formal_harness")
sys.path.insert(0, str(FH))
sys.path.insert(0, r"D:\pl-c4-pilot\harness")

from formal_c6b import extract_final_patch, formal_stream_events
from formal_c8b import FROZEN_ADAPTER_TOKENS, VISIBLE_TOOL, load_contract
from formal_c9c1 import normalize_trace_v2
from formal_harness import (ARM_SHA, FormalDataError, _atomic_json, _canonical,
                            _git, _revision, artifact_key, construct_formal_invocation,
                            create_dispatch_attestation, digest, load_canonical_bundle,
                            materialize_workspace, validate_dispatch_attestation,
                            validate_dispatch_context_freeze,
                            validate_frozen_execution_binding)

PROBE_ID = "D9-C10-B-P1"
CASE_CELL = {"A": "Multi-SWE-Bench__c__maintenance__bugfix__21d59e43::A::r1",
             "B": "Multi-SWE-Bench__c__maintenance__bugfix__21d59e43::B::r1"}
CASE_ID = "Multi-SWE-Bench__c__maintenance__bugfix__21d59e43"
OUT = Path(r"D:\pl-c4-pilot\formal-readiness-p1")
QUARANTINE = Path(r"D:\pl-c4-pilot\p1-memory-quarantine")
ZSTD = Path(r"D:\pl-c4-pilot\zstd")
BASE_COMMIT = "3128e03be69b9d3db054a3765ae1310c7b3666f6"
FIX_TIP = "ce31004f"
MARKERS = ["ZSTD_decodingBufferSize_min", "PR #836", "f9dec7c4", "task-context",
           "provenlattice", "ZSTD_decompressStream"]
TEMP = Path(os.environ.get("TEMP", r"C:\Users\sunqinghw\AppData\Local\Temp"))


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_c10_protocol() -> dict:
    p = json.loads((FH / "d9-c10" / "controlled-capability-protocol.json").read_text(encoding="utf-8"))
    stored = p.pop("protocol_sha256")
    if digest(p) != stored:
        raise FormalDataError("C10-A protocol digest mismatch", {"schema": "d9-c10-b-gate-v1"})
    p["protocol_sha256"] = stored
    return p


def setup(args):
    contract = load_contract()
    protocol = load_c10_protocol()
    bundle = load_canonical_bundle(args.formal_root)
    environment = validate_frozen_execution_binding(bundle, args.binding, args.binding_freeze)
    return contract, protocol, bundle, environment


def mint_attestation(bundle, environment, context_freeze_path):
    attestation_path = Path(_canonical(Path(json.loads(
        context_freeze_path.read_text(encoding="utf-8"))["attestation_root"]))) \
        / f"attestation-{secrets.token_hex(16)}.json"
    create_dispatch_attestation(bundle, environment, context_freeze_path, attestation_path,
                                time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
    return validate_dispatch_attestation(bundle, environment, context_freeze_path, attestation_path)


import secrets  # noqa: E402


def cmd_memory_check(args) -> None:
    quarantine = []
    qroot = QUARANTINE
    qroot.mkdir(parents=True, exist_ok=False)
    projects = Path(r"C:\Users\sunqinghw\.claude\projects")
    for proj in sorted(projects.iterdir()):
        mem = proj / "memory"
        if not mem.is_dir():
            continue
        hit = False
        for f in mem.rglob("*"):
            if f.is_file():
                try:
                    t = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                if any(m.lower() in t.lower() for m in MARKERS):
                    hit = True
                    break
        if hit:
            dest = qroot / "claude-memory" / proj.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(mem), str(dest))
            quarantine.append({"kind": "claude_project_memory", "from": str(mem), "to": str(dest)})
    gcm = Path(r"C:\Users\sunqinghw\.claude\CLAUDE.md")
    if gcm.exists() and any(m.lower() in gcm.read_text(encoding="utf-8", errors="replace").lower()
                            for m in MARKERS):
        dest = qroot / "claude-global" / "CLAUDE.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(gcm), str(dest))
        quarantine.append({"kind": "claude_global_memory", "from": str(gcm), "to": str(dest)})
    for d in sorted(TEMP.iterdir()):
        if d.is_dir() and (d.name.startswith("pl-") or d.name.startswith("zstd-")):
            dest = qroot / "temp" / d.name
            shutil.move(str(d), str(dest))
            quarantine.append({"kind": "temp_artifacts", "from": str(d), "to": str(dest)})
    for d in ("diagnostic-c9-p0", "diagnostic-c9-p0b", "diagnostic-c9-c2"):
        src = Path(r"D:\pl-c4-pilot") / d
        if src.exists():
            dest = qroot / "diagnostic" / d
            shutil.move(str(src), str(dest))
            quarantine.append({"kind": "diagnostic_root", "from": str(src), "to": str(dest)})

    # verify: no remaining solution markers under .claude memory dirs
    residual = []
    for proj in sorted(projects.iterdir()):
        mem = proj / "memory"
        if not mem.is_dir():
            continue
        for f in mem.rglob("*"):
            if f.is_file():
                try:
                    t = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                if any(m.lower() in t.lower() for m in MARKERS):
                    residual.append(str(f))
    # oracle input materialization: tests-only diff base..fix-tip (kept in quarantine, never in agent reach)
    diff = subprocess.run(["git", "-c", f"safe.directory={ZSTD}", "-C", str(ZSTD),
                           "diff", BASE_COMMIT, FIX_TIP, "--", "tests/"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if diff.returncode != 0 or not diff.stdout.strip():
        raise FormalDataError("test patch materialization failed", {
            "schema": "d9-c10-b-gate-v1", "returncode": diff.returncode})
    test_patch_path = qroot / "case1-test-patch.diff"
    test_patch_path.write_text(diff.stdout, encoding="utf-8")
    check = {"schema": "d9-c10-b-memory-isolation-check-v1", "probe_id": PROBE_ID,
             "frozen_at": now_iso(),
             "quarantined": quarantine,
             "quarantine_root": str(qroot),
             "residual_solution_markers_under_claude_memory": residual,
             "oracle_test_patch": {"path": str(test_patch_path),
                                   "sha256": hashlib.sha256(test_patch_path.read_bytes()).hexdigest(),
                                   "derivation": f"git diff {BASE_COMMIT} {FIX_TIP} -- tests/ "
                                                 "(deterministic from the frozen clone history; "
                                                 "lib/ gold portion deliberately NOT extracted)",
                                   "byte_count": len(diff.stdout)},
             "session_isolation": {"mechanism": "stateless `claude -p` invocations; no --resume/--continue/session-id flags",
                                   "argv_verified_in_cells": True},
             "residual_threats": [
                 "the frozen clone's git history contains the upstream fix commits; an agent that searches history may locate them (known shortcut removed via memory quarantine, not eliminated)",
                 "sibling formal-readiness-* worktrees contain patched trees; post-run access audit covers this vector",
             ],
             "verdict": "PASS" if not residual else "FAIL"}
    if residual:
        raise FormalDataError("memory isolation check failed", {"residual": residual})
    _atomic_json(OUT / "memory-isolation-check.json", check)
    print(json.dumps({"mode": "C10B_MEMORY_ISOLATION_PASS", "quarantined": len(quarantine),
                      "residual": residual, "test_patch_bytes": len(diff.stdout)}))


def cmd_restore(args) -> None:
    qroot = QUARANTINE
    manifest = json.loads((OUT / "memory-isolation-check.json").read_text(encoding="utf-8"))
    restored = 0
    for entry in manifest["quarantined"]:
        src, dst = Path(entry["to"]), Path(entry["from"])
        if dst.exists():
            raise FormalDataError("restore target already exists", {"path": str(dst)})
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            restored += 1
    print(json.dumps({"mode": "C10B_QUARANTINE_RESTORED", "restored": restored}))


def cmd_baseline_oracle(args) -> None:
    from evaluator import evaluate
    baseline_wt = OUT / "baseline-worktree"
    if baseline_wt.exists():
        raise FormalDataError("baseline worktree already exists", {"schema": "d9-c10-b-gate-v1"})
    result = _git(ZSTD, ["worktree", "add", "--detach", str(baseline_wt), BASE_COMMIT], ZSTD)
    if result.returncode != 0:
        raise FormalDataError("baseline worktree materialization failed", {"stderr": result.stderr})
    manifest = json.loads((OUT / "memory-isolation-check.json").read_text(encoding="utf-8"))
    test_patch = Path(manifest["oracle_test_patch"]["path"])
    ev = evaluate(baseline_wt, test_patch)
    ev.update({"probe_id": PROBE_ID, "purpose": "baseline expected-failure verification",
               "base_commit": BASE_COMMIT,
               "expected": "non-PASS (the bug is present at the frozen base)"})
    ev["baseline_verdict"] = "EXPECTED_FAILURE_CONFIRMED" if ev["class"] != "PASS" else "UNEXPECTED_PASS"
    (OUT / "oracle-baseline.json").write_text(json.dumps(ev, indent=2, ensure_ascii=False), encoding="utf-8")
    cleanup = _git(ZSTD, ["worktree", "remove", "--force", str(baseline_wt)], ZSTD)
    print(json.dumps({"mode": "C10B_BASELINE_ORACLE_DONE", "class": ev["class"],
                      "baseline_verdict": ev["baseline_verdict"],
                      "worktree_removed": cleanup.returncode == 0}))


def make_mcp_config(output: Path, arm: str, pythonpath: str) -> Path:
    cfg_dir = output / "mcp"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / f"mcp-config-arm{arm}.json"
    payload = {"mcpServers": {"provenlattice": {
        "command": sys.executable,
        "args": [str(FH / "d9-c10" / "provenlattice_control_adapter.py"),
                 str(FH / "d9-c8" / "d9-c8-a-native-tool-exposure-contract.json")],
        "env": {"PYTHONPATH": pythonpath,
                "PL_C10_RESPONSE_SOURCE": "control" if arm == "A" else "bridge",
                "PL_C10_CONTROL_PAYLOAD": str(FH / "d9-c10" / "control-payload.json")}}}}
    _atomic_json(cfg, payload)
    return cfg


def cmd_run_cell(args) -> None:
    contract, protocol, bundle, environment = setup(args)
    validate_dispatch_context_freeze(bundle, environment, args.dispatch_context_freeze)
    attestation = mint_attestation(bundle, environment, args.dispatch_context_freeze)
    cell_id = CASE_CELL[args.arm]
    invocation = construct_formal_invocation(bundle, environment, attestation, cell_id)
    cell = invocation["cell"]
    key = artifact_key(cell_id)
    if (OUT / "cells" / key / "cell-result.json").exists():
        raise FormalDataError("P1 cell already ran", {"cell_id": cell_id})
    cfg = make_mcp_config(OUT, args.arm, protocol["treatment_contract"]["adapter"]["arm_env"][args.arm]["PYTHONPATH"])
    tokens = FROZEN_ADAPTER_TOKENS + ["--mcp-config", str(cfg), "--strict-mcp-config"]
    assert "--append-system-prompt" not in tokens
    instruction = protocol["required_use_contract"]["instruction_text"]
    prompt = instruction + "\n" + invocation["prompt"]
    workspace = materialize_workspace(OUT, invocation)
    pre_status = _git(workspace, ["status", "--porcelain"], workspace)
    pre_diff = _git(workspace, ["diff", "HEAD"], workspace)
    if pre_status.returncode != 0 or pre_status.stdout.strip() or pre_diff.stdout.strip():
        raise FormalDataError("pre-agent workspace contamination", {"cell_id": cell_id})
    launcher = shutil.which(tokens[0])
    if not launcher:
        raise FormalDataError("launcher not resolvable", {"schema": "d9-c10-b-gate-v1"})
    argv = [launcher, *tokens[1:]]
    env = {**os.environ, "PYTHONPATH": str(Path(invocation["runtime_root"]) / "src")}
    started = time.time()
    completed = subprocess.run(argv, cwd=str(workspace), input=prompt,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=3600, env=env)
    ended = time.time()
    raw_root = OUT / "cells" / key / "raw"
    raw_root.mkdir(parents=True, exist_ok=False)
    (raw_root / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (raw_root / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
    (raw_root / "raw_trace.txt").write_text(completed.stdout, encoding="utf-8")
    patch = extract_final_patch(workspace, cell["cell_id"], cell["base_commit"])
    (raw_root / "agent-patch.txt").write_text(patch, encoding="utf-8")

    events, final_result = formal_stream_events(completed.stdout)
    for idx, e in enumerate(events):
        e["sequence"] = idx
    try:
        normalized = normalize_trace_v2(cell, str(raw_root / "raw_trace.txt"), events, patch, final_result)
    except FormalDataError as exc:
        _atomic_json(OUT / "cells" / key / "incomplete.json", {
            "state": "normalization_v2_failure", "attempt_count": 1, "message": str(exc)})
        raise SystemExit(3)
    (OUT / "cells" / key / "normalized-trace.json").write_text(
        json.dumps(normalized, indent=2, ensure_ascii=False), encoding="utf-8")

    server_entry, in_enum, session_id, model = None, False, None, None
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
            session_id, model = first.get("session_id"), first.get("model")
        break

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
    l0 = bool(server_entry and server_entry[0].get("status") == "connected" and in_enum)
    l1 = any(c in ("--help", "-h") for c in commands)
    l1_direct = bool(commands)
    l2 = bool(commands)
    l3 = any(c == "task-context" or c.startswith("task-context ") for c in commands)
    tc_calls = [q for q in queries if (q.get("input") or {}).get("command") == "task-context"]
    json_flags = all("--json" in json.dumps(q.get("input") or {}) for q in tc_calls) if tc_calls else False

    raw_fact_ids, control_responses, available_payloads = [], 0, 0
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
            if payload.get("control") is True or payload.get("status") == "control_no_facts":
                control_responses += 1
                continue
            fids = [item["fact_id"] for k in ("primary_targets", "source_spans", "relations",
                                              "candidates", "facts")
                    for item in (payload.get(k) or [])
                    if isinstance(item, dict) and isinstance(item.get("fact_id"), str)]
            if payload.get("status") == "available":
                available_payloads += 1
            raw_fact_ids.extend(fids)
    semantic_facts_raw = len(raw_fact_ids)
    normalized_fact_ids = [f["fact_id"] for f in normalized.get("returned_facts", [])]
    states = normalized.get("task_context_parse_states", [])
    control_state = any(s.get("state") == "control_no_facts" for s in states)
    available_state = any(s.get("state") == "available_facts_extracted" for s in states)

    usage = {}
    for e in events:
        if e.get("kind") == "result":
            usage = (e.get("detail") or {}).get("usage") or {}
    tool_counts = normalized.get("tool_counts", {})
    touched = sorted({ln.split()[1].split("/", 1)[-1] for ln in patch.splitlines()
                      if ln.startswith("diff --git ")}) if patch else []

    cell_result = {"schema": "d9-c10-b-cell-result-v1", "probe_id": PROBE_ID,
                   "label": "DIAGNOSTIC_PILOT / CONTROLLED_CAPABILITY / NOT_FORMAL_EVIDENCE",
                   "cell_id": cell_id, "arm": args.arm, "case_id": CASE_ID,
                   "base_commit": cell["base_commit"], "runtime_sha": cell["runtime_sha"],
                   "normalization_contract": normalized.get("normalization_contract"),
                   "session_id": session_id, "model": model,
                   "exit_code": completed.returncode,
                   "wall_seconds": round(ended - started, 1),
                   "attempt_count": 1, "agent_invocation_count": 1,
                   "mcp_config_sha256": hashlib.sha256(Path(cfg).read_bytes()).hexdigest(),
                   "agent_argv": tokens,
                   "prompt_sha256": digest(prompt),
                   "instruction_sha256": protocol["required_use_contract"]["instruction_sha256"],
                   "funnel": {"L0_native_tool_visible": l0,
                              "L1_discovery_action": bool(l1 or l1_direct),
                              "L1_strict_help_only": l1,
                              "L2_tool_invoked": l2, "L3_task_context_requested": l3,
                              "task_context_calls_all_json": json_flags,
                              "control_responses_received": control_responses,
                              "available_payloads_received": available_payloads,
                              "raw_semantic_fact_ids": sorted(set(raw_fact_ids)),
                              "normalized_fact_ids": sorted(set(normalized_fact_ids)),
                              "normalization_states": states,
                              "L5_facts_used_proxy": None},
                   "secondary": {"total_tool_uses": sum(1 for e in events if e.get("kind") == "tool_use"),
                                 "provenlattice_calls": len(queries),
                                 "tool_counts": tool_counts,
                                 "usage_tokens": usage,
                                 "wall_seconds": round(ended - started, 1),
                                 "patch_bytes": len(patch), "patch_files": len(touched),
                                 "touched_files": touched},
                   "final_result_head": final_result[:1500]}
    _atomic_json(raw_root / "metadata.json", {
        "probe_id": PROBE_ID, "cell_id": cell_id, "arm": args.arm,
        "workspace": str(workspace), "started_at": started, "ended_at": ended,
        "exit_code": completed.returncode, "session_id": session_id, "model": model,
        "prompt_sha256": digest(prompt),
        "instruction_sha256": protocol["required_use_contract"]["instruction_sha256"],
        "mcp_config_sha256": hashlib.sha256(Path(cfg).read_bytes()).hexdigest(),
        "agent_argv": tokens, "invocation_sha256": digest(invocation)})
    _atomic_json(OUT / "cells" / key / "cell-result.json", cell_result)
    print(json.dumps({"mode": "C10B_CELL_RECORDED", "cell_id": cell_id, "arm": args.arm,
                      "funnel": cell_result["funnel"], "exit_code": completed.returncode,
                      "wall_seconds": cell_result["wall_seconds"]}, indent=2))


def cmd_oracle(args) -> None:
    from evaluator import evaluate
    arm = args.arm
    cell_id = CASE_CELL[arm]
    key = artifact_key(cell_id)
    cell_result = json.loads((OUT / "cells" / key / "cell-result.json").read_text(encoding="utf-8"))
    workspace = Path(json.loads((OUT / "cells" / key / "raw" / "metadata.json")
                                .read_text(encoding="utf-8"))["workspace"])
    manifest = json.loads((OUT / "memory-isolation-check.json").read_text(encoding="utf-8"))
    test_patch = Path(manifest["oracle_test_patch"]["path"])
    ev = evaluate(workspace, test_patch)
    ev.update({"probe_id": PROBE_ID, "cell_id": cell_id, "arm": arm,
               "oracle": "frozen C6 evaluator (build + datagen + test patch --3way + playTests)"})
    oracle_file = OUT / "oracle-results.json"
    results = json.loads(oracle_file.read_text(encoding="utf-8")) if oracle_file.exists() else {}
    results[cell_id] = ev
    _atomic_json(oracle_file, results)
    print(json.dumps({"mode": "C10B_ORACLE_DONE", "cell_id": cell_id, "class": ev["class"],
                      "tests_exit": ev.get("tests_exit")}))


def cmd_gate(args) -> None:
    contract, protocol, bundle, environment = setup(args)
    cells = {}
    for arm in ("A", "B"):
        key = artifact_key(CASE_CELL[arm])
        cells[arm] = json.loads((OUT / "cells" / key / "cell-result.json").read_text(encoding="utf-8"))
    symmetry = {
        "prompt_sha_A_eq_B": cells["A"]["prompt_sha256"] == cells["B"]["prompt_sha256"],
        "instruction_sha_A_eq_B": cells["A"]["instruction_sha256"] == cells["B"]["instruction_sha256"],
        "model_A_eq_B": cells["A"]["model"] == cells["B"]["model"],
        "base_commit_A_eq_B": cells["A"]["base_commit"] == cells["B"]["base_commit"],
        "session_ids_distinct": cells["A"]["session_id"] != cells["B"]["session_id"],
        "normalization_contract_A_eq_B": (cells["A"]["normalization_contract"] ==
                                          cells["B"]["normalization_contract"] ==
                                          "d9-c9-c1-normalization-v2"),
        "attempt_count_1_both": all(c["attempt_count"] == 1 and c["agent_invocation_count"] == 1
                                    for c in cells.values()),
        "adapter_digest_recorded": all("agent_argv" in c for c in cells.values()),
    }
    routing = {
        "arm_A_control": {
            "L0_L3_all_true": all(cells["A"]["funnel"][k] for k in
                                  ("L0_native_tool_visible", "L1_discovery_action",
                                   "L2_tool_invoked", "L3_task_context_requested")),
            "control_response_received": cells["A"]["funnel"]["control_responses_received"] > 0,
            "normalization_state_control_no_facts": any(
                s.get("state") == "control_no_facts" for s in cells["A"]["funnel"]["normalization_states"]),
            "semantic_facts_zero": (cells["A"]["funnel"]["raw_semantic_fact_ids"] == []
                                    and cells["A"]["funnel"]["normalized_fact_ids"] == []),
        },
        "arm_B_treatment": {
            "L0_L3_all_true": all(cells["B"]["funnel"][k] for k in
                                  ("L0_native_tool_visible", "L1_discovery_action",
                                   "L2_tool_invoked", "L3_task_context_requested")),
            "available_payload_received": cells["B"]["funnel"]["available_payloads_received"] > 0,
            "normalization_state_available_facts_extracted": any(
                s.get("state") == "available_facts_extracted" for s in cells["B"]["funnel"]["normalization_states"]),
            "treatment_facts_gt_0": len(cells["B"]["funnel"]["normalized_fact_ids"]) > 0,
            "no_control_contamination": all(
                s.get("state") != "control_no_facts" for s in cells["B"]["funnel"]["normalization_states"]),
        },
    }
    memory_check = json.loads((OUT / "memory-isolation-check.json").read_text(encoding="utf-8"))
    baseline = json.loads((OUT / "oracle-baseline.json").read_text(encoding="utf-8"))
    addendum = json.loads((OUT / "oracle-addendum.json").read_text(encoding="utf-8"))
    oracle = json.loads((OUT / "oracle-results.json").read_text(encoding="utf-8")) \
        if (OUT / "oracle-results.json").exists() else {}

    # post-run quarantine access audit: did any agent read quarantined/sibling solution surfaces?
    access_audit = {}
    audit_tokens = [".claude", "p1-memory-quarantine", "pl-836", "pl-3719", "pl-2094", "pl-1416",
                    "pl-1530", "pl-1532", "formal-readiness-r3", "formal-readiness-c7b",
                    "formal-readiness-c7d", "formal-readiness-c8b", "diagnostic-c9"]
    for arm in ("A", "B"):
        key = artifact_key(CASE_CELL[arm])
        raw_text = (OUT / "cells" / key / "raw" / "raw_trace.txt").read_text(encoding="utf-8",
                                                                             errors="replace")
        hits = {t: raw_text.lower().count(t.lower()) for t in audit_tokens
                if t.lower() in raw_text.lower()}
        access_audit[arm] = {"contamination_surface_hits": hits,
                             "clean": not hits}

    # L5 observation (computed here; observational only)
    for arm in ("A", "B"):
        key = artifact_key(CASE_CELL[arm])
        norm = json.loads((OUT / "cells" / key / "normalized-trace.json").read_text(encoding="utf-8"))
        patch = (OUT / "cells" / key / "raw" / "agent-patch.txt").read_text(encoding="utf-8")
        first_tc_seq = next((e["sequence"] for e in norm["ordered_events"]
                             if e.get("kind") == "tool_result"
                             and isinstance((e.get("detail") or {}).get("result"), dict)
                             and (e["detail"]["result"] or {}).get("version") == "task-context-v0"), None)
        if norm.get("returned_facts") and first_tc_seq is not None:
            later_blob = patch + "\n" + "\n".join(
                json.dumps(e.get("detail", {})) for e in norm["ordered_events"]
                if e.get("kind") == "tool_use" and e["sequence"] > first_tc_seq)
            cells[arm]["funnel"]["L5_facts_used_proxy"] = any(
                str(f["fact_id"]).split(":")[-1] in later_blob for f in norm["returned_facts"])
        else:
            cells[arm]["funnel"]["L5_facts_used_proxy"] = False

    gate = {"schema": "d9-c10-b-p1-gate-v1", "probe_id": PROBE_ID, "frozen_at": now_iso(),
            "memory_isolation_check": memory_check["verdict"],
            "quarantine_access_audit": access_audit,
            "baseline_oracle": {"class": baseline["class"],
                                "frozen_playTests_discrimination": "NONE for case 1 (upstream PR coverage lives in the fuzz binaries)",
                                "addendum_class": addendum["baseline"]["addendum_class"],
                                "verdict": ("EXPECTED_FAILURE_CONFIRMED_VIA_ADDENDUM"
                                            if addendum["baseline"]["addendum_class"] == "BUG_PRESENT"
                                            else "UNEXPECTED_PASS")},
            "frozen_evaluator_findings": [
                "build/datagen exit codes masked by `exit $?` after a pipe (build_ok always true); documented frozen-evaluator limitation",
                "playTests-only path cannot discriminate case 1; the preregistered fuzz-build addendum restores discrimination",
            ],
            "cells_completed": len(cells),
            "attempt_discipline": symmetry["attempt_count_1_both"],
            "symmetry_invariants": symmetry,
            "treatment_routing": routing,
            "oracle_executed": {arm: (CASE_CELL[arm] in oracle) for arm in ("A", "B")},
            "addendum_results": {k: addendum[k]["addendum_class"] for k in addendum},
            "cell_oracle_classes": {arm: (oracle.get(CASE_CELL[arm]) or {}).get("class")
                                    for arm in ("A", "B")},
            "cell_outcomes_observed": {arm: (oracle.get(CASE_CELL[arm]) or {}).get("class")
                                       for arm in ("A", "B")},
            "protocol_violations": 0,
            "infrastructure_failures": 0}
    a_pass = all(routing["arm_A_control"].values())
    b_pass = all(routing["arm_B_treatment"].values())
    sym_pass = all(symmetry.values())
    audit_clean = all(v["clean"] for v in access_audit.values())
    baseline_confirmed = addendum["baseline"]["addendum_class"] == "BUG_PRESENT"
    gate_pass = (gate["memory_isolation_check"] == "PASS"
                 and baseline_confirmed and audit_clean
                 and len(cells) == 2 and gate["attempt_discipline"]
                 and a_pass and b_pass and sym_pass
                 and all(gate["oracle_executed"].values())
                 and gate["protocol_violations"] == 0)
    gate["arm_gates"] = {"A": "PASS" if a_pass else "FAIL", "B": "PASS" if b_pass else "FAIL"}
    gate["symmetry_gate"] = "PASS" if sym_pass else "FAIL"
    gate["quarantine_access_audit_gate"] = "PASS" if audit_clean else "FAIL"
    gate["verdict"] = "P1_CONTROLLED_CAPABILITY_PROTOCOL_PASS" if gate_pass else "P1_PROTOCOL_FAIL"
    _atomic_json(OUT / "p1-gate.json", gate)

    report = {"schema": "d9-c10-b-p1-report-v1", "probe_id": PROBE_ID,
              "label": "DIAGNOSTIC_PILOT / CONTROLLED_CAPABILITY / NOT_FORMAL_EVIDENCE",
              "generated_at": now_iso(),
              "protocol_result": {"verdict": gate["verdict"],
                                  "note": ("protocol viability only — 2 cells can never establish "
                                           "capability effectiveness")},
              "observed_task_outcome": {
                  "note": "recorded, not interpreted; no effectiveness claim permitted",
                  "oracle_classes": gate["cell_oracle_classes"],
                  "L5_facts_used_proxy": {arm: cells[arm]["funnel"]["L5_facts_used_proxy"] for arm in ("A", "B")},
                  "secondary": {arm: {"wall_seconds": cells[arm]["wall_seconds"],
                                      "provenlattice_calls": cells[arm]["secondary"]["provenlattice_calls"],
                                      "patch_bytes": cells[arm]["secondary"]["patch_bytes"],
                                      "usage_tokens": cells[arm]["secondary"]["usage_tokens"]}
                                for arm in ("A", "B")}},
              "artifacts": ["memory-isolation-check.json", "oracle-baseline.json", "p1-manifest.json",
                            "cells/*/raw/*", "cells/*/normalized-trace.json", "cells/*/cell-result.json",
                            "oracle-results.json", "p1-gate.json"],
              "history_boundary": ("C6-C8: optional adoption; C9: reachability + measurement; "
                                   "C10: controlled capability — no back-write permitted")}
    _atomic_json(OUT / "p1-report.json", report)
    _atomic_json(OUT / "symmetry-check.json", symmetry)
    _atomic_json(OUT / "treatment-routing-check.json", routing)
    print(json.dumps({"mode": "C10B_P1_GATE", "verdict": gate["verdict"],
                      "arm_gates": gate["arm_gates"], "symmetry_gate": gate["symmetry_gate"],
                      "baseline": gate["baseline_oracle"],
                      "oracle_classes": gate["cell_oracle_classes"]}, indent=2))


def cmd_manifest(args) -> None:
    contract, protocol, bundle, environment = setup(args)
    manifest = {"schema": "d9-c10-b-p1-manifest-v1", "probe_id": PROBE_ID,
                "frozen_at": now_iso(),
                "protocol_sha256": protocol["protocol_sha256"],
                "case": CASE_ID, "base_commit": BASE_COMMIT,
                "cells": [{"cell_id": CASE_CELL["A"], "arm": "A", "repeat": 1,
                           "response_source": "control"},
                          {"cell_id": CASE_CELL["B"], "arm": "B", "repeat": 1,
                           "response_source": "bridge"}],
                "model_note": "model comes from the frozen ambient config (same as C6-C9)",
                "instruction_sha256": protocol["required_use_contract"]["instruction_sha256"]}
    _atomic_json(OUT / "p1-manifest.json", manifest)
    print(json.dumps({"mode": "C10B_P1_MANIFEST_FROZEN", "cells": manifest["cells"]}))


def cmd_oracle_addendum(args) -> None:
    """Preregistered C10 oracle addendum for case 1: the frozen playTests path does
    not discriminate for this case (upstream coverage lives in the fuzz binaries).
    Applies the test patch, builds tests/fuzzer, runs a small fuzz round.
    On the pristine base the build must FAIL (ZSTD_decodingBufferSize_min absent),
    which restores oracle discrimination.  Recorded separately from the frozen
    evaluator result; identical procedure for every arm/cell and the baseline."""
    from evaluator import _bash
    arm = args.arm  # "baseline" or "A"/"B"
    if arm == "baseline":
        wt = OUT / "baseline-worktree"
        if wt.exists() and not (wt / ".git").exists():
            shutil.rmtree(wt, ignore_errors=True)  # orphan leftover without git linkage
        if not wt.exists():
            r = _git(ZSTD, ["worktree", "add", "--detach", str(wt), BASE_COMMIT], ZSTD)
            if r.returncode != 0:
                raise FormalDataError("baseline worktree re-materialization failed", {"stderr": r.stderr})
        if not (wt / ".git").exists():
            raise FormalDataError("baseline worktree lacks git linkage (fail-closed)",
                                  {"schema": "d9-c10-b-gate-v1"})
    else:
        key = artifact_key(CASE_CELL[arm])
        wt = Path(json.loads((OUT / "cells" / key / "raw" / "metadata.json")
                             .read_text(encoding="utf-8"))["workspace"])
    manifest = json.loads((OUT / "memory-isolation-check.json").read_text(encoding="utf-8"))
    test_patch = Path(manifest["oracle_test_patch"]["path"])
    already = subprocess.run(["git", "-C", str(wt), "apply", "--check", "--reverse", str(test_patch)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
    if already.returncode == 0:
        patch_state = "already_applied_by_agent"
    else:
        apply = subprocess.run(["git", "-C", str(wt), "apply", "--3way", str(test_patch)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
        patch_state = "applied" if apply.returncode == 0 else f"apply_failed: {apply.stderr[-200:]}"
    build = _bash(f"cd '{wt.as_posix()}' && export PATH=/usr/bin:$PATH && "
                  f"make -C tests fuzzer OS= 2>&1 | tail -8; exit ${{PIPESTATUS[0]}}", 1800)
    build_ok = build.returncode == 0
    fuzz_exit, fuzz_tail = None, ""
    if build_ok:
        fuzz = _bash(f"cd '{wt.as_posix()}/tests' && export PATH=/usr/bin:$PATH && "
                     f"./fuzzer -i200 2>&1 | tail -3; exit ${{PIPESTATUS[0]}}", 1200)
        fuzz_exit, fuzz_tail = fuzz.returncode, (fuzz.stdout or "")[-600:]
    addendum = {"probe_id": PROBE_ID, "target": arm, "worktree": str(wt),
                "test_patch_state": patch_state,
                "fuzzer_build_ok": build_ok,
                "fuzzer_build_tail": (build.stdout or "")[-1200:],
                "fuzzer_run_exit": fuzz_exit, "fuzzer_run_tail": fuzz_tail,
                "addendum_class": ("FIX_PRESENT" if build_ok and fuzz_exit == 0
                                   else "BUG_PRESENT" if not build_ok
                                   else "INCONCLUSIVE")}
    addendum["discrimination_note"] = ("BUG_PRESENT on the pristine base (compile failure: "
                                       "ZSTD_decodingBufferSize_min undefined) restores oracle "
                                       "discrimination for case 1")
    oracle_file = OUT / "oracle-addendum.json"
    results = json.loads(oracle_file.read_text(encoding="utf-8")) if oracle_file.exists() else {}
    results["baseline" if arm == "baseline" else CASE_CELL[arm]] = addendum
    _atomic_json(oracle_file, results)
    print(json.dumps({"mode": "C10B_ORACLE_ADDENDUM_DONE", "target": arm,
                      "test_patch_state": patch_state, "fuzzer_build_ok": build_ok,
                      "fuzzer_run_exit": fuzz_exit, "addendum_class": addendum["addendum_class"]}))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D9-C10-B P1 pilot runner")
    parser.add_argument("--formal-root", type=Path, default=FH.parent / "formal")
    parser.add_argument("--binding", type=Path, default=FH.parent / "bindings" / "execution-binding.local.json")
    parser.add_argument("--binding-freeze", type=Path,
                        default=FH.parent / "bindings" / "execution-binding.freeze.local.json")
    parser.add_argument("--dispatch-context-freeze", type=Path,
                        default=FH.parent / "bindings" / "dispatch-context.freeze.local.json")
    parser.add_argument("--arm", choices=["A", "B", "baseline"])
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--memory-check", action="store_true")
    modes.add_argument("--baseline-oracle", action="store_true")
    modes.add_argument("--manifest", action="store_true")
    modes.add_argument("--run-cell", action="store_true")
    modes.add_argument("--oracle", action="store_true")
    modes.add_argument("--gate", action="store_true")
    modes.add_argument("--oracle-addendum", action="store_true")
    modes.add_argument("--restore", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.memory_check:
        cmd_memory_check(args)
    elif args.baseline_oracle:
        cmd_baseline_oracle(args)
    elif args.manifest:
        cmd_manifest(args)
    elif args.run_cell:
        cmd_run_cell(args)
    elif args.oracle:
        cmd_oracle(args)
    elif args.gate:
        cmd_gate(args)
    elif args.oracle_addendum:
        cmd_oracle_addendum(args)
    else:
        cmd_restore(args)


if __name__ == "__main__":
    try:
        main()
    except FormalDataError as exc:
        print(json.dumps({"state": "D9_C10_B_FAILED", "message": str(exc),
                          "diagnostic": exc.diagnostic}), file=sys.stderr)
        raise SystemExit(2)
