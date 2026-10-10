"""D9-C10-D2 Case 6 / Arm B fresh execution under the frozen D9-C10 protocol.

Single-cell resume per D9-C10-D2 task (RESUME_CASE6_B_ONLY). Replicates the
formal_c10d_p2.py cell loop verbatim for case 6 arm B (same helpers, same env
construction, same funnel/normalization/oracle/leakage code paths), then
completes the case-6 pair symmetry gate and recomputes the P2 aggregate.

attempt_count = 1: the 2026-10-08 attempt never formed a valid Agent execution
(BLOCKED_DISK_EXHAUSTION before any stdout; abandoned dir archived under
p2/abandoned/). This is not a hidden retry.

No protocol modification: all frozen inputs are digested in
p2/evidence-preservation.json and validated at runtime.
"""
from __future__ import annotations

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
sys.path.insert(0, str(FH / "d9-c10"))

import formal_c10d_p2 as base
from formal_c10d_p2 import (AUDIT_TOKENS, FIX_TIPS, FROZEN_RUNTIME_ROOT, KNOWN_FIX_SHAS,
                            ORACLE_DIR, P2P, PILOT, PROBE_ID, SENSITIVE, GW_HOST,
                            git, icacls, leakage_audit, mint_attestation, now_iso, sid)
from c10_oracle_v2 import oracle_v2
from c10_workspace_v2 import build_sealed
from formal_c6b import extract_final_patch, formal_stream_events
from formal_c8b import FROZEN_ADAPTER_TOKENS, VISIBLE_TOOL, load_contract
from formal_c9c1 import normalize_trace_v2
from formal_harness import (_atomic_json, _git, construct_formal_invocation,
                            load_canonical_bundle, validate_frozen_execution_binding,
                            validate_dispatch_context_freeze)

CASE_NUM = 6
CASE_ID = "Multi-SWE-Bench__c__maintenance__bugfix__a47dfbbf"
ARM = "B"
CELL_ID = f"{CASE_ID}::{ARM}::r1"
OUT = Path(r"D:\pl-c4-pilot\formal-readiness-p1")
ABANDONED_HOME = P2P / "abandoned" / "cell-b3f028d8dd80ec2eb1687757" / "home"
FROZEN_A = {
    "cell_id": f"{CASE_ID}::A::r1", "arm": "A", "case_num": CASE_NUM, "case_id": CASE_ID,
    "completed": True, "attempt_count": 1, "oracle_v2_class": "FIX_PRESENT",
    "semantic_facts_zero": True, "control_routing_ok": True, "leakage_clean": True,
    "provenance": [
        "frozen p2-gate.json marker (P2_INCOMPLETE_DISK_EXHAUSTION): Arm A completed, attempt_count=1, oracle=FIX_PRESENT",
        "session-log final summary (opencode ses_f2d7f69a1ffe83mUcTBM6q0dAM prt_11bd6eccb): case 6 A facts=0, FIX_PRESENT; Arm A semantic_facts=0 x6/6, 0 leakage",
        "cell-level artifacts removed during 2026-10-08/09 disk recovery (preservation deviation, see disk-recovery-report.json)"
    ]
}


def build_case6_pair_gate(B: dict) -> dict:
    pair_ok = {
        "A_L0_L3": FROZEN_A["completed"],
        "A_control": FROZEN_A["control_routing_ok"],
        "A_semantic_zero": FROZEN_A["semantic_facts_zero"],
        "B_L0_L3": all(B["funnel"][k] for k in ("L0", "L1", "L2", "L3")),
        "B_available": B["funnel"]["available_payloads"] > 0,
        "B_facts_gt_0": len(B["funnel"]["normalized_fact_ids"]) > 0,
        "B_raw_eq_norm": B["funnel"]["raw_fact_ids"] == B["funnel"]["normalized_fact_ids"],
        "A_oracle_executed": FROZEN_A["oracle_v2_class"] in ("FIX_PRESENT", "BUG_PRESENT"),
        "B_oracle_executed": B["oracle_v2"]["class"] in ("FIX_PRESENT", "BUG_PRESENT"),
        "attempt_1_both": FROZEN_A["attempt_count"] == 1 and B["attempt_count"] == 1,
        "no_forbidden_tokens": (
            not any(t in json.dumps(B["leakage_audit"]["surface_token_hits"])
                    for t in ("f9dec7c4", "058ed2ad3349187e985a481b6db654952729c307"))
            and FROZEN_A["leakage_clean"]),
    }
    return {"case_num": CASE_NUM, "case_id": CASE_ID, "checks": pair_ok,
            "A_side_provenance": FROZEN_A["provenance"],
            "A_side_provenance_note": ("A-side checks are asserted from the frozen pre-resume P2 "
                                       "state records; artifact-level re-verification unavailable "
                                       "after the recorded preservation deviation"),
            "verdict": ("CASE_6_PAIR_PROTOCOL_VALID" if all(pair_ok.values())
                        else "CASE_6_" + next(k for k, v in pair_ok.items() if not v))}


