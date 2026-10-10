"""D9-C7-D structured discovery surface formal run.

Identical to the C7-B batch except the frozen C7-A one-line pointer is replaced
by the frozen C7-C structured discovery surface (the ONLY variable).  Adds the
frozen L0 transport gate and the preregistered non-gate observation metric
``first_provenlattice_action_tool_call``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
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
SURFACE_PATH = FH / "d9-c7" / "c7-c-discovery-surface.json"
CONTRACT_PATH = FH / "d9-c7" / "d9-c7-c-discovery-surface-contract.json"
CONTRACT_SHA256 = "35165db62c83c6f94e8a4f969bb72f1d4032b49302e725219fc3a7f5cf568164"
SURFACE_ARTIFACT_SHA256 = "c6ade621587fb4ef2e52cf3cc8af7ce21918be3ff34174ea86ee5297b8e22e0b"
DISCOVERY_TEXT_SHA256 = "fa93ca5e79472229ac575c2ad078dd64d17b0b3d60bfcfe49baf03ab8ab794f0"
MANIFEST_SHA256 = "3916b3c78c79a5ac491f33182bed57cf44f749db533a75097bbb6517fd367bea"
MATERIALIZATION_SCHEMA = "d9-c7-d-materialization-v1"
RECORD_SCHEMA = "d9-c7-d-cell-record-v1"
FROZEN_ADAPTER_TOKENS = "claude -p --output-format stream-json --verbose --dangerously-skip-permissions".split()
PL_RE = re.compile(r"python\s+-m\s+provenlattice", re.IGNORECASE)
TC_RE = re.compile(r"\btask-context\b", re.IGNORECASE)
HELP_RE = re.compile(r"(--help|-h)\b")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_c7d_surface() -> tuple[str, dict]:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    stored = contract.pop("contract_sha256")
    if digest(contract) != CONTRACT_SHA256:
        raise FormalDataError("C7-C contract digest mismatch", {
            "schema": "d9-c7-d-gate-v1", "gate": "contract_digest"})
    contract["contract_sha256"] = stored
    surface = json.loads(SURFACE_PATH.read_text(encoding="utf-8"))
    self_sha = surface.pop("artifact_sha256")
    if digest(surface) != SURFACE_ARTIFACT_SHA256:
        raise FormalDataError("C7-C surface artifact digest mismatch", {
            "schema": "d9-c7-d-gate-v1", "gate": "surface_digest"})
    surface["artifact_sha256"] = self_sha
    text = surface["discovery_text"]
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != DISCOVERY_TEXT_SHA256:
        raise FormalDataError("discovery text digest mismatch", {
            "schema": "d9-c7-d-gate-v1", "gate": "discovery_text_digest"})
    if contract["surface"]["text"] != text:
        raise FormalDataError("contract and surface text diverge", {
            "schema": "d9-c7-d-gate-v1", "gate": "surface_consistency"})
    return text, contract


def c7d_argv(text: str) -> list[str]:
    return FROZEN_ADAPTER_TOKENS + ["--append-system-prompt", text]


def symmetry_invariants(invocation: dict, text: str, command_digest: str) -> dict:
    cell = invocation["cell"]
    tokens = invocation["agent_argv"]
    checks = {
        "inv1_discovery_bytes_arm_independent": (
            tokens[-2:] == ["--append-system-prompt", text]
            and hashlib.sha256(text.encode("utf-8")).hexdigest() == DISCOVERY_TEXT_SHA256
            and tokens[:-2] == FROZEN_ADAPTER_TOKENS),
        "inv2_prompt_bytes_arm_independent": (
            invocation["prompt"] == _BUNDLE_PROMPTS[cell["case_id"]]
            and "provenlattice" not in invocation["prompt"].lower()),
        "inv3_runtime_root_only_arm_difference": (
            invocation["runtime_root"] == invocation["approved_runtime_root"]
            and _revision(Path(invocation["runtime_root"]), Path(invocation["runtime_root"])) == ARM_SHA[cell["arm"]]),
        "inv4_command_sha256_arm_independent": digest(tokens) == command_digest,
    }
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        raise FormalDataError("symmetry invariant failed: batch must stop", {
            "schema": "d9-c7-d-gate-v1", "gate": "symmetry_invariants",
            "cell_id": cell["cell_id"], "failed": failed, "checks": checks})
    return checks


_BUNDLE_PROMPTS: dict[str, str] = {}


def l0_transport(outcome: dict, tokens: list[str], text: str) -> dict:
    """Frozen L0: byte-identical surface in the invocation + CLI accepted (valid stream)."""
    tokens_ok = tokens[-2:] == ["--append-system-prompt", text] and \
        hashlib.sha256(text.encode("utf-8")).hexdigest() == DISCOVERY_TEXT_SHA256
    stream_ok = False
    for line in outcome["stdout"].splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            first = json.loads(line)
            stream_ok = isinstance(first, dict) and first.get("type") == "system"
        except json.JSONDecodeError:
            stream_ok = False
        break
    return {"delivered": bool(tokens_ok and stream_ok), "tokens_ok": tokens_ok,
            "stream_ok": stream_ok}


def funnel_from_evidence(commands: list[str], ordered_events: list[dict],
                         returned_fact_ids: list[str], final_patch: str) -> dict:
    tool_uses = [e for e in ordered_events if e["kind"] == "tool_use"]
    pl_seqs = [e["sequence"] for e in tool_uses
               if PL_RE.search((e.get("detail") or {}).get("command", ""))]
    first_action = None
    if pl_seqs:
        ordinal = sum(1 for e in tool_uses if e["sequence"] <= min(pl_seqs))
        first_action = ordinal
    l1 = any(PL_RE.search(c) and HELP_RE.search(c) for c in commands)
    l2 = any(PL_RE.search(c) for c in commands)
    l3 = any(PL_RE.search(c) and TC_RE.search(c) for c in commands)
    first_tc_seq = None
    for e in ordered_events:
        if e["kind"] == "tool_result":
            payload = (e.get("detail") or {}).get("result")
            if isinstance(payload, dict) and payload.get("version") == "task-context-v0":
                first_tc_seq = e["sequence"]
                break
    l4 = len(returned_fact_ids) > 0
    l5 = False
    if l4 and first_tc_seq is not None:
        later_blob = final_patch + "\n" + "\n".join(
            json.dumps(e.get("detail", {})) for e in ordered_events
            if e["kind"] == "tool_use" and e["sequence"] > first_tc_seq)
        l5 = any(str(f).split(":")[-1] in later_blob for f in returned_fact_ids)
    return {"L0_exposure_delivered": None,  # filled by caller with transport evidence
            "L1_discovery_action": l1, "L2_CLI_invoked": l2, "L3_task_context_requested": l3,
            "L4_facts_returned": l4, "L5_facts_used_proxy": l5,
            "observation": {"first_provenlattice_action_tool_call": first_action,
                            "total_tool_uses": len(tool_uses),
                            "provenlattice_commands": sum(1 for c in commands if PL_RE.search(c)),
                            "task_context_commands": sum(1 for c in commands if PL_RE.search(c) and TC_RE.search(c)),
                            "first_task_context_v0_sequence": first_tc_seq,
                            "returned_fact_count": len(returned_fact_ids)}}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D9-C7-D structured discovery surface runner")
    parser.add_argument("--formal-root", required=True, type=Path)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--binding-freeze", required=True, type=Path)
    parser.add_argument("--dispatch-context-freeze", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cell-id", required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--materialize-only", action="store_true")
    modes.add_argument("--execute", action="store_true")
    modes.add_argument("--aggregate-funnel", action="store_true")
    return parser.parse_args()


def common_setup(args):
    text, contract = load_c7d_surface()
    if contract["relation_to_prior_frozen_state"]["basis_frozen_from_d9_c6"]["manifest_sha256"] != MANIFEST_SHA256:
        raise FormalDataError("C7-D contract does not bind the frozen manifest", {
            "schema": "d9-c7-d-gate-v1", "gate": "manifest_binding"})
    bundle = load_canonical_bundle(args.formal_root)
    environment = validate_frozen_execution_binding(bundle, args.binding, args.binding_freeze)
    _BUNDLE_PROMPTS.update(bundle["prompts"])
    return text, contract, bundle, environment


def mint_attestation(bundle, environment, context_freeze_path) -> dict:
    attestation_path = Path(_canonical(Path(json.loads(context_freeze_path.read_text(encoding="utf-8"))["attestation_root"]))) \
        / f"attestation-{secrets.token_hex(16)}.json"
    create_dispatch_attestation(bundle, environment, context_freeze_path, attestation_path,
                                time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
    return validate_dispatch_attestation(bundle, environment, context_freeze_path, attestation_path)


def do_materialize(args) -> None:
    text, contract, bundle, environment = common_setup(args)
    validate_dispatch_context_freeze(bundle, environment, args.dispatch_context_freeze)
    attestation = mint_attestation(bundle, environment, args.dispatch_context_freeze)
    invocation = construct_formal_invocation(bundle, environment, attestation, args.cell_id)
    command_digest = digest(c7d_argv(text))
    invocation = {**invocation, "agent_argv": c7d_argv(text)}
    cell = invocation["cell"]
    key = artifact_key(cell["cell_id"])
    checks = symmetry_invariants(invocation, text, command_digest)
    cell_root = args.output / "cells" / key
    readiness_path = args.output / "readiness" / "materialization" / f"{key}.json"
    if cell_root.exists() or readiness_path.exists():
        raise FormalDataError("C7-D cell already materialized or recorded", {
            "schema": "d9-c7-d-gate-v1", "gate": "fresh_state", "cell_id": cell["cell_id"]})
    workspace = materialize_workspace(args.output, invocation)
    readiness_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(readiness_path, {
        "schema": MATERIALIZATION_SCHEMA, "cell_id": cell["cell_id"],
        "workspace": str(workspace), "runtime_sha": cell["runtime_sha"],
        "base_commit": cell["base_commit"], "command_sha256": command_digest,
        "prompt_sha256": digest(invocation["prompt"]),
        "discovery_text_sha256": DISCOVERY_TEXT_SHA256,
        "execution_binding_sha256": environment.binding_sha256,
        "runner_identity": invocation["runner_identity"],
        "dispatch_context": invocation["dispatch_context"],
        "dispatch_attestation_sha256": attestation["attestation_sha256"],
        "symmetry_invariants": checks, "agent_invocation_count": 0})
    print(json.dumps({"mode": "C7D_MATERIALIZED_NO_DISPATCH", "cell_id": cell["cell_id"],
                      "binding_sha256": environment.binding_sha256, "agent_invocation_count": 0}))


def revalidate(args, bundle, environment, text, command_digest) -> dict:
    key = artifact_key(args.cell_id)
    record_path = args.output / "readiness" / "materialization" / f"{key}.json"
    if not record_path.exists():
        raise FormalDataError("no C7-D readiness record", {
            "schema": "d9-c7-d-gate-v1", "gate": "readiness_record", "cell_id": args.cell_id})
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
        "discovery": record.get("discovery_text_sha256") == DISCOVERY_TEXT_SHA256,
    }
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        raise FormalDataError("C7-D readiness revalidation failed", {
            "schema": "d9-c7-d-gate-v1", "gate": "revalidation", "failed": failed})
    workspace = Path(record["workspace"])
    head = _git(workspace, ["rev-parse", "HEAD"], workspace)
    status = _git(workspace, ["status", "--porcelain"], workspace)
    if head.returncode != 0 or head.stdout.strip() != cell["base_commit"] \
            or status.returncode != 0 or status.stdout.strip():
        raise FormalDataError("workspace is not a clean frozen worktree", {
            "schema": "d9-c7-d-gate-v1", "gate": "workspace_state"})
    if (args.output / "cells" / key).exists():
        raise FormalDataError("duplicate formal cell", {"schema": "d9-c7-d-gate-v1"})
    return record


def _infra(args, cell, reason, message) -> None:
    cell_root = args.output / "cells" / artifact_key(cell["cell_id"])
    cell_root.mkdir(parents=True, exist_ok=True)
    _atomic_json(cell_root / "incomplete.json", {
        "state": "infrastructure_failure", "attempt_count": 1,
        "cell_id": cell["cell_id"], "reason": reason, "message": message})
    print(json.dumps({"mode": "C7D_INFRASTRUCTURE_FAILURE", "cell_id": cell["cell_id"], "reason": reason}))
    raise SystemExit(3)


def do_execute(args) -> None:
    text, contract, bundle, environment = common_setup(args)
    validate_dispatch_context_freeze(bundle, environment, args.dispatch_context_freeze)
    command_digest = digest(c7d_argv(text))
    readiness = revalidate(args, bundle, environment, text, command_digest)
    attestation = mint_attestation(bundle, environment, args.dispatch_context_freeze)
    invocation = construct_formal_invocation(bundle, environment, attestation, args.cell_id)
    invocation = {**invocation, "agent_argv": c7d_argv(text)}
    cell = invocation["cell"]
    key = artifact_key(cell["cell_id"])
    symmetry = symmetry_invariants(invocation, text, command_digest)
    workspace = Path(readiness["workspace"])
    import shutil
    launcher = shutil.which(invocation["agent_argv"][0])
    if not launcher:
        _infra(args, cell, "launcher", "agent launcher not resolvable")
        return
    try:
        argv = [launcher, *invocation["agent_argv"][1:]]
        runtime = Path(invocation["runtime_root"])
        if _revision(runtime, runtime) != ARM_SHA[cell["arm"]]:
            raise FormalDataError("runtime revision drift", {"schema": "d9-c7-d-gate-v1"})
        env_vars = {**__import__("os").environ, **invocation["environment"],
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
        commands = [e["detail"].get("command", "") for e in events
                    if e.get("kind") == "tool_use" and "command" in (e.get("detail") or {})]
        l0 = l0_transport(outcome, invocation["agent_argv"], text)
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
                    "workspace": str(workspace), "agent_argv": invocation["agent_argv"],
                    "launcher_resolved": launcher, "command_sha256": command_digest,
                    "prompt_sha256": digest(invocation["prompt"]),
                    "discovery_text_sha256": DISCOVERY_TEXT_SHA256,
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
        funnel = funnel_from_evidence(commands, normalized["ordered_events"], fact_ids, patch)
        funnel["L0_exposure_delivered"] = l0["delivered"]
        record = {"schema": RECORD_SCHEMA, "cell_id": cell["cell_id"],
                  "provenance_sha256": cell["provenance_sha256"],
                  "case_id": cell["case_id"], "arm": cell["arm"], "repeat": cell["repeat"],
                  "runtime_sha": cell["runtime_sha"], "base_commit": cell["base_commit"],
                  "execution_binding_sha256": invocation.get("binding_sha256"),
                  "runner_identity": invocation.get("runner_identity"),
                  "dispatch_context": invocation.get("dispatch_context"),
                  "dispatch_attestation_sha256": attestation["attestation_sha256"],
                  "dispatcher_version": DISPATCHER_VERSION, "attempt_count": 1,
                  "discovery_text_sha256": DISCOVERY_TEXT_SHA256,
                  "command_sha256": command_digest, "trace_parse_pass": True,
                  "raw_artifacts": str(raw_root),
                  "normalized_trace": str(cell_root / "normalized-trace.json"),
                  "returned_fact_ids": fact_ids, "adoption_funnel": funnel,
                  "l0_transport": l0,
                  "final_reviews": [], "machine_metrics": {}, "evaluator": None,
                  "execution": metadata}
        _atomic_json(cell_root / "cell-record.json", record)
        print(json.dumps({"mode": "C7D_FORMAL_CELL_RECORDED", "cell_id": cell["cell_id"],
                          "completion_state": metadata["completion_state"],
                          "exit_code": metadata["exit_code"],
                          "wall_seconds": metadata["wall_seconds"],
                          "L0": l0["delivered"],
                          "funnel": {k: v for k, v in funnel.items() if k.startswith("L")},
                          "observation": funnel["observation"],
                          "attempt_count": 1}))
    except FormalDataError:
        raise
    except Exception as exc:
        _infra(args, cell, "execution_failure", f"{type(exc).__name__}: {exc}")


def do_aggregate(args) -> None:
    text, contract, bundle, environment = common_setup(args)
    records = [json.loads(p.read_text(encoding="utf-8"))
               for p in sorted((args.output / "cells").glob("*/cell-record.json"))]
    incompletes = list((args.output / "cells").glob("*/incomplete.json"))
    if len(records) != 36 or incompletes:
        raise FormalDataError("batch incomplete", {"records": len(records),
                                                   "incomplete": len(incompletes)})
    command_digest = digest(c7d_argv(text))
    if any(r["attempt_count"] != 1 or r["execution"]["agent_invocation_count"] != 1 for r in records):
        raise FormalDataError("attempt discipline violated", {"schema": "d9-c7-d-gate-v1"})
    if len({r["command_sha256"] for r in records}) != 1 or command_digest not in {r["command_sha256"] for r in records}:
        raise FormalDataError("command sha drift (invariant 4)", {"schema": "d9-c7-d-gate-v1"})
    if any(r["discovery_text_sha256"] != DISCOVERY_TEXT_SHA256 for r in records):
        raise FormalDataError("discovery sha drift", {"schema": "d9-c7-d-gate-v1"})
    per_cell = {}
    for r in records:
        norm = json.loads(Path(r["normalized_trace"]).read_text(encoding="utf-8"))
        commands = [e["detail"].get("command", "") for e in norm["ordered_events"]
                    if e["kind"] == "tool_use" and "command" in (e.get("detail") or {})]
        funnel = funnel_from_evidence(commands, norm["ordered_events"], r["returned_fact_ids"],
                                      norm.get("final_patch") or "")
        l0 = l0_transport({"stdout": (Path(r["raw_artifacts"]) / "raw_trace.txt").read_text(encoding="utf-8")},
                          r["execution"]["agent_argv"], text)
        funnel["L0_exposure_delivered"] = l0["delivered"]
        if funnel != r["adoption_funnel"]:
            raise FormalDataError("independent funnel recomputation disagrees", {
                "cell_id": r["cell_id"]})
        per_cell[r["cell_id"]] = {"arm": r["arm"], "case_id": r["case_id"], "repeat": r["repeat"],
                                  "funnel": dict(funnel)}
    levels = ["L0_exposure_delivered", "L1_discovery_action", "L2_CLI_invoked",
              "L3_task_context_requested", "L4_facts_returned", "L5_facts_used_proxy"]
    by_arm = {}
    for arm in ("A", "B"):
        cells = [c for c in per_cell.values() if c["arm"] == arm]
        by_arm[arm] = {"cells": len(cells),
                       **{lvl: sum(1 for c in cells if c["funnel"][lvl]) for lvl in levels}}
    l0_ok = sum(1 for c in per_cell.values() if c["funnel"]["L0_exposure_delivered"])
    first_actions = [c["funnel"]["observation"]["first_provenlattice_action_tool_call"]
                     for c in per_cell.values()
                     if c["funnel"]["observation"]["first_provenlattice_action_tool_call"] is not None]
    if l0_ok != 36:
        state = "INVALID_EXPOSURE_TRANSPORT"
    elif by_arm["B"]["L1_discovery_action"] == 0 and by_arm["B"]["L2_CLI_invoked"] == 0:
        state = "EXPOSURE_DELIVERED_BUT_NO_DISCOVERY_ACTION"
    elif by_arm["B"]["L3_task_context_requested"] == 0:
        state = "ADOPTION_GAP_AT_SELECTION"
    elif by_arm["B"]["L4_facts_returned"] == 0:
        state = "CAPABILITY_INCONCLUSIVE / REQUESTED_BUT_NO_FACTS_RETURNED"
    else:
        state = "CAPABILITY_REACHABLE"
    gates = {
        "GATE_EXPOSURE_L0": {"rule": "L0 == 36/36", "l0_cells": l0_ok,
                             "state": "PASS" if l0_ok == 36 else "FAIL"},
        "GATE_DISCOVERY": {"rule": "B (L1>=1 or L2>=1)",
                           "b_l1": by_arm["B"]["L1_discovery_action"],
                           "b_l2": by_arm["B"]["L2_CLI_invoked"],
                           "state": "PASS" if (by_arm["B"]["L1_discovery_action"]
                                               or by_arm["B"]["L2_CLI_invoked"]) else "FAIL"},
        "GATE_SELECTION": {"rule": "if discovery then B L3 >= 1",
                           "state": "PASS" if by_arm["B"]["L3_task_context_requested"] >= 1
                           else ("FAIL" if (by_arm["B"]["L1_discovery_action"]
                                           or by_arm["B"]["L2_CLI_invoked"]) else "NOT_EVALUATED")},
        "GATE_CAPABILITY_REACHABLE": {"rule": "B L4 >= 1",
                                      "b_l4": by_arm["B"]["L4_facts_returned"],
                                      "state": "PASS" if by_arm["B"]["L4_facts_returned"] >= 1 else "FAIL"},
    }
    case_distribution = {}
    for cell_id, c in sorted(per_cell.items()):
        case_distribution.setdefault(f"{c['case_id']}::{c['arm']}", []).append(
            {"repeat": c["repeat"], **{k: v for k, v in c["funnel"].items() if k.startswith("L")},
             "first_action": c["funnel"]["observation"]["first_provenlattice_action_tool_call"]})
    artifact = {"schema": "d9-c7-d-funnel-gates-v1", "experiment_id": "D9-C7-FORMAL",
                "frozen_at": now_iso(), "contract_sha256": CONTRACT_SHA256,
                "discovery_text_sha256": DISCOVERY_TEXT_SHA256,
                "cells": 36, "incomplete": 0,
                "symmetry_invariants_all_pass": all(
                    all(r["execution"]["symmetry_invariants"].values()) for r in records),
                "attempt_discipline": {"attempt_count_not_1": 0, "unexpected_retries": 0},
                "funnel_by_arm": by_arm,
                "funnel_case_distribution": case_distribution,
                "observation_first_provenlattice_action_tool_call": {
                    "note": "non-gate observation metric (1-based ordinal among agent tool calls)",
                    "values": sorted(v for v in first_actions if v is not None)},
                "gates": gates, "gate_state": state,
                "next_phase": "blind review permitted only if state == CAPABILITY_REACHABLE"}
    path = args.output / "funnel" / "funnel-gates.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(path, artifact)
    print(json.dumps({"mode": "C7D_FUNNEL_GATES_FROZEN", "gate_state": state,
                      "funnel_by_arm": by_arm, "gates": {k: v["state"] for k, v in gates.items()},
                      "first_action_observations": artifact["observation_first_provenlattice_action_tool_call"]["values"],
                      "artifact": str(path), "review_phase_entered": state == "CAPABILITY_REACHABLE",
                      "agent_invocation_count": 36, "r1_reviews": 0, "r2_reviews": 0}))


def main() -> None:
    args = parse_args()
    if args.materialize_only:
        do_materialize(args)
    elif args.execute:
        do_execute(args)
    else:
        do_aggregate(args)


if __name__ == "__main__":
    try:
        main()
    except FormalDataError as exc:
        print(json.dumps({"state": "D9_C7_D_FAILED", "message": str(exc),
                          "diagnostic": exc.diagnostic}), file=sys.stderr)
        raise SystemExit(2)
