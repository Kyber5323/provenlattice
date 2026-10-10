"""D9-C10-C P1' controlled-capability re-run (2 cells) under effective protocol v2.

Per cell: fresh sealed standalone repo + fresh per-cell HOME/USERPROFILE/TMP/TEMP
+ isolation window (ACL denies, %TEMP% ACL denies, dead-proxy, git protocol
closure, --disallowed-tools) + one Agent invocation + normalization-v2 +
oracle-v2.  Cross-arm isolation: the other arm's cell home is denied during the
window.  Post-run leakage audit inside the window; window always closed via
try/finally.
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
from urllib.parse import urlsplit

FH = Path(r"D:\ChatGPT\codegraph\provenlattice\experiments\context_bundle_v0\formal_harness")
sys.path.insert(0, str(FH))
sys.path.insert(0, str(FH / "d9-c10"))

from c10_oracle_v2 import oracle_v2  # noqa: E402
from c10_workspace_v2 import build_sealed  # noqa: E402
from formal_c6b import extract_final_patch, formal_stream_events  # noqa: E402
from formal_c8b import FROZEN_ADAPTER_TOKENS, VISIBLE_TOOL, load_contract  # noqa: E402
from formal_c9c1 import normalize_trace_v2  # noqa: E402
from formal_harness import (FormalDataError, _atomic_json, _git,  # noqa: E402
                            construct_formal_invocation, create_dispatch_attestation,
                            digest, load_canonical_bundle, validate_dispatch_attestation,
                            validate_dispatch_context_freeze,
                            validate_frozen_execution_binding)

PROBE_ID = "D9-C10-C-P1-PRIME"
CASE_CELL = {"A": "Multi-SWE-Bench__c__maintenance__bugfix__21d59e43::A::r1",
             "B": "Multi-SWE-Bench__c__maintenance__bugfix__21d59e43::B::r1"}
BASE = "3128e03be69b9d3db054a3765ae1310c7b3666f6"
P1P = Path(r"D:\pl-c10-sealed\p1p")
PILOT = Path(r"D:\pl-c4-pilot")
GW_HOST = urlsplit(os.environ.get("ANTHROPIC_BASE_URL", "")).hostname
SENSITIVE = ["zstd", "refs", "runs", "smoke", "control-40aed09e", "control-p1",
             "formal-readiness-r3", "formal-readiness-c7b", "formal-readiness-c7d",
             "formal-readiness-c8b", "formal-readiness-p1", "p1-memory-quarantine",
             "diagnostic-c9-p0", "diagnostic-c9-p0b", "diagnostic-c9-c2",
             "harness", "formal-dispatch-attestations"]
AUDIT_TOKENS = [".claude", "p1-memory-quarantine", "pl-836", "pl-3719", "pl-2094",
                "pl-1416", "pl-1530", "pl-1532", "formal-readiness-r3", "formal-readiness-c7b",
                "formal-readiness-c7d", "formal-readiness-c8b", "formal-readiness-p1",
                "diagnostic-c9", "058ed2ad", "b3f33ccf", "ce31004f", "f9dec7c4", "PR #836"]


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def sid() -> str:
    return subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
                          text=True, encoding="utf-8").stdout.strip().split(",")[1].strip('"')


def icacls(path: Path, mode: str, user: str) -> subprocess.CompletedProcess:
    arg = f"*{user}:(OI)(CI)(F)" if mode == "deny" else f"*{user}"
    flag = "/deny" if mode == "deny" else "/remove:d"
    return subprocess.run(["icacls", str(path), flag, arg], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def git(args, cwd, check=True):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise SystemExit(f"git {' '.join(args[:2])} failed: {r.stderr[-200:]}")
    return r


def cell_paths(arm: str) -> dict:
    cell_id = CASE_CELL[arm]
    key = "cell-" + hashlib.sha256(cell_id.encode()).hexdigest()[:24]
    c = P1P / "cells" / key
    return {"cell_id": cell_id, "key": key, "root": c,
            "workspace": c / "workspace", "home": c / "home", "tmp": c / "tmp"}


def apply_isolation(user: str, other_home: Path | None) -> list:
    applied = []
    for child in SENSITIVE:
        p = PILOT / child
        if p.exists():
            r = icacls(p, "deny", user)
            applied.append({"path": str(p), "ok": r.returncode == 0})
    ambient = Path(r"C:\Users\sunqinghw\.claude\projects\D--pl-c4-pilot-zstd")
    if ambient.exists():
        r = icacls(ambient, "deny", user)
        applied.append({"path": str(ambient), "ok": r.returncode == 0})
    if other_home and other_home.exists():
        r = icacls(other_home, "deny", user)
        applied.append({"path": str(other_home), "ok": r.returncode == 0,
                        "purpose": "cross-arm isolation"})
    return applied


def remove_isolation(user: str) -> int:
    n = 0
    for child in SENSITIVE:
        p = PILOT / child
        if p.exists():
            r = icacls(p, "remove", user)
            n += 1 if r.returncode == 0 else 0
    ambient = Path(r"C:\Users\sunqinghw\.claude\projects\D--pl-c4-pilot-zstd")
    if ambient.exists():
        r = icacls(ambient, "remove", user)
        n += 1 if r.returncode == 0 else 0
    return n


def deny_temp_dirs(user: str) -> list:
    temp = Path(os.environ.get("TEMP", r"C:\Users\sunqinghw\AppData\Local\Temp"))
    denied = []
    for d in sorted(temp.iterdir()):
        if d.is_dir() and (d.name.startswith("pl-") or d.name.startswith("zstd-")):
            r = icacls(d, "deny", user)
            denied.append({"path": str(d), "ok": r.returncode == 0})
    return denied


def restore_temp_dirs(user: str, denied: list) -> int:
    n = 0
    for entry in denied:
        r = icacls(Path(entry["path"]), "remove", user)
        n += 1 if r.returncode == 0 else 0
    return n


def mint_attestation(bundle, environment, context_freeze_path):
    import secrets
    attestation_path = Path(str(Path(json.loads(
        context_freeze_path.read_text(encoding="utf-8"))["attestation_root"]).resolve())) \
        / f"attestation-{secrets.token_hex(16)}.json"
    create_dispatch_attestation(bundle, environment, context_freeze_path, attestation_path,
                                time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
    return validate_dispatch_attestation(bundle, environment, context_freeze_path, attestation_path)


def leakage_audit(arm: str, stdout: str, cell_id: str) -> dict:
    hits = {t: stdout.lower().count(t.lower()) for t in AUDIT_TOKENS if t.lower() in stdout.lower()}
    shas = ["058ed2ad3349187e985a481b6db654952729c307",
            "b3f33ccfb3b4fcc73df82126fd5ecfb751268fc6",
            "ce31004f205bb5dcb5ba6766b86009ffbcc90f00"]
    ws = P1P / "cells" / ("cell-" + hashlib.sha256(cell_id.encode()).hexdigest()[:24]) / "workspace"
    unavail = []
    for sha in shas:
        r = git(["cat-file", "-e", sha], ws, check=False)
        unavail.append(r.returncode != 0)
    return {"surface_token_hits": hits,
            "known_fix_shas_object_unavailable": all(unavail),
            "verdict": "CLEAN" if (not hits and all(unavail)) else "SOLUTION_LEAKAGE_INDICATORS"}


def run_cell(arm: str, user: str) -> dict:
    contract = load_contract()
    bundle = load_canonical_bundle(FH.parent / "formal")
    environment = validate_frozen_execution_binding(
        bundle, FH.parent / "bindings" / "execution-binding.local.json",
        FH.parent / "bindings" / "execution-binding.freeze.local.json")
    context_freeze = FH.parent / "bindings" / "dispatch-context.freeze.local.json"
    validate_dispatch_context_freeze(bundle, environment, context_freeze)
    attestation = mint_attestation(bundle, environment, context_freeze)
    invocation = construct_formal_invocation(bundle, environment, attestation, CASE_CELL[arm])
    cell = invocation["cell"]
    cp = cell_paths(arm)
    cell_id = cp["cell_id"]

    protocol = json.loads((FH / "d9-c10" / "controlled-capability-protocol.json")
                          .read_text(encoding="utf-8"))
    instruction = protocol["required_use_contract"]["instruction_text"]
    prompt = instruction + "\n" + invocation["prompt"]

    ws_attest = build_sealed(f"{PROBE_ID}-{arm}", BASE, cp["workspace"])
    head = git(["rev-parse", "HEAD"], cp["workspace"]).stdout.strip()
    tree = git(["rev-parse", "HEAD^{tree}"], cp["workspace"]).stdout.strip()
    fix_shas = ["058ed2ad3349187e985a481b6db654952729c307",
                "b3f33ccfb3b4fcc73df82126fd5ecfb751268fc6",
                "ce31004f205bb5dcb5ba6766b86009ffbcc90f00"]
    pre = {"workspace_clean": not _git(cp["workspace"], ["status", "--porcelain"],
                                        cp["workspace"]).stdout.strip(),
           "head_eq_base": head == BASE,
           "tree_eq_frozen": tree == ws_attest["base_tree_sha"],
           "fix_objects_unavailable": all(
               git(["cat-file", "-e", s], cp["workspace"], check=False).returncode != 0
               for s in fix_shas),
           "remotes_none": not git(["remote", "-v"], cp["workspace"]).stdout.strip(),
           "alternates_none": not (cp["workspace"] / ".git" / "objects" / "info" / "alternates").exists()}
    if not all(pre.values()):
        raise SystemExit(f"P1_PRIME_WORKSPACE_PRECHECK_FAIL: {pre}")
    _atomic_json(cp["root"] / "workspace-attestation.json", {
        "schema": "d9-c10-c-workspace-attestation-v1", "cell_id": cell_id,
        "frozen_at": now_iso(), "head_commit": head, "head_tree": tree,
        "leakage_tests_pass": ws_attest["leakage_tests_pass"], "pre_checks": pre,
        "sealed_root": str(cp["workspace"])})

    cp["home"].mkdir(parents=True, exist_ok=True)
    cp["tmp"].mkdir(parents=True, exist_ok=True)

    other_home = cell_paths("B" if arm == "A" else "A")["home"]
    applied = apply_isolation(user, other_home if arm == "A" else None)
    temp_denied = deny_temp_dirs(user)
    try:
        env_closures = {"dead_proxy": "http://127.0.0.1:9",
                        "no_proxy": GW_HOST,
                        "git_config_injection": ["protocol.allow=never"],
                        "disallowed_tools": ["WebFetch", "WebSearch"]}
        _atomic_json(cp["root"] / "isolation-attestation.json", {
            "schema": "d9-c10-c-isolation-attestation-v1", "cell_id": cell_id,
            "frozen_at": now_iso(), "acl_applied": applied,
            "temp_dirs_denied": [d["path"] for d in temp_denied],
            "env_closures": env_closures,
            "per_cell_home": str(cp["home"]), "per_cell_tmp": str(cp["tmp"]),
            "normalization_contract": "d9-c9-c1-normalization-v2"})

        cfg_dir = cp["root"] / "mcp"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg = cfg_dir / f"mcp-config-arm{arm}.json"
        runtime_src = Path(r"D:\pl-c10-sealed\runtimes") / arm
        _atomic_json(cfg, {"mcpServers": {"provenlattice": {
            "command": sys.executable,
            "args": [str(FH / "d9-c10" / "provenlattice_control_adapter.py"),
                     str(FH / "d9-c8" / "d9-c8-a-native-tool-exposure-contract.json")],
            "env": {"PYTHONPATH": str(runtime_src),
                    "PL_C10_RESPONSE_SOURCE": "control" if arm == "A" else "bridge",
                    "PL_C10_CONTROL_PAYLOAD": str(FH / "d9-c10" / "control-payload.json")}}}})

        launcher = shutil.which(FROZEN_ADAPTER_TOKENS[0])
        assert launcher, "launcher missing"
        argv = [launcher, *FROZEN_ADAPTER_TOKENS[1:],
                "--disallowed-tools", "WebFetch", "WebSearch",
                "--mcp-config", str(cfg), "--strict-mcp-config"]
        agent_env = {**os.environ,
                     "HOME": str(cp["home"]), "USERPROFILE": str(cp["home"]),
                     "TMP": str(cp["tmp"]), "TEMP": str(cp["tmp"]),
                     "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
                     "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": GW_HOST or "",
                     "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.allow",
                     "GIT_CONFIG_VALUE_0": "never",
                     "PYTHONPATH": str(runtime_src)}

        started = time.time()
        completed = subprocess.run(argv, cwd=str(cp["workspace"]), input=prompt,
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=3600, env=agent_env)
        ended = time.time()
        raw_root = cp["root"] / "raw"
        raw_root.mkdir(parents=True, exist_ok=False)
        (raw_root / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
        (raw_root / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
        (raw_root / "raw_trace.txt").write_text(completed.stdout, encoding="utf-8")
        patch = extract_final_patch(cp["workspace"], cell_id, cell["base_commit"])
        (raw_root / "agent-patch.txt").write_text(patch, encoding="utf-8")

        events, final_result = formal_stream_events(completed.stdout)
        for i, e in enumerate(events):
            e["sequence"] = i
        try:
            normalized = normalize_trace_v2(cell, str(raw_root / "raw_trace.txt"), events,
                                            patch, final_result)
        except FormalDataError as exc:
            _atomic_json(cp["root"] / "incomplete.json", {
                "state": "normalization_v2_failure", "attempt_count": 1, "message": str(exc),
                "normalization_contract": "d9-c9-c1-normalization-v2"})
            raise SystemExit(3)
        (cp["root"] / "normalized-trace.json").write_text(
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
                server_entry = [s for s in first.get("mcp_servers", [])
                                if s.get("name") == "provenlattice"]
                in_enum = VISIBLE_TOOL in (first.get("tools") or [])
                session_id, model = first.get("session_id"), first.get("model")
            break
        l0 = bool(server_entry and server_entry[0].get("status") == "connected" and in_enum)

        queries, pending = [], {}
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
                    for e in events
                    if e.get("kind") == "tool_use" and e.get("tool") == VISIBLE_TOOL]
        l1 = any(c in ("--help", "-h") for c in commands)
        l1_direct = bool(commands)
        l2 = bool(commands)
        l3 = any(c == "task-context" or c.startswith("task-context ") for c in commands)

        raw_fact_ids, control_responses, available_payloads = [], 0, 0
        for entry in queries:
            result_text = entry.get("result") or ""
            try:
                arr = json.loads(result_text)
                inner = "".join(x.get("text", "") for x in arr
                                if isinstance(x, dict) and x.get("type") == "text") \
                    if isinstance(arr, list) else result_text
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
                fids = [item["fact_id"] for k in ("primary_targets", "source_spans",
                                                  "relations", "candidates", "facts")
                        for item in (payload.get(k) or [])
                        if isinstance(item, dict) and isinstance(item.get("fact_id"), str)]
                if payload.get("status") == "available":
                    available_payloads += 1
                raw_fact_ids.extend(fids)
        normalized_fact_ids = [f["fact_id"] for f in normalized.get("returned_facts", [])]
        states = normalized.get("task_context_parse_states", [])
        control_state = any(s.get("state") == "control_no_facts" for s in states)
        available_state = any(s.get("state") == "available_facts_extracted" for s in states)

        audit = leakage_audit(arm, completed.stdout, cell_id)
        usage = next(((e.get("detail") or {}).get("usage") or {} for e in events
                      if e.get("kind") == "result"), {})
        tool_counts = normalized.get("tool_counts", {})
        tc_calls = [q for q in queries if (q.get("input") or {}).get("command") == "task-context"]

        first_tc_seq = next((e["sequence"] for e in events if e.get("kind") == "tool_result"
                             and isinstance((e.get("detail") or {}).get("result"), dict)
                             and (e["detail"]["result"] or {}).get("version") == "task-context-v0"),
                            None)
        l5 = False
        if normalized_fact_ids and first_tc_seq is not None:
            later_blob = patch + "\n" + "\n".join(
                json.dumps(e.get("detail", {})) for e in events
                if e.get("kind") == "tool_use" and e["sequence"] > first_tc_seq)
            l5 = any(str(f).split(":")[-1] in later_blob for f in normalized_fact_ids)

        _atomic_json(cp["root"] / "cell-result.json", {
            "schema": "d9-c10-c-cell-result-v1", "probe_id": PROBE_ID,
            "label": "DIAGNOSTIC_PILOT / CONTROLLED_CAPABILITY / NOT_FORMAL_EVIDENCE",
            "cell_id": cell_id, "arm": arm, "case_id": cell["case_id"],
            "base_commit": cell["base_commit"], "runtime_sha": cell["runtime_sha"],
            "normalization_contract": normalized.get("normalization_contract"),
            "session_id": session_id, "model": model,
            "exit_code": completed.returncode,
            "wall_seconds": round(ended - started, 1),
            "attempt_count": 1, "agent_invocation_count": 1,
            "funnel": {"L0_native_tool_visible": l0,
                       "L1_discovery_action": bool(l1 or l1_direct),
                       "L1_strict_help_only": l1,
                       "L2_tool_invoked": l2, "L3_task_context_requested": l3,
                       "task_context_calls": len(tc_calls),
                       "task_context_calls_all_json": (all("--json" in json.dumps(q.get("input") or {})
                                                           for q in tc_calls) if tc_calls else False),
                       "control_responses_received": control_responses,
                       "available_payloads_received": available_payloads,
                       "raw_fact_ids": sorted(set(raw_fact_ids)),
                       "normalized_fact_ids": sorted(set(normalized_fact_ids)),
                       "normalization_states": states,
                       "L5_facts_used_proxy": l5},
            "arm_gates": {
                "control_state_present": control_state,
                "available_state_present": available_state,
                "semantic_facts_zero": (arm != "A" or len(raw_fact_ids) == 0),
                "treatment_facts_gt_zero": (arm != "B" or len(normalized_fact_ids) > 0)},
            "secondary": {"total_tool_uses": sum(1 for e in events if e.get("kind") == "tool_use"),
                          "provenlattice_calls": len(queries),
                          "tool_counts": tool_counts, "usage_tokens": usage,
                          "wall_seconds": round(ended - started, 1),
                          "patch_bytes": len(patch),
                          "touched_files": sorted({ln.split()[1].split("/", 1)[-1]
                                                   for ln in patch.splitlines()
                                                   if ln.startswith("diff --git ")}),
                          "final_result_head": final_result[:1200]},
            "raw_to_normalized_correspondence": {
                "raw_distinct": sorted(set(raw_fact_ids)),
                "normalized_distinct": sorted(set(normalized_fact_ids)),
                "equal": set(raw_fact_ids) == set(normalized_fact_ids)},
            "post_run_leakage_audit": audit,
            "isolation_attestation": str(cp["root"] / "isolation-attestation.json"),
            "workspace_attestation": str(cp["root"] / "workspace-attestation.json")})

        test_patch = FH / "d9-c10" / "oracle-v2" / "case1-test-patch.diff"
        ov2 = oracle_v2(cp["workspace"], test_patch)
        _atomic_json(cp["root"] / "oracle-v2-result.json", {
            "schema": "d9-c10-c-oracle-v2-result-v1", "cell_id": cell_id, "arm": arm,
            "oracle_impl": "c10_oracle_v2", **ov2})

        cell_result = json.loads((cp["root"] / "cell-result.json").read_text(encoding="utf-8"))
        cell_result["oracle_v2"] = {"class": ov2["class_v2"],
                                    "fuzzer_build_exit": ov2["addendum"]["fuzzer_build_exit"],
                                    "fuzzer_run_exit": ov2["addendum"]["fuzzer_run_exit"],
                                    "legacy_class": ov2["legacy"]["class"]}
        cell_result["funnel"]["L5_facts_used_proxy"] = l5
        _atomic_json(cp["root"] / "cell-result.json", cell_result)

        print(json.dumps({"mode": "C10C_CELL_COMPLETE", "cell_id": cell_id, "arm": arm,
                          "funnel": cell_result["funnel"],
                          "oracle_v2_class": ov2["class_v2"],
                          "leakage_audit": audit["verdict"],
                          "exit_code": completed.returncode,
                          "wall_seconds": round(ended - started, 1)}, indent=2))
        return cell_result
    finally:
        remove_isolation(user)
        restore_temp_dirs(user, temp_denied)


def mint_attestation(bundle, environment, context_freeze_path):
    import secrets
    attestation_path = Path(str(Path(json.loads(
        context_freeze_path.read_text(encoding="utf-8"))["attestation_root"]).resolve())) \
        / f"attestation-{secrets.token_hex(16)}.json"
    create_dispatch_attestation(bundle, environment, context_freeze_path, attestation_path,
                                time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
    return validate_dispatch_attestation(bundle, environment, context_freeze_path, attestation_path)


if __name__ == "__main__":
    user = sid()
    preflight = json.loads((P1P / "runtime-delta-preflight.json").read_text(encoding="utf-8"))
    if preflight["preflight_verdict"] != "PASS":
        print(json.dumps({"state": "P1_PRIME_BLOCKED_RUNTIME_DRIFT"}))
        raise SystemExit(2)
    if "--run-cell" in sys.argv:
        arm = sys.argv[sys.argv.index("--run-cell") + 1]
        run_cell(arm, user)
