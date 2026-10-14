#!/usr/bin/env python3
"""MCP stdio server exposing Antigravity's Google Search as a tool.

Tool: google_search(query) -> summary with source links, produced by a
search-only Gemini request (tools = [googleSearch]) through Antigravity Tools.
Standard library only; reuses run_search() from shim.py.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shim  # noqa: E402

SERVER_INFO = {"name": "antigravity-search", "version": "1.0.0"}
TOOL = {
    "name": "google_search",
    "description": (
        "Search the web with Google (via Antigravity Tools) and return a summary "
        "of current results with source links. Use it for recent information, "
        "documentation, release notes, error messages, or anything that may "
        "have changed after your training data."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Search query."}},
        "required": ["query"],
    },
}


def auth_header():
    return shim.antigravity_auth()


def reply(msg_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": msg_id}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req):
    method, msg_id, params = req.get("method"), req.get("id"), req.get("params") or {}
    if msg_id is None:
        return
    if method == "initialize":
        reply(msg_id, {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    elif method == "ping":
        reply(msg_id, {})
    elif method == "tools/list":
        reply(msg_id, {"tools": [TOOL]})
    elif method == "tools/call":
        if params.get("name") != TOOL["name"]:
            return reply(msg_id, error={"code": -32602, "message": f"unknown tool {params.get('name')}"})
        query = ((params.get("arguments") or {}).get("query") or "").strip()
        if not query:
            return reply(msg_id, {"content": [{"type": "text", "text": "query is required"}], "isError": True})
        try:
            text, _ = shim.run_search(query, auth_header())
            reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as e:
            reply(msg_id, {"content": [{"type": "text", "text": f"search failed: {e}"}], "isError": True})
    else:
        reply(msg_id, error={"code": -32601, "message": f"method not found: {method}"})


def main():
    shim.Config.upstream = os.environ.get("ANTIGRAVITY_URL", shim.Config.upstream).rstrip("/")
    shim.Config.search_model = os.environ.get("ANTIGRAVITY_SEARCH_MODEL", shim.Config.search_model)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            reply(None, error={"code": -32700, "message": "parse error"})
            continue
        handle(req)


if __name__ == "__main__":
    main()
