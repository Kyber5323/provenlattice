"""D9-C5-C formal Context Bundle harness.

This module plans, records, and validates formal evidence; it never invokes an
Agent itself.  A future executor supplies raw traces to ``record_cell`` only
after the 36-cell experiment is explicitly authorized.  Qualification uses
synthetic traces exclusively.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import time
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ARM_SHA = {
    "A": "cf323c5c6128725f942520a8445a41ab7897213f",
    "B": "e385681146f9d70b08466093060b8cc8204c65a8",
}
PROTOCOL_VERSION = "d9-c5-review-v1"
DISPATCHER_VERSION = "d9-c6-a2-dispatcher-v1"
REQUIRED_RUNNER_IDENTITY = "desktop-u1amjdf\\sunqinghw"
FROZEN_INVENTORY_A = frozenset({"analysis", "analyze", "callees", "callers", "definition", "dependencies", "dependents", "document", "evidence", "explain-module", "explain-symbol", "explain_module", "explain_symbol", "find-related-code", "find-related-documents", "find_related_code", "find_related_documents", "implemented", "index", "knowledge", "references", "requirements", "shard", "status", "subgraph", "symbol", "trace-evidence", "trace_evidence", "update"})
FROZEN_INVENTORY_B = FROZEN_INVENTORY_A | {"task-context"}
YES_REASONS = {"EXPLICIT_REASONING_USE", "TOOL_SELECTION_USE", "CODE_NAVIGATION_USE", "PATCH_ACTION_USE"}
NO_REASONS = {"NO_OBSERVABLE_USE", "FACT_DUPLICATED_BY_INDEPENDENT_RETRIEVAL", "FACT_NOT_ACTIONABLE_IN_TRACE"}
R3_REASON = "EVIDENCE_INTERPRETATION_DISAGREEMENT"
FROZEN_CASES = {
    "Multi-SWE-Bench__c__maintenance__bugfix__5adf8efc": ("facebook__zstd-3942", "372fddf4e6a6db6776b745f31c02a7c8c8dfc83f"),
    "Multi-SWE-Bench__c__maintenance__bugfix__a47dfbbf": ("facebook__zstd-2094", "6b4a3e019f8eeb3423065f7b24d790358e8cbc59"),
    "Multi-SWE-Bench__c__maintenance__bugfix__9a7be8ba": ("facebook__zstd-1532", "eb3a7a38270dc2a1f533c1b347f8b9b56e789f8f"),
    "Multi-SWE-Bench__c__maintenance__bugfix__2b5e5e23": ("facebook__zstd-1530", "54e9412ddd02ca4c6663d09dbf44f5a209a9a7ce"),
    "Multi-SWE-Bench__c__maintenance__bugfix__616889c2": ("facebook__zstd-1416", "c584e84e68109e6722e32cf0157a2c3706ca8f0d"),
    "Multi-SWE-Bench__c__maintenance__bugfix__21d59e43": ("facebook__zstd-836", "3128e03be69b9d3db054a3765ae1310c7b3666f6"),
}


class FormalDataError(ValueError):
    """Fail-closed validation failure; no effect aggregate may be produced."""

    def __init__(self, message: str, diagnostic: dict[str, Any] | None = None):
        super().__init__(message)
        self.diagnostic = diagnostic


_VALIDATED_ENVIRONMENT_SEAL = object()


@dataclass(frozen=True)
class ValidatedExecutionEnvironment:
    """Resolver-issued paths for the formal route only.

    The private seal deliberately prevents a plain mapping (or a casually
    constructed dataclass) from standing in for resolver output.
    """
    binding_sha256: str
    source_repository: str
    runtimes: tuple[tuple[str, str], ...]
    _seal: object = field(repr=False, compare=False, default=None)

    def runtime_for(self, arm: str) -> str:
        values = dict(self.runtimes)
        if arm not in values:
            raise FormalDataError("validated environment has no runtime for arm")
        return values[arm]

    def is_resolver_issued(self) -> bool:
        return self._seal is _VALIDATED_ENVIRONMENT_SEAL


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cell_id(case_id: str, arm: str, repeat: int) -> str:
    return f"{case_id}::{arm}::r{repeat}"


def artifact_key(cell_identifier: str) -> str:
    """Windows-safe stable directory identity; the full cell ID remains metadata."""
    return "cell-" + hashlib.sha256(cell_identifier.encode("utf-8")).hexdigest()[:24]


def generate_matrix(run_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate exactly the preregistered 6 × 2 × 3 plan without executing it."""
    if set(run_config["case_prompts"]) != set(FROZEN_CASES):
        raise FormalDataError("non-frozen or missing formal case")
    matrix = []
    for case_id, (original_id, base_commit) in sorted(FROZEN_CASES.items()):
        prompt = run_config["case_prompts"][case_id]
        for arm in ("A", "B"):
            for repeat in (1, 2, 3):
                cell = {
                    "cell_id": cell_id(case_id, arm, repeat), "case_id": case_id,
                    "original_instance_id": original_id, "base_commit": base_commit,
                    "arm": arm, "repeat": repeat, "runtime_sha": ARM_SHA[arm],
                    "model": run_config["model"], "agent_runtime": run_config["agent_runtime"],
                    "prompt_sha256": digest(prompt), "timeout_s": run_config["timeout_s"],
                    "command_inventory": sorted(run_config["inventories"][arm]),
                    "environment_fingerprint": run_config["environment_fingerprint"],
                    "evaluator_sha256": run_config["evaluator_sha256"],
                    "metric_pipeline_sha256": run_config["metric_pipeline_sha256"],
                    "review_protocol_version": PROTOCOL_VERSION,
                    "review_protocol_sha256": run_config["review_protocol_sha256"],
                }
                cell["provenance_sha256"] = digest(cell)
                matrix.append(cell)
    validate_matrix(matrix)
    return matrix


