"""OpenAI-compatible HTTP shim over the Gemini web client (stdlib only).

Endpoints:
  GET  /v1/models
  POST /v1/chat/completions   (non-stream + SSE stream, tools best-effort, image_url parts)

Tool calling is EMULATED: tool schemas are injected as text instructions and the
model's fenced ```tool_calls JSON block is parsed back into tool_calls. This works
for simple cases and breaks on complex ones — the web RPC has no native function
calling. Image parts (http/data URLs) are uploaded and attached per request.
"""
import base64
import io
import json
import re
import secrets
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOL_FENCE_RE = re.compile(r"```tool_calls\s*\n(.*?)```", re.S)
TOOL_FENCE_ALT_RE = re.compile(r"```json\s*\n(.*?)```", re.S)


def message_text(msg):
    content = msg.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for p in content:
        if isinstance(p, dict) and p.get("type") == "text":
            parts.append(p.get("text", ""))
    return "\n".join(x for x in parts if x)


def image_parts(msg):
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    out = []
    for p in content:
        if isinstance(p, dict) and p.get("type") == "image_url":
            url = (p.get("image_url") or {}).get("url", "")
            if url:
                out.append(url)
    return out


def build_transcript(messages, tools=None, tool_choice=None):
    """Flatten OpenAI messages into one prompt. Returns (prompt, image_urls)."""
    lines, images = [], []
    for m in messages or []:
        role = m.get("role", "user")
        if role == "system":
            lines.append("[System instructions]\n" + message_text(m))
        elif role == "assistant":
            tcs = m.get("tool_calls") or []
            if tcs:
                lines.append("[Assistant tool calls]\n" + json.dumps(tcs)[:4000])
            t = message_text(m)
            if t:
                lines.append("[Assistant]\n" + t)
        elif role == "tool":
            lines.append("[Tool result for call %s]\n%s" % (m.get("tool_call_id", "?"), message_text(m)))
        else:
            t = message_text(m)
            if t:
                lines.append("[User]\n" + t)
        images.extend(image_parts(m))
    prompt = "\n\n".join(x for x in lines if x)
    if tools and (tool_choice or "auto") != "none":
        prompt += ("\n\n[Available tools — use them when they help. "
                   "To call one or more tools, reply with ONLY this fenced block and nothing else:\n"
                   "```tool_calls\n"
                   "[{\"name\": \"<tool_name>\", \"arguments\": {<args object>}}]\n"
                   "```\nTools:\n" + json.dumps(tools)[:12000] + "\n"
                   "If no tool is needed, reply with plain text and no fenced block.]")
    elif tool_choice == "required":
        prompt += ("\n\n[You MUST call a tool now. Reply with ONLY:\n```tool_calls\n"
                   "[{\"name\": \"<tool_name>\", \"arguments\": {}}]\n```]")
    return prompt, images


def parse_tool_calls(text):
    """Extract [{name, arguments}] from fenced blocks. arguments normalized to JSON str."""
    for rx in (TOOL_FENCE_RE, TOOL_FENCE_ALT_RE):
        m = rx.search(text or "")
        if not m:
            continue
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else data.get("tool_calls", [])
        out = []
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            fn = it.get("function", it)
            name = it.get("name") or fn.get("name")
            if not name:
                continue
            args = it.get("arguments", fn.get("arguments", {}))
            out.append({"name": name,
                        "arguments": args if isinstance(args, str) else json.dumps(args)})
        if out:
            return out, rx.sub("", text or "").strip()
    return [], text or ""


def to_openai_tool_calls(parsed):
    return [{"id": "call_" + secrets.token_hex(12), "type": "function",
             "function": {"name": p["name"], "arguments": p["arguments"]}} for p in parsed]


def fetch_image_bytes(url, timeout=60):
    if url.startswith("data:"):
        head, _, b64 = url.partition(",")
        return base64.b64decode(b64), "image.png"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise ValueError("image over 25MB, skipping")
    name = url.split("?")[0].rstrip("/").split("/")[-1] or "image.png"
    if "." not in name:
        name += ".png"
    return data, name


