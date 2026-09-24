"""D9-C5 formal execution entry point.

The formal CLI deliberately accepts only hash-closed experiment artifacts and
one validated environment binding. Synthetic qualification helpers stay in
``formal_harness.py``; they are not an input mode of this runner.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from formal_harness import (FormalDataError, construct_formal_invocation, dispatch_cell,
                            load_canonical_bundle, preflight_formal_execution,
                            prepare_cell_execution, validate_frozen_execution_binding,
                            validate_dispatch_attestation,
                            subprocess_executor, write_materialization_readiness)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D9-C5 canonical formal runner")
    parser.add_argument("--formal-root", required=True, type=Path,
                        help="directory containing the hash-closed formal manifest, contract, and prompts")
    parser.add_argument("--binding", required=True, type=Path,
                        help="local execution-environment binding; raw runtime/repository paths are not CLI inputs")
    parser.add_argument("--binding-freeze", required=True, type=Path,
                        help="local SHA freeze evidence for the only binding allowed in this formal batch")
    parser.add_argument("--dispatch-context-freeze", required=True, type=Path,
                        help="local approved-dispatch context freeze evidence")
    parser.add_argument("--dispatch-attestation", required=True, type=Path,
                        help="wrapper-issued approved-dispatch attestation")
    parser.add_argument("--output", required=True, type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--execute", action="store_true", help="dispatch exactly one preflight-qualified manifest cell")
    modes.add_argument("--materialize-only", action="store_true", help="run the shared pre-Agent lifecycle and stop")
    modes.add_argument("--validate-invocation", action="store_true", help="resolve and construct one invocation without materializing or dispatching")
    parser.add_argument("--cell-id", required=True, help="the sole execution selector; all other cell fields come from the canonical manifest")
    args = parser.parse_args()
    if not args.execute and not args.materialize_only and not args.validate_invocation:
        parser.error("one execution mode is required")
    return args


def main() -> None:
    args = parse_args()
    bundle = load_canonical_bundle(args.formal_root)
    environment = validate_frozen_execution_binding(bundle, args.binding, args.binding_freeze)
    dispatch_attestation = validate_dispatch_attestation(bundle, environment, args.dispatch_context_freeze, args.dispatch_attestation)
    gate = preflight_formal_execution(bundle, args.output, args.cell_id)
    invocation = construct_formal_invocation(bundle, environment, dispatch_attestation, args.cell_id)
    if args.validate_invocation:
        print(json.dumps({"mode": "INVOCATION_VALIDATED_NO_DISPATCH", "preflight": gate, "cell_id": args.cell_id,
                          "binding_sha256": environment.binding_sha256, "runtime_root": invocation["runtime_root"],
                          "source_repository": invocation["target_repository"], "agent_invocation_count": 0,
                          "materialization_cells": 0, "runner_identity": invocation["runner_identity"],
                          "dispatch_context": invocation["dispatch_context"]}))
        return
    if args.materialize_only:
        prepared = prepare_cell_execution(args.output, invocation)
        readiness = write_materialization_readiness(args.output, prepared)
        print(json.dumps({"mode": "MATERIALIZED_NO_DISPATCH", "preflight": gate, "cell_id": args.cell_id, "readiness": str(readiness), "binding_sha256": environment.binding_sha256, "agent_invocation_count": 0}))
        return
    result = dispatch_cell(args.output, invocation, subprocess_executor)
    print(json.dumps({"mode": "DISPATCHED", "preflight": gate, "cell_id": args.cell_id, "binding_sha256": environment.binding_sha256, "result": result}))


if __name__ == "__main__":
    try:
        main()
    except FormalDataError as exc:
        print(json.dumps({"state": "FORMAL_PREPARE_FAILED", "message": str(exc),
                          "diagnostic": exc.diagnostic}), file=sys.stderr)
        raise SystemExit(2)
