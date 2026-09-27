#!/usr/bin/env python3
"""Zero-dependency MCP server exposing the deterministic tools in village/tools.py.

Implements the Model Context Protocol's JSON-RPC 2.0 / stdio transport with only the
standard library: initialize, tools/list, tools/call. No network, no filesystem
access, no dependency on the official MCP SDK. Any MCP-compatible client (this
session, an editor, a future integration) can use it; AI Village residents do not
depend on it, since native tool-calling is not confirmed for the currently loaded
models — they call the same functions through the calc_operation action instead
(see village/actions.py and docs/analysis/MCP-GRAPHRAG-PLAN-2026-09-27.md).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from village.tools import TOOLS, call_tool

PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "ai-village-deterministic-tools", "version": "1.0.0"}

TOOL_SCHEMAS = {
    "calc": {
        "description": "Evaluate one arithmetic expression (+ - * / // % **, parentheses). No code execution.",
        "inputSchema": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
    },
    "unit_convert": {
        "description": "Convert a numeric value between a fixed set of unit pairs (MiB/GiB/KiB/bytes, s/min/h, Celsius/Fahrenheit).",
        "inputSchema": {"type": "object", "properties": {
            "value": {"type": "number"}, "from_unit": {"type": "string"}, "to_unit": {"type": "string"}},
            "required": ["value", "from_unit", "to_unit"]},
    },
    "subnet_info": {
        "description": "Return network/broadcast address, usable host count and privacy for an IPv4 or IPv6 CIDR block.",
        "inputSchema": {"type": "object", "properties": {"cidr": {"type": "string"}}, "required": ["cidr"]},
    },
    "hash_digest": {
        "description": "Return a SHA-256 or SHA-512 hex digest of a string, for provenance citations.",
        "inputSchema": {"type": "object", "properties": {
            "text": {"type": "string"}, "algorithm": {"type": "string", "enum": ["sha256", "sha512"]}},
            "required": ["text"]},
    },
    "stats_summary": {
        "description": "Return mean/median/stdev/min/max for a bounded list of numbers.",
        "inputSchema": {"type": "object", "properties": {
            "numbers": {"type": "array", "items": {"type": "number"}}}, "required": ["numbers"]},
    },
}
assert set(TOOL_SCHEMAS) == set(TOOLS), "every village.tools function needs an MCP schema entry"


def _error(code: int, message: str) -> Dict[str, Any]:
    return {"code": code, "message": message}


def handle_request(request: Dict[str, Any]) -> Dict[str, Any] | None:
    """Return one JSON-RPC response object, or None for a notification (no id)."""
    req_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    is_notification = "id" not in request

    if method == "initialize":
        result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}
    elif method == "notifications/initialized":
        return None
    elif method == "tools/list":
        result = {"tools": [{"name": name, **TOOL_SCHEMAS[name]} for name in sorted(TOOLS)]}
    elif method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            tool_result = call_tool(name, arguments)
        except ValueError as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except TypeError as exc:
            result = {"content": [{"type": "text", "text": f"invalid arguments: {exc}"}], "isError": True}
        else:
            result = {"content": [{"type": "text", "text": json.dumps(tool_result, ensure_ascii=False)}], "isError": False}
    else:
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "error": _error(-32601, f"method not found: {method}")}

    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def serve(input_stream=sys.stdin, output_stream=sys.stdout) -> None:
    """Read newline-delimited JSON-RPC requests from input_stream, write responses to output_stream."""
    for line in input_stream:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None, "error": _error(-32700, "invalid JSON")}
        else:
            response = handle_request(request) if isinstance(request, dict) else \
                {"jsonrpc": "2.0", "id": None, "error": _error(-32600, "invalid request")}
        if response is not None:
            output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            output_stream.flush()


if __name__ == "__main__":
    serve()
