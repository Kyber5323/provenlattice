"""Create local D9-C5 execution-binding freeze evidence after resolver validation."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from formal_harness import freeze_execution_binding, load_canonical_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze one validated local D9-C5 execution binding")
    parser.add_argument("--formal-root", required=True, type=Path)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    bundle = load_canonical_bundle(args.formal_root)
    evidence = freeze_execution_binding(bundle, args.binding, args.output,
                                        datetime.now(timezone.utc).isoformat())
    print(json.dumps({"execution_binding_sha256": evidence["execution_binding_sha256"],
                      "binding_id": evidence["binding_id"], "output": str(args.output),
                      "agent_invocation_count": 0, "materialization_cells": 0}))


if __name__ == "__main__":
    main()
