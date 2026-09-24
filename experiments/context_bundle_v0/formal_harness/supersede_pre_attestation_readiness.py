"""Retire one zero-Agent pre-attestation readiness record through approved dispatch."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from formal_harness import (load_canonical_bundle, supersede_pre_attestation_readiness,
                            validate_dispatch_attestation,
                            validate_frozen_execution_binding)


def main() -> None:
    parser = argparse.ArgumentParser(description="Supersede one pre-attestation readiness workspace")
    parser.add_argument("--formal-root", required=True, type=Path)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--binding-freeze", required=True, type=Path)
    parser.add_argument("--dispatch-context-freeze", required=True, type=Path)
    parser.add_argument("--dispatch-attestation", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--cell-id", required=True)
    args = parser.parse_args()
    bundle = load_canonical_bundle(args.formal_root)
    environment = validate_frozen_execution_binding(bundle, args.binding, args.binding_freeze)
    attestation = validate_dispatch_attestation(bundle, environment, args.dispatch_context_freeze, args.dispatch_attestation)
    path = supersede_pre_attestation_readiness(args.artifact_root, Path(environment.source_repository), args.cell_id,
                                               environment.binding_sha256, attestation)
    print(json.dumps({"state": "PRE_ATTESTATION_READINESS_SUPERSEDED", "invalidation": str(path),
                      "cell_id": args.cell_id, "agent_invocation_count": 0, "formal_runs": 0}))


if __name__ == "__main__":
    main()
