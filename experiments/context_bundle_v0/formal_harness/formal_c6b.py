"""D9-C6-B formal execution against active readiness workspaces.

Exactly one Agent invocation per frozen manifest cell, executed in the
already-materialized readiness workspace (never re-materialized).  Every gate
reuses the frozen ``formal_harness`` primitives; raw evidence is persisted
before C5-C normalization; there is no implicit retry.

Exit codes: 0 = formal cell record written (task/model outcome, whatever it
was); 2 = validation gate failure (HOLD); 3 = infrastructure/protocol outcome
(HOLD).
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
from pathlib import Path
from typing import Any

from formal_harness import (ARM_SHA, DISPATCHER_VERSION, FormalDataError,
                            REQUIRED_RUNNER_IDENTITY,
                            _atomic_json, _canonical, _git, _git_failure,
                            _launcher_sha256, _revision, artifact_key, construct_formal_invocation,
                            create_dispatch_attestation, digest, load_canonical_bundle,
                            normalize_trace, validate_dispatch_attestation,
                            validate_dispatch_context_freeze,
                            validate_frozen_execution_binding)

INPUT_KEYS = ("command", "file_path", "pattern", "path", "url", "description",
              "old_string", "new_string", "content")
RESULT_KEEP_FULL = ("provenlattice",)
RESULT_CAP = 8000
INPUT_CAP = 2000


def formal_stream_events(stdout: str) -> tuple[list[dict[str, Any]], str]:
    """Convert raw claude stream-json stdout into the canonical C5-C event
    shape proven by the normalize_trace contract: tool_use events carry
    ``tool`` + ``detail`` inputs; tool_result events carry
    ``detail.result`` (parsed dict when the result is JSON) so that
    ``task-context-v0`` payloads become returned facts."""
    events: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    final_result = ""
    seq = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            events.append({"kind": "raw", "detail": {"text": line[:500]}})
            seq += 1
            continue
        if not isinstance(value, dict):
            continue
        kind = value.get("type")
        if kind == "system":
            events.append({"kind": "system", "detail": {"subtype": value.get("subtype"),
                                                        "session_id": value.get("session_id"),
                                                        "model": value.get("model")}})
        elif kind == "assistant":
            for content in (value.get("message") or {}).get("content") or []:
                ctype = content.get("type")
                if ctype == "text":
                    events.append({"kind": "text", "detail": {"chars": len(content.get("text") or "")}})
                elif ctype == "tool_use":
                    tool_input = content.get("input") or {}
                    if not isinstance(tool_input, dict):
                        tool_input = {"input": tool_input}
                    detail = {key: str(tool_input.get(key))[:INPUT_CAP]
                              for key in INPUT_KEYS if tool_input.get(key) is not None}
                    record = {"kind": "tool_use", "tool": content.get("name"), "detail": detail}
                    events.append(record)
                    pending[str(content.get("id"))] = record
        elif kind == "user":
            for content in (value.get("message") or {}).get("content") or []:
                if content.get("type") != "tool_result":
                    continue
                use = pending.get(str(content.get("tool_use_id")), {})
                text = _result_text(content.get("content"))
                keep_full = any(marker in str(use.get("detail", {}).get("command", ""))
                                for marker in RESULT_KEEP_FULL)
                payload: Any
                try:
                    parsed = json.loads(text)
                    payload = parsed if isinstance(parsed, dict) else text
                except json.JSONDecodeError:
                    payload = text if keep_full else text[:RESULT_CAP]
                if not keep_full and isinstance(payload, str):
                    payload = payload[:RESULT_CAP]
                events.append({"kind": "tool_result",
                               "detail": {"result": payload, "chars": len(text),
                                          "is_error": bool(content.get("is_error"))}})
        elif kind == "result":
            result_text = value.get("result")
            if isinstance(result_text, str):
                final_result = result_text
            events.append({"kind": "result",
                           "detail": {"subtype": value.get("subtype"),
                                      "is_error": bool(value.get("is_error")),
                                      "num_turns": value.get("num_turns"),
                                      "duration_ms": value.get("duration_ms"),
                                      "model": value.get("model"),
                                      "usage": value.get("usage") or {}}})
        seq += 1
    return events, final_result


def _result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text") or "")
        else:
            parts.append(json.dumps(item, ensure_ascii=False)[:2000])
    return "\n".join(parts)


def revalidate_readiness(artifact_root: Path, bundle: dict[str, Any], environment,
                         invocation: dict[str, Any]) -> dict[str, Any]:
    """Pre-execution revalidation of the active readiness state for one cell."""
    cell = invocation["cell"]
    key = artifact_key(cell["cell_id"])
    checks: dict[str, Any] = {}
    cell_root = artifact_root / "cells" / key
    if cell_root.exists():
        raise FormalDataError("duplicate formal cell: cell artifacts already exist", {
            "schema": "d9-c6-b-gate-v1", "gate": "no_existing_formal_cell",
            "cell_id": cell["cell_id"], "cell_root": str(cell_root)})
    record_path = artifact_root / "readiness" / "materialization" / f"{key}.json"
    if not record_path.exists():
        raise FormalDataError("no active readiness record for cell", {
            "schema": "d9-c6-b-gate-v1", "gate": "active_readiness_record",
            "cell_id": cell["cell_id"], "expected_record": str(record_path)})
    record = json.loads(record_path.read_text(encoding="utf-8"))
    checks["readiness_schema"] = record.get("schema") == "d9-c6-a4b-materialization-v1"
    checks["readiness_cell_id"] = record.get("cell_id") == cell["cell_id"]
    checks["readiness_zero_agent_invocations"] = record.get("agent_invocation_count") == 0
    checks["readiness_binding_sha"] = record.get("execution_binding_sha256") == environment.binding_sha256
    checks["readiness_runner_identity"] = record.get("runner_identity") == REQUIRED_RUNNER_IDENTITY
    checks["readiness_dispatch_context"] = record.get("dispatch_context") == "formal-approved"
    checks["readiness_base_commit"] = record.get("base_commit") == cell["base_commit"]
    checks["readiness_runtime_sha"] = record.get("runtime_sha") == cell["runtime_sha"]
    checks["readiness_prompt_sha"] = record.get("prompt_sha256") == digest(invocation["prompt"])
    checks["readiness_command_sha"] = record.get("command_sha256") == digest(invocation["agent_argv"])
    checks["readiness_attestation_sha_present"] = bool(record.get("dispatch_attestation_sha256"))
    attestation = _find_valid_readiness_attestation(record.get("dispatch_attestation_sha256"))
    checks["readiness_attestation_contract_valid"] = attestation is not None
    binding_path = artifact_root / "worktree-bindings" / f"{key}.json"
    binding = json.loads(binding_path.read_text(encoding="utf-8")) if binding_path.exists() else {}
    checks["worktree_binding_cell_id"] = binding.get("cell_id") == cell["cell_id"]
    checks["worktree_binding_base_commit"] = binding.get("base_commit") == cell["base_commit"]
    expected_workspace = _canonical(artifact_root / "worktrees" / key)
    checks["worktree_binding_workspace"] = _canonical(Path(binding.get("workspace", "/"))) == expected_workspace
    checks["readiness_workspace_identity"] = _canonical(Path(record.get("workspace", "/"))) == expected_workspace
    workspace = expected_workspace
    head = _git(workspace, ["rev-parse", "HEAD"], workspace)
    checks["workspace_head_matches_frozen_base"] = (head.returncode == 0
                                                    and head.stdout.strip() == cell["base_commit"])
    status = _git(workspace, ["status", "--porcelain"], workspace)
    checks["workspace_initial_state_clean"] = (status.returncode == 0 and not status.stdout.strip())
    runtime = Path(invocation["runtime_root"])
    checks["runtime_head_matches_arm_sha"] = _revision(runtime, runtime) == ARM_SHA[cell["arm"]]
    failed = sorted(name for name, ok in checks.items() if not ok)
    if failed:
        raise FormalDataError("readiness revalidation failed", {
            "schema": "d9-c6-b-gate-v1", "gate": "readiness_revalidation",
            "cell_id": cell["cell_id"], "failed_checks": failed, "checks": checks})
    return {"schema": "d9-c6-b-revalidation-v1", "cell_id": cell["cell_id"], "checks": checks,
            "readiness_record_sha256": digest(record),
            "readiness_attestation_sha256": record.get("dispatch_attestation_sha256"),
            "workspace": str(workspace)}


def _find_valid_readiness_attestation(expected_sha: str | None):
    if not expected_sha:
        return None
    root = Path(_context_freeze_evidence()["attestation_root"])
    for path in sorted(root.glob("attestation-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if digest(payload) != expected_sha:
            continue
        if (payload.get("artifact_class") == "formal_dispatch_attestation"
                and payload.get("schema") == "d9-c6-approved-dispatch-v1"
                and payload.get("experiment_id") == "D9-C5-FORMAL"
                and payload.get("dispatch_context") == "formal-approved"
                and payload.get("runner_identity") == REQUIRED_RUNNER_IDENTITY
                and payload.get("execution_binding_sha256") == _current_binding_sha()
                and payload.get("launcher_sha256") == _launcher_sha256()
                and payload.get("attestation_nonce")):
            return {"path": str(path), **payload}
    return None


_CONTEXT_FREEZE_EVIDENCE: dict[str, Any] | None = None
_CURRENT_BINDING_SHA: str | None = None


def _context_freeze_evidence() -> dict[str, Any]:
    assert _CONTEXT_FREEZE_EVIDENCE is not None
    return _CONTEXT_FREEZE_EVIDENCE


def _current_binding_sha() -> str:
    assert _CURRENT_BINDING_SHA is not None
    return _CURRENT_BINDING_SHA


def extract_final_patch(workspace: Path, cell_id: str, base_commit: str) -> str:
    """Pilot-proven non-interactive patch capture; runs after the Agent exits."""
    staged = _git(workspace, ["add", "-A"], workspace)
    if staged.returncode != 0:
        raise _git_failure("final patch capture", "git add -A", staged,
                           {"cell": {"cell_id": cell_id, "base_commit": base_commit},
                            "approved_source_repository": str(workspace)}, workspace)
    diff = _git(workspace, ["diff", "--cached", "--binary"], workspace)
    if diff.returncode != 0:
        raise _git_failure("final patch capture", "git diff --cached --binary", diff,
                           {"cell": {"cell_id": cell_id, "base_commit": base_commit},
                            "approved_source_repository": str(workspace)}, workspace)
    return diff.stdout


def execute_readiness_cell(artifact_root: Path, invocation: dict[str, Any],
                           dispatch_attestation: dict[str, Any],
                           revalidation: dict[str, Any]) -> dict[str, Any]:
    """Exactly one Agent invocation in the readiness workspace, then evidence."""
    cell = invocation["cell"]
    key = artifact_key(cell["cell_id"])
    cell_root = artifact_root / "cells" / key
    raw_root = cell_root / "raw"
    workspace = Path(revalidation["workspace"])
    runtime = Path(invocation["runtime_root"])
    missing_env = [name for name in _required_env_names() if not os.environ.get(name)]
    if missing_env:
        return {"infrastructure": True, "reason": "required agent environment missing",
                "missing": missing_env}
    if _canonical(runtime) != _canonical(Path(invocation["approved_runtime_root"])):
        return {"infrastructure": True, "reason": "runtime path mismatch"}
    if _revision(runtime, runtime) != ARM_SHA[cell["arm"]]:
        return {"infrastructure": True, "reason": "runtime revision drift"}
    launcher = shutil.which(invocation["agent_argv"][0])
    if not launcher:
        return {"infrastructure": True, "reason": "agent launcher not resolvable",
                "argv0": invocation["agent_argv"][0]}
    argv = [launcher, *invocation["agent_argv"][1:]]
    environment = {**os.environ, **invocation["environment"],
                   "PYTHONPATH": str(runtime / "src")}
    started = time.time()
    try:
        completed = subprocess.run(argv, cwd=str(workspace), input=invocation["prompt"],
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=invocation["timeout_s"],
                                   env=environment)
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
    raw_root.mkdir(parents=True, exist_ok=False)
    (raw_root / "stdout.txt").write_text(outcome["stdout"], encoding="utf-8")
    (raw_root / "stderr.txt").write_text(outcome["stderr"], encoding="utf-8")
    (raw_root / "raw_trace.txt").write_text(raw_trace, encoding="utf-8")
    (raw_root / "agent-patch.txt").write_text(patch, encoding="utf-8")
    events, final_result = formal_stream_events(outcome["stdout"])
    metadata = {"cell_id": cell["cell_id"], "case_id": cell["case_id"], "arm": cell["arm"],
                "repeat": cell["repeat"], "runtime_sha": cell["runtime_sha"],
                "base_commit": cell["base_commit"],
                "execution_binding_sha256": invocation.get("binding_sha256"),
                "runner_identity": invocation.get("runner_identity"),
                "dispatch_context": invocation.get("dispatch_context"),
                "dispatch_attestation_sha256": dispatch_attestation["attestation_sha256"],
                "attempt_count": 1, "agent_invocation_count": 1,
                "started_at": started, "ended_at": ended,
                "wall_seconds": round(ended - started, 1),
                "exit_code": outcome["exit_code"],
                "completion_state": outcome["completion_state"],
                "workspace": str(workspace),
                "agent_argv": invocation["agent_argv"],
                "launcher_resolved": launcher,
                "launcher_sha256": hashlib.sha256(Path(launcher).read_bytes()).hexdigest(),
                "command_sha256": digest(invocation["agent_argv"]),
                "prompt_sha256": digest(invocation["prompt"]),
                "final_patch_sha256": digest(patch) if patch else None,
                "final_result_sha256": digest(final_result) if final_result else None,
                "manifest_sha256": _manifest_sha(), "dispatch_contract_sha256": _dispatch_sha(),
                "invocation_sha256": digest(invocation),
                "pre_execution_revalidation": revalidation,
                "timeout_s": invocation["timeout_s"]}
    _atomic_json(raw_root / "metadata.json", metadata)
    try:
        normalized = normalize_trace(cell, str(raw_root / "raw_trace.txt"), events, patch, final_result)
    except FormalDataError as exc:
        return {"infrastructure": True, "reason": "normalization_failure",
                "message": str(exc), "diagnostic": exc.diagnostic}
    _atomic_json(cell_root / "normalized-trace.json", normalized)
    record = {"cell_id": cell["cell_id"], "provenance_sha256": cell["provenance_sha256"],
              "case_id": cell["case_id"], "arm": cell["arm"], "repeat": cell["repeat"],
              "runtime_sha": cell["runtime_sha"], "base_commit": cell["base_commit"],
              "execution_binding_sha256": invocation.get("binding_sha256"),
              "runner_identity": invocation.get("runner_identity"),
              "dispatch_context": invocation.get("dispatch_context"),
              "dispatch_attestation_sha256": dispatch_attestation["attestation_sha256"],
              "dispatcher_version": DISPATCHER_VERSION, "attempt_count": 1,
              "trace_parse_pass": True, "raw_artifacts": str(raw_root),
              "normalized_trace": str(cell_root / "normalized-trace.json"),
              "returned_fact_ids": [f["fact_id"] for f in normalized["returned_facts"]],
              "final_reviews": [], "machine_metrics": {}, "evaluator": None,
              "execution": metadata}
    _atomic_json(cell_root / "cell-record.json", record)
    return {"infrastructure": False, "record": record, "metadata": metadata,
            "tool_uses": sum(1 for e in events if e.get("kind") == "tool_use"),
            "returned_facts": len(normalized["returned_facts"])}


_MANIFEST_SHA: str | None = None
_DISPATCH_SHA: str | None = None
_REQUIRED_ENV: list[str] | None = None


def _manifest_sha() -> str:
    assert _MANIFEST_SHA is not None
    return _MANIFEST_SHA


def _dispatch_sha() -> str:
    assert _DISPATCH_SHA is not None
    return _DISPATCH_SHA


def _required_env_names() -> list[str]:
    assert _REQUIRED_ENV is not None
    return _REQUIRED_ENV


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D9-C6-B formal readiness execution")
    parser.add_argument("--formal-root", required=True, type=Path)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--binding-freeze", required=True, type=Path)
    parser.add_argument("--dispatch-context-freeze", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cell-id", required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--execute", action="store_true", help="revalidate then run exactly one formal Agent invocation")
    modes.add_argument("--validate-readiness", action="store_true", help="revalidate readiness only; no attestation, no spawn, no writes")
    return parser.parse_args()


def main() -> None:
    global _CONTEXT_FREEZE_EVIDENCE, _CURRENT_BINDING_SHA, _MANIFEST_SHA, _DISPATCH_SHA, _REQUIRED_ENV
    args = parse_args()
    bundle = load_canonical_bundle(args.formal_root)
    _MANIFEST_SHA, _DISPATCH_SHA = bundle["manifest_sha256"], bundle["dispatch_sha256"]
    _REQUIRED_ENV = bundle["contract"].get("environment_required", [])
    environment = validate_frozen_execution_binding(bundle, args.binding, args.binding_freeze)
    _CURRENT_BINDING_SHA = environment.binding_sha256
    context = validate_dispatch_context_freeze(bundle, environment, args.dispatch_context_freeze)
    _CONTEXT_FREEZE_EVIDENCE = context
    attestation_for_construction = {
        "attestation_sha256": "revalidation-only", "runner_identity": REQUIRED_RUNNER_IDENTITY,
        "dispatch_context": "formal-approved"}
    invocation = construct_formal_invocation(bundle, environment, attestation_for_construction, args.cell_id)
    revalidation = revalidate_readiness(args.output, bundle, environment, invocation)
    if args.validate_readiness:
        print(json.dumps({"mode": "READINESS_REVALIDATED_NO_EXECUTION", "cell_id": args.cell_id,
                          "revalidation": revalidation, "agent_invocation_count": 0}))
        return
    attestation_path = Path(context["attestation_root"]) / f"attestation-{secrets.token_hex(16)}.json"
    create_dispatch_attestation(bundle, environment, args.dispatch_context_freeze, attestation_path,
                                time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
    dispatch_attestation = validate_dispatch_attestation(bundle, environment,
                                                         args.dispatch_context_freeze, attestation_path)
    invocation = construct_formal_invocation(bundle, environment, dispatch_attestation, args.cell_id)
    outcome = execute_readiness_cell(args.output, invocation, dispatch_attestation, revalidation)
    if outcome.get("infrastructure"):
        cell = invocation["cell"]
        cell_root = args.output / "cells" / artifact_key(cell["cell_id"])
        cell_root.mkdir(parents=True, exist_ok=True)
        _atomic_json(cell_root / "incomplete.json", {
            "state": "infrastructure_failure", "attempt_count": 1,
            "cell_id": cell["cell_id"], "reason": outcome.get("reason"),
            "message": outcome.get("message"), "diagnostic": outcome.get("diagnostic"),
            "detail": {k: v for k, v in outcome.items()
                       if k not in ("infrastructure", "reason", "message", "diagnostic")}})
        print(json.dumps({"mode": "FORMAL_INFRASTRUCTURE_FAILURE", "cell_id": cell["cell_id"],
                          "reason": outcome.get("reason")}))
        raise SystemExit(3)
    metadata = outcome["metadata"]
    print(json.dumps({"mode": "FORMAL_CELL_RECORDED", "cell_id": metadata["cell_id"],
                      "completion_state": metadata["completion_state"],
                      "exit_code": metadata["exit_code"],
                      "wall_seconds": metadata["wall_seconds"],
                      "tool_uses": outcome["tool_uses"],
                      "returned_facts": outcome["returned_facts"],
                      "attempt_count": 1, "agent_invocation_count": 1,
                      "dispatch_attestation": str(attestation_path),
                      "binding_sha256": metadata["execution_binding_sha256"]}))


if __name__ == "__main__":
    try:
        main()
    except FormalDataError as exc:
        print(json.dumps({"state": "FORMAL_C6B_GATE_FAILED", "message": str(exc),
                          "diagnostic": exc.diagnostic}), file=sys.stderr)
        raise SystemExit(2)
