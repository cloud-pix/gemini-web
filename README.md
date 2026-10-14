# gemini-web ✨

Use the **Gemini web app** (`gemini.google.com`) from your terminal — no API key needed. Chat, analyze videos, generate images, even plug it into your AI coding tools.

Free and open source (AGPL-3.0-or-later). Runs on Termux/Android too 📱 — just Python + `curl_cffi`, no heavy dependencies.

> Protocol know-how borrowed from [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API). Thanks! 🙏

## Install

```bash
pip install -e .
```

## Quickstart 🔑

Gemini needs you logged in. Log in once in your browser, grab your cookies, and you're set:

```bash
# easiest: export cookies with yt-dlp, then log in
yt-dlp --cookies-from-browser chrome --cookies gcookies.txt https://gemini.google.com/

export GEMINI_PSID='paste-your-__Secure-1PSID-here'
# GEMINI_PSIDTS too, if you have it (optional)

gemini-web login --out gemini_session.json
gemini-web check        # tiny test prompt — proves everything works ✅
gemini-web models       # see which models your account can use
```

Only the `__Secure-1PSID` cookie is used. Nothing else is touched.

## Everyday use 💬

```bash
# just chat
gemini-web ask "Reply with exactly: OK"
gemini-web ask "Explain this idea simply" --model gemini-flash

# describe a video
gemini-web analyze reel.mp4 --prompt "Describe this video in detail..." -o out.txt

# describe a whole folder (skips ones already done, pauses politely between them)
gemini-web analyze-batch *.mp4 -o ./analyses --delay 8

# make an image 🖼️
gemini-web gen-image "a baby red panda in a yellow raincoat" -o panda.png
gemini-web gen-image "..." -o panda.png --preview   # smaller + faster (~1MB vs ~8MB)
```

Every generated image automatically gets the little Gemini sparkle watermark ✨ cleaned off (the original is kept as `.orig.png`, just in case). Don't want that? Add `--no-logo`. Got an old image with the sparkle? `gemini-web remove-logo pic.png`.

## Picking a model 🎛️

```bash
gemini-web ask "..." --model gemini-flash
```

Run `gemini-web models` to see yours — the list comes from your own account, so it's always accurate. Short names work too (`flash`, `4-argon`). One honest note: the free account can run Flash and Argon, but the server refuses Pro even though it's listed. Unknown names get a friendly error showing what's actually available.

## Thinking levels 🧠

```bash
gemini-web ask "..." --thinking high   # low | medium | high, default: low
```

Works on `ask`, `analyze`, `analyze-batch`, and `gen-image`. These are the real levels from the Gemini app — not guesses. Fair warning: on a free account the difference is subtle, since only Pro truly "thinks" and Pro won't run without a paid plan.

## For nerds: plug it into your tools 🔌

**OpenAI-compatible server** — point any OpenAI-style client at it:

```bash
gemini-web serve --port 8123 --api-key SECRET --default-model gemini-flash
```

```jsonc
// opencode.jsonc
{ "providers": { "gemini-web": {
  "package": "@opencode/ai/providers/openai-compatible",
  "settings": { "baseURL": "http://127.0.0.1:8123/v1" },
  "models": { "flash": { "modelID": "gemini-flash" } } } } }
```

**MCP server** — let AI models generate images through you:

```bash
gemini-web mcp --session-file gemini_session.json --outdir ./gen_out
```

```jsonc
// opencode.jsonc
{ "mcp": { "servers": { "gemini-image": {
  "type": "local",
  "command": ["gemini-web", "mcp", "--session-file", "/path/to/gemini_session.json", "--outdir", "/path/to/gen_out"] } } } }
```

Same thing works in Claude Desktop under `mcpServers`. The model gets three tools: `generate_image`, `ask`, `list_models`. Images default to the fast preview size so one call can't explode the model's context window 💥 — the full file is always saved to disk.

## Honest limits ⚠️

- Chat, streaming, and image upload all work. **Tool calling is faked** (the web app has no real function-calling, so schemas are sent as text and parsed back) — fine for simple stuff, flaky for complex agents. For heavy agentic work, use a real Gemini API key.
- Each request is stateless — history is re-sent every turn.
- Google rate-limits aggressively. HTTP 429 means "change network or wait" — don't hammer. The batch command stops itself when this happens instead of burning your quota.
- The watermark remover only clears the *visible* sparkle. Gemini also hides an invisible SynthID signal in every image — that stays, and no tool can strip it.

## How it works (the 30-second version) 🔧

Cookie login → `SNlM0e` token from `/app` → file uploads via `content-push` → prompts via `StreamGenerate` → replies parsed from length-prefixed frames. TLS impersonates Chrome 145 so Google doesn't get suspicious. If you're curious, it's all in `gemini_web/protocol.py` — heavily commented.
