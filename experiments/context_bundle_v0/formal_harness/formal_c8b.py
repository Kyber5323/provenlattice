"""D9-C8-B native MCP exposure formal runner.

The exposure surface is the frozen C8-A MCP tool (mcp__provenlattice__query);
the C7 system-prompt surface is REMOVED (single-variable rule).  Modes:
--infra-check (pre-batch gates), --materialize-only, --execute,
--aggregate-funnel.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from formal_harness import (ARM_SHA, DISPATCHER_VERSION, FormalDataError,
                            REQUIRED_RUNNER_IDENTITY, _atomic_json, _canonical,
                            _git, _revision, artifact_key, construct_formal_invocation,
                            create_dispatch_attestation, digest, load_canonical_bundle,
                            materialize_workspace, normalize_trace,
                            validate_dispatch_attestation,
                            validate_dispatch_context_freeze,
                            validate_frozen_execution_binding)

from formal_c6b import extract_final_patch, formal_stream_events

FH = Path(__file__).parent
CONTRACT_PATH = FH / "d9-c8" / "d9-c8-a-native-tool-exposure-contract.json"
CONTRACT_SHA256 = "7fda2facee1de99f61950bbeafc53d1d662586eea06615ca34ded6eac9bed47e"
TOOL_ENTRY_SHA256 = "8f032b61402d43f04d3b2e0cbdb4afc6042c365ed5c4bb11831e40b59b2ad2bf"
VISIBLE_TOOL = "mcp__provenlattice__query"
SERVER_NAME = "provenlattice"
MANIFEST_SHA256 = "3916b3c78c79a5ac491f33182bed57cf44f749db533a75097bbb6517fd367bea"
FROZEN_ADAPTER_TOKENS = "claude -p --output-format stream-json --verbose --dangerously-skip-permissions".split()
MATERIALIZATION_SCHEMA = "d9-c8-b-materialization-v1"
RECORD_SCHEMA = "d9-c8-b-cell-record-v1"
TC_COMMAND = "task-context"
HELP_COMMANDS = {"--help", "-h"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_contract() -> dict:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    stored = contract.pop("contract_sha256")
    if digest(contract) != CONTRACT_SHA256 or stored != CONTRACT_SHA256:
        raise FormalDataError("C8-A contract digest mismatch", {"schema": "d9-c8-b-gate-v1"})
    contract["contract_sha256"] = stored
    if digest(contract["mechanism"]["tool_entry_frozen_bytes"]) != TOOL_ENTRY_SHA256:
        raise FormalDataError("tool entry digest mismatch", {"schema": "d9-c8-b-gate-v1"})
    return contract


def abstract_command_digest(tokens: list[str]) -> str:
    """Command identity with the arm-specific MCP config path abstracted."""
    abstracted = ["<MCP_CONFIG>" if i > 0 and tokens[i - 1] == "--mcp-config" else t
                  for i, t in enumerate(tokens)]
    return digest(abstracted)


def write_mcp_config(output: Path, arm: str, pythonpath: str) -> Path:
    cfg_dir = output / "mcp"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / f"mcp-config-arm{arm}.json"
    payload = {"mcpServers": {SERVER_NAME: {
        "command": sys.executable,
        "args": [str(FH / "provenlattice_mcp_server.py"), str(CONTRACT_PATH)],
        "env": {"PYTHONPATH": pythonpath}}}}
    _atomic_json(cfg, payload)
    return cfg


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D9-C8-B native MCP exposure runner")
    parser.add_argument("--formal-root", required=True, type=Path)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--binding-freeze", required=True, type=Path)
    parser.add_argument("--dispatch-context-freeze", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cell-id", required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--infra-check", action="store_true")
    modes.add_argument("--materialize-only", action="store_true")
    modes.add_argument("--execute", action="store_true")
    modes.add_argument("--aggregate-funnel", action="store_true")
    return parser.parse_args()


def common_setup(args):
    contract = load_contract()
    bundle = load_canonical_bundle(args.formal_root)
    environment = validate_frozen_execution_binding(bundle, args.binding, args.binding_freeze)
    _BUNDLE_PROMPTS.update(bundle["prompts"])
    return contract, bundle, environment


def mcp_request(proc: subprocess.Popen, payload: dict) -> dict | None:
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    if payload.get("id") is None:
        return None
    while True:
        line = proc.stdout.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("id") == payload.get("id"):
            return msg


def do_infra_check(args) -> None:
    contract, bundle, environment = common_setup(args)
    tool_entry = contract["mechanism"]["tool_entry_frozen_bytes"]
    checks: dict[str, object] = {}
    adapter = FH / "provenlattice_mcp_server.py"
    arm_results = {}
    served_digests = set()
    for arm, binding in contract["mechanism"]["arm_env_bindings"].items():
        cfg = write_mcp_config(args.output, arm, binding["PYTHONPATH"])
        env = {**os.environ, "PYTHONPATH": binding["PYTHONPATH"], "PL_MCP_CONTRACT": str(CONTRACT_PATH)}
        proc = subprocess.Popen([sys.executable, str(adapter), str(CONTRACT_PATH)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
        try:
            mcp_request(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2024-11-05", "capabilities": {}}})
            mcp_request(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            listing = mcp_request(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            tools = (listing or {}).get("result", {}).get("tools", [])
            byte_identity = len(tools) == 1 and digest(tools[0]) == TOOL_ENTRY_SHA256 \
                and digest(tools[0]) == digest(tool_entry)
            served_digests.add(digest(tools[0]) if tools else "NONE")
            call = mcp_request(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                      "params": {"arguments": {"command": "--help"}}})
            result_text = (((call or {}).get("result") or {}).get("content") or [{}])[0].get("text", "")
            direct = subprocess.run([sys.executable, "-m", "provenlattice", "--help"],
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", env=env, timeout=300)
            passthrough = result_text == direct.stdout and direct.returncode == 0
            arm_results[arm] = {"tools_list_byte_identity": byte_identity,
                                "passthrough_byte_preserving": passthrough,
                                "help_returncode": direct.returncode,
                                "has_task_context_in_help": "task-context" in direct.stdout}
        finally:
            proc.stdin.close()
            proc.terminate()
    checks["adapter_tools_list_byte_identity_both_arms"] = all(
        r["tools_list_byte_identity"] for r in arm_results.values())
    checks["adapter_passthrough_byte_preserving_both_arms"] = all(
        r["passthrough_byte_preserving"] for r in arm_results.values())
    checks["arm_adapters_serve_identical_bytes"] = served_digests == {TOOL_ENTRY_SHA256}

    # registration smoke through the real claude CLI (non-formal, throwaway cwd)
    cfg = write_mcp_config(args.output, "A", contract["mechanism"]["arm_env_bindings"]["A"]["PYTHONPATH"])
    smoke_dir = args.output / "infra"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    smoke_out = smoke_dir / "registration-smoke.jsonl"
    argv = [shutil.which(FROZEN_ADAPTER_TOKENS[0]), *FROZEN_ADAPTER_TOKENS[1:],
            "--mcp-config", str(cfg), "--strict-mcp-config"]
    completed = subprocess.run(argv, cwd=str(smoke_dir), input="Reply with the single word READY.",
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=600)
    smoke_out.write_text(completed.stdout, encoding="utf-8")
    init = json.loads(completed.stdout.splitlines()[0])
    server_entry = [s for s in init.get("mcp_servers", []) if s.get("name") == SERVER_NAME]
    in_enumeration = VISIBLE_TOOL in (init.get("tools") or [])
    checks["mcp_init_connected"] = bool(server_entry) and server_entry[0].get("status") == "connected"
    checks["native_tool_in_enumeration"] = in_enumeration
    checks["no_system_prompt_surface"] = "--append-system-prompt" not in argv
    checks["probe_not_formal_labeled"] = True
    failed = [k for k, v in checks.items() if not v]
    if failed:
        raise FormalDataError("pre-batch infra gates failed: batch must not start", {
            "schema": "d9-c8-b-gate-v1", "gate": "pre_batch_infra", "failed": failed,
            "arm_results": arm_results})
    _atomic_json(smoke_dir / "infra-check.json", {
        "schema": "d9-c8-b-infra-check-v1", "frozen_at": now_iso(),
        "checks": checks, "arm_results": arm_results,
        "smoke_stream_sha256": hashlib.sha256(smoke_out.read_bytes()).hexdigest(),
        "label": "PROBE / NOT_FORMAL_EVIDENCE"})
    print(json.dumps({"mode": "C8B_INFRA_CHECK_PASS", "checks": checks,
                      "arm_results": arm_results,
                      "smoke_mcp_servers": server_entry,
                      "task_context_visible_in_arm_b_help_only": arm_results["B"]["has_task_context_in_help"]}))


def do_materialize(args) -> None:
    contract, bundle, environment = common_setup(args)
    validate_dispatch_context_freeze(bundle, environment, args.dispatch_context_freeze)
    attestation = mint_attestation(bundle, environment, args.dispatch_context_freeze)
    invocation = construct_formal_invocation(bundle, environment, attestation, args.cell_id)
    cell = invocation["cell"]
    key = artifact_key(cell["cell_id"])
    cfg = write_mcp_config(args.output, cell["arm"],
                           contract["mechanism"]["arm_env_bindings"][cell["arm"]]["PYTHONPATH"])
    tokens = FROZEN_ADAPTER_TOKENS + ["--mcp-config", str(cfg), "--strict-mcp-config"]
    command_digest = abstract_command_digest(tokens)
    if "--append-system-prompt" in tokens:
        raise FormalDataError("system-prompt surface must not exist in C8", {
            "schema": "d9-c8-b-gate-v1", "gate": "no_system_prompt_surface"})
    checks = symmetry_invariants(invocation, tokens, command_digest)
    cell_root = args.output / "cells" / key
    readiness_path = args.output / "readiness" / "materialization" / f"{key}.json"
    if cell_root.exists() or readiness_path.exists():
        raise FormalDataError("C8-B cell already materialized or recorded", {
            "schema": "d9-c8-b-gate-v1", "gate": "fresh_state", "cell_id": cell["cell_id"]})
    workspace = materialize_workspace(args.output, invocation)
    readiness_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(readiness_path, {
        "schema": MATERIALIZATION_SCHEMA, "cell_id": cell["cell_id"],
        "workspace": str(workspace), "runtime_sha": cell["runtime_sha"],
        "base_commit": cell["base_commit"], "command_sha256": command_digest,
        "prompt_sha256": digest(invocation["prompt"]),
        "mcp_config_sha256": hashlib.sha256(cfg.read_bytes()).hexdigest(),
        "visible_tool": VISIBLE_TOOL,
        "execution_binding_sha256": environment.binding_sha256,
        "runner_identity": invocation["runner_identity"],
        "dispatch_context": invocation["dispatch_context"],
        "dispatch_attestation_sha256": attestation["attestation_sha256"],
        "symmetry_invariants": checks, "agent_invocation_count": 0})
    print(json.dumps({"mode": "C8B_MATERIALIZED_NO_DISPATCH", "cell_id": cell["cell_id"],
                      "binding_sha256": environment.binding_sha256, "agent_invocation_count": 0}))


def symmetry_invariants(invocation: dict, tokens: list[str], command_digest: str) -> dict:
    cell = invocation["cell"]
    checks = {
        "inv1_native_surface_only": ("--append-system-prompt" not in tokens
                                     and "--mcp-config" in tokens and "--strict-mcp-config" in tokens
                                     and tokens[:len(FROZEN_ADAPTER_TOKENS)] == FROZEN_ADAPTER_TOKENS),
        "inv2_prompt_bytes_arm_independent": (
            invocation["prompt"] == _BUNDLE_PROMPTS[cell["case_id"]]
            and "provenlattice" not in invocation["prompt"].lower()),
        "inv3_runtime_root_only_arm_difference": (
            invocation["runtime_root"] == invocation["approved_runtime_root"]
            and _revision(Path(invocation["runtime_root"]), Path(invocation["runtime_root"])) == ARM_SHA[cell["arm"]]),
        "inv4_command_sha256_arm_independent": abstract_command_digest(tokens) == command_digest,
    }
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        raise FormalDataError("symmetry invariant failed: batch must stop", {
            "schema": "d9-c8-b-gate-v1", "gate": "symmetry_invariants",
            "cell_id": cell["cell_id"], "failed": failed, "checks": checks})
    return checks


_BUNDLE_PROMPTS: dict[str, str] = {}


def mint_attestation(bundle, environment, context_freeze_path) -> dict:
    attestation_path = Path(_canonical(Path(json.loads(context_freeze_path.read_text(encoding="utf-8"))["attestation_root"]))) \
        / f"attestation-{secrets.token_hex(16)}.json"
    create_dispatch_attestation(bundle, environment, context_freeze_path, attestation_path,
                                time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
    return validate_dispatch_attestation(bundle, environment, context_freeze_path, attestation_path)


def revalidate(args, bundle, environment, command_digest) -> dict:
    key = artifact_key(args.cell_id)
    record_path = args.output / "readiness" / "materialization" / f"{key}.json"
    if not record_path.exists():
        raise FormalDataError("no C8-B readiness record", {
            "schema": "d9-c8-b-gate-v1", "gate": "readiness_record", "cell_id": args.cell_id})
    record = json.loads(record_path.read_text(encoding="utf-8"))
    cell = next(c for c in bundle["manifest"]["cells"] if c["cell_id"] == args.cell_id)
    checks = {
        "schema": record.get("schema") == MATERIALIZATION_SCHEMA,
        "cell": record.get("cell_id") == args.cell_id,
        "zero_prior": record.get("agent_invocation_count") == 0,
        "binding": record.get("execution_binding_sha256") == environment.binding_sha256,
        "identity": record.get("runner_identity") == REQUIRED_RUNNER_IDENTITY,
        "context": record.get("dispatch_context") == "formal-approved",
        "base": record.get("base_commit") == cell["base_commit"],
        "runtime": record.get("runtime_sha") == cell["runtime_sha"],
        "prompt": record.get("prompt_sha256") == digest(bundle["prompts"][cell["case_id"]]),
        "command": record.get("command_sha256") == command_digest,
        "visible_tool": record.get("visible_tool") == VISIBLE_TOOL,
    }
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        raise FormalDataError("C8-B readiness revalidation failed", {
            "schema": "d9-c8-b-gate-v1", "gate": "revalidation", "failed": failed})
    workspace = Path(record["workspace"])
    head = _git(workspace, ["rev-parse", "HEAD"], workspace)
    status = _git(workspace, ["status", "--porcelain"], workspace)
    if head.returncode != 0 or head.stdout.strip() != cell["base_commit"] \
            or status.returncode != 0 or status.stdout.strip():
        raise FormalDataError("workspace is not a clean frozen worktree", {
            "schema": "d9-c8-b-gate-v1", "gate": "workspace_state"})
    if (args.output / "cells" / key).exists():
        raise FormalDataError("duplicate formal cell", {"schema": "d9-c8-b-gate-v1"})
    return record


def native_registration_evidence(stdout: str) -> dict:
    server_entry, in_enum = None, False
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            first = json.loads(line)
        except json.JSONDecodeError:
            break
        if isinstance(first, dict) and first.get("type") == "system":
            server_entry = [s for s in first.get("mcp_servers", []) if s.get("name") == SERVER_NAME]
            in_enum = VISIBLE_TOOL in (first.get("tools") or [])
        break
    return {"mcp_server_entry": server_entry, "tool_in_enumeration": in_enum,
            "visible_tool": VISIBLE_TOOL,
            "l0_native_tool_visible": bool(server_entry and server_entry[0].get("status") == "connected"
                                           and in_enum)}


def funnel_from_evidence(events: list[dict], returned_fact_ids: list[str],
                         final_patch: str) -> dict:
    tool_uses = [e for e in events if e.get("kind") == "tool_use" and e.get("tool") == VISIBLE_TOOL]
    pl_seqs = [e["sequence"] for e in tool_uses]
    first_action = min(pl_seqs) if pl_seqs else None
    first_action_index = None
    if pl_seqs:
        all_tool_uses = [e for e in events if e.get("kind") == "tool_use"]
        first_action_index = sum(1 for e in all_tool_uses if e["sequence"] <= first_action)
    commands = [((e.get("detail") or {}).get("command") or "").strip() for e in tool_uses]
    first_query_command = commands[0] if commands else None
    l1 = any(c in HELP_COMMANDS for c in commands)
    l2 = bool(commands)
    l3 = any(c == TC_COMMAND for c in commands)
    first_tc_seq = None
    for e in events:
        if e.get("kind") == "tool_result":
            payload = (e.get("detail") or {}).get("result")
            if isinstance(payload, dict) and payload.get("version") == "task-context-v0":
                first_tc_seq = e["sequence"]
                break
    l4 = len(returned_fact_ids) > 0
    l5 = False
    if l4 and first_tc_seq is not None:
        later_blob = final_patch + "\n" + "\n".join(
            json.dumps(e.get("detail", {})) for e in events
            if e.get("kind") == "tool_use" and e["sequence"] > first_tc_seq)
        l5 = any(str(f).split(":")[-1] in later_blob for f in returned_fact_ids)
    return {"L0_native_tool_visible": None, "L1_discovery_action": l1, "L2_tool_invoked": l2,
            "L3_task_context_requested": l3, "L4_facts_returned": l4, "L5_facts_used_proxy": l5,
            "observation": {"first_provenlattice_tool_call_index": first_action_index,
                            "first_query_command": first_query_command,
                            "total_query_calls": len(commands),
                            "returned_fact_count": len(returned_fact_ids)}}


def _infra(args, cell, reason, message) -> None:
    cell_root = args.output / "cells" / artifact_key(cell["cell_id"])
    cell_root.mkdir(parents=True, exist_ok=True)
    _atomic_json(cell_root / "incomplete.json", {
        "state": "infrastructure_failure", "attempt_count": 1,
        "cell_id": cell["cell_id"], "reason": reason, "message": message})
    print(json.dumps({"mode": "C8B_INFRASTRUCTURE_FAILURE", "cell_id": cell["cell_id"], "reason": reason}))
    raise SystemExit(3)


def do_execute(args) -> None:
    contract, bundle, environment = common_setup(args)
    validate_dispatch_context_freeze(bundle, environment, args.dispatch_context_freeze)
    cell = next(c for c in bundle["manifest"]["cells"] if c["cell_id"] == args.cell_id)
    cfg = args.output / "mcp" / f"mcp-config-arm{cell['arm']}.json"
    tokens = FROZEN_ADAPTER_TOKENS + ["--mcp-config", str(cfg), "--strict-mcp-config"]
    command_digest = abstract_command_digest(tokens)
    readiness = revalidate(args, bundle, environment, command_digest)
    attestation = mint_attestation(bundle, environment, args.dispatch_context_freeze)
    invocation = construct_formal_invocation(bundle, environment, attestation, args.cell_id)
    cell = invocation["cell"]
    key = artifact_key(cell["cell_id"])
    symmetry = symmetry_invariants(invocation, tokens, command_digest)
    workspace = Path(readiness["workspace"])
    launcher = shutil.which(tokens[0])
    if not launcher:
        _infra(args, cell, "launcher", "agent launcher not resolvable")
        return
    try:
        argv = [launcher, *tokens[1:]]
        runtime = Path(invocation["runtime_root"])
        if _revision(runtime, runtime) != ARM_SHA[cell["arm"]]:
            raise FormalDataError("runtime revision drift", {"schema": "d9-c8-b-gate-v1"})
        env_vars = {**os.environ, **invocation["environment"],
                    "PYTHONPATH": str(runtime / "src")}
        started = time.time()
        try:
            completed = subprocess.run(argv, cwd=str(workspace), input=invocation["prompt"],
                                       capture_output=True, text=True, encoding="utf-8",
                                       errors="replace", timeout=invocation["timeout_s"], env=env_vars)
            outcome = {"stdout": completed.stdout, "stderr": completed.stderr,
                       "exit_code": completed.returncode, "completion_state": "completed"}
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", "replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            outcome = {"stdout": stdout, "stderr": stderr, "exit_code": None,
                       "completion_state": "timeout"}
        ended = time.time()
        raw_trace = outcome["stdout"]
        patch = extract_final_patch(workspace, cell["cell_id"], cell["base_commit"])
        cell_root = args.output / "cells" / key
        raw_root = cell_root / "raw"
        raw_root.mkdir(parents=True, exist_ok=False)
        (raw_root / "stdout.txt").write_text(outcome["stdout"], encoding="utf-8")
        (raw_root / "stderr.txt").write_text(outcome["stderr"], encoding="utf-8")
        (raw_root / "raw_trace.txt").write_text(raw_trace, encoding="utf-8")
        (raw_root / "agent-patch.txt").write_text(patch, encoding="utf-8")
        events, final_result = formal_stream_events(outcome["stdout"])
        registration = native_registration_evidence(outcome["stdout"])
        metadata = {"cell_id": cell["cell_id"], "case_id": cell["case_id"], "arm": cell["arm"],
                    "repeat": cell["repeat"], "runtime_sha": cell["runtime_sha"],
                    "base_commit": cell["base_commit"],
                    "execution_binding_sha256": invocation.get("binding_sha256"),
                    "runner_identity": invocation.get("runner_identity"),
                    "dispatch_context": invocation.get("dispatch_context"),
                    "dispatch_attestation_sha256": attestation["attestation_sha256"],
                    "attempt_count": 1, "agent_invocation_count": 1,
                    "started_at": started, "ended_at": ended,
                    "wall_seconds": round(ended - started, 1),
                    "exit_code": outcome["exit_code"],
                    "completion_state": outcome["completion_state"],
                    "workspace": str(workspace), "agent_argv": tokens,
                    "launcher_resolved": launcher, "command_sha256": command_digest,
                    "prompt_sha256": digest(invocation["prompt"]),
                    "mcp_config_sha256": hashlib.sha256(cfg.read_bytes()).hexdigest(),
                    "visible_tool": VISIBLE_TOOL,
                    "symmetry_invariants": symmetry,
                    "invocation_sha256": digest(invocation),
                    "timeout_s": invocation["timeout_s"]}
        _atomic_json(raw_root / "metadata.json", metadata)
        try:
            normalized = normalize_trace(cell, str(raw_root / "raw_trace.txt"), events, patch, final_result)
        except FormalDataError as exc:
            _infra(args, cell, "normalization_failure", str(exc))
            return
        _atomic_json(cell_root / "normalized-trace.json", normalized)
        fact_ids = [f["fact_id"] for f in normalized["returned_facts"]]
        funnel = funnel_from_evidence(events, fact_ids, patch)
        funnel["L0_native_tool_visible"] = registration["l0_native_tool_visible"]
        record = {"schema": RECORD_SCHEMA, "cell_id": cell["cell_id"],
                  "provenance_sha256": cell["provenance_sha256"],
                  "case_id": cell["case_id"], "arm": cell["arm"], "repeat": cell["repeat"],
                  "runtime_sha": cell["runtime_sha"], "base_commit": cell["base_commit"],
                  "execution_binding_sha256": invocation.get("binding_sha256"),
                  "runner_identity": invocation.get("runner_identity"),
                  "dispatch_context": invocation.get("dispatch_context"),
                  "dispatch_attestation_sha256": attestation["attestation_sha256"],
                  "dispatcher_version": DISPATCHER_VERSION, "attempt_count": 1,
                  "mcp_config_sha256": hashlib.sha256(cfg.read_bytes()).hexdigest(),
                  "visible_tool": VISIBLE_TOOL, "command_sha256": command_digest,
                  "trace_parse_pass": True, "raw_artifacts": str(raw_root),
                  "normalized_trace": str(cell_root / "normalized-trace.json"),
                  "returned_fact_ids": fact_ids,
                  "native_registration": registration,
                  "adoption_funnel": funnel,
                  "final_reviews": [], "machine_metrics": {}, "evaluator": None,
                  "execution": metadata}
        _atomic_json(cell_root / "cell-record.json", record)
        print(json.dumps({"mode": "C8B_FORMAL_CELL_RECORDED", "cell_id": cell["cell_id"],
                          "completion_state": metadata["completion_state"],
                          "exit_code": metadata["exit_code"],
                          "wall_seconds": metadata["wall_seconds"],
                          "L0": registration["l0_native_tool_visible"],
                          "funnel": {k: v for k, v in funnel.items() if k.startswith("L")},
                          "observation": funnel["observation"],
                          "attempt_count": 1}))
    except FormalDataError:
        raise
    except Exception as exc:
        _infra(args, cell, "execution_failure", f"{type(exc).__name__}: {exc}")


def do_aggregate(args) -> None:
    contract, bundle, environment = common_setup(args)
    records = [json.loads(p.read_text(encoding="utf-8"))
               for p in sorted((args.output / "cells").glob("*/cell-record.json"))]
    incompletes = list((args.output / "cells").glob("*/incomplete.json"))
    if len(records) != 36 or incompletes:
        raise FormalDataError("batch incomplete", {"records": len(records),
                                                   "incomplete": len(incompletes)})
    if any(r["attempt_count"] != 1 or r["execution"]["agent_invocation_count"] != 1 for r in records):
        raise FormalDataError("attempt discipline violated", {"schema": "d9-c8-b-gate-v1"})
    if len({r["command_sha256"] for r in records}) != 1:
        raise FormalDataError("command sha drift (invariant 4)", {"schema": "d9-c8-b-gate-v1"})
    # cross-arm registration byte identity
    registration_signatures = {}
    for r in records:
        entry = json.dumps(r["native_registration"]["mcp_server_entry"], sort_keys=True)
        registration_signatures.setdefault((r["arm"], entry, r["native_registration"]["visible_tool"]), []).append(r["cell_id"])
    cross_arm_identical = len({k[1] for k in registration_signatures}) == 1 and \
        len({k[0] for k in registration_signatures}) == 2
    per_cell = {}
    for r in records:
        norm = json.loads(Path(r["normalized_trace"]).read_text(encoding="utf-8"))
        funnel = funnel_from_evidence(norm["ordered_events"], r["returned_fact_ids"],
                                      norm.get("final_patch") or "")
        reg = native_registration_evidence(
            (Path(r["raw_artifacts"]) / "raw_trace.txt").read_text(encoding="utf-8"))
        funnel["L0_native_tool_visible"] = reg["l0_native_tool_visible"]
        if funnel != r["adoption_funnel"] or reg != r["native_registration"]:
            raise FormalDataError("independent recomputation disagrees with record", {
                "cell_id": r["cell_id"]})
        per_cell[r["cell_id"]] = {"arm": r["arm"], "case_id": r["case_id"], "repeat": r["repeat"],
                                  "funnel": dict(funnel)}
    levels = ["L0_native_tool_visible", "L1_discovery_action", "L2_tool_invoked",
              "L3_task_context_requested", "L4_facts_returned", "L5_facts_used_proxy"]
    by_arm = {}
    for arm in ("A", "B"):
        cells = [c for c in per_cell.values() if c["arm"] == arm]
        by_arm[arm] = {"cells": len(cells),
                       **{lvl: sum(1 for c in cells if c["funnel"][lvl]) for lvl in levels}}
    l0_ok = sum(1 for c in per_cell.values() if c["funnel"]["L0_native_tool_visible"])
    first_indices = sorted(c["funnel"]["observation"]["first_provenlattice_tool_call_index"]
                           for c in per_cell.values()
                           if c["funnel"]["observation"]["first_provenlattice_tool_call_index"] is not None)
    first_commands = sorted({c["funnel"]["observation"]["first_query_command"]
                             for c in per_cell.values()
                             if c["funnel"]["observation"]["first_query_command"] is not None})
    if l0_ok != 36:
        state = "INVALID_EXPOSURE_TRANSPORT"
    elif by_arm["B"]["L1_discovery_action"] == 0 and by_arm["B"]["L2_tool_invoked"] == 0:
        state = "NATIVE_TOOL_VISIBLE_BUT_NO_DISCOVERY_ACTION"
    elif by_arm["B"]["L3_task_context_requested"] == 0:
        state = "ADOPTION_GAP_AT_SELECTION"
    elif by_arm["B"]["L4_facts_returned"] == 0:
        state = "CAPABILITY_INCONCLUSIVE / REQUESTED_BUT_NO_FACTS_RETURNED"
    else:
        state = "CAPABILITY_REACHABLE"
    gates = {
        "GATE_NATIVE_REGISTRATION": {"rule": "L0 == 36/36", "l0_cells": l0_ok,
                                     "state": "PASS" if l0_ok == 36 else "FAIL"},
        "GATE_DISCOVERY": {"rule": "B (L1>=1 or L2>=1)",
                           "b_l1": by_arm["B"]["L1_discovery_action"],
                           "b_l2": by_arm["B"]["L2_tool_invoked"],
                           "state": "PASS" if (by_arm["B"]["L1_discovery_action"]
                                               or by_arm["B"]["L2_tool_invoked"]) else "FAIL"},
        "GATE_SELECTION": {"rule": "if discovery then B L3 >= 1",
                           "state": "PASS" if by_arm["B"]["L3_task_context_requested"] >= 1
                           else ("FAIL" if (by_arm["B"]["L1_discovery_action"]
                                           or by_arm["B"]["L2_tool_invoked"]) else "NOT_EVALUATED")},
        "GATE_CAPABILITY_REACHABLE": {"rule": "B L4 >= 1",
                                      "b_l4": by_arm["B"]["L4_facts_returned"],
                                      "state": "PASS" if by_arm["B"]["L4_facts_returned"] >= 1 else "FAIL"},
    }
    case_distribution = {}
    for cell_id, c in sorted(per_cell.items()):
        case_distribution.setdefault(f"{c['case_id']}::{c['arm']}", []).append(
            {"repeat": c["repeat"], **{k: v for k, v in c["funnel"].items() if k.startswith("L")},
             "first_action_index": c["funnel"]["observation"]["first_provenlattice_tool_call_index"],
             "first_command": c["funnel"]["observation"]["first_query_command"],
             "facts": c["funnel"]["observation"]["returned_fact_count"]})
    artifact = {"schema": "d9-c8-b-funnel-gates-v1", "experiment_id": "D9-C8-FORMAL",
                "frozen_at": now_iso(), "contract_sha256": CONTRACT_SHA256,
                "visible_tool": VISIBLE_TOOL,
                "cells": 36, "incomplete": 0,
                "symmetry_invariants_all_pass": all(
                    all(r["execution"]["symmetry_invariants"].values()) for r in records),
                "cross_arm_registration_bytes_identical": cross_arm_identical,
                "attempt_discipline": {"attempt_count_not_1": 0, "unexpected_retries": 0},
                "funnel_by_arm": by_arm,
                "funnel_case_distribution": case_distribution,
                "observation_first_call": {
                    "note": "non-gate observation metrics",
                    "first_provenlattice_tool_call_index": first_indices,
                    "first_query_commands": first_commands},
                "gates": gates, "gate_state": state,
                "next_phase": "blind review permitted only if state == CAPABILITY_REACHABLE"}
    path = args.output / "funnel" / "funnel-gates.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(path, artifact)
    print(json.dumps({"mode": "C8B_FUNNEL_GATES_FROZEN", "gate_state": state,
                      "funnel_by_arm": by_arm, "gates": {k: v["state"] for k, v in gates.items()},
                      "cross_arm_registration_bytes_identical": cross_arm_identical,
                      "first_call_observations": {"indices": first_indices, "commands": first_commands},
                      "artifact": str(path), "review_phase_entered": state == "CAPABILITY_REACHABLE",
                      "agent_invocation_count": 36, "r1_reviews": 0, "r2_reviews": 0}))


def main() -> None:
    args = parse_args()
    if args.infra_check:
        do_infra_check(args)
    elif args.materialize_only:
        do_materialize(args)
    elif args.execute:
        do_execute(args)
    else:
        do_aggregate(args)


if __name__ == "__main__":
    try:
        main()
    except FormalDataError as exc:
        print(json.dumps({"state": "D9_C8_B_FAILED", "message": str(exc),
                          "diagnostic": exc.diagnostic}), file=sys.stderr)
        raise SystemExit(2)
