"""Offline checks for the Codex CLI brain and its MCP tool bridge."""

import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import brain
import codex_mcp_bridge as bridge
from config import Config


SCHEMA = {"type": "function", "function": {
    "name": "list_dir", "description": "List a directory.",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}


class CodexBrainTests(unittest.TestCase):
    def test_routing(self):
        cfg = Config(force_mock=False)
        b, model = brain.brain_for("codex", cfg)
        self.assertIsInstance(b, brain.CodexBrain)
        self.assertEqual(model, "default")
        b, model = brain.brain_for("codex/gpt-5.6-luna", cfg)
        self.assertEqual(model, "gpt-5.6-luna")
        self.assertEqual(b.reasoning_effort, "medium")
        self.assertIsInstance(brain.brain_for("anthropic/x", cfg)[0], brain.OpenRouterBrain)

    def test_transcript_includes_system_history_tools_and_current(self):
        text = brain.codex_transcript([
            {"role": "system", "content": "SYSTEM RULES"},
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "list_dir", "arguments": '{"path": "."}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "x" * 5000},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second question"},
        ])
        self.assertIn("SYSTEM RULES", text)
        self.assertIn('[tool call] list_dir({"path": "."})', text)
        self.assertIn("…[truncated]", text)
        self.assertLess(text.count("x"), 2100)
        history, current = text.split("<current_message>")
        self.assertIn("first question", history)
        self.assertIn("first answer", history)
        self.assertIn("second question", current)
        self.assertNotIn("second question", history)

    def test_command_isolates_config_and_wires_bridge(self):
        b = brain.CodexBrain(command="codex", workdir=Path("/vault"), reasoning_effort="low")
        endpoint = brain._ToolEndpoint(lambda n, a: "ok")
        self.addCleanup(endpoint.close)
        cmd = b._command("gpt-x", "/tmp/tools.json", endpoint)
        self.assertEqual(cmd[:2], ["codex", "exec"])
        for flag in ("--json", "--ephemeral", "--ignore-user-config"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("-s") + 1], "read-only")
        self.assertEqual(cmd[cmd.index("-C") + 1], "/vault")
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-x")
        joined = " ".join(cmd)
        self.assertIn('model_reasoning_effort="low"', joined)
        self.assertIn("codex_mcp_bridge.py", joined)
        self.assertIn(endpoint.url, joined)
        self.assertEqual(cmd[-1], "-")
        self.assertNotIn("-m", b._command("default", None, None))
        self.assertNotIn("mcp_servers", " ".join(b._command("default", None, None)))

    def test_endpoint_requires_token_and_runs_executor(self):
        seen = []
        endpoint = brain._ToolEndpoint(lambda n, a: seen.append((n, a)) or f"ran {n}")
        self.addCleanup(endpoint.close)

        def post(token):
            req = urllib.request.Request(
                endpoint.url, data=json.dumps({"name": "list_dir",
                                               "arguments": {"path": "."}}).encode(),
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())

        with self.assertRaises(urllib.error.HTTPError):
            post("wrong")
        self.assertEqual(post(endpoint.token), {"result": "ran list_dir"})
        self.assertEqual(seen, [("list_dir", {"path": "."})])

    def test_stream_parses_codex_events(self):
        events = [
            {"type": "thread.started"},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "hmm"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "One."}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Two."}},
            {"type": "turn.completed"},
        ]
        script = "import sys\nsys.stdin.read()\n" + "".join(
            f"print({json.dumps(json.dumps(e))})\n" for e in events)
        b = brain.CodexBrain(command=f"{sys.executable} -c {json.dumps(script)} --")
        with patch.object(brain.CodexBrain, "_command",
                          lambda self, *a: [sys.executable, "-c", script]):
            out = list(b.stream_with_tools([{"role": "user", "content": "hi"}], "default"))
        self.assertEqual(out, [("text", "One."), ("text", "\n\nTwo.")])

    def test_missing_cli_reports_cleanly(self):
        b = brain.CodexBrain(command="definitely-not-a-codex-binary")
        out = list(b.stream([{"role": "user", "content": "hi"}], "default"))
        self.assertIn("Codex CLI not found", out[0])


class BridgeTests(unittest.TestCase):
    def test_initialize_list_and_unknown(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump([SCHEMA], f)
        self.addCleanup(os.unlink, f.name)
        with patch.dict(os.environ, {"WB_TOOLS_FILE": f.name}):
            init = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-03-26"}})
            self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
            listed = bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool = listed["result"]["tools"][0]
        self.assertEqual(tool["name"], "list_dir")
        self.assertEqual(tool["inputSchema"]["properties"]["path"]["type"], "string")
        self.assertIsNone(bridge.handle({"jsonrpc": "2.0",
                                         "method": "notifications/initialized"}))
        self.assertEqual(bridge.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})
                         ["error"]["code"], -32601)

    def test_tools_call_forwards_to_endpoint(self):
        endpoint = brain._ToolEndpoint(lambda n, a: f"{n}:{a['path']}")
        self.addCleanup(endpoint.close)
        env = {"WB_TOOL_URL": endpoint.url, "WB_TOOL_TOKEN": endpoint.token}
        with patch.dict(os.environ, env):
            reply = bridge.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                   "params": {"name": "list_dir",
                                              "arguments": {"path": "_System"}}})
        self.assertEqual(reply["result"]["content"][0]["text"], "list_dir:_System")


if __name__ == "__main__":
    unittest.main()