def validate_matrix(matrix: list[dict[str, Any]]) -> None:
    if len(matrix) != 36:
        raise FormalDataError("expected exactly 36 planned cells")
    ids = [c.get("cell_id") for c in matrix]
    if len(ids) != len(set(ids)):
        raise FormalDataError("duplicate planned cell")
    expected = {cell_id(case, arm, rep) for case in FROZEN_CASES for arm in ("A", "B") for rep in (1, 2, 3)}
    if set(ids) != expected:
        raise FormalDataError("missing, extra, or non-frozen planned cell")
    inventories = {}
    for cell in matrix:
        if cell["runtime_sha"] != ARM_SHA[cell["arm"]]:
            raise FormalDataError("wrong runtime")
        expected_instance, expected_commit = FROZEN_CASES.get(cell["case_id"], (None, None))
        if cell["case_id"] not in FROZEN_CASES or cell["repeat"] not in (1, 2, 3) or \
           cell.get("original_instance_id") != expected_instance or cell.get("base_commit") != expected_commit:
            raise FormalDataError("invalid formal cell")
        if cell.get("review_protocol_version") != PROTOCOL_VERSION:
            raise FormalDataError("review protocol schema drift")
        inventories[cell["arm"]] = set(cell["command_inventory"])
    if inventories.get("B", set()) - inventories.get("A", set()) != {"task-context"} or \
       inventories.get("A", set()) - inventories.get("B", set()):
        raise FormalDataError("command inventory delta is not task-context only")
    if inventories["A"] != FROZEN_INVENTORY_A or inventories["B"] != FROZEN_INVENTORY_B:
        raise FormalDataError("command inventory differs from frozen runtime baseline")


def write_manifest(root: Path, matrix: list[dict[str, Any]], run_config: dict[str, Any]) -> Path:
    validate_matrix(matrix)
    root.mkdir(parents=True, exist_ok=False)
    manifest = {"namespace": "D9-C5-FORMAL", "pilot_artifacts": 0, "matrix": matrix,
                "run_config_sha256": digest({k: v for k, v in run_config.items() if k != "case_prompts"}),
                "dispatch_contract_sha256": digest(run_config.get("dispatch", {})),
                "schema": "d9-c5-formal-manifest-v1"}
    path = root / "formal-manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return path


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("namespace") != "D9-C5-FORMAL" or manifest.get("pilot_artifacts") != 0 or manifest.get("schema") != "d9-c5-formal-manifest-v1":
        raise FormalDataError("invalid or pilot formal manifest")
    validate_matrix(manifest.get("matrix", []))
    return manifest


def load_canonical_bundle(root: Path) -> dict[str, Any]:
    """Load only the formal, hash-closed artifact class (never synthetic input)."""
    manifest_path, contract_path = root / "formal-manifest.json", root / "dispatch-contract.json"
    manifest, contract = json.loads(manifest_path.read_text(encoding="utf-8")), json.loads(contract_path.read_text(encoding="utf-8"))
    if manifest.get("experiment_id") != "D9-C5-FORMAL" or manifest.get("artifact_class") != "formal" or manifest.get("schema") != "d9-c5-formal-manifest-v1" or len(manifest.get("cells", [])) != 36:
        raise FormalDataError("not a canonical formal manifest")
    if contract.get("experiment_id") != "D9-C5-FORMAL" or contract.get("artifact_class") != "formal" or contract.get("schema") != "d9-c5-formal-dispatch-contract-v1":
        raise FormalDataError("not a canonical formal dispatch contract")
    if digest(manifest) != (root / "formal-manifest.sha256").read_text().strip() or digest(contract) != (root / "dispatch-contract.sha256").read_text().strip() or manifest.get("dispatch_contract_sha256") != digest(contract):
        raise FormalDataError("canonical artifact hash mismatch")
    prompts = {}
    for case, expected in manifest["prompt_config_sha256"].items():
        payload = json.loads((root / "prompts" / f"{case}.json").read_text(encoding="utf-8"))
        if digest(payload) != expected: raise FormalDataError("prompt artifact hash mismatch")
        prompts[case] = payload["problem_statement"]
    expected_cells = {cell_id(case, arm, repeat) for case in FROZEN_CASES for arm in ("A", "B") for repeat in (1, 2, 3)}
    if {cell.get("cell_id") for cell in manifest["cells"]} != expected_cells:
        raise FormalDataError("canonical formal matrix drift")
    for cell in manifest["cells"]:
        expected_original, expected_commit = FROZEN_CASES[cell["case_id"]]
        if cell.get("source_repo") != contract.get("source_repo") or cell.get("original_instance_id") != expected_original or cell.get("base_commit") != expected_commit or cell.get("runtime_sha") != contract["runtime_sha"].get(cell.get("arm")):
            raise FormalDataError("canonical formal cell identity drift")
    return {"manifest": manifest, "contract": contract, "prompts": prompts, "manifest_sha256": digest(manifest), "dispatch_sha256": digest(contract)}


def resolve_execution_environment(bundle: dict[str, Any], binding_path: Path) -> ValidatedExecutionEnvironment:
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    if binding.get("artifact_class") != "execution_environment_binding" or binding.get("schema") != "d9-c5-execution-binding-v1":
        raise FormalDataError("not an environment binding")
    forbidden = {"prompt", "model", "timeout", "case", "repeat", "inventory"}
    if forbidden & set(binding): raise FormalDataError("binding contains experiment semantics")
    source = binding.get("source_repositories", {}).get("facebook/zstd")
    runtimes = binding.get("runtime_roots", {})
    if not source or set(runtimes) != {"A", "B"}: raise FormalDataError("missing unique environment binding")
    source_path = Path(source["path"]); remote = _git(source_path, ["remote", "get-url", "origin"], source_path)
    if remote.returncode or remote.stdout.strip().replace(".git", "") != source["expected_origin"].replace(".git", ""): raise FormalDataError("source remote mismatch")
    required_commits = set(source.get("required_base_commits", []))
    manifest_commits = {cell["base_commit"] for cell in bundle["manifest"]["cells"]}
    if required_commits != manifest_commits:
        raise FormalDataError("binding base-commit contract mismatch")
    for commit in required_commits:
        if not _revision(source_path, source_path, commit): raise FormalDataError("base commit unavailable")
    resolved_runtimes = []
    for arm, expected in bundle["contract"]["runtime_sha"].items():
        item = runtimes[arm]; path = Path(item["path"])
        if item.get("expected_sha") != expected or _revision(path, path) != expected: raise FormalDataError("runtime identity mismatch")
        resolved_runtimes.append((arm, str(_canonical(path))))
    return ValidatedExecutionEnvironment(binding_sha256=digest(binding), source_repository=str(_canonical(source_path)),
                                         runtimes=tuple(sorted(resolved_runtimes)), _seal=_VALIDATED_ENVIRONMENT_SEAL)


