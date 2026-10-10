"""D9-C10-D P2 six-case controlled capability stability runner.

Pair-by-pair execution: case N / Arm A+r1, then Arm B+r1, then pair gate.
Fail-fast: any pair protocol invalid stops the batch.
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
from urllib.parse import urlsplit

FH = Path(r"D:\ChatGPT\codegraph\provenlattice\experiments\context_bundle_v0\formal_harness")
sys.path.insert(0, str(FH))
sys.path.insert(0, str(FH / "d9-c10"))

from c10_oracle_v2 import oracle_v2
from c10_workspace_v2 import build_sealed
from formal_c6b import extract_final_patch, formal_stream_events
from formal_c8b import FROZEN_ADAPTER_TOKENS, VISIBLE_TOOL, load_contract
from formal_c9c1 import normalize_trace_v2
FROZEN_RUNTIME_ROOT = Path(r"D:\pl-c10-sealed\runtimes")
from formal_harness import (FormalDataError, _atomic_json, _git,
                            construct_formal_invocation, create_dispatch_attestation,
                            digest, load_canonical_bundle, validate_dispatch_attestation,
                            validate_dispatch_context_freeze,
                            validate_frozen_execution_binding)

PROBE_ID = "D9-C10-D-P2"
P2P = Path(r"D:\pl-c10-sealed\p2")
PILOT = Path(r"D:\pl-c4-pilot")
GW_HOST = urlsplit(os.environ.get("ANTHROPIC_BASE_URL", "")).hostname
SENSITIVE = ["zstd", "refs", "runs", "smoke", "control-40aed09e", "control-p1",
             "formal-readiness-r3", "formal-readiness-c7b", "formal-readiness-c7d",
             "formal-readiness-c8b", "formal-readiness-p1", "p1-memory-quarantine",
             "diagnostic-c9-p0", "diagnostic-c9-p0b", "diagnostic-c9-c2",
             "harness", "formal-dispatch-attestations", "p1-arms"]
AUDIT_TOKENS = [".claude", "p1-memory-quarantine", "pl-836", "pl-3719", "pl-2094",
                "pl-1416", "pl-1530", "pl-1532", "formal-readiness-r3", "formal-readiness-c7b",
                "formal-readiness-c7d", "formal-readiness-c8b", "formal-readiness-p1",
                "diagnostic-c9", "058ed2ad", "b3f33ccf", "ce31004f", "f9dec7c4", "PR #836",
                "p1-arms"]

ORACLE_DIR = FH / "d9-c10" / "oracle-v2"
FIX_TIPS = {
    "Multi-SWE-Bench__c__maintenance__bugfix__21d59e43": "274f7ecd8484ee6323cfb8dce9df938f968599f4",
    "Multi-SWE-Bench__c__maintenance__bugfix__2b5e5e23": "eb3a7a38270dc2a1f533c1b347f8b9b56e789f8f",
    "Multi-SWE-Bench__c__maintenance__bugfix__5adf8efc": "8688c6ed92434c243d5fffed4c653403118c1482",
    "Multi-SWE-Bench__c__maintenance__bugfix__616889c2": "f15f1bfefb218a29f777493302be892a89dfe1be",
    "Multi-SWE-Bench__c__maintenance__bugfix__9a7be8ba": "be3bd70c57a23383401f8a883cdecaca1d10a4d7",
    "Multi-SWE-Bench__c__maintenance__bugfix__a47dfbbf": "0ed07f6dfe942627e7724e5b910e23ec3cd8ece8",
}
KNOWN_FIX_SHAS = ["058ed2ad3349187e985a481b6db654952729c307",
                  "b3f33ccfb3b4fcc73df82126fd5ecfb751268fc6",
                  "ce31004f205bb5dcb5ba6766b86009ffbcc90f00"]


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def sid():
    return subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
                          text=True).stdout.strip().split(",")[1].strip('"')


def icacls(path, mode, user):
    arg = f"*{user}:(OI)(CI)(F)" if mode == "deny" else f"*{user}"
    flag = "/deny" if mode == "deny" else "/remove:d"
    return subprocess.run(["icacls", str(path), flag, arg], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def git(args, cwd, check=True):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise SystemExit(f"git failed: {r.stderr[-200:]}")
    return r


def mint_attestation(bundle, environment, cfp):
    import secrets
    ap = Path(str(Path(json.loads(cfp.read_text(encoding="utf-8"))["attestation_root"]).resolve())) \
        / f"attestation-{secrets.token_hex(16)}.json"
    create_dispatch_attestation(bundle, environment, cfp, ap,
                                time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
    return validate_dispatch_attestation(bundle, environment, cfp, ap)


def leakage_audit(stdout, workspace, cell_id):
    hits = {t: stdout.lower().count(t.lower()) for t in AUDIT_TOKENS
            if t.lower() in stdout.lower()}
    shas_ok = all(git(["cat-file", "-e", s], workspace, check=False).returncode != 0
                  for s in KNOWN_FIX_SHAS)
    return {"surface_token_hits": hits, "known_fix_shas_object_unavailable": shas_ok,
            "verdict": "CLEAN" if (not hits and shas_ok) else "INDICATORS_PRESENT"}


def run_p2():
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
    cases = list(FIX_TIPS.keys())
    launcher = shutil.which(FROZEN_ADAPTER_TOKENS[0])
    results = {}
    pair_gates = {}

    for case_num, case_id in enumerate(cases, 1):
        base = json.loads((FH.parent / "formal" / "formal-manifest.json")
                          .read_text(encoding="utf-8"))
        base_commit = next(c["base_commit"] for c in base["cells"]
                          if c["case_id"] == case_id)
        test_patch = ORACLE_DIR / f"test-patch-{case_id.split('__')[-1]}.diff"
        pair_tag = f"case{case_num}"
        pair_results = {}

        for arm in ("A", "B"):
            cell_id = f"{case_id}::{arm}::r1"
            key = "cell-" + hashlib.sha256(cell_id.encode()).hexdigest()[:24]
            cp = P2P / "cells" / key
            cp.mkdir(parents=True, exist_ok=False)
            ws = cp / "workspace"
            home = cp / "home"
            tmp = cp / "tmp"
            home.mkdir(parents=True, exist_ok=True)
            tmp.mkdir(parents=True, exist_ok=True)

            attestation = mint_attestation(bundle, environment, cfp)
            invocation = construct_formal_invocation(bundle, environment, attestation, cell_id)
            cell = invocation["cell"]

            # sealed repo
            build_sealed(f"{PROBE_ID}-{case_num}-{arm}", base_commit, ws)
            head = git(["rev-parse", "HEAD"], ws).stdout.strip()
            pre = {"head_eq_base": head == base_commit,
                   "clean": not _git(ws, ["status", "--porcelain"], ws).stdout.strip(),
                   "fix_unavailable": all(git(["cat-file", "-e", s], ws, check=False).returncode != 0
                                          for s in KNOWN_FIX_SHAS),
                   "remotes_none": not git(["remote", "-v"], ws).stdout.strip(),
                   "alternates_none": not (ws / ".git" / "objects" / "info" / "alternates").exists()}
            _atomic_json(cp / "workspace-attestation.json", {
                "cell_id": cell_id, "head": head, "pre_checks": pre,
                "base_commit": base_commit})

            # isolation window
            acl_applied = []
            for child in SENSITIVE:
                p = PILOT / child
                if p.exists():
                    acl_applied.append({"path": str(p), "ok": icacls(p, "deny", user).returncode == 0})
            ambient = Path(r"C:\Users\sunqinghw\.claude\projects\D--pl-c4-pilot-zstd")
            if ambient.exists():
                acl_applied.append({"path": str(ambient), "ok": icacls(ambient, "deny", user).returncode == 0})
            # deny other arm's home + previous case's home
            for prev_key in [k for k in results if k != key]:
                prev_home = P2P / "cells" / prev_key / "home"
                if prev_home.exists():
                    acl_applied.append({"path": str(prev_home), "ok": icacls(prev_home, "deny", user).returncode == 0})
            temp_dirs_denied = []
            for d in sorted(Path(os.environ.get("TEMP", "")).iterdir()):
                if d.is_dir() and (d.name.startswith("pl-") or d.name.startswith("zstd-")):
                    r = icacls(d, "deny", user)
                    temp_dirs_denied.append({"path": str(d), "ok": r.returncode == 0})

            env_closures = {"dead_proxy": "http://127.0.0.1:9",
                            "no_proxy": GW_HOST,
                            "git_protocol": "never",
                            "disallowed_tools": ["WebFetch", "WebSearch"]}
            _atomic_json(cp / "isolation-attestation.json", {
                "cell_id": cell_id, "acl_applied": acl_applied,
                "temp_dirs_denied": [d["path"] for d in temp_dirs_denied],
                "env_closures": env_closures})

            # MCP config
            cfg_dir = cp / "mcp"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            cfg = cfg_dir / f"mcp-config-arm{arm}.json"
            runtime_src = FROZEN_RUNTIME_ROOT / arm
            _atomic_json(cfg, {"mcpServers": {"provenlattice": {
                "command": sys.executable,
                "args": [str(FH / "d9-c10" / "provenlattice_control_adapter.py"),
                         str(FH / "d9-c8" / "d9-c8-a-native-tool-exposure-contract.json")],
                "env": {"PYTHONPATH": str(runtime_src),
                        "PL_C10_RESPONSE_SOURCE": "control" if arm == "A" else "bridge",
                        "PL_C10_CONTROL_PAYLOAD": str(FH / "d9-c10" / "control-payload.json")}}}})

            prompt = instruction + "\n" + invocation["prompt"]
            launcher = shutil.which(FROZEN_ADAPTER_TOKENS[0])
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
                         "PYTHONPATH": str(P2P / "runtimes" / arm)}

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
            patch = extract_final_patch(ws, cell_id, base_commit)
            (raw_root / "agent-patch.txt").write_text(patch, encoding="utf-8")

            events, final_result = formal_stream_events(completed.stdout)
            for i, e in enumerate(events):
                e["sequence"] = i
            normalized = normalize_trace_v2(cell, str(raw_root / "raw_trace.txt"), events,
                                            patch, final_result)
            (cp / "normalized-trace.json").write_text(
                json.dumps(normalized, indent=2, ensure_ascii=False), encoding="utf-8")

            # funnel
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

            audit = leakage_audit(completed.stdout, ws, cell_id)
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

            # oracle v2
            ov2 = oracle_v2(ws, test_patch)

            # remove isolation
            for child in SENSITIVE:
                p = PILOT / child
                if p.exists():
                    icacls(p, "remove", user)
            if ambient.exists():
                icacls(ambient, "remove", user)
            for d in temp_dirs_denied:
                icacls(Path(d["path"]), "remove", user)

            cell_result = {"cell_id": cell_id, "arm": arm, "case_num": case_num,
                           "case_id": case_id, "base_commit": base_commit,
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
            pair_results[arm] = cell_result
            print(json.dumps({"mode": "P2_CELL_DONE", "case_num": case_num, "arm": arm,
                              "L0": l0, "L3": l3, "facts": len(norm_fids),
                              "oracle": ov2["class_v2"], "wall": round(ended - started, 1)},
                             indent=2))

        # pair gate
        A, B = pair_results["A"], pair_results["B"]
        pair_ok = {
            "A_L0_L3": all(A["funnel"][k] for k in ("L0", "L1", "L2", "L3")),
            "A_control": A["funnel"]["control_responses"] > 0,
            "A_semantic_zero": A["funnel"]["raw_fact_ids"] == [] and A["funnel"]["normalized_fact_ids"] == [],
            "B_L0_L3": all(B["funnel"][k] for k in ("L0", "L1", "L2", "L3")),
            "B_available": B["funnel"]["available_payloads"] > 0,
            "B_facts_gt_0": len(B["funnel"]["normalized_fact_ids"]) > 0,
            "B_raw_eq_norm": B["funnel"]["raw_fact_ids"] == B["funnel"]["normalized_fact_ids"],
            "A_oracle_executed": A["oracle_v2"]["class"] in ("FIX_PRESENT", "BUG_PRESENT"),
            "B_oracle_executed": B["oracle_v2"]["class"] in ("FIX_PRESENT", "BUG_PRESENT"),
            "attempt_1_both": all(c["attempt_count"] == 1 for c in (A, B)),
            "no_forbidden_tokens": all(
                not any(t in json.dumps(c["leakage_audit"]["surface_token_hits"])
                        for t in ("f9dec7c4", "058ed2ad3349187e985a481b6db654952729c307"))
                for c in (A, B)),
        }
        pg = {"case_num": case_num, "case_id": case_id, "checks": pair_ok,
              "verdict": ("CASE_%d_PAIR_PROTOCOL_VALID" % case_num
                          if all(pair_ok.values()) else
                          "CASE_%d_" % case_num + next(k for k, v in pair_ok.items() if not v))}
        pair_gates[case_num] = pg
        results.update({r["cell_id"]: r for r in pair_results.values()})
        _atomic_json(P2P / f"pair-gate-case{case_num}.json", pg)
        print(json.dumps({"mode": "P2_PAIR_GATE", "case": case_num, "verdict": pg["verdict"],
                          "pair_ok": pair_ok}, indent=2))
        if not all(pair_ok.values()):
            _atomic_json(P2P / "p2-gate.json", {
                "verdict": f"P2_PROTOCOL_FAIL_AT_CASE_{case_num}",
                "pair_gates": pair_gates, "results": results})
            print(json.dumps({"mode": "P2_FAIL_FAST", "case": case_num}))
            sys.exit(2)

    # aggregate gate
    all_facts_a = all(results[f"{cid}::A::r1"]["funnel"]["raw_fact_ids"] == []
                      and results[f"{cid}::A::r1"]["funnel"]["normalized_fact_ids"] == []
                      for cid in cases)
    all_facts_b = all(results[f"{cid}::B::r1"]["funnel"]["normalized_fact_ids"] and
                      results[f"{cid}::B::r1"]["funnel"]["raw_fact_ids"] == results[f"{cid}::B::r1"]["funnel"]["normalized_fact_ids"]
                      for cid in cases)
    all_oracle = all(results[f"{cid}::{arm}::r1"]["oracle_v2"]["class"] in ("FIX_PRESENT", "BUG_PRESENT")
                     for cid in cases for arm in ("A", "B"))
    agg_pass = (len(pair_gates) == 6 and all("VALID" in pg["verdict"] for pg in pair_gates.values())
                and all_facts_a and all_facts_b and all_oracle)
    aggregate = {"schema": "d9-c10-d-p2-gate-v1", "probe_id": PROBE_ID,
                 "frozen_at": now_iso(),
                 "pairs_valid": len(pair_gates),
                 "cells_completed": len(results),
                 "attempt_discipline": all(r["attempt_count"] == 1 for r in results.values()),
                 "arm_A_semantic_facts_zero_6_of_6": all_facts_a,
                 "arm_B_treatment_facts_gt_0_6_of_6": all_facts_b,
                 "oracle_v2_executed_12_of_12": all_oracle,
                 "verdict": "P2_CONTROLLED_CAPABILITY_STABILITY_PASS" if agg_pass
                            else "P2_STABILITY_FAIL"}
    (P2P / "p2-gate.json").write_text(json.dumps(aggregate, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "p2-gate.json").write_text(json.dumps(aggregate, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"mode": "P2_AGGREGATE_GATE", "verdict": aggregate["verdict"],
                      "pairs_valid": len(pair_gates)}, indent=2))


if __name__ == "__main__":
    run_p2()
