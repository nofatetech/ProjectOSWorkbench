"""Stdio MCP server that exposes Workbench's chat tools to the Codex CLI.

Codex spawns this per chat turn (see brain.CodexBrain). It advertises the
turn's tool schemas and forwards every call to a localhost endpoint served by
the running app, so tools execute in-app with the normal confirm dialog and
inline markers. Stdlib only; newline-delimited JSON-RPC 2.0 per MCP stdio.

Env: WB_TOOLS_FILE (JSON list of OpenAI-style function schemas),
     WB_TOOL_URL (app endpoint), WB_TOOL_TOKEN (bearer secret for this turn).
"""

import json
import os
import sys
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2025-06-18"


def _tools() -> list[dict]:
    with open(os.environ["WB_TOOLS_FILE"], encoding="utf-8") as f:
        schemas = json.load(f)
    return [{"name": s["function"]["name"],
             "description": s["function"].get("description", ""),
             "inputSchema": s["function"].get("parameters") or {"type": "object"}}
            for s in schemas]


def _call(name: str, arguments: dict) -> str:
    req = urllib.request.Request(
        os.environ["WB_TOOL_URL"],
        data=json.dumps({"name": name, "arguments": arguments}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ['WB_TOOL_TOKEN']}"})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            return json.loads(r.read())["result"]
    except (urllib.error.URLError, OSError, ValueError, KeyError) as ex:
        return f"[workbench bridge error: {ex}]"


def handle(msg: dict):
    """Response dict for a request, or None for notifications."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": (msg.get("params") or {}).get(
                      "protocolVersion", PROTOCOL_VERSION),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "workbench", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": _tools()}
    elif method == "tools/call":
        params = msg.get("params") or {}
        text = _call(params.get("name", ""), params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except Exception as ex:  # keep serving; report on the request if possible
            reply = {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32603, "message": str(ex)}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
