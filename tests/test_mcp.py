"""MCP server tests — FakeClient only (no network, KBs of RAM)."""
import base64
import io
import json
import sys

from gemini_web.mcp import MCPServer

# 1x1 transparent PNG (68 bytes)
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class FakeClient:
    def __init__(self):
        self.seen = {}

    def init(self):
        return {}

    def list_models(self):
        return [{"model_name": "gemini-flash", "display_name": "Flash",
                 "is_available": True}]

    def ask(self, prompt, timeout=None, model=None, thinking="low"):
        self.seen["ask"] = (prompt, model, thinking)
        return "reply:" + prompt

    def generate_image(self, prompt, dest, timeout=None, model=None,
                       full_size=True, thinking="low"):
        self.seen["gen"] = (prompt, full_size, model, thinking)
        with open(dest, "wb") as f:
            f.write(TINY_PNG)
        return dest


def _srv(tmp_path):
    return MCPServer(lambda: FakeClient(), outdir=str(tmp_path))


def test_initialize_negotiates_version():
    s = _srv("/tmp")
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-03-26"}})
    assert r["result"]["protocolVersion"] == "2025-03-26"
    assert r["result"]["serverInfo"]["name"] == "gemini-web"
    r2 = s.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                   "params": {"protocolVersion": "9999-99-99"}})
    assert r2["result"]["protocolVersion"] == "2025-06-18"


def test_tools_list_has_generate_image():
    s = _srv("/tmp")
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in r["result"]["tools"]]
    assert names == ["generate_image", "ask", "list_models"]


def test_notifications_get_no_reply():
    s = _srv("/tmp")
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method():
    s = _srv("/tmp")
    r = s.handle({"jsonrpc": "2.0", "id": 9, "method": "nope/nothing"})
    assert r["error"]["code"] == -32601


def test_generate_image_returns_file_and_inline_image(tmp_path):
    s = _srv(tmp_path)
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "generate_image",
                             "arguments": {"prompt": "a red panda",
                                           "remove_logo": False}}})
    content = r["result"]["content"]
    assert r["result"].get("isError") is not True
    assert content[0]["type"] == "text" and "saved ->" in content[0]["text"]
    img = next(c for c in content if c["type"] == "image")
    assert img["mimeType"] == "image/png"
    assert base64.b64decode(img["data"]) == TINY_PNG
    # default is preview (small), prompt forwarded
    assert s._client.seen["gen"][0] == "a red panda"
    assert s._client.seen["gen"][1] is False
    assert len(list(tmp_path.glob("gen_*.png"))) == 1


def test_generate_image_needs_prompt(tmp_path):
    s = _srv(tmp_path)
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "generate_image", "arguments": {}}})
    assert r["result"]["isError"] is True


def test_ask_and_list_models(tmp_path):
    s = _srv(tmp_path)
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "ask", "arguments": {"prompt": "hi"}}})
    assert r["result"]["content"][0]["text"] == "reply:hi"
    r2 = s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "list_models", "arguments": {}}})
    rows = json.loads(r2["result"]["content"][0]["text"])
    assert rows[0]["model_name"] == "gemini-flash"


def test_unknown_tool_is_error_not_crash(tmp_path):
    s = _srv(tmp_path)
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "teleport", "arguments": {}}})
    assert r["result"]["isError"] is True


def test_embed_cap():
    from gemini_web.mcp import _embed_image_bytes
    small = _embed_image_bytes(b"1234", "image/png", max_bytes=100)
    assert small["type"] == "image"
    assert base64.b64decode(small["data"]) == b"1234"


def test_stdio_smoke(monkeypatch, tmp_path, capsys):
    def _mk():
        raise AssertionError("no tool calls in this test, client must not init")
    s = MCPServer(_mk, outdir=str(tmp_path))
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        "not json {{{",
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(
        m if isinstance(m, str) else json.dumps(m) for m in msgs) + "\n"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    s.run_stdio()
    lines = [l for l in sys.stdout.getvalue().splitlines() if l.strip()]
    assert len(lines) == 2  # initialize + tools/list; notification + garbage silent
    assert json.loads(lines[0])["result"]["protocolVersion"] == "2024-11-05"
    assert "generate_image" in json.dumps(json.loads(lines[1]))