class Shim:
    def __init__(self, make_client, api_key=None, default_model=None, timeout=300):
        self.make_client = make_client
        self.api_key = api_key
        self.default_model = default_model
        self.timeout = timeout
        self._lock = threading.Lock()
        self._client = None

    def client(self):
        with self._lock:
            if self._client is None:
                cl = self.make_client()
                cl.init()
                self._client = cl
            return self._client

    def check_auth(self, headers):
        if not self.api_key:
            return True
        got = headers.get("Authorization", "")
        return got == "Bearer " + self.api_key

    def models_payload(self):
        try:
            models = self.client().list_models()
            data = [{"id": m["model_name"], "object": "model",
                     "created": int(time.time()), "owned_by": "gemini-web"} for m in models]
        except Exception:
            name = self.default_model or "gemini-flash"
            data = [{"id": name, "object": "model", "created": int(time.time()),
                     "owned_by": "gemini-web"}]
        return {"object": "list", "data": data}

    def complete(self, req):
        model = req.get("model") or self.default_model
        prompt, images = build_transcript(req.get("messages"), req.get("tools"),
                                         req.get("tool_choice"))
        eff = str(req.get("reasoning_effort", "low") or "low").lower()
        thinking = {"minimal": "low", "low": "low", "medium": "medium",
                    "high": "high"}.get(eff, "low")
        cl = self.client()
        fids, fnames = [], []
        for i, url in enumerate(images[:4]):
            try:
                data, name = fetch_image_bytes(url)
                fid = cl.upload_bytes(data, name or f"image{i}.png")
                fids.append(fid)
                fnames.append(name)
            except Exception:
                continue
        text = cl.generate(prompt, fids, fnames, timeout=self.timeout, model=model,
                           thinking=thinking)
        parsed, remaining = parse_tool_calls(text)
        tcs = to_openai_tool_calls(parsed)
        msg = {"role": "assistant", "content": remaining or None}
        finish = "stop"
        if tcs:
            msg["tool_calls"] = tcs
            finish = "tool_calls"
        return {"id": "chatcmpl-" + secrets.token_hex(12), "object": "chat.completion",
                "created": int(time.time()), "model": req.get("model") or "gemini-web",
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": {"prompt_tokens": max(1, len(prompt) // 4),
                          "completion_tokens": max(1, len(text) // 4),
                          "total_tokens": max(2, (len(prompt) + len(text)) // 4)}}

    def stream_chunks(self, req):
        resp = self.complete(req)
        msg = resp["choices"][0]["message"]
        rid = resp["id"]
        model = resp["model"]
        yield {"id": rid, "object": "chat.completion.chunk", "created": resp["created"],
               "model": model, "choices": [{"index": 0, "delta": {"role": "assistant"},
                                            "finish_reason": None}]}
        content = msg.get("content") or ""
        for i in range(0, len(content), 200):
            yield {"id": rid, "object": "chat.completion.chunk", "created": resp["created"],
                   "model": model, "choices": [{"index": 0,
                                                "delta": {"content": content[i:i + 200]},
                                                "finish_reason": None}]}
        for tc in msg.get("tool_calls") or []:
            yield {"id": rid, "object": "chat.completion.chunk", "created": resp["created"],
                   "model": model, "choices": [{"index": 0,
                                                "delta": {"tool_calls": [{**tc, "index": 0}]},
                                                "finish_reason": None}]}
        yield {"id": rid, "object": "chat.completion.chunk", "created": resp["created"],
               "model": model, "choices": [{"index": 0, "delta": {},
                                            "finish_reason": resp["choices"][0]["finish_reason"]}]}


def make_handler(shim):
    class H(BaseHTTPRequestHandler):
        server_version = "gemini-web-shim"

        def log_message(self, *a):
            pass

        def _send(self, code, obj=None, sse=False):
            body = b""
            if obj is not None:
                body = (obj if isinstance(obj, bytes) else json.dumps(obj).encode())
            self.send_response(code)
            self.send_header("Content-Type",
                             "text/event-stream" if sse else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _err(self, code, msg):
            self._send(code, {"error": {"message": msg, "type": "shim_error"}})

        def do_GET(self):
            if self.path.rstrip("/") in ("/v1/models", "/models"):
                if not shim.check_auth(self.headers):
                    return self._err(401, "invalid API key")
                try:
                    return self._send(200, shim.models_payload())
                except Exception as e:
                    return self._err(500, f"{type(e).__name__}: {e}"[:300])
            return self._err(404, "use GET /v1/models or POST /v1/chat/completions")

        def do_POST(self):
            if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
                return self._err(404, "use POST /v1/chat/completions")
            if not shim.check_auth(self.headers):
                return self._err(401, "invalid API key")
            try:
                ln = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(ln) or b"{}")
            except Exception:
                return self._err(400, "invalid JSON body")
            try:
                if req.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    for ch in shim.stream_chunks(req):
                        self.wfile.write(("data: " + json.dumps(ch) + "\n\n").encode())
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                else:
                    self._send(200, shim.complete(req))
            except Exception as e:
                try:
                    self._err(500, f"{type(e).__name__}: {str(e)}"[:300])
                except Exception:
                    pass
    return H


def serve(shim, host="127.0.0.1", port=8123):
    srv = ThreadingHTTPServer((host, port), make_handler(shim))
    srv.serve_forever()
