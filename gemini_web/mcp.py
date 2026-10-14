"""MCP (Model Context Protocol) stdio server over the Gemini web client.

Stdlib only — no `mcp` package needed (it doesn't build on Termux/aarch64).
Speaks JSON-RPC 2.0 over stdio, one message per line:

  client -> {"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}
  server -> {"jsonrpc":"2.0","id":1,"result":{...}}
  client -> {"jsonrpc":"2.0","method":"notifications/initialized"}
  client -> {"jsonrpc":"2.0","id":2,"method":"tools/list"}
  client -> {"jsonrpc":"2.0","id":3,"method":"tools/call",
             "params":{"name":"generate_image","arguments":{"prompt":"..."}}}

Tools:
  generate_image  prompt -> PNG saved to disk + image content back to the model
  ask             prompt -> text reply
  list_models     -> models available to this account

Run: gemini-web mcp --session-file gemini_session.json
(see README for the opencode.jsonc / Claude Desktop wiring).
"""

import base64
import json
import mimetypes
import os
import pathlib
import sys
import time

SERVER_NAME = "gemini-web"
SERVER_VERSION = "0.2.0"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
# Don't inline huge files into one JSON line: embed up to this many bytes,
# downscale a copy for embedding when PIL is available, else return path only.
DEFAULT_MAX_EMBED_BYTES = 2_500_000