def freeze_execution_binding(bundle: dict[str, Any], binding_path: Path, freeze_path: Path, frozen_at: str) -> dict[str, Any]:
    """Validate and locally freeze the sole machine-specific batch binding."""
    if freeze_path.exists():
        raise FormalDataError("execution binding freeze evidence already exists")
    environment = resolve_execution_environment(bundle, binding_path)
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    evidence = {
        "artifact_class": "execution_binding_freeze_evidence",
        "schema": "d9-c5-execution-binding-freeze-v1",
        "experiment_id": "D9-C5-FORMAL",
        "binding_id": binding.get("binding_id"),
        "binding_path": str(_canonical(binding_path)),
        "execution_binding_sha256": environment.binding_sha256,
        "source_logical_id": "facebook/zstd",
        "source_repository": environment.source_repository,
        "runtime_sha": bundle["contract"]["runtime_sha"],
        "runtime_roots": dict(environment.runtimes),
        "required_base_commits": sorted({cell["base_commit"] for cell in bundle["manifest"]["cells"]}),
        "frozen_at": frozen_at,
    }
    freeze_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(freeze_path, evidence)
    return evidence


def validate_frozen_execution_binding(bundle: dict[str, Any], binding_path: Path, freeze_path: Path) -> ValidatedExecutionEnvironment:
    """Reject changed, substituted, or semantically mismatched local bindings."""
    evidence = json.loads(freeze_path.read_text(encoding="utf-8"))
    if evidence.get("artifact_class") != "execution_binding_freeze_evidence" or evidence.get("schema") != "d9-c5-execution-binding-freeze-v1" or evidence.get("experiment_id") != "D9-C5-FORMAL":
        raise FormalDataError("invalid execution binding freeze evidence")
    if evidence.get("runtime_sha") != bundle["contract"]["runtime_sha"] or evidence.get("source_logical_id") != bundle["contract"]["source_repo"]:
        raise FormalDataError("execution binding freeze semantics mismatch")
    environment = resolve_execution_environment(bundle, binding_path)
    if environment.binding_sha256 != evidence.get("execution_binding_sha256"):
        raise FormalDataError("execution binding SHA mismatch")
    if environment.source_repository != evidence.get("source_repository") or dict(environment.runtimes) != evidence.get("runtime_roots"):
        raise FormalDataError("execution binding resolved path mismatch")
    return environment


