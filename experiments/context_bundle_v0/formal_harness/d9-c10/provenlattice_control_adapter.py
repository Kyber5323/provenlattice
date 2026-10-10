"""D9-C10 dual-mode ProvenLattice MCP adapter.

Identical MCP/tool surface as the frozen C8-A adapter (same tool entry served
from the C8-A contract).  The ONLY arm-dependent configuration is the response
source:

  PL_C10_RESPONSE_SOURCE=bridge   transparent bridge to `python -m provenlattice`
                                  (Arm B — real task-context-v0 facts)
  PL_C10_RESPONSE_SOURCE=control  `task-context` requests receive the frozen
                                  deterministic CONTROL payload (Arm A — no
                                  repository semantic facts); every other
                                  command is bridged identically so the
                                  repository-analysis infrastructure stays
                                  symmetric.

No command recommendation, no auto task-context, no rewriting, no case routing,
no fallback.  Bridge stdout is returned byte-preserving.
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


def load_control_payload() -> dict:
    path = os.environ.get("PL_C10_CONTROL_PAYLOAD", "")
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return doc["payload"]


def control_response(arguments: dict) -> dict:
    payload = load_control_payload()
    args = arguments.get("args") or []
    anchor = next((str(a) for a in args if not str(a).startswith("--")), None)
    payload = dict(payload)
    payload["anchor"] = anchor
    return {"content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
            "isError": False}


def bridge(arguments: dict) -> dict:
    command = arguments.get("command")
    extra = arguments.get("args") or []
    if not isinstance(command, str) or not command.strip():
        return {"content": [{"type": "text", "text": "adapter: missing command"}], "isError": True}
    if not isinstance(extra, list):
        extra = [str(extra)]
    argv = [sys.executable, "-m", "provenlattice", command, *[str(a) for a in extra]]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=COMMAND_TIMEOUT_S,
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
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def main() -> None:
    contract_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PL_MCP_CONTRACT", "")
    contract = json.loads(Path(contract_path).read_text(encoding="utf-8"))
    tool_entry = contract["mechanism"]["tool_entry_frozen_bytes"]
    server_name = contract["mechanism"]["server_name"]
    source = os.environ.get("PL_C10_RESPONSE_SOURCE", "bridge")
    if source not in ("bridge", "control"):
        raise SystemExit(f"unknown PL_C10_RESPONSE_SOURCE: {source}")

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
            arguments = (req.get("params") or {}).get("arguments") or {}
            if source == "control" and arguments.get("command") == "task-context":
                send({"jsonrpc": "2.0", "id": rid, "result": control_response(arguments)})
            else:
                send({"jsonrpc": "2.0", "id": rid, "result": bridge(arguments)})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": "method not found: " + str(method)}})


if __name__ == "__main__":
    main()
