"""Tests for the Parallel Search MCP client (offline, HTTP mocked)."""
import unittest
from unittest import mock

import config as config_mod
import websearch


class TestParseBody(unittest.TestCase):
    def test_plain_json(self):
        msg = websearch._parse_body('{"jsonrpc":"2.0","id":1}', "application/json")
        self.assertEqual(msg["id"], 1)

    def test_sse_framed(self):
        body = ('event: message\n'
                'data: {"jsonrpc":"2.0","id":1,"result":{}}\n\n')
        msg = websearch._parse_body(body, "text/event-stream")
        self.assertEqual(msg["result"], {})

    def test_sse_keeps_last_message(self):
        body = ('data: {"id":1}\n'
                'data: {"id":2}\n')
        msg = websearch._parse_body(body, "text/event-stream")
        self.assertEqual(msg["id"], 2)

    def test_empty_body(self):
        self.assertIsNone(websearch._parse_body("", "application/json"))

    def test_bad_json_raises(self):
        with self.assertRaises(websearch.MCPError):
            websearch._parse_body("not json", "application/json")


class TestCallTool(unittest.TestCase):
    def test_full_handshake(self):
        seq = iter([
            ({"jsonrpc": "2.0", "id": 1, "result": {}}, "S"),
            (None, "S"),
            ({"jsonrpc": "2.0", "id": 2,
              "result": {"content": [{"type": "text", "text": "line1"},
                                     {"type": "text", "text": "line2"}]}}, "S"),
        ])
        seen = []

        def fake_post(self2, payload, session=None):
            seen.append((payload, session))
            return next(seq)

        with mock.patch.object(websearch.MCPClient, "_post", new=fake_post):
            out = websearch.call_tool("web_search", {"objective": "x"},
                                      api_key="k")
        self.assertEqual(out, "line1\nline2")
        methods = [p.get("method") for p, _ in seen]
        self.assertEqual(methods, ["initialize", "notifications/initialized",
                                   "tools/call"])
        # session header propagated to later calls
        self.assertEqual(seen[1][1], "S")
        self.assertEqual(seen[2][1], "S")

    def test_rpc_error_raises(self):
        seq = iter([
            ({"id": 1, "result": {}}, None),
            (None, None),
            ({"error": {"message": "rate limited"}}, None),
        ])
        with mock.patch.object(websearch.MCPClient, "_post",
                               new=lambda s, p, session=None: next(seq)):
            with self.assertRaises(websearch.MCPError) as cm:
                websearch.call_tool("web_search", {})
        self.assertIn("rate limited", str(cm.exception))

    def test_is_error_result_raises(self):
        seq = iter([
            ({"id": 1, "result": {}}, None),
            (None, None),
            ({"result": {"isError": True,
                         "content": [{"type": "text", "text": "boom"}]}}, None),
        ])
        with mock.patch.object(websearch.MCPClient, "_post",
                               new=lambda s, p, session=None: next(seq)):
            with self.assertRaises(websearch.MCPError) as cm:
                websearch.call_tool("web_fetch", {})
        self.assertIn("boom", str(cm.exception))


class TestToolArgs(unittest.TestCase):
    def setUp(self):
        self.cfg = config_mod.Config({"parallel_api_key": "pk"})

    @mock.patch.object(websearch, "call_tool", return_value="results")
    def test_search_args(self, call_mock):
        out = websearch.search(self.cfg, "find zebras",
                               ["zebra facts", "zebra news"],
                               session_id="keirai-100-main" * 20)
        self.assertEqual(out, "results")
        name, args = call_mock.call_args[0][0], call_mock.call_args[0][1]
        self.assertEqual(name, "web_search")
        self.assertEqual(args["objective"], "find zebras")
        self.assertEqual(args["search_queries"], ["zebra facts", "zebra news"])
        self.assertLessEqual(len(args["session_id"]), 100)
        self.assertEqual(call_mock.call_args[1]["api_key"], "pk")

    @mock.patch.object(websearch, "call_tool", return_value="md")
    def test_fetch_args(self, call_mock):
        websearch.fetch(self.cfg, ["https://a.example", "https://b.example"],
                        objective="the price", full_content=True)
        args = call_mock.call_args[0][1]
        self.assertEqual(args["urls"], ["https://a.example", "https://b.example"])
        self.assertEqual(args["objective"], "the price")
        self.assertTrue(args["full_content"])

    @mock.patch.object(websearch, "call_tool", return_value="md")
    def test_fetch_defaults_omit_optionals(self, call_mock):
        websearch.fetch(self.cfg, ["https://a.example"])
        args = call_mock.call_args[0][1]
        self.assertNotIn("objective", args)
        self.assertNotIn("full_content", args)


if __name__ == "__main__":
    unittest.main()
