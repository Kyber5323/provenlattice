"""Approved local launcher for the D9-C5 formal runner.

This launcher is the only supported path that creates a dispatch attestation;
it is intended to be invoked from the approved external dispatch context.
"""
from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from formal_harness import (create_dispatch_attestation, load_canonical_bundle,
                            validate_dispatch_context_freeze,
                            validate_frozen_execution_binding)


def main() -> None:
    parser = argparse.ArgumentParser(description="D9-C5 approved formal dispatch launcher")
    parser.add_argument("--formal-root", required=True, type=Path)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--binding-freeze", required=True, type=Path)
    parser.add_argument("--dispatch-context-freeze", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cell-id", required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--execute", action="store_true")
    modes.add_argument("--materialize-only", action="store_true")
    modes.add_argument("--validate-invocation", action="store_true")
    args = parser.parse_args()
    bundle = load_canonical_bundle(args.formal_root)
    environment = validate_frozen_execution_binding(bundle, args.binding, args.binding_freeze)
    context = validate_dispatch_context_freeze(bundle, environment, args.dispatch_context_freeze)
    attestation = Path(context["attestation_root"]) / f"attestation-{secrets.token_hex(16)}.json"
    create_dispatch_attestation(bundle, environment, args.dispatch_context_freeze, attestation,
                                datetime.now(timezone.utc).isoformat())
    runner = Path(__file__).with_name("formal_runner.py")
    command = [sys.executable, str(runner), "--formal-root", str(args.formal_root), "--binding", str(args.binding),
               "--binding-freeze", str(args.binding_freeze), "--dispatch-context-freeze", str(args.dispatch_context_freeze),
               "--dispatch-attestation", str(attestation), "--output", str(args.output), "--cell-id", args.cell_id,
               "--execute" if args.execute else "--materialize-only" if args.materialize_only else "--validate-invocation"]
    result = subprocess.run(command)
    print(json.dumps({"dispatch_attestation": str(attestation), "runner_exit_code": result.returncode,
                      "agent_invocation_count": 0 if args.validate_invocation else None}))
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
