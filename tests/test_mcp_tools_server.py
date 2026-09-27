"""P30: zero-dependency MCP server (JSON-RPC 2.0 / stdio) exposing village/tools.py.

No dependency on the official MCP SDK; standard library json/sys only. Tests drive
handle_request()/serve() directly, the same way stdio would.
"""
import importlib.util
import io
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mcp_tools_server", ROOT / "scripts/mcp-tools-server.py")
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)


class InitializeTests(unittest.TestCase):
    def test_initialize_reports_protocol_and_tool_capability(self):
        resp = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(resp["result"]["protocolVersion"], mcp.PROTOCOL_VERSION)
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_initialized_notification_has_no_response(self):
        self.assertIsNone(mcp.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}))


class ToolsListTests(unittest.TestCase):
    def test_every_tool_module_function_is_listed_with_a_schema(self):
        from village.tools import TOOLS
        resp = mcp.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertEqual(names, set(TOOLS))
        for tool in resp["result"]["tools"]:
            self.assertIn("inputSchema", tool)
            self.assertIn("required", tool["inputSchema"])


class ToolsCallTests(unittest.TestCase):
    def call(self, name, arguments):
        resp = mcp.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                   "params": {"name": name, "arguments": arguments}})
        return resp["result"]

    def test_successful_call_returns_json_text_content(self):
        result = self.call("subnet_info", {"cidr": "10.0.0.0/30"})
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["usable_hosts"], 2)

    def test_invalid_arguments_are_a_tool_error_not_a_protocol_error(self):
        result = self.call("subnet_info", {"cidr": "not-a-cidr"})
        self.assertTrue(result["isError"])
        self.assertIn("invalid CIDR", result["content"][0]["text"])

    def test_code_injection_attempt_is_rejected_as_a_tool_error(self):
        result = self.call("calc", {"expression": "__import__('os').system('id')"})
        self.assertTrue(result["isError"])

    def test_unknown_tool_name_is_a_tool_error(self):
        result = self.call("delete_everything", {})
        self.assertTrue(result["isError"])

    def test_missing_required_argument_is_a_tool_error_not_a_crash(self):
        result = self.call("calc", {})
        self.assertTrue(result["isError"])


class ProtocolEdgeCaseTests(unittest.TestCase):
    def test_unknown_method_is_a_json_rpc_error(self):
        resp = mcp.handle_request({"jsonrpc": "2.0", "id": 9, "method": "bogus/method"})
        self.assertEqual(resp["error"]["code"], -32601)

    def test_unknown_method_as_notification_has_no_response(self):
        self.assertIsNone(mcp.handle_request({"jsonrpc": "2.0", "method": "bogus/method"}))


class ServeStdioTests(unittest.TestCase):
    def test_serve_reads_newline_delimited_requests_and_writes_responses(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "hash_digest", "arguments": {"text": "hi"}}},
        ]
        input_stream = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
        output_stream = io.StringIO()
        mcp.serve(input_stream, output_stream)
        lines = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual(len(lines), 2)  # the notification produced no output line
        self.assertEqual(lines[0]["id"], 1)
        self.assertEqual(lines[1]["id"], 2)

    def test_malformed_json_line_yields_parse_error_and_serve_continues(self):
        input_stream = io.StringIO('not json\n{"jsonrpc": "2.0", "id": 5, "method": "initialize", "params": {}}\n')
        output_stream = io.StringIO()
        mcp.serve(input_stream, output_stream)
        lines = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual(lines[0]["error"]["code"], -32700)
        self.assertEqual(lines[1]["id"], 5)

    def test_blank_lines_are_skipped(self):
        input_stream = io.StringIO('\n\n{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}\n')
        output_stream = io.StringIO()
        mcp.serve(input_stream, output_stream)
        self.assertEqual(len(output_stream.getvalue().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
