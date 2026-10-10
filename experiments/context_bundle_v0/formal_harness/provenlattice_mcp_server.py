"""D9-C8-B ProvenLattice MCP adapter.

Transparent bridge: MCP query -> `python -m provenlattice <command> <args...>`.
The single tool entry is served verbatim from the frozen C8-A contract (single
source of truth).  No command recommendation, no auto task-context, no result
summarization, no rewriting, no case routing, no fallback.  CLI stdout is
returned byte-preserving as the text content; stderr appears only on the
failure path when stdout is empty.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

COMMAND_TIMEOUT_S = 900


def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main() -> None:
    contract_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PL_MCP_CONTRACT", "")
    contract = json.loads(Path(contract_path).read_text(encoding="utf-8"))
    tool_entry = contract["mechanism"]["tool_entry_frozen_bytes"]
    server_name = contract["mechanism"]["server_name"]

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid,
                  "result": {"protocolVersion": (req.get("params") or {}).get("protocolVersion", "2024-11-05"),
                             "capabilities": {"tools": {}},
                             "serverInfo": {"name": server_name, "version": "1.0.0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [tool_entry]}})
        elif method == "tools/call":
            params = req.get("params") or {}
            args = params.get("arguments") or {}
            command = args.get("command")
            extra = args.get("args") or []
            if not isinstance(command, str) or not command.strip():
                send({"jsonrpc": "2.0", "id": rid,
                      "result": {"content": [{"type": "text", "text": "adapter: missing command"}],
                                 "isError": True}})
                continue
            if not isinstance(extra, list):
                extra = [str(extra)]
            argv = [sys.executable, "-m", "provenlattice", command, *[str(a) for a in extra]]
            try:
                completed = subprocess.run(argv, capture_output=True, text=True,
                                           encoding="utf-8", errors="replace",
                                           timeout=COMMAND_TIMEOUT_S,
                                           env={**os.environ})
                text = completed.stdout
                is_error = completed.returncode != 0
                if is_error and not text.strip():
                    text = completed.stderr
            except subprocess.TimeoutExpired:
                text = f"adapter: command timed out after {COMMAND_TIMEOUT_S}s"
                is_error = True
            except Exception as exc:  # spawn-level failure only
                text = f"adapter: {type(exc).__name__}: {exc}"
                is_error = True
            send({"jsonrpc": "2.0", "id": rid,
                  "result": {"content": [{"type": "text", "text": text}], "isError": is_error}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": "method not found: " + str(method)}})


if __name__ == "__main__":
    main()
