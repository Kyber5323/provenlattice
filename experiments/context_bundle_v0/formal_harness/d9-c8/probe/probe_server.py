"""Minimal stdio MCP probe server (feasibility probe only, NOT formal evidence)."""
import json
import sys

TOOL = {
    "name": "repo_probe_ping",
    "description": "Ping a repository probe service and echo back a token.",
    "inputSchema": {"type": "object",
                    "properties": {"token": {"type": "string", "description": "Any string to echo"}},
                    "required": []},
}


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


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
                         "serverInfo": {"name": "repo-probe", "version": "0.1.0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [TOOL]}})
    elif method == "tools/call":
        args = (req.get("params") or {}).get("arguments") or {}
        send({"jsonrpc": "2.0", "id": rid,
              "result": {"content": [{"type": "text", "text": "PONG:" + str(args.get("token", ""))}]}})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid,
              "error": {"code": -32601, "message": "method not found: " + str(method)}})