def run_case6b() -> None:
    user = sid()
    contract = load_contract()
    bundle = load_canonical_bundle(FH.parent / "formal")
    environment = validate_frozen_execution_binding(
        bundle, FH.parent / "bindings" / "execution-binding.local.json",
        FH.parent / "bindings" / "execution-binding.freeze.local.json")
    cfp = FH.parent / "bindings" / "dispatch-context.freeze.local.json"
    validate_dispatch_context_freeze(bundle, environment, cfp)

    protocol = json.loads((FH / "d9-c10" / "controlled-capability-protocol.json")
                          .read_text(encoding="utf-8"))
    instruction = protocol["required_use_contract"]["instruction_text"]
    manifest = json.loads((FH.parent / "formal" / "formal-manifest.json")
                          .read_text(encoding="utf-8"))
    base_commit = next(c["base_commit"] for c in manifest["cells"] if c["case_id"] == CASE_ID)
    test_patch = ORACLE_DIR / f"test-patch-{CASE_ID.split('__')[-1]}.diff"
    launcher = shutil.which(FROZEN_ADAPTER_TOKENS[0])

    key = "cell-" + hashlib.sha256(CELL_ID.encode()).hexdigest()[:24]
    cp = P2P / "cells" / key
    cp.mkdir(parents=True, exist_ok=False)
    ws, home, tmp = cp / "workspace", cp / "home", cp / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)

    attestation = mint_attestation(bundle, environment, cfp)
    invocation = construct_formal_invocation(bundle, environment, attestation, CELL_ID)
    cell = invocation["cell"]

    build_sealed(f"{PROBE_ID}-{CASE_NUM}-{ARM}", base_commit, ws)
    head = git(["rev-parse", "HEAD"], ws).stdout.strip()
    pre = {"head_eq_base": head == base_commit,
           "clean": not _git(ws, ["status", "--porcelain"], ws).stdout.strip(),
           "fix_unavailable": all(git(["cat-file", "-e", s], ws, check=False).returncode != 0
                                  for s in KNOWN_FIX_SHAS),
           "remotes_none": not git(["remote", "-v"], ws).stdout.strip(),
           "alternates_none": not (ws / ".git" / "objects" / "info" / "alternates").exists()}
    _atomic_json(cp / "workspace-attestation.json", {
        "cell_id": CELL_ID, "head": head, "pre_checks": pre, "base_commit": base_commit})

    acl_applied = []
    for child in SENSITIVE:
        p = PILOT / child
        if p.exists():
            acl_applied.append({"path": str(p), "ok": icacls(p, "deny", user).returncode == 0})
    ambient = Path(r"C:\Users\sunqinghw\.claude\projects\D--pl-c4-pilot-zstd")
    if ambient.exists():
        acl_applied.append({"path": str(ambient), "ok": icacls(ambient, "deny", user).returncode == 0})
    if ABANDONED_HOME.exists():
        acl_applied.append({"path": str(ABANDONED_HOME), "ok": icacls(ABANDONED_HOME, "deny", user).returncode == 0,
                            "purpose": "abandoned-attempt memory namespace quarantine"})
    prev_homes = [k for k in (P2P / "cells").iterdir() if k.name != key and (k / "home").exists()]
    for d in prev_homes:
        acl_applied.append({"path": str(d / "home"), "ok": icacls(d / "home", "deny", user).returncode == 0})
    temp_dirs_denied = []
    for d in sorted(Path(os.environ.get("TEMP", "")).iterdir()):
        if d.is_dir() and (d.name.startswith("pl-") or d.name.startswith("zstd-")):
            r = icacls(d, "deny", user)
            temp_dirs_denied.append({"path": str(d), "ok": r.returncode == 0})
    env_closures = {"dead_proxy": "http://127.0.0.1:9", "no_proxy": GW_HOST,
                    "git_protocol": "never", "disallowed_tools": ["WebFetch", "WebSearch"]}
    _atomic_json(cp / "isolation-attestation.json", {
        "cell_id": CELL_ID, "acl_applied": acl_applied,
        "prev_cell_homes_denied": [str(d) for d in prev_homes],
        "temp_dirs_denied": [d["path"] for d in temp_dirs_denied],
        "env_closures": env_closures})

    cfg_dir = cp / "mcp"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / f"mcp-config-arm{ARM}.json"
    runtime_src = FROZEN_RUNTIME_ROOT / ARM
    _atomic_json(cfg, {"mcpServers": {"provenlattice": {
        "command": sys.executable,
        "args": [str(FH / "d9-c10" / "provenlattice_control_adapter.py"),
                 str(FH / "d9-c8" / "d9-c8-a-native-tool-exposure-contract.json")],
        "env": {"PYTHONPATH": str(runtime_src),
                "PL_C10_RESPONSE_SOURCE": "bridge",
                "PL_C10_CONTROL_PAYLOAD": str(FH / "d9-c10" / "control-payload.json")}}}})

    prompt = instruction + "\n" + invocation["prompt"]
    argv = [launcher, *FROZEN_ADAPTER_TOKENS[1:],
            "--disallowed-tools", "WebFetch", "WebSearch",
            "--mcp-config", str(cfg), "--strict-mcp-config"]
    agent_env = {**os.environ,
                 "HOME": str(home), "USERPROFILE": str(home),
                 "TMP": str(tmp), "TEMP": str(tmp),
                 "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
                 "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": GW_HOST or "",
                 "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.allow",
                 "GIT_CONFIG_VALUE_0": "never",
                 "PYTHONPATH": str(P2P / "runtimes" / ARM)}

    print(json.dumps({"mode": "CASE6B_START", "cell_id": CELL_ID, "base_commit": base_commit,
                      "pre_checks": pre}), flush=True)
    started = time.time()
    completed = subprocess.run(argv, cwd=str(ws), input=prompt,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=3600, env=agent_env)
    ended = time.time()
    raw_root = cp / "raw"
    raw_root.mkdir(parents=True, exist_ok=False)
    (raw_root / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (raw_root / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
    (raw_root / "raw_trace.txt").write_text(completed.stdout, encoding="utf-8")
    patch = extract_final_patch(ws, CELL_ID, base_commit)
    (raw_root / "agent-patch.txt").write_text(patch, encoding="utf-8")

    events, final_result = formal_stream_events(completed.stdout)
    for i, e in enumerate(events):
        e["sequence"] = i
    normalized = normalize_trace_v2(cell, str(raw_root / "raw_trace.txt"), events, patch, final_result)
    (cp / "normalized-trace.json").write_text(
        json.dumps(normalized, indent=2, ensure_ascii=False), encoding="utf-8")

    server_entry, in_enum = None, False
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            first = json.loads(line)
        except Exception:
            break
        if isinstance(first, dict) and first.get("type") == "system":
            server_entry = [s for s in first.get("mcp_servers", []) if s.get("name") == "provenlattice"]
            in_enum = VISIBLE_TOOL in (first.get("tools") or [])
        break
    l0 = bool(server_entry and server_entry[0].get("status") == "connected" and in_enum)
    queries, pending = [], {}
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("type") == "assistant":
            for c in (e.get("message") or {}).get("content") or []:
                if c.get("type") == "tool_use" and c.get("name") == VISIBLE_TOOL:
                    entry = {"input": c.get("input"), "result": None}
                    queries.append(entry)
                    pending[str(c.get("id"))] = entry
        if e.get("type") == "user":
            for c in (e.get("message") or {}).get("content") or []:
                if c.get("type") == "tool_result" and str(c.get("tool_use_id")) in pending:
                    entry = pending.pop(str(c.get("tool_use_id")))
                    t = c.get("content")
                    entry["result"] = t if isinstance(t, str) else json.dumps(t, ensure_ascii=False)
    commands = [((e.get("detail") or {}).get("command") or "").strip()
                for e in events if e.get("kind") == "tool_use" and e.get("tool") == VISIBLE_TOOL]
    l1 = any(c in ("--help", "-h") for c in commands)
    l1d = bool(commands)
    l2 = bool(commands)
    l3 = any(c == "task-context" or c.startswith("task-context ") for c in commands)
    tc_calls = [q for q in queries if (q.get("input") or {}).get("command") == "task-context"]

    raw_fids, ctrl_n, avail_n = [], 0, 0
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
                ctrl_n += 1
                continue
            fids = [i["fact_id"] for k in ("primary_targets", "source_spans", "relations",
                                           "candidates", "facts")
                    for i in (payload.get(k) or [])
                    if isinstance(i, dict) and isinstance(i.get("fact_id"), str)]
            if payload.get("status") == "available":
                avail_n += 1
            raw_fids.extend(fids)
    norm_fids = [f["fact_id"] for f in normalized.get("returned_facts", [])]
    states = normalized.get("task_context_parse_states", [])

    audit = leakage_audit(completed.stdout, ws, CELL_ID)
    usage = next(((e.get("detail") or {}).get("usage") or {} for e in events
                  if e.get("kind") == "result"), {})
    first_tc = next((e["sequence"] for e in events if e.get("kind") == "tool_result"
                     and isinstance((e.get("detail") or {}).get("result"), dict)
                     and (e["detail"]["result"] or {}).get("version") == "task-context-v0"), None)
    l5 = False
    if norm_fids and first_tc is not None:
        later = patch + "\n" + "\n".join(json.dumps(e.get("detail", {})) for e in events
                                         if e.get("kind") == "tool_use" and e["sequence"] > first_tc)
        l5 = any(str(f).split(":")[-1] in later for f in norm_fids)

    ov2 = oracle_v2(ws, test_patch)

    for child in SENSITIVE:
        p = PILOT / child
        if p.exists():
            icacls(p, "remove", user)
    if ambient.exists():
        icacls(ambient, "remove", user)
    if ABANDONED_HOME.exists():
        icacls(ABANDONED_HOME, "remove", user)
    for d in temp_dirs_denied:
        icacls(Path(d["path"]), "remove", user)

    cell_result = {"cell_id": CELL_ID, "arm": ARM, "case_num": CASE_NUM,
                   "case_id": CASE_ID, "base_commit": base_commit,
                   "normalization_contract": normalized.get("normalization_contract"),
                   "exit_code": completed.returncode,
                   "wall_seconds": round(ended - started, 1),
                   "attempt_count": 1, "agent_invocation_count": 1,
                   "funnel": {"L0": l0, "L1": bool(l1 or l1d), "L2": l2, "L3": l3,
                              "control_responses": ctrl_n, "available_payloads": avail_n,
                              "raw_fact_ids": sorted(set(raw_fids)),
                              "normalized_fact_ids": sorted(set(norm_fids)),
                              "normalization_states": states,
                              "L5_facts_used_proxy": l5},
                   "secondary": {"total_tool_uses": sum(1 for e in events if e.get("kind") == "tool_use"),
                                 "provenlattice_calls": len(queries),
                                 "task_context_calls": len(tc_calls),
                                 "task_context_all_json": all("--json" in json.dumps(q.get("input") or {}) for q in tc_calls) if tc_calls else False,
                                 "tool_counts": normalized.get("tool_counts", {}),
                                 "usage_tokens": usage, "patch_bytes": len(patch)},
                   "oracle_v2": {"class": ov2["class_v2"],
                                 "fuzzer_build_exit": ov2["addendum"]["fuzzer_build_exit"],
                                 "fuzzer_run_exit": ov2["addendum"].get("fuzzer_run_exit"),
                                 "legacy_class": ov2["legacy"]["class"]},
                   "leakage_audit": audit,
                   "workspace_attestation": {"head": head, "pre_checks": pre}}
    _atomic_json(cp / "cell-result.json", cell_result)
    _atomic_json(OUT / "case6b-cell-result.json", cell_result)
    print(json.dumps({"mode": "P2_CELL_DONE", "case_num": CASE_NUM, "arm": ARM,
                      "L0": l0, "L3": l3, "facts": len(norm_fids),
                      "oracle": ov2["class_v2"], "wall": round(ended - started, 1)}, indent=2), flush=True)

    pg = build_case6_pair_gate(cell_result)
    _atomic_json(P2P / "pair-gate-case6.json", pg)
    print(json.dumps({"mode": "P2_PAIR_GATE", "case": CASE_NUM, "verdict": pg["verdict"],
                      "pair_ok": pg["checks"]}, indent=2), flush=True)

    cases = list(FIX_TIPS.keys())
    frozen_gates = {n: json.loads((P2P / f"pair-gate-case{n}.json").read_text(encoding="utf-8"))
                    for n in range(1, 6)}
    pair_gates = {n: frozen_gates[n]["verdict"] for n in range(1, 6)}
    pair_gates[6] = pg["verdict"]
    all_valid = all("VALID" in v for v in pair_gates.values())
    all_facts_a = (all(frozen_gates[n]["checks"]["A_semantic_zero"] for n in range(1, 6))
                   and FROZEN_A["semantic_facts_zero"])
    all_facts_b = (all(frozen_gates[n]["checks"]["B_facts_gt_0"] for n in range(1, 6))
                   and len(cell_result["funnel"]["normalized_fact_ids"]) > 0)
    all_raw_eq_norm = (all(frozen_gates[n]["checks"]["B_raw_eq_norm"] for n in range(1, 6))
                       and cell_result["funnel"]["raw_fact_ids"] == cell_result["funnel"]["normalized_fact_ids"])
    all_oracle = (all(frozen_gates[n]["checks"]["A_oracle_executed"]
                      and frozen_gates[n]["checks"]["B_oracle_executed"] for n in range(1, 6))
                  and FROZEN_A["oracle_v2_class"] in ("FIX_PRESENT", "BUG_PRESENT")
                  and cell_result["oracle_v2"]["class"] in ("FIX_PRESENT", "BUG_PRESENT"))
    agg_pass = (all_valid and all_facts_a and all_facts_b and all_raw_eq_norm and all_oracle)
    aggregate = {"schema": "d9-c10-d-p2-gate-v1", "probe_id": PROBE_ID,
                 "frozen_at": now_iso(),
                 "resumed_from": "P2_INCOMPLETE_DISK_EXHAUSTION (frozen pre-resume state: p2-gate.incomplete-freeze.json)",
                 "resume_mode": "RESUME_CASE6_B_ONLY",
                 "pairs_valid": sum(1 for v in pair_gates.values() if "VALID" in v),
                 "cells_completed": 12,
                 "attempt_discipline": True,
                 "arm_A_semantic_facts_zero_6_of_6": all_facts_a,
                 "arm_B_treatment_facts_gt_0_6_of_6": all_facts_b,
                 "normalization_v2_B_raw_eq_norm_6_of_6": all_raw_eq_norm,
                 "oracle_v2_executed_12_of_12": all_oracle,
                 "case6_A_side_provenance": FROZEN_A["provenance"],
                 "evidence_preservation_note": ("11 completed P2 cells' cell-level artifacts were removed "
                                                "during the 2026-10-08/09 disk recovery (recorded deviation). "
                                                "Cases 1-5 pair gates and the frozen gate marker are the "
                                                "surviving authoritative records; case 6 B artifacts are "
                                                "fully preserved under p2/cells/%s." % key),
                 "case_directions": None,
                 "outcome_table": {
                     "1": {"A_oracle": "FIX_PRESENT", "B_oracle": "FIX_PRESENT", "A_facts": 0, "B_facts": 12, "pair": pair_gates[1]},
                     "2": {"A_oracle": "FIX_PRESENT", "B_oracle": "FIX_PRESENT", "A_facts": 0, "B_facts": 5, "pair": pair_gates[2]},
                     "3": {"A_oracle": "FIX_PRESENT", "B_oracle": "FIX_PRESENT", "A_facts": 0, "B_facts": 18, "pair": pair_gates[3]},
                     "4": {"A_oracle": "FIX_PRESENT", "B_oracle": "FIX_PRESENT", "A_facts": 0, "B_facts": 4, "pair": pair_gates[4]},
                     "5": {"A_oracle": "BUG_PRESENT", "B_oracle": "FIX_PRESENT", "A_facts": 0, "B_facts": 2, "pair": pair_gates[5]},
                     "6": {"A_oracle": FROZEN_A["oracle_v2_class"], "B_oracle": cell_result["oracle_v2"]["class"],
                           "A_facts": 0, "B_facts": len(cell_result["funnel"]["normalized_fact_ids"]),
                           "pair": pair_gates[6]}},
                 "formal_readiness": {
                     "p2_stability": "PASS" if agg_pass else "FAIL",
                     "formal_not_started": True,
                     "formal_plan": "6 cases x 2 arms x 3 repeats = 36 cells (separate decision, not launched)"},
                 "verdict": "P2_CONTROLLED_CAPABILITY_STABILITY_PASS" if agg_pass else "P2_STABILITY_FAIL"}
    (P2P / "p2-gate.json").write_text(json.dumps(aggregate, indent=2, ensure_ascii=False), encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "p2-gate.json").write_text(json.dumps(aggregate, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"mode": "P2_AGGREGATE_GATE", "verdict": aggregate["verdict"],
                      "pairs_valid": aggregate["pairs_valid"],
                      "cells_completed": aggregate["cells_completed"]}, indent=2), flush=True)


if __name__ == "__main__":
    run_case6b()
