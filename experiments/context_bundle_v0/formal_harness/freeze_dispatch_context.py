"""Freeze the local approved-dispatch context for one D9-C5 batch."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from formal_harness import (freeze_dispatch_context, load_canonical_bundle,
                            validate_frozen_execution_binding)


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze a local approved dispatch context")
    parser.add_argument("--formal-root", required=True, type=Path)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--binding-freeze", required=True, type=Path)
    parser.add_argument("--attestation-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    bundle = load_canonical_bundle(args.formal_root)
    environment = validate_frozen_execution_binding(bundle, args.binding, args.binding_freeze)
    evidence = freeze_dispatch_context(bundle, environment, args.output, args.attestation_root,
                                       datetime.now(timezone.utc).isoformat())
    print(json.dumps({"required_runner_identity": evidence["required_runner_identity"],
                      "execution_binding_sha256": evidence["execution_binding_sha256"],
                      "output": str(args.output), "agent_invocation_count": 0}))


if __name__ == "__main__":
    main()
