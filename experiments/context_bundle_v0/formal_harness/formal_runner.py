"""Formal-harness planning entry point.

It materializes only a frozen 36-cell manifest.  Agent execution is deliberately
not available during D9-C5-C qualification; a later, separately authorized
execution task must consume this manifest without changing it.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from formal_harness import generate_matrix, preflight_execution, write_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="D9-C5 formal manifest dry-run")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute", action="store_true", help="enter execution-ready state after preflight; never dispatches an Agent")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    matrix = generate_matrix(config)
    path = write_manifest(args.output, matrix, config)
    gate = preflight_execution(matrix, args.output)
    mode = "EXECUTION_READY" if args.execute else "DRY_RUN"
    print(json.dumps({"mode": mode, "planned_cells": len(matrix), "manifest": str(path), "preflight": gate}))


if __name__ == "__main__":
    main()