def _runner_identity() -> str:
    result = subprocess.run(["whoami"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not result.stdout.strip():
        raise FormalDataError("runner identity unavailable")
    return result.stdout.strip().lower()


def _launcher_sha256() -> str:
    return hashlib.sha256(Path(__file__).with_name("formal_dispatch_launcher.py").read_bytes()).hexdigest()


def freeze_dispatch_context(bundle: dict[str, Any], environment: ValidatedExecutionEnvironment,
                            freeze_path: Path, attestation_root: Path, frozen_at: str) -> dict[str, Any]:
    if freeze_path.exists():
        raise FormalDataError("dispatch-context freeze evidence already exists")
    if _runner_identity() != REQUIRED_RUNNER_IDENTITY:
        raise FormalDataError("runner identity is not approved for dispatch-context freeze")
    evidence = {
        "artifact_class": "formal_dispatch_context_freeze_evidence",
        "schema": "d9-c6-approved-dispatch-v1",
        "experiment_id": "D9-C5-FORMAL",
        "required_runner_identity": REQUIRED_RUNNER_IDENTITY,
        "required_dispatch_context": "formal-approved",
        "execution_binding_sha256": environment.binding_sha256,
        "attestation_root": str(_canonical(attestation_root)),
        "launcher_sha256": _launcher_sha256(),
        "frozen_at": frozen_at,
    }
    freeze_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(freeze_path, evidence)
    return evidence


def validate_dispatch_context_freeze(bundle: dict[str, Any], environment: ValidatedExecutionEnvironment,
                                     freeze_path: Path) -> dict[str, Any]:
    evidence = json.loads(freeze_path.read_text(encoding="utf-8"))
    if evidence.get("artifact_class") != "formal_dispatch_context_freeze_evidence" or evidence.get("schema") != "d9-c6-approved-dispatch-v1" or evidence.get("experiment_id") != "D9-C5-FORMAL":
        raise FormalDataError("invalid dispatch-context freeze evidence")
    if evidence.get("required_runner_identity") != REQUIRED_RUNNER_IDENTITY or evidence.get("required_dispatch_context") != "formal-approved" or evidence.get("execution_binding_sha256") != environment.binding_sha256 or evidence.get("launcher_sha256") != _launcher_sha256():
        raise FormalDataError("dispatch-context freeze mismatch")
    return evidence


def create_dispatch_attestation(bundle: dict[str, Any], environment: ValidatedExecutionEnvironment,
                                context_freeze: Path, attestation_path: Path, issued_at: str) -> dict[str, Any]:
    freeze = validate_dispatch_context_freeze(bundle, environment, context_freeze)
    identity = _runner_identity()
    if identity != freeze["required_runner_identity"]:
        raise FormalDataError("runner identity is not approved for formal dispatch")
    root = Path(freeze["attestation_root"])
    if _canonical(attestation_path.parent) != _canonical(root) or attestation_path.exists():
        raise FormalDataError("attestation path is outside approved dispatch context")
    root.mkdir(parents=True, exist_ok=True)
    attestation = {
        "artifact_class": "formal_dispatch_attestation",
        "schema": "d9-c6-approved-dispatch-v1",
        "experiment_id": "D9-C5-FORMAL",
        "dispatch_context": "formal-approved",
        "dispatch_context_version": "d9-c6-approved-dispatch-v1",
        "runner_identity": identity,
        "execution_binding_sha256": environment.binding_sha256,
        "launcher_sha256": freeze["launcher_sha256"],
        "attestation_nonce": secrets.token_hex(16),
        "issued_at": issued_at,
    }
    _atomic_json(attestation_path, attestation)
    return attestation


def validate_dispatch_attestation(bundle: dict[str, Any], environment: ValidatedExecutionEnvironment,
                                  context_freeze: Path, attestation_path: Path) -> dict[str, Any]:
    freeze = validate_dispatch_context_freeze(bundle, environment, context_freeze)
    if _canonical(attestation_path.parent) != _canonical(Path(freeze["attestation_root"])):
        raise FormalDataError("attestation is outside approved dispatch context")
    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    identity = _runner_identity()
    if attestation.get("artifact_class") != "formal_dispatch_attestation" or attestation.get("schema") != freeze["schema"] or attestation.get("experiment_id") != "D9-C5-FORMAL" or attestation.get("dispatch_context") != "formal-approved" or attestation.get("runner_identity") != identity or identity != freeze["required_runner_identity"] or attestation.get("execution_binding_sha256") != environment.binding_sha256 or attestation.get("launcher_sha256") != freeze["launcher_sha256"] or not attestation.get("attestation_nonce"):
        raise FormalDataError("approved dispatch attestation validation failed")
    return {**attestation, "attestation_sha256": digest(attestation)}


def construct_formal_invocation(bundle: dict[str, Any], environment: ValidatedExecutionEnvironment,
                                dispatch_attestation: dict[str, Any], selected_cell_id: str) -> dict[str, Any]:
    """Formal-only invocation: raw paths are impossible at this interface."""
    cell = next((c for c in bundle["manifest"]["cells"] if c["cell_id"] == selected_cell_id), None)
    if cell is None or cell["runtime_sha"] != bundle["contract"]["runtime_sha"][cell["arm"]]:
        raise FormalDataError("formal cell/runtime mismatch")
    if not isinstance(environment, ValidatedExecutionEnvironment) or not environment.is_resolver_issued() or cell["source_repo"] != "facebook/zstd" or not dispatch_attestation.get("attestation_sha256"):
        raise FormalDataError("validated environment required")
    runtime_root = environment.runtime_for(cell["arm"])
    formal_cell = {**cell, "provenance_sha256": digest({"manifest_sha256": bundle["manifest_sha256"], "cell": cell})}
    return {"dispatcher_version": DISPATCHER_VERSION, "cell": formal_cell, "runtime_root": runtime_root,
            "approved_runtime_root": runtime_root, "target_repository": environment.source_repository,
            "approved_source_repository": environment.source_repository, "binding_sha256": environment.binding_sha256,
            "runner_identity": dispatch_attestation["runner_identity"], "dispatch_context": dispatch_attestation["dispatch_context"],
            "dispatch_attestation_sha256": dispatch_attestation["attestation_sha256"],
            "agent_argv": bundle["contract"]["agent_adapter"].split(), "prompt": bundle["prompts"][cell["case_id"]],
            "environment": {}, "timeout_s": bundle["contract"]["timeout_s"]}


def construct_invocation(manifest: dict[str, Any], run_config: dict[str, Any], selected_cell_id: str) -> dict[str, Any]:
    """Build an immutable invocation from a manifest cell, never CLI overrides."""
    matrix = generate_matrix(run_config)
    if matrix != manifest.get("matrix") or digest(run_config.get("dispatch", {})) != manifest.get("dispatch_contract_sha256"):
        raise FormalDataError("manifest/config identity drift")
    cell = next((c for c in matrix if c["cell_id"] == selected_cell_id), None)
    if cell is None:
        raise FormalDataError("selected cell is not in frozen manifest")
    dispatch = run_config.get("dispatch")
    if not isinstance(dispatch, dict) or not {"runtime_roots", "target_repositories", "agent_argv"} <= set(dispatch):
        raise FormalDataError("dispatch contract missing")
    runtime_root = dispatch["runtime_roots"].get(cell["arm"])
    target_repo = dispatch["target_repositories"].get(cell["case_id"])
    if not runtime_root or not target_repo:
        raise FormalDataError("dispatch target missing")
    return {"dispatcher_version": DISPATCHER_VERSION, "cell": cell, "runtime_root": runtime_root,
            "target_repository": target_repo, "agent_argv": list(dispatch["agent_argv"]),
            "prompt": run_config["case_prompts"][cell["case_id"]],
            "environment": deepcopy(dispatch.get("environment", {})), "timeout_s": cell["timeout_s"],
            "approved_source_repository": str(_canonical(Path(target_repo))),
            "approved_runtime_root": str(_canonical(Path(runtime_root)))}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _canonical(path: Path) -> Path:
    return path.resolve()


def _git(repo: Path, args: list[str], approved_repo: Path) -> subprocess.CompletedProcess:
    """Trust only one already-approved canonical repository for one command."""
    canonical_repo, canonical_approved = _canonical(repo), _canonical(approved_repo)
    if canonical_repo != canonical_approved:
        raise FormalDataError("Git path is outside the approved repository contract")
    return subprocess.run(["git", "-c", f"safe.directory={canonical_repo}", "-C", str(canonical_repo), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def _revision(repo: Path, approved_repo: Path, ref: str = "HEAD") -> str:
    result = _git(repo, ["rev-parse", ref], approved_repo)
    return result.stdout.strip() if result.returncode == 0 else ""


def _git_failure(stage: str, operation: str, result: subprocess.CompletedProcess,
                 invocation: dict[str, Any], workspace: Path | None = None) -> FormalDataError:
    """Make a fail-closed Git failure reproducible without writing formal evidence."""
    cell = invocation["cell"]
    argv = result.args if isinstance(result.args, list) else [str(result.args)]
    return FormalDataError(f"Git failure during {stage}", {
        "schema": "d9-c6-a4c-git-failure-v1",
        "stage": stage,
        "operation": operation,
        "cell_id": cell["cell_id"],
        "execution_binding_sha256": invocation.get("binding_sha256"),
        "source_repo": invocation.get("approved_source_repository"),
        "target_workspace": str(workspace) if workspace else None,
        "expected_base_commit": cell["base_commit"],
        "git_argv": argv,
        "git_returncode": result.returncode,
        "git_stdout": result.stdout,
        "git_stderr": result.stderr,
    })


def materialize_workspace(artifact_root: Path, invocation: dict[str, Any]) -> Path:
    """Create one clean detached workspace, permanently bound to one cell."""
    cell = invocation["cell"]
    source = Path(invocation["target_repository"])
    approved_source = Path(invocation["approved_source_repository"])
    workspace = artifact_root / "worktrees" / artifact_key(cell["cell_id"])
    workspace_root = artifact_root / "worktrees"
    if _canonical(source) != _canonical(approved_source) or _canonical(workspace.parent) != _canonical(workspace_root):
        raise FormalDataError("source or workspace path is outside dispatcher contract")
    if workspace.exists():
        raise FormalDataError("workspace already exists; cell cannot be reused", {
            "schema": "d9-c6-a4c-git-failure-v1", "stage": "detached-worktree materialization",
            "operation": "workspace existence check", "cell_id": cell["cell_id"],
            "execution_binding_sha256": invocation.get("binding_sha256"), "source_repo": str(approved_source),
            "target_workspace": str(workspace), "expected_base_commit": cell["base_commit"],
            "git_returncode": None, "git_stdout": "", "git_stderr": "workspace already exists",
        })
    source_status = _git(source, ["status", "--porcelain"], approved_source)
    if source_status.returncode != 0:
        raise _git_failure("source clean-state validation", "git status --porcelain", source_status, invocation, workspace)
    if source_status.stdout.strip():
        raise _git_failure("source clean-state validation", "git status --porcelain", source_status, invocation, workspace)
    base_check = _git(source, ["rev-parse", cell["base_commit"]], approved_source)
    if base_check.returncode != 0:
        raise _git_failure("frozen base-commit validation", "git rev-parse", base_check, invocation, workspace)
    added = _git(source, ["worktree", "add", "--detach", str(workspace), cell["base_commit"]], approved_source)
    if added.returncode != 0:
        raise _git_failure("detached-worktree materialization", "git worktree add", added, invocation, workspace)
    workspace_head = _git(workspace, ["rev-parse", "HEAD"], workspace)
    workspace_status = _git(workspace, ["status", "--porcelain"], workspace)
    if workspace_head.returncode != 0:
        raise _git_failure("workspace HEAD validation", "git rev-parse HEAD", workspace_head, invocation, workspace)
    if workspace_head.stdout.strip() != cell["base_commit"]:
        raise _git_failure("workspace HEAD validation", "git rev-parse HEAD", workspace_head, invocation, workspace)
    if workspace_status.returncode != 0 or workspace_status.stdout.strip():
        raise _git_failure("workspace clean-state validation", "git status --porcelain", workspace_status, invocation, workspace)
    binding = artifact_root / "worktree-bindings" / f"{artifact_key(cell['cell_id'])}.json"
    binding.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(binding, {"cell_id": cell["cell_id"], "workspace": str(workspace), "base_commit": cell["base_commit"], "dispatcher_version": DISPATCHER_VERSION})
    return workspace


def prepare_cell_execution(artifact_root: Path, invocation: dict[str, Any]) -> dict[str, Any]:
    """The shared pre-Agent half of materialize-only and real dispatch."""
    cell = invocation["cell"]
    if cell["runtime_sha"] != ARM_SHA[cell["arm"]] or invocation.get("dispatcher_version") != DISPATCHER_VERSION:
        raise FormalDataError("runtime or dispatcher identity mismatch before Agent start")
    workspace = materialize_workspace(artifact_root, invocation)
    prepared = {**invocation, "target_repository": str(workspace), "approved_workspace": str(workspace)}
    return {"invocation": prepared, "workspace": workspace,
            "command_sha256": digest(prepared["agent_argv"]),
            "prompt_sha256": digest(prepared["prompt"]),
            "runtime_sha": cell["runtime_sha"], "base_commit": cell["base_commit"]}


def write_materialization_readiness(readiness_root: Path, prepared: dict[str, Any]) -> Path:
    """Record no-dispatch evidence outside the formal cells namespace."""
    cell = prepared["invocation"]["cell"]
    path = readiness_root / "readiness" / "materialization" / f"{artifact_key(cell['cell_id'])}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FormalDataError("readiness artifact already exists for cell")
    _atomic_json(path, {"schema": "d9-c6-a4b-materialization-v1", "cell_id": cell["cell_id"],
                        "workspace": str(prepared["workspace"]), "runtime_sha": prepared["runtime_sha"],
                        "base_commit": prepared["base_commit"], "command_sha256": prepared["command_sha256"],
                        "prompt_sha256": prepared["prompt_sha256"], "execution_binding_sha256": prepared["invocation"].get("binding_sha256"),
                        "runner_identity": prepared["invocation"].get("runner_identity"), "dispatch_context": prepared["invocation"].get("dispatch_context"),
                        "dispatch_attestation_sha256": prepared["invocation"].get("dispatch_attestation_sha256"),
                        "agent_invocation_count": 0})
    return path


def active_readiness_records(artifact_root: Path) -> list[dict[str, Any]]:
    """Only top-level materialization records are active; superseded records remain auditable."""
    directory = artifact_root / "readiness" / "materialization"
    if not directory.exists():
        return []
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))]


def supersede_pre_attestation_readiness(artifact_root: Path, source_repository: Path,
                                        cell_identifier: str, expected_binding_sha256: str,
                                        dispatch_attestation: dict[str, Any]) -> Path:
    """Retire exactly one zero-Agent readiness workspace from the pre-attestation contract."""
    key = artifact_key(cell_identifier)
    record_path = artifact_root / "readiness" / "materialization" / f"{key}.json"
    invalidation = artifact_root / "readiness" / "invalidation" / f"{key}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    workspace = Path(record.get("workspace", ""))
    expected_workspace = artifact_root / "worktrees" / key
    cell_root = artifact_root / "cells" / key
    missing_attestation = not record.get("runner_identity") and not record.get("dispatch_context") and not record.get("dispatch_attestation_sha256")
    if record.get("schema") != "d9-c6-a4b-materialization-v1" or record.get("cell_id") != cell_identifier or record.get("agent_invocation_count") != 0 or record.get("execution_binding_sha256") != expected_binding_sha256 or not missing_attestation:
        raise FormalDataError("readiness is not eligible for pre-attestation supersession")
    if cell_root.exists() or invalidation.exists() or _canonical(workspace) != _canonical(expected_workspace) or not workspace.exists():
        raise FormalDataError("readiness workspace or formal-artifact binding is unsafe")
    workspace_head = _git(workspace, ["rev-parse", "HEAD"], workspace)
    workspace_status = _git(workspace, ["status", "--porcelain"], workspace)
    if workspace_head.returncode != 0 or workspace_head.stdout.strip() != record.get("base_commit") or workspace_status.returncode != 0 or workspace_status.stdout.strip():
        raise FormalDataError("readiness workspace is not a clean frozen worktree")
    removed = _git(source_repository, ["worktree", "remove", str(workspace)], source_repository)
    if removed.returncode != 0:
        invocation = {"cell": {"cell_id": cell_identifier, "base_commit": record["base_commit"]},
                      "binding_sha256": expected_binding_sha256, "approved_source_repository": str(source_repository)}
        raise _git_failure("pre-attestation workspace retirement", "git worktree remove", removed, invocation, workspace)
    archive = record_path.parent / "superseded" / record_path.name
    archive.parent.mkdir(parents=True, exist_ok=True)
    os.replace(record_path, archive)
    invalidation.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(invalidation, {
        "schema": "d9-c6-a4f-r2-pre-attestation-supersession-v1",
        "reason": "PRE_ATTESTATION_CONTRACT",
        "cell_id": cell_identifier,
        "execution_binding_sha256": expected_binding_sha256,
        "original_readiness_sha256": digest(record),
        "original_readiness_archive": str(archive),
        "retired_workspace": str(workspace),
        "agent_invocation_count": 0,
        "formal_runs": 0,
        "dispatch_attestation_sha256": dispatch_attestation["attestation_sha256"],
    })
    return invalidation


def dispatch_cell(artifact_root: Path, invocation: dict[str, Any], executor) -> dict[str, Any]:
    """Execute exactly once through an injected executor and atomically record it.

    ``executor`` is the sole runtime boundary: production will use a subprocess
    adapter; qualification injects a fake. It must return raw strings/events and
    never receives a caller-overridable cell field.
    """
    cell = invocation["cell"]
    cell_root = artifact_root / "cells" / artifact_key(cell["cell_id"])
    if cell_root.exists():
        raise FormalDataError("completed or incomplete cell cannot be rerun")
    prepared = prepare_cell_execution(artifact_root, invocation)
    invocation, workspace = prepared["invocation"], prepared["workspace"]
    raw_root = cell_root / "raw"
    raw_root.mkdir(parents=True)
    started = time.time()
    outcome = executor(deepcopy(invocation))  # exactly one call: no implicit retry
    ended = time.time()
    for name in ("stdout", "stderr", "raw_trace"):
        value = outcome.get(name)
        if not isinstance(value, str):
            _atomic_json(cell_root / "incomplete.json", {"state": "infrastructure_failure", "reason": f"missing {name}", "attempt_count": 1})
            return {"state": "incomplete", "attempt_count": 1}
        (raw_root / f"{name}.txt").write_text(value, encoding="utf-8")
    metadata = {"cell_id": cell["cell_id"], "runtime_sha": cell["runtime_sha"], "execution_binding_sha256": invocation.get("binding_sha256"), "runner_identity": invocation.get("runner_identity"), "dispatch_context": invocation.get("dispatch_context"), "dispatch_attestation_sha256": invocation.get("dispatch_attestation_sha256"), "attempt_count": 1,
                "started_at": started, "ended_at": ended, "exit_code": outcome.get("exit_code"),
                "completion_state": outcome.get("completion_state"), "workspace": str(workspace), "invocation_sha256": digest(invocation)}
    _atomic_json(raw_root / "metadata.json", metadata)
    if outcome.get("infrastructure_failure"):
        _atomic_json(cell_root / "incomplete.json", {"state": "infrastructure_failure", "attempt_count": 1})
        return {"state": "incomplete", "attempt_count": 1}
    try:
        normalized = normalize_trace(cell, str(raw_root / "raw_trace.txt"), outcome.get("events"),
                                     outcome.get("final_patch", ""), outcome.get("final_result", ""))
        _atomic_json(cell_root / "normalized-trace.json", normalized)
        record = {"cell_id": cell["cell_id"], "provenance_sha256": cell["provenance_sha256"], "execution_binding_sha256": invocation.get("binding_sha256"), "runner_identity": invocation.get("runner_identity"), "dispatch_context": invocation.get("dispatch_context"), "dispatch_attestation_sha256": invocation.get("dispatch_attestation_sha256"),
                  "dispatcher_version": DISPATCHER_VERSION, "attempt_count": 1, "trace_parse_pass": True,
                  "raw_artifacts": str(raw_root), "normalized_trace": str(cell_root / "normalized-trace.json"),
                  "returned_fact_ids": [f["fact_id"] for f in normalized["returned_facts"]],
                  "final_reviews": [], "machine_metrics": {}, "evaluator": None,
                  "execution": metadata}
        _atomic_json(cell_root / "cell-record.json", record)
        return {"state": "completed", "attempt_count": 1, "record": record}
    except Exception as exc:
        _atomic_json(cell_root / "incomplete.json", {"state": "normalization_failure", "reason": str(exc), "attempt_count": 1})
        return {"state": "incomplete", "attempt_count": 1}


def subprocess_executor(invocation: dict[str, Any]) -> dict[str, Any]:
    """The sole production adapter; called only by formal_runner --execute."""
    runtime = Path(invocation["runtime_root"])
    target = Path(invocation["target_repository"])
    expected_runtime = invocation["cell"]["runtime_sha"]
    expected_target = invocation["cell"]["base_commit"]
    if _canonical(runtime) != _canonical(Path(invocation["approved_runtime_root"])) or _canonical(target) != _canonical(Path(invocation["approved_workspace"])):
        return {"stdout": "", "stderr": "runtime or workspace path mismatch", "raw_trace": "", "events": [], "completion_state": "preflight_failed", "infrastructure_failure": True}
    if _revision(runtime, runtime) != expected_runtime or _revision(target, target) != expected_target:
        return {"stdout": "", "stderr": "runtime or target revision mismatch", "raw_trace": "", "events": [], "completion_state": "preflight_failed", "infrastructure_failure": True}
    environment = {**os.environ, **invocation["environment"], "PYTHONPATH": str(runtime / "src")}
    try:
        result = subprocess.run(invocation["agent_argv"], cwd=target, input=invocation["prompt"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=invocation["timeout_s"], env=environment)
        raw_trace = result.stdout
        events = []
        for line in raw_trace.splitlines():
            try:
                decoded = json.loads(line)
                if isinstance(decoded, dict): events.append(decoded)
            except json.JSONDecodeError:
                continue
        return {"stdout": result.stdout, "stderr": result.stderr, "raw_trace": raw_trace, "events": events,
                "exit_code": result.returncode, "completion_state": "completed"}
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""; stderr = exc.stderr or ""
        if isinstance(stdout, bytes): stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes): stderr = stderr.decode("utf-8", "replace")
        return {"stdout": stdout, "stderr": stderr, "raw_trace": stdout, "events": [],
                "completion_state": "timeout", "infrastructure_failure": True}
def preflight_execution(matrix: list[dict[str, Any]], artifact_root: Path, current_cell_id: str | None = None) -> dict[str, Any]:
    """Validate a frozen manifest before dispatch; never starts an Agent."""
    validate_matrix(matrix)
    if artifact_root.exists():
        if current_cell_id:
            cell_root = artifact_root / "cells" / artifact_key(current_cell_id)
            if cell_root.exists():
                raise FormalDataError("current cell already completed or incomplete")
        manifest = artifact_root / "formal-manifest.json"
        if manifest.exists():
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            if saved.get("namespace") != "D9-C5-FORMAL" or saved.get("matrix") != matrix:
                raise FormalDataError("existing manifest drift")
    arm_counts = Counter(c["arm"] for c in matrix)
    case_counts = Counter(c["case_id"] for c in matrix)
    return {"state": "EXECUTION_READY", "agent_invocation_count": 0,
            "planned_cells": len(matrix), "arm_counts": dict(arm_counts),
            "case_counts": dict(case_counts), "runtime_sha": ARM_SHA,
            "artifact_schema": {"manifest": "d9-c5-formal-manifest-v1",
                                "trace": "d9-c5-normalized-trace-v1",
                                "packet": "d9-c5-blinded-packet-v1",
                                "review": PROTOCOL_VERSION}}


def preflight_formal_execution(bundle: dict[str, Any], artifact_root: Path, current_cell_id: str) -> dict[str, Any]:
    """Preflight the canonical formal route without accepting raw path config."""
    cells = bundle["manifest"]["cells"]
    if not any(cell["cell_id"] == current_cell_id for cell in cells):
        raise FormalDataError("selected cell is not in canonical formal manifest")
    if artifact_root.exists():
        cell_root = artifact_root / "cells" / artifact_key(current_cell_id)
        if cell_root.exists():
            raise FormalDataError("current cell already completed or incomplete")
    return {"state": "EXECUTION_READY", "agent_invocation_count": 0,
            "planned_cells": len(cells), "binding_required": True,
            "manifest_sha256": bundle["manifest_sha256"],
            "dispatch_sha256": bundle["dispatch_sha256"]}


def normalize_trace(cell: dict[str, Any], raw_trace_pointer: str, events: list[dict[str, Any]],
                    final_patch: str, final_result: str) -> dict[str, Any]:
    """Retain ordered raw-derived events and the fact payload needed by reviewers."""
    if "PILOT_ONLY" in raw_trace_pointer:
        raise FormalDataError("pilot artifact cannot become formal evidence")
    ordered = []
    facts = []
    for sequence, event in enumerate(events):
        item = {"sequence": sequence, "kind": event.get("kind"), "tool": event.get("tool"),
                "detail": deepcopy(event.get("detail", {})), "timestamp": event.get("timestamp")}
        ordered.append(item)
        payload = item["detail"].get("result")
        if isinstance(payload, dict) and payload.get("version") == "task-context-v0":
            for fact in payload.get("facts", []):
                if "fact_id" not in fact:
                    raise FormalDataError("returned fact lacks stable identity")
                facts.append({"fact_id": fact["fact_id"], "payload": deepcopy(fact), "returned_sequence": sequence})
    counts = Counter(e["tool"] for e in ordered if e["kind"] == "tool_use")
    return {"schema": "d9-c5-normalized-trace-v1", "internal_cell_id": cell["cell_id"],
            "raw_trace_pointer": raw_trace_pointer, "ordered_events": ordered,
            "tool_counts": dict(counts), "returned_facts": facts,
            "final_patch": final_patch, "final_result": final_result}


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        forbidden = {"arm", "runtime_sha", "correctness", "evaluator", "other_arm", "reviewer", "provenance_sha256"}
        return {k: _scrub(v) for k, v in value.items() if k not in forbidden}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, str):
        return value.replace("pl-arm-a", "runtime").replace("pl-arm-b", "runtime").replace("PILOT_ONLY", "REJECTED")
    return value


