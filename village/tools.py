"""Deterministic, pure computation tools: no network, no filesystem, no LLM.

Every function takes plain JSON-safe arguments and returns a JSON-safe result or
raises ValueError with a precise message. Nothing here ever calls eval()/exec() on
attacker- or model-supplied text; calc() parses a restricted arithmetic grammar via
``ast`` with a strict node allowlist.
"""
from __future__ import annotations

import ast
import hashlib
import ipaddress
import operator
import statistics
from typing import Any, Dict, List, Sequence

MAX_EXPRESSION_LENGTH = 200
MAX_NUMBERS = 256

_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left, right = _eval_node(node.left), _eval_node(node.right)
        if type(node.op) is ast.Pow and (abs(right) > 64 or abs(left) > 1e6):
            raise ValueError("exponent or base too large")
        try:
            return _BIN_OPS[type(node.op)](left, right)
        except ZeroDivisionError as exc:
            raise ValueError("division by zero") from exc
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError(f"unsupported expression element: {type(node).__name__}")


def calc(expression: str) -> Dict[str, Any]:
    """Evaluate one arithmetic expression (+ - * / // % **, parentheses, unary +/-).

    Never uses eval(); the AST is walked with an explicit node allowlist so no name,
    call, attribute, comprehension or string ever executes.
    """
    text = str(expression).strip()
    if not text:
        raise ValueError("expression is empty")
    if len(text) > MAX_EXPRESSION_LENGTH:
        raise ValueError(f"expression exceeds {MAX_EXPRESSION_LENGTH} characters")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid expression syntax: {exc.msg}") from exc
    result = _eval_node(tree)
    return {"expression": text, "result": result}


_UNIT_TABLE = {
    ("mib", "gib"): 1 / 1024, ("gib", "mib"): 1024,
    ("kib", "mib"): 1 / 1024, ("mib", "kib"): 1024,
    ("bytes", "mib"): 1 / (1024 ** 2), ("mib", "bytes"): 1024 ** 2,
    ("s", "min"): 1 / 60, ("min", "s"): 60,
    ("s", "h"): 1 / 3600, ("h", "s"): 3600,
    ("min", "h"): 1 / 60, ("h", "min"): 60,
    ("celsius", "fahrenheit"): None, ("fahrenheit", "celsius"): None,
}


def unit_convert(value: float, from_unit: str, to_unit: str) -> Dict[str, Any]:
    """Convert one numeric value between a fixed, documented set of unit pairs."""
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be numeric") from exc
    src, dst = str(from_unit).strip().lower(), str(to_unit).strip().lower()
    if src == dst:
        return {"value": amount, "from_unit": src, "to_unit": dst, "result": amount}
    if (src, dst) == ("celsius", "fahrenheit"):
        result = amount * 9 / 5 + 32
    elif (src, dst) == ("fahrenheit", "celsius"):
        result = (amount - 32) * 5 / 9
    elif (src, dst) in _UNIT_TABLE:
        result = amount * _UNIT_TABLE[(src, dst)]
    else:
        raise ValueError(f"unsupported unit pair: {src} -> {dst}")
    return {"value": amount, "from_unit": src, "to_unit": dst, "result": result}


def subnet_info(cidr: str) -> Dict[str, Any]:
    """Return network/broadcast/host-count facts for one IPv4 or IPv6 CIDR block."""
    try:
        network = ipaddress.ip_network(str(cidr).strip(), strict=False)
    except ValueError as exc:
        raise ValueError(f"invalid CIDR: {exc}") from exc
    usable = max(0, network.num_addresses - (2 if network.version == 4 and network.prefixlen < 31 else 0))
    return {
        "cidr": str(network), "version": network.version,
        "network_address": str(network.network_address),
        "broadcast_address": str(network.broadcast_address) if network.version == 4 else None,
        "netmask": str(network.netmask) if network.version == 4 else str(network.prefixlen),
        "prefixlen": network.prefixlen, "num_addresses": network.num_addresses,
        "usable_hosts": usable, "is_private": network.is_private,
    }


def hash_digest(text: str, algorithm: str = "sha256") -> Dict[str, Any]:
    """Return a hex digest of a string for provenance/evidence citations."""
    algo = str(algorithm).strip().lower()
    if algo not in ("sha256", "sha512"):
        raise ValueError("algorithm must be sha256 or sha512")
    payload = str(text).encode("utf-8")
    digest = hashlib.new(algo, payload).hexdigest()
    return {"algorithm": algo, "length_bytes": len(payload), "digest": digest}


def stats_summary(numbers: Sequence[float]) -> Dict[str, Any]:
    """Return median/mean/stdev/min/max for a bounded list of numbers."""
    if not isinstance(numbers, (list, tuple)) or not numbers:
        raise ValueError("numbers must be a non-empty list")
    if len(numbers) > MAX_NUMBERS:
        raise ValueError(f"numbers exceeds {MAX_NUMBERS} values")
    try:
        values = [float(n) for n in numbers]
    except (TypeError, ValueError) as exc:
        raise ValueError("every element must be numeric") from exc
    return {
        "count": len(values), "mean": statistics.fmean(values), "median": statistics.median(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
        "min": min(values), "max": max(values),
    }


TOOLS = {
    "calc": calc, "unit_convert": unit_convert, "subnet_info": subnet_info,
    "hash_digest": hash_digest, "stats_summary": stats_summary,
}


def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch to a named tool with keyword arguments; raises ValueError for anything invalid."""
    if name not in TOOLS:
        raise ValueError(f"unknown tool: {name}")
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    return TOOLS[name](**arguments)
