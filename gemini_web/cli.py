"""CLI: login/check/ask/analyze/analyze-batch."""
import argparse
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from gemini_web import cookies as C
from gemini_web.client import Client, GeminiError

DEFAULT_PROMPT = ("Describe this video in detail: who/what is visible, on-screen text, "
                  "action from start to end, mood, and any meme format. Be concrete.")


def build():
    p = argparse.ArgumentParser(prog="gemini-web",
        description="FOSS Gemini web-app client (cookie login, video upload + analysis).")
    p.add_argument("--cookies", default=None, help="Netscape cookies.txt or JSON with __Secure-1PSID")
    p.add_argument("--psid", default=None, help="__Secure-1PSID value (or GEMINI_PSID env)")
    p.add_argument("--psidts", default=None, help="__Secure-1PSIDTS value (or GEMINI_PSIDTS env)")
    p.add_argument("--session-file", default=None, help="JSON session to save/load cookies")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    l = sub.add_parser("login", help="verify cookies via init, save session")
    l.add_argument("--out", default="gemini_session.json")

    m = sub.add_parser("models", help="list models available to this account")

    g = sub.add_parser("gen-image", help="generate an image and save it")
    g.add_argument("prompt")
    g.add_argument("-o", "--out", default="gen.png")
    g.add_argument("--timeout", type=int, default=300)
    g.add_argument("--model", default=None)
    g.add_argument("--thinking", choices=["low", "medium", "high"], default="low")
    g.add_argument("--preview", action="store_true", help="smaller preview instead of full size")
    g.add_argument("--no-logo", action="store_true", help="skip automatic watermark removal")
    g.add_argument("--no-original", action="store_true", help="don't keep .orig backup")

    r = sub.add_parser("remove-logo", help="remove the Gemini sparkle watermark from an image")
    r.add_argument("image")
    r.add_argument("-o", "--out", default=None)
    r.add_argument("--no-original", action="store_true")

    c = sub.add_parser("check", help="init + tiny ask to prove execution works")

    a = sub.add_parser("ask", help="text prompt -> reply")
    a.add_argument("prompt")
    a.add_argument("--timeout", type=int, default=120)
    a.add_argument("--model", default=None, help="model name/alias (see models)")
    a.add_argument("--thinking", choices=["low", "medium", "high"], default="low")

    v = sub.add_parser("analyze", help="upload video + prompt -> analysis")
    v.add_argument("video")
    v.add_argument("--prompt", default=DEFAULT_PROMPT)
    v.add_argument("--timeout", type=int, default=300)
    v.add_argument("--model", default=None)
    v.add_argument("--thinking", choices=["low", "medium", "high"], default="low")
    v.add_argument("-o", "--out", default=None, help="write reply text to file")

    b = sub.add_parser("analyze-batch", help="analyze many videos with resume + delay")
    b.add_argument("videos", nargs="+")
    b.add_argument("--prompt", default=DEFAULT_PROMPT)
    b.add_argument("--timeout", type=int, default=300)
    b.add_argument("--model", default=None)
    b.add_argument("--thinking", choices=["low", "medium", "high"], default="low")
    b.add_argument("-o", "--outdir", default="./analyses")
    b.add_argument("--delay", type=int, default=8, help="seconds between items")
    b.add_argument("--resume", action="store_true", default=True)

    s = sub.add_parser("serve", help="OpenAI-compatible HTTP shim (for opencode custom provider)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8123)
    s.add_argument("--api-key", default=None, help="required Bearer key (or GEMINI_WEB_API_KEY env)")
    s.add_argument("--default-model", default=None)
    s.add_argument("--timeout", type=int, default=300)
    return p


def make_client(a):
    ck = C.load_any(a.cookies, a.psid, a.psidts)
    if a.session_file and pathlib.Path(a.session_file).exists() and not ck.get("__Secure-1PSID"):
        try:
            ck.update(json.loads(pathlib.Path(a.session_file).read_text()))
        except Exception:
            pass
    if not ck.get("__Secure-1PSID"):
        print("no __Secure-1PSID: pass --cookies/--psid/GEMINI_PSID (export from a logged-in browser)")
        sys.exit(2)
    return Client(ck, verbose=a.verbose), ck


def main(argv=None):
    p = build()
    a = p.parse_args(argv)
    try:
        if a.cmd == "login":
            cl, ck = make_client(a)
            vals = cl.init()
            cl.rotate()
            for k, v in cl.cookies.items():
                if k in ("__Secure-1PSID", "__Secure-1PSIDTS"):
                    ck[k] = v
            C.save(a.out, ck)
            print(json.dumps({"ok": True, "session": a.out,
                              "push_id": bool(vals.get("push_id")),
                              "language": vals.get("language")}, indent=2))
            return 0
        if a.cmd == "check":
            cl, _ = make_client(a)
            cl.init()
            t0 = time.time()
            rep = cl.ask("Reply with exactly: OK", timeout=60)
            print(json.dumps({"ok": "OK" in rep, "secs": round(time.time() - t0, 1),
                              "reply": rep[:200]}, indent=2))
            return 0
        if a.cmd == "models":
            cl, _ = make_client(a)
            cl.init()
            models = cl.list_models()
            print(json.dumps([{
                "model_name": m["model_name"], "display_name": m["display_name"],
                "description": m["description"][:120], "available": m["is_available"],
            } for m in models], indent=2, ensure_ascii=False))
            return 0
        if a.cmd == "gen-image":
            cl, _ = make_client(a)
            cl.init()
            dest = cl.generate_image(a.prompt, a.out, timeout=a.timeout,
                                     model=a.model, full_size=not a.preview,
                                     thinking=a.thinking)
            print(f"saved -> {dest}")
            if not a.no_logo:
                from gemini_web import logo as _logo
                info = _logo.remove_logo(dest, keep_original=not a.no_original)
                print("logo: " + (f"removed (score {info.get('score')}, backup {info.get('original', 'kept in place')})"
                                  if info.get("found") else "not found, left as-is"))
            return 0
        if a.cmd == "remove-logo":
            from gemini_web import logo as _logo
            info = _logo.remove_logo(a.image, a.out, keep_original=not a.no_original)
            print(json.dumps(info, indent=2))
            return 0
        if a.cmd == "serve":
            from gemini_web.server import Shim, serve
            api_key = a.api_key or os.environ.get("GEMINI_WEB_API_KEY")

            def _mk():
                cl, _ = make_client(a)
                return cl
            shim = Shim(_mk, api_key=api_key, default_model=a.default_model,
                        timeout=a.timeout)
            print(f"serving OpenAI-compatible API on http://{a.host}:{a.port}/v1"
                  + (" (API key required)" if api_key else " (no auth)"))
            print("opencode.jsonc provider: {\"gemini-web\": {\"package\": "
                  "\"@opencode/ai/providers/openai-compatible\", \"settings\": "
                  f"{{\"baseURL\": \"http://{a.host}:{a.port}/v1\"}}}}")
            serve(shim, a.host, a.port)
            return 0
        if a.cmd == "ask":
            cl, _ = make_client(a)
            cl.init()
            print(cl.ask(a.prompt, timeout=a.timeout, model=a.model, thinking=a.thinking))
            return 0
        if a.cmd == "analyze":
            cl, _ = make_client(a)
            cl.init()
            rep = cl.analyze_video(a.video, a.prompt, timeout=a.timeout, model=a.model,
                                     thinking=a.thinking)
            if a.out:
                pathlib.Path(a.out).write_text(rep)
                print(f"wrote {len(rep)} chars -> {a.out}")
            else:
                print(rep)
            return 0
        if a.cmd == "analyze-batch":
            cl, _ = make_client(a)
            cl.init()
            outdir = pathlib.Path(a.outdir)
            outdir.mkdir(parents=True, exist_ok=True)
            done, failed = 0, []
            items = [v for v in a.videos if pathlib.Path(v).is_file()]
            for i, v in enumerate(items, 1):
                name = pathlib.Path(v).stem + ".json"
                dest = outdir / name
                if a.resume and dest.exists():
                    print(f"[{i}/{len(items)}] skip (exists) {name}")
                    done += 1
                    continue
                try:
                    rep = cl.analyze_video(v, a.prompt, timeout=a.timeout, model=a.model,
                                        thinking=a.thinking)
                    dest.write_text(json.dumps({"video": v, "analysis": rep,
                                                "prompt": a.prompt}, indent=2, ensure_ascii=False))
                    print(f"[{i}/{len(items)}] ok {name} ({len(rep)} chars)")
                    done += 1
                except Exception as e:
                    print(f"[{i}/{len(items)}] FAIL {v}: {str(e)[:150]}")
                    failed.append(v)
                    if "429" in str(e) or "rate-limited" in str(e).lower():
                        print("rate limited, stopping batch")
                        break
                if i < len(items):
                    time.sleep(a.delay)
            print(f"batch done={done} failed={len(failed)} -> {outdir}")
            return 0 if not failed else 1
    except GeminiError as e:
        print(f"ERROR: {e}")
        return 1
    except KeyboardInterrupt:
        print("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