def make_packet(cell: dict[str, Any], normalized: dict[str, Any], fact: dict[str, Any], problem_statement: str) -> dict[str, Any]:
    if cell["arm"] != "B":
        raise FormalDataError("only B-arm returned facts are reviewable")
    review_id = "blind-" + digest({"cell": cell["cell_id"], "fact": fact["fact_id"]})[:20]
    packet = {"schema": "d9-c5-blinded-packet-v1", "blinded_review_id": review_id,
              "fact_id": fact["fact_id"], "problem_statement": problem_statement,
              "returned_fact": _scrub(fact), "relevant_trace": _scrub(normalized["ordered_events"]),
              "final_patch": normalized["final_patch"], "final_explanation": normalized["final_result"]}
    assert_packet_blind(packet)
    return packet


def assert_packet_blind(packet: dict[str, Any]) -> None:
    blob = json.dumps(packet, sort_keys=True).lower()
    forbidden = ("\"arm\"", "runtime_sha", ARM_SHA["A"].lower(), ARM_SHA["B"].lower(),
                 "correctness", "gold_context", "reference_patch", "test_patch", "r1_verdict", "r2_verdict")
    if any(token in blob for token in forbidden):
        raise FormalDataError("blinded packet leaks forbidden metadata")


def validate_review(record: dict[str, Any]) -> None:
    required = {"blinded_review_id", "fact_id", "reviewer_role", "verdict", "reasons", "schema_version", "submitted_at", "evidence_note", "trace_references"}
    if not required <= set(record) or record.get("schema_version") != PROTOCOL_VERSION:
        raise FormalDataError("invalid reviewer schema")
    if any(k in record for k in ("arm", "runtime_sha")):
        raise FormalDataError("review record leaks arm")
    role, verdict, reasons = record["reviewer_role"], record["verdict"], set(record["reasons"])
    if role not in {"R1", "R2"} or verdict not in {"YES", "NO"}:
        raise FormalDataError("invalid independent reviewer verdict")
    if not reasons <= (YES_REASONS if verdict == "YES" else NO_REASONS):
        raise FormalDataError("invalid reason enum")


