"""Formal-harness planning entry point.

It materializes only a frozen 36-cell manifest.  Agent execution is deliberately
not available during D9-C5-C qualification; a later, separately authorized
execution task must consume this manifest without changing it.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from formal_harness import generate_matrix, write_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="D9-C5 formal manifest dry-run")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute", action="store_true", help="reserved; forbidden during qualification")
    args = parser.parse_args()
    if args.execute:
        parser.error("formal execution is not enabled by D9-C5-C qualification")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    matrix = generate_matrix(config)
    path = write_manifest(args.output, matrix, config)
    print(json.dumps({"mode": "DRY_RUN", "planned_cells": len(matrix), "manifest": str(path)}))


if __name__ == "__main__":
    main()
