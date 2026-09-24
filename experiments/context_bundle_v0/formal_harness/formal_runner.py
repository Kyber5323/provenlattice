"""Formal-harness planning entry point.

It materializes only a frozen 36-cell manifest.  Agent execution is deliberately
not available during D9-C5-C qualification; a later, separately authorized
execution task must consume this manifest without changing it.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from formal_harness import construct_invocation, dispatch_cell, generate_matrix, load_manifest, preflight_execution, subprocess_executor, write_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="D9-C5 formal manifest dry-run")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute", action="store_true", help="dispatch exactly one preflight-qualified manifest cell")
    parser.add_argument("--cell-id", help="the sole execution selector; all other cell fields come from manifest")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    matrix = generate_matrix(config)
    if not args.execute:
        path = write_manifest(args.output, matrix, config)
        print(json.dumps({"mode": "DRY_RUN", "planned_cells": len(matrix), "manifest": str(path)}))
        return
    if not args.cell_id:
        parser.error("--execute requires --cell-id")
    manifest = load_manifest(args.output / "formal-manifest.json")
    gate = preflight_execution(manifest["matrix"], args.output)
    invocation = construct_invocation(manifest, config, args.cell_id)
    result = dispatch_cell(args.output, invocation, subprocess_executor)
    print(json.dumps({"mode": "DISPATCHED", "preflight": gate, "cell_id": args.cell_id, "result": result}))


if __name__ == "__main__":
    main()