def finalize_review(records: list[dict[str, Any]], adjudication: dict[str, Any] | None = None) -> dict[str, Any]:
    for record in records:
        validate_review(record)
    if {r["reviewer_role"] for r in records} != {"R1", "R2"} or len(records) != 2:
        return {"state": "INCOMPLETE"}
    if len({r["blinded_review_id"] for r in records}) != 1 or len({r["fact_id"] for r in records}) != 1:
        raise FormalDataError("mixed review units")
    by_role = {r["reviewer_role"]: r for r in records}
    if by_role["R1"]["verdict"] == by_role["R2"]["verdict"]:
        if adjudication is not None:
            raise FormalDataError("R3 cannot override agreement")
        return {"state": "FINAL", "fact_id": by_role["R1"]["fact_id"], "final_verdict": by_role["R1"]["verdict"]}
    if adjudication is None:
        return {"state": "ADJUDICATION_REQUIRED"}
    needed = {"blinded_review_id", "fact_id", "reviewer_role", "r1_verdict", "r2_verdict", "final_verdict", "reasons", "schema_version", "submitted_at", "evidence_note", "trace_references"}
    if not needed <= set(adjudication) or adjudication["reviewer_role"] != "R3" or adjudication["schema_version"] != PROTOCOL_VERSION:
        raise FormalDataError("invalid adjudication schema")
    if adjudication["r1_verdict"] != by_role["R1"]["verdict"] or adjudication["r2_verdict"] != by_role["R2"]["verdict"] or adjudication["final_verdict"] not in {"YES", "NO"} or set(adjudication["reasons"]) != {R3_REASON}:
        raise FormalDataError("invalid adjudication")
    return {"state": "FINAL", "fact_id": adjudication["fact_id"], "final_verdict": adjudication["final_verdict"]}


