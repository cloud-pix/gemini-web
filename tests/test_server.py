from gemini_web.server import (
    build_transcript, fetch_image_bytes, image_parts, message_text,
    parse_tool_calls, to_openai_tool_calls,
)


def test_message_text():
    assert message_text({"content": "hi"}) == "hi"
    assert message_text({"content": [{"type": "text", "text": "a"},
                                      {"type": "image_url", "image_url": {"url": "u"}}]}) == "a"
    assert image_parts({"content": [{"type": "image_url",
                                     "image_url": {"url": "http://x/y.png"}}]}) == ["http://x/y.png"]


def test_build_transcript_roles():
    msgs = [{"role": "system", "content": "be nice"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "tool", "tool_call_id": "c1", "content": "42"}]
    prompt, images = build_transcript(msgs)
    assert "[System instructions]" in prompt and "[User]" in prompt
    assert "[Assistant]" in prompt and "c1" in prompt and images == []


def test_build_transcript_tools_injected():
    tools = [{"type": "function", "function": {"name": "sh", "parameters": {}}}]
    prompt, _ = build_transcript([{"role": "user", "content": "run ls"}], tools, "auto")
    assert "tool_calls" in prompt and '"name": "sh"' in prompt or '"sh"' in prompt
    prompt2, _ = build_transcript([{"role": "user", "content": "hi"}], tools, "none")
    assert "tool_calls" not in prompt2


def test_parse_tool_calls():
    text = 'thinking\n```tool_calls\n[{"name": "sh", "arguments": {"cmd": "ls"}}]\n```\ntail'
    parsed, rest = parse_tool_calls(text)
    assert parsed == [{"name": "sh", "arguments": '{"cmd": "ls"}'}]
    assert rest == "thinking\n\ntail"
    assert parse_tool_calls("plain text") == ([], "plain text")
    assert parse_tool_calls("```tool_calls\nnot json```") == ([], "```tool_calls\nnot json```")


def test_to_openai_tool_calls_shape():
    tcs = to_openai_tool_calls([{"name": "sh", "arguments": "{}"}])
    assert len(tcs) == 1 and tcs[0]["type"] == "function"
    assert tcs[0]["id"].startswith("call_") and tcs[0]["function"]["name"] == "sh"


def test_fetch_data_url():
    import base64
    data, name = fetch_image_bytes("data:image/png;base64," + base64.b64encode(b"123").decode())
    assert data == b"123"


class FakeClient:
    def __init__(self, text):
        self.text = text
        self.seen = {}

    def init(self):
        return {}

    def list_models(self):
        return [{"model_name": "gemini-flash", "display_name": "Flash",
                 "description": "d", "is_available": True, "aliases": [],
                 "model_id": "x", "capacity": 1, "capacity_field": 12,
                 "model_number": 1, "header": {}}]

    def upload_bytes(self, data, name):
        self.seen["upload"] = (len(data), name)
        return "/fid/1"

    def generate(self, prompt, fids=(), fnames=(), timeout=None, model=None,
                 thinking=False):
        self.seen["gen"] = (prompt[:50], fids, model)
        return self.text


def test_shim_complete_and_stream():
    from gemini_web.server import Shim
    shim = Shim(lambda: FakeClient("```tool_calls\n[{\"name\": \"sh\", \"arguments\": {}}]\n```"),
                api_key="K")
    assert shim.check_auth({"Authorization": "Bearer K"})
    assert not shim.check_auth({"Authorization": "Bearer X"})
    req = {"model": "gemini-flash",
           "messages": [{"role": "user", "content": "run ls"}],
           "tools": [{"type": "function", "function": {"name": "sh"}}]}
    resp = shim.complete(req)
    assert resp["object"] == "chat.completion"
    assert resp["choices"][0]["finish_reason"] == "tool_calls"
    assert resp["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "sh"
    chunks = list(shim.stream_chunks(req))
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_shim_models_payload_offline():
    from gemini_web.server import Shim

    class Dead:
        def list_models(self):
            raise RuntimeError("no net")

    shim = Shim(lambda: Dead(), default_model="gemini-flash")
    payload = shim.models_payload()
    assert payload["data"][0]["id"] == "gemini-flash"


def test_shim_reasoning_effort_mapping():
    from gemini_web.server import Shim

    seen = {}

    class FakeClient:
        def init(self):
            return {}

        def generate(self, prompt, fids=(), fnames=(), timeout=None, model=None,
                     thinking=False):
            seen["thinking"] = thinking
            return "ok"

    shim = Shim(lambda: FakeClient())
    req = {"messages": [{"role": "user", "content": "hi"}]}
    shim.complete(req)
    assert seen["thinking"] == "low"
    shim.complete({**req, "reasoning_effort": "high"})
    assert seen["thinking"] == "high"
    shim.complete({**req, "reasoning_effort": "medium"})
    assert seen["thinking"] == "medium"
    shim.complete({**req, "reasoning_effort": "none"})
    assert seen["thinking"] == "low"