TOOLS = [
    {
        "name": "generate_image",
        "description": ("Generate an image with the Gemini web app and get it back "
                        "inline. Saves a PNG to disk and returns the image content "
                        "plus the saved path."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string",
                           "description": "Image description, e.g. 'a baby red panda in a yellow raincoat'"},
                "out": {"type": "string",
                        "description": "Optional output filename. Defaults to gen_<timestamp>.png in the server outdir."},
                "full_size": {"type": "boolean", "default": False,
                              "description": "True = full-size original (~8MB, slow). False = preview (~1MB, fast, default)."},
                "model": {"type": ["string", "null"],
                          "description": "Model name/alias (see list_models). Omit for default."},
                "thinking": {"type": "string", "enum": ["low", "medium", "high"],
                             "default": "low"},
                "remove_logo": {"type": "boolean", "default": True,
                                "description": "Remove the Gemini sparkle watermark (keeps .orig backup)."},
                "timeout": {"type": "integer", "default": 300},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "ask",
        "description": "Text prompt -> Gemini web app reply.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "model": {"type": ["string", "null"]},
                "thinking": {"type": "string", "enum": ["low", "medium", "high"],
                             "default": "low"},
                "timeout": {"type": "integer", "default": 120},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "list_models",
        "description": "List Gemini web models available to this account.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _err(code, message, mid=None):
    e = {"jsonrpc": "2.0", "error": {"code": code, "message": message}}
    if mid is not None:
        e["id"] = mid
    return e


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _tool_error(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _mime_for(path):
    mt, _ = mimetypes.guess_type(str(path))
    return mt or "image/png"


def _embed_image_bytes(data, mime, max_bytes=DEFAULT_MAX_EMBED_BYTES):
    """Return MCP image content for data, or None if too large to inline.

    Large files are downscaled to a <=1024px copy for embedding when PIL is
    available (original file on disk stays full-size). Without PIL, large
    files are path-only.
    """
    if len(data) <= max_bytes:
        return {"type": "image", "data": base64.b64encode(data).decode(),
                "mimeType": mime}
    try:
        from PIL import Image as _PILImage
        import io as _io
        im = _PILImage.open(_io.BytesIO(data))
        im.thumbnail((1024, 1024))
        buf = _io.BytesIO()
        im.save(buf, "PNG")
        small = buf.getvalue()
        if len(small) <= max_bytes:
            return {"type": "image", "data": base64.b64encode(small).decode(),
                    "mimeType": "image/png"}
    except Exception:
        pass
    return None


class MCPServer:
    def __init__(self, make_client, outdir=".", timeout=300,
                 max_embed_bytes=DEFAULT_MAX_EMBED_BYTES):
        self.make_client = make_client
        self.outdir = pathlib.Path(outdir or ".")
        self.timeout = timeout
        self.max_embed_bytes = max_embed_bytes
        self._client = None
        self._n = 0

    def client(self):
        if self._client is None:
            cl = self.make_client()
            cl.init()
            self._client = cl
        return self._client

    # -- dispatch ------------------------------------------------------
    def handle(self, msg):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _err(-32600, "invalid JSON-RPC 2.0 message", msg.get("id") if isinstance(msg, dict) else None)
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            want = (params.get("protocolVersion") if isinstance(params, dict) else None)
            ver = want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _ok(mid, {
                "protocolVersion": ver,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        if method == "ping":
            return _ok(mid, {})
        if method in ("tools/list", "tools\\list"):
            return _ok(mid, {"tools": TOOLS})
        if method in ("resources/list", "prompts/list"):
            key = method.split("/")[0]
            return _ok(mid, {key: []})
        if method == "tools/call":
            if mid is None:
                return None
            if not isinstance(params, dict):
                return _err(-32602, "params must be an object", mid)
            name = params.get("name")
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                return _err(-32602, "arguments must be an object", mid)
            try:
                result = self.call_tool(name, args)
            except Exception as e:  # never break the session on tool errors
                result = _tool_error(f"{type(e).__name__}: {e}"[:500])
            return _ok(mid, result)
        if method and method.startswith("notifications/"):
            return None  # notifications get no reply
        if mid is None:
            return None
        return _err(-32601, f"unknown method: {method}", mid)

    # -- tools ---------------------------------------------------------
    def call_tool(self, name, args):
        if name == "generate_image":
            return self.tool_generate_image(args)
        if name == "ask":
            return self.tool_ask(args)
        if name == "list_models":
            return self.tool_list_models()
        return _tool_error(f"unknown tool: {name}")

    def _dest(self, out):
        self.outdir.mkdir(parents=True, exist_ok=True)
        if out:
            p = pathlib.Path(out)
            return str(p if p.is_absolute() else self.outdir / p)
        self._n += 1
        ts = time.strftime("%Y%m%d_%H%M%S")
        return str(self.outdir / f"gen_{ts}_{self._n}.png")

    def tool_generate_image(self, args):
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return _tool_error("prompt is required")
        thinking = args.get("thinking") or "low"
        if thinking not in ("low", "medium", "high"):
            return _tool_error("thinking must be low|medium|high")
        try:
            timeout = int(args.get("timeout") or self.timeout)
        except (TypeError, ValueError):
            return _tool_error("timeout must be an integer")
        dest = self._dest(args.get("out"))
        try:
            cl = self.client()
            cl.generate_image(prompt, dest, timeout=timeout,
                              model=args.get("model") or None,
                              full_size=bool(args.get("full_size", False)),
                              thinking=thinking)
        except Exception as e:
            return _tool_error(f"generation failed: {type(e).__name__}: {e}"[:500])
        notes = []
        if args.get("remove_logo", True):
            try:
                from gemini_web import logo as _logo
                info = _logo.remove_logo(dest, keep_original=True)
                if info.get("found"):
                    notes.append(f"watermark removed (score {info.get('score')}).")
            except Exception as e:
                notes.append(f"logo removal skipped: {e}"[:120])
        try:
            data = pathlib.Path(dest).read_bytes()
        except Exception as e:
            return _tool_error(f"saved but unreadable ({dest}): {e}"[:300])
        mime = _mime_for(dest)
        content = [{"type": "text",
                    "text": f"saved -> {dest} ({len(data)//1024}KB). " + " ".join(notes)}]
        img = _embed_image_bytes(data, mime, self.max_embed_bytes)
        if img is None:
            content[0]["text"] += (f" Image too large to inline ({len(data)//1024}KB); "
                                   "read it from the saved path.")
        else:
            if img.get("mimeType") == "image/png" and mime != "image/png":
                pass
            content.append(img)
        return {"content": content}

    def tool_ask(self, args):
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return _tool_error("prompt is required")
        thinking = args.get("thinking") or "low"
        if thinking not in ("low", "medium", "high"):
            return _tool_error("thinking must be low|medium|high")
        try:
            timeout = int(args.get("timeout") or 120)
        except (TypeError, ValueError):
            return _tool_error("timeout must be an integer")
        try:
            text = self.client().ask(prompt, timeout=timeout,
                                     model=args.get("model") or None,
                                     thinking=thinking)
        except Exception as e:
            return _tool_error(f"ask failed: {type(e).__name__}: {e}"[:500])
        return {"content": [{"type": "text", "text": text}]}

    def tool_list_models(self):
        try:
            models = self.client().list_models()
            rows = [{"model_name": m.get("model_name"),
                     "display_name": m.get("display_name"),
                     "available": m.get("is_available")} for m in models]
        except Exception as e:
            return _tool_error(f"list_models failed: {type(e).__name__}: {e}"[:300])
        return {"content": [{"type": "text", "text": json.dumps(rows, indent=2)}]}

    # -- stdio loop ----------------------------------------------------
    def run_stdio(self):
        inp = sys.stdin
        out = sys.stdout
        while True:
            line = inp.readline()
            if line == "":
                break  # EOF: client disconnected
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # ignore garbage, keep session alive
            try:
                resp = self.handle(msg)
            except Exception as e:
                mid = msg.get("id") if isinstance(msg, dict) else None
                resp = _err(-32603, f"{type(e).__name__}: {e}"[:300], mid)
            if resp is None:
                continue
            try:
                out.write(json.dumps(resp) + "\n")
                out.flush()
            except (BrokenPipeError, ValueError):
                break


def serve_stdio(make_client, outdir=".", timeout=300,
                max_embed_bytes=DEFAULT_MAX_EMBED_BYTES):
    MCPServer(make_client, outdir=outdir, timeout=timeout,
              max_embed_bytes=max_embed_bytes).run_stdio()


def main(argv=None):
    """`gemini-web mcp` entry: same auth flags as the CLI, then stdio loop."""
    import argparse
    import pathlib as _pl
    from gemini_web import cookies as _C
    ap = argparse.ArgumentParser(prog="gemini-web mcp")
    ap.add_argument("--cookies", default=None)
    ap.add_argument("--psid", default=None)
    ap.add_argument("--psidts", default=None)
    ap.add_argument("--session-file", default=None)
    ap.add_argument("--outdir", default=os.environ.get("GEMINI_WEB_OUTDIR", "."))
    ap.add_argument("--timeout", type=int, default=300)
    ns = ap.parse_args(argv)

    def _mk():
        from gemini_web.client import Client
        ck = _C.load_any(ns.cookies, ns.psid, ns.psidts)
        if ns.session_file and _pl.Path(ns.session_file).exists() and not ck.get("__Secure-1PSID"):
            try:
                ck.update(json.loads(_pl.Path(ns.session_file).read_text()))
            except Exception:
                pass
        if not ck.get("__Secure-1PSID"):
            raise SystemExit("no __Secure-1PSID: pass --cookies/--psid/GEMINI_PSID")
        return Client(ck)

    serve_stdio(_mk, outdir=ns.outdir, timeout=ns.timeout)