def completeness_gate(matrix: list[dict[str, Any]], records: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        validate_matrix(matrix)
        expected = {c["cell_id"]: c for c in matrix}
        if len(records) != 36 or len({r.get("cell_id") for r in records}) != 36 or set(r.get("cell_id") for r in records) != set(expected):
            raise FormalDataError("formal records incomplete or duplicate")
        for record in records:
            if record.get("marking") == "PILOT_ONLY" or "PILOT_ONLY" in str(record):
                raise FormalDataError("pilot artifact in formal aggregation")
            cell = expected[record["cell_id"]]
            if record.get("provenance_sha256") != cell["provenance_sha256"] or record.get("dispatcher_version") != DISPATCHER_VERSION or record.get("attempt_count") != 1 or not record.get("trace_parse_pass") or "evaluator" not in record:
                raise FormalDataError("record provenance, trace, or evaluator missing")
            returned = set(record.get("returned_fact_ids", []))
            reviewed = set()
            for final in record.get("final_reviews", []):
                if final.get("state") != "FINAL":
                    raise FormalDataError("review coverage incomplete")
                reviewed.add(final.get("fact_id"))
            if cell["arm"] == "B" and reviewed != returned:
                raise FormalDataError("reviewed facts do not match returned facts")
        return {"state": "PASS", "planned_cells": 36, "valid_records": 36}
    except FormalDataError as exc:
        return {"state": "FORMAL_DATA_INCOMPLETE", "reason": str(exc)}


def aggregate(matrix: list[dict[str, Any]], records: list[dict[str, Any]]) -> dict[str, Any]:
    gate = completeness_gate(matrix, records)
    if gate["state"] != "PASS":
        raise FormalDataError("aggregate prohibited before completeness gate")
    human_yes = sum(1 for r in records for f in r.get("final_reviews", []) if f.get("final_verdict") == "YES")
    machine = {"retrieval_replacement_events": sum(r["machine_metrics"].get("retrieval_replacement_events", 0) for r in records),
               "redundant_read_events": sum(r["machine_metrics"].get("redundant_read_events", 0) for r in records),
               "grep_calls": sum(r["machine_metrics"].get("grep_calls", 0) for r in records),
               "read_calls": sum(r["machine_metrics"].get("read_calls", 0) for r in records)}
    return {"gate": gate, "human": {"consumed_fact_count": human_yes}, "machine": machine}
