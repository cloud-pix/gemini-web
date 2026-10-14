# gemini-web (FOSS, MIT)

Sync Python client for the **Google Gemini web app** (`gemini.google.com`) — no API key.
Cookie login, file/video upload, `StreamGenerate` prompts. Stdlib + `curl_cffi` only
(no `orjson`/`pydantic`/`loguru`, so it installs on Termux/aarch64).

Protocol derived from [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API)
(init `SNlM0e`/`bl`/`f.sid`/`push_id`, `content-push` upload, 81-slot Relay-style
`StreamGenerate` body, length-prefixed frame parsing, candidate `[1][0]` text).

## Install

```bash
pip install -e .
```

## Login first (required)

The web app needs a logged-in Google session. Export from a browser where you use Gemini:

- Chrome: `⋮ → Settings → Privacy → Third-party cookies → See all site data → google.com`
  — or simply: log into https://gemini.google.com once in Chrome, then
  `yt-dlp --cookies-from-browser chrome --cookies gcookies.txt https://gemini.google.com/`
- Only `__Secure-1PSID` (+ optional `__Secure-1PSIDTS`) is used. Nothing else is read.

```bash
export GEMINI_PSID='paste-__Secure-1PSID'
export GEMINI_PSIDTS='paste-__Secure-1PSIDTS'   # optional
gemini-web login --out gemini_session.json
gemini-web check                               # init + tiny ask, proves execution
gemini-web models                              # models this account can pick
```

## Model selection: yes

Models are discovered per account from the `GetUserStatus` RPC (no hardcoded list),
and `--model` works on `ask`/`analyze`/`analyze-batch` (name or alias, e.g.
`gemini-flash`, `flash`, `4-argon`). Verified live: `gemini-flash-lite`,
`gemini-flash`, `gemini-argon` execute; `gemini-pro` is listed but the server
rejects execution on a free account. Unknown names fail fast with the valid list.

Bot-detection notes: `chrome145` TLS impersonation (avoids Device Bound Session
Credentials), `Origin/Referer: gemini.google.com`, `X-Same-Domain`, `RotateCookies`
refresh, and honest errors — HTTP 429 means Google flagged the IP (change network/VPN,
don't hammer).

## Use

```bash
gemini-web ask "Reply with exactly: OK"
gemini-web ask "Explain this" --model gemini-flash
gemini-web analyze reel.mp4 --prompt "Describe this video in detail..." -o out.txt
gemini-web analyze-batch *.mp4 -o ./analyses --delay 8   # resume-aware, stops on 429
gemini-web gen-image "a baby red panda in a yellow raincoat" -o panda.png
# --preview: smaller =s1024 preview instead of full-size RPC + =s2048 (~1MB vs ~8MB, faster)
gemini-web gen-image "..." -o panda.png --preview
# every gen-image auto-removes the Gemini sparkle watermark (original kept as .orig.png;
# --no-logo skips, --no-original skips backup). Standalone: gemini-web remove-logo in.png
```

## Thinking levels: `--thinking low|medium|high` (you were right)

The level selector exists per model in the account's `GetUserStatus` policy block
(`"Thinking level"`: Low=1 "Quick and efficient", Medium=4 "Balanced depth",
High=2 "Extra thorough") — same codes on all four models. Implemented end to end:
flag on `ask`/`analyze`/`analyze-batch`/`gen-image`, `reasoning_effort` mapping on
the shim, level code mirrored into `inner[80]` and the model header. Default `low`
(= previous behavior). Honest caveat: levels only matter on thinking-capable
models, and this account can't execute Pro; observable differences on Flash are
small.

## Logo remover

`gemini_web/logo.py` (PIL + numpy, no cv2): finds the semi-transparent 4-point
sparkle near bottom-right via template NCC on a ≤640px copy (memory-capped, a few
MB peak), inpaints the bbox region, blends seams, keeps a `.orig` backup. Only
the visible mark — the invisible SynthID signal stays.

## Thinking: `--thinking low|medium|high`

Levels come from the account policy (Low=1, Medium=4, High=2), not guesses.
`--thinking` is supported on `ask`/`analyze`/`analyze-batch`/`gen-image`
(and `reasoning_effort` on the shim maps to it).

`analyze-batch` writes `{stem}.json` (`video`/`prompt`/`analysis`) per file, skips
existing outputs, waits `--delay` between items, and aborts the run on rate limiting
instead of burning quota.

## `serve`: OpenAI-compatible shim (for OpenCode custom provider)

```bash
gemini-web serve --port 8123 --api-key SECRET --default-model gemini-flash
# GET  /v1/models
# POST /v1/chat/completions   (stream:true SSE, tools best-effort, image_url parts)
```

Wire into `opencode.jsonc`:

```jsonc
{ "providers": { "gemini-web": {
  "package": "@opencode/ai/providers/openai-compatible",
  "settings": { "baseURL": "http://127.0.0.1:8123/v1" },
  "models": { "flash": { "modelID": "gemini-flash" } } } } }
```

Honest limits, all verified live: plain chat + SSE streaming + `/v1/models` +
`image_url` upload-attach work; **tool calling is emulated** (schemas injected as
text, fenced ` ```tool_calls ` parsed back) and the model frequently refuses fake
tools or errors on `tool_choice: required`. Context is stateless per request
(history re-sent each turn by the client). Fine for light use, not for heavy
agentic loops — for that use a real Gemini API key instead.
