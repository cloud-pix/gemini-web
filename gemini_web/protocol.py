"""Protocol constants + pure helpers (no network). Derived from HanaokaYuzu/Gemini-API."""
import json
import re
import uuid

INIT_URL = "https://gemini.google.com/app"
GENERATE_URL = "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"
UPLOAD_URL = "https://content-push.googleapis.com/upload"
ROTATE_URL = "https://accounts.google.com/RotateCookies"

# chrome145 default avoids Device Bound Session Credentials issues
IMPERSONATE = "chrome145"

SN_RE = re.compile(r'"SNlM0e":\s*"(.*?)"')
BL_RE = re.compile(r'"cfb2h":\s*"(.*?)"')
SID_RE = re.compile(r'"FdrFJe":\s*"(.*?)"')
LANG_RE = re.compile(r'"TuX5cc":\s*"(.*?)"')
PUSH_RE = re.compile(r'"qKIAYe":\s*"(.*?)"')

COOKIE_1PSID = "__Secure-1PSID"
COOKIE_1PSIDTS = "__Secure-1PSIDTS"

BATCH_EXEC_URL = "https://gemini.google.com/_/BardChatUi/data/batchexecute"

RPC_GET_USER_STATUS = "otAQ7b"

MODEL_HEADER_KEY = "x-goog-ext-525001261-jspb"
BATCH_EXEC_HEADER_BASE = "[1,null,null,null,null,null,null,null,[4,5,6,8],null,null,null,null,null,null,null]"


def build_model_header(model_id, capacity_tail, model_number):
    return {
        MODEL_HEADER_KEY: f'[1,null,null,null,"{model_id}",null,null,0,[4,5,6,8],null,null,{capacity_tail}, null,null,{model_number}]',
        "x-goog-ext-73010989-jspb": "[0]",
        "x-goog-ext-73010990-jspb": "[0,0,0]",
    }


def compute_capacity(tier_flags, capability_flags):
    tier_flags = tier_flags or []
    capability_flags = capability_flags or []
    if 21 in tier_flags:
        return 1, 13
    if 22 in tier_flags:
        return 2, 13
    if 115 in capability_flags:
        return 4, 12
    if 16 in tier_flags or 106 in capability_flags:
        return 3, 12
    if 8 in tier_flags or 19 in capability_flags:
        return 2, 12
    return 1, 12


def derive_name_aliases(model_id, category_name, display_name):
    import re as _re
    aliases = set()
    if model_id:
        aliases.add(model_id.lower())
    cat = (category_name or "").strip()
    disp = (display_name or "").strip()
    m = _re.search(r"(\d+)(?:\.\d+)?", disp)
    ver = m.group(1) if m else None
    if cat:
        slug = cat.lower().replace(" ", "-")
        aliases.update([cat.lower(), slug, f"gemini-{slug}"])
        if ver:
            aliases.add(f"gemini-{ver}-{slug}")
    if disp:
        dslug = disp.lower().replace(" ", "-")
        aliases.update([disp.lower(), dslug, f"gemini-{dslug}"])
    name = f"gemini-{cat.lower().replace(' ', '-')}" if cat else (
        f"gemini-{disp.lower().replace(' ', '-')}" if disp else f"gemini-{model_id}")
    aliases.add(name)
    return name, sorted(aliases)


def model_from_rpc(model_data, capacity=1, capacity_field=12, unavailable=False):
    if not isinstance(model_data, list) or not model_data:
        return None
    model_id = nested(model_data, [0], "")
    if not model_id or not isinstance(model_id, str):
        return None
    category = str(nested(model_data, [1], "") or nested(model_data, [10], ""))
    display = str(nested(model_data, [11], "") or nested(model_data, [19], "")
                  or nested(model_data, [1], ""))
    desc = str(nested(model_data, [12], "") or nested(model_data, [2], ""))
    raw = nested(model_data, [17])
    if isinstance(raw, int):
        number = raw
    else:
        alt = nested(model_data, [9])
        number = alt if isinstance(alt, int) else 1
    name, aliases = derive_name_aliases(model_id, category, display)
    tail = f"null,{capacity}" if capacity_field == 13 else str(capacity)
    return {
        "model_id": model_id,
        "model_name": name,
        "display_name": display or category or model_id,
        "description": desc,
        "capacity": capacity,
        "capacity_field": capacity_field,
        "model_number": number,
        "is_available": not unavailable,
        "aliases": aliases,
        "header": build_model_header(model_id, tail, number),
    }


def parse_models(parts, unauthenticated=False):
    """AvailableModel dicts from batchexecute parts (GET_USER_STATUS)."""
    models = []
    for part in parts or []:
        if not isinstance(part, list) or len(part) < 3:
            continue
        if nested(part, [1]) != RPC_GET_USER_STATUS:
            continue
        body = nested(part, [2])
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                continue
        if not isinstance(body, list):
            continue
        items = nested(body, [15])
        if not isinstance(items, list):
            continue
        tier = nested(body, [16], []) or []
        caps = nested(body, [17], []) or []
        capacity, field = compute_capacity(tier if isinstance(tier, list) else [],
                                           caps if isinstance(caps, list) else [])
        for m in items:
            inst = model_from_rpc(m, capacity, field,
                                  unavailable=unauthenticated and bool(models))
            if inst:
                models.append(inst)
        if models:
            break
    return models


def batch_f_req(rpcid, payload="[]", identifier="generic"):
    return json.dumps([[[rpcid, payload, None, identifier]]])

BASE_HEADERS = {
    "Origin": "https://gemini.google.com",
    "Referer": "https://gemini.google.com/",
}
GENERATE_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
    **BASE_HEADERS,
}
UPLOAD_HEADERS = {
    "X-Tenant-Id": "bard-storage",
    **BASE_HEADERS,
}
SAME_DOMAIN = {"X-Same-Domain": "1"}

DEFAULT_METADATA = ["", "", "", None, None, None, None, None, None, ""]


def extract_init_values(html):
    """SNlM0e/bl/session/lang/push_id from the /app HTML. None values when absent."""
    def grab(rx):
        m = rx.search(html or "")
        return m.group(1) if m else None
    return {
        "access_token": grab(SN_RE),
        "build_label": grab(BL_RE),
        "session_id": grab(SID_RE),
        "language": grab(LANG_RE) or "en",
        "push_id": grab(PUSH_RE),
    }


def build_inner_request(prompt, req_file_data=None, language="en", metadata=None,
                        thinking="low"):
    """The 81-slot Relay-style inner request list for StreamGenerate.

    thinking: low/medium/high — option codes from the account's "Thinking level"
    policy (Low=1, Medium=4, High=2), written into inner[80]. True/False map to
    high/low for backward compatibility.
    """
    if isinstance(thinking, str):
        level = {"low": 1, "medium": 4, "high": 2}.get(thinking.lower(), 1)
    else:
        level = 2 if thinking else 1
    inner = [None] * 81
    inner[0] = [prompt, 0, None, req_file_data, None, None, 0]
    inner[1] = [language]
    inner[2] = metadata if metadata is not None else list(DEFAULT_METADATA)
    inner[6] = [1]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[0]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    inner[41] = [1]
    inner[53] = 0
    inner[59] = str(uuid.uuid4()).upper()
    inner[61] = []
    inner[68] = 1
    inner[79] = 1
    inner[80] = {"low": 1, "medium": 4, "high": 2}.get(
        thinking.lower(), 1) if isinstance(thinking, str) else (2 if thinking else 1)
    return inner


def build_file_data(upload_ids, filenames):
    """Attachment block: [[[id]], name] per file."""
    return [[ [uid], name] for uid, name in zip(upload_ids, filenames)]


def build_generate_body(access_token, inner):
    return {
        "at": access_token or "",
        "f.req": json.dumps([None, json.dumps(inner)]),
    }


def build_generate_params(language, reqid, build_label=None, session_id=None):
    params = {"hl": language, "_reqid": reqid, "rt": "c"}
    if build_label:
        params["bl"] = build_label
    if session_id:
        params["f.sid"] = session_id
    return params


class FrameParser:
    """Google length-prefixed stream frames (UTF-16 unit accounting). Port of upstream."""

    def __init__(self):
        self.buf = ""
        self.expected = None
        self.start = 0
        self.checked = False

    def feed(self, text):
        if text:
            self.buf += text
        if not self.checked:
            if self.buf.startswith(")]}'"):
                self.buf = self.buf[4:].lstrip()
            self.checked = True
        out = []
        while True:
            if self.expected is None:
                self.buf = self.buf.lstrip()
                m = re.match(r"(\d+)\n", self.buf)
                if not m:
                    break
                self.expected = int(m.group(1))
                self.start = len(m.group(1))
            # count UTF-16 units of available payload
            units, chars, i = 0, 0, self.start
            while units < self.expected and i < len(self.buf):
                w = 2 if ord(self.buf[i]) > 0xFFFF else 1
                if units + w > self.expected:
                    break
                units += w
                chars += 1
                i += 1
            if units < self.expected:
                break
            chunk = self.buf[self.start:self.start + chars]
            self.buf = self.buf[self.start + chars:]
            self.expected = None
            self.start = 0
            if not chunk.strip():
                continue
            try:
                parsed = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                out.extend(parsed)
            else:
                out.append(parsed)
        return out

    def flush(self):
        return self.feed("")


def parse_frames(text):
    p = FrameParser()
    out = p.feed(text or "")
    out.extend(p.flush())
    return out


def nested(data, path, default=None):
    cur = data
    for k in path:
        if isinstance(cur, list) and isinstance(k, int) and -len(cur) <= k < len(cur):
            cur = cur[k]
        elif isinstance(cur, dict) and isinstance(k, str) and k in cur:
            cur = cur[k]
        else:
            return default
    return default if cur is None else cur


def iter_candidates(parts):
    """Yield (rcid, text) from parsed stream parts. Also surfaces fatal error codes."""
    for part in parts:
        code = nested(part, [5, 2, 0, 1, 0])
        if code:
            raise RuntimeError(f"gemini error code {code}")
        inner = nested(part, [2])
        if not inner or not isinstance(inner, str):
            continue
        try:
            pj = json.loads(inner)
        except json.JSONDecodeError:
            continue
        for cand in nested(pj, [4], []) or []:
            rcid = nested(cand, [0])
            text = nested(cand, [1, 0], "")
            if rcid and text:
                yield rcid, text


def best_text(parts):
    """Longest candidate text across frames (streams send cumulative snapshots)."""
    best = ""
    for _, text in iter_candidates(parts):
        if len(text) > len(best):
            best = text
    return best


def iter_candidate_full(parts):
    """Yield (cid, rid, rcid, candidate) with conversation ids tracked across frames."""
    cid, rid = "", ""
    for part in parts or []:
        inner = nested(part, [2])
        if not inner or not isinstance(inner, str):
            continue
        try:
            pj = json.loads(inner)
        except json.JSONDecodeError:
            continue
        m = nested(pj, [1])
        if m:
            cid = nested(m, [0]) or cid
            rid = nested(m, [1]) or rid
        for cand in nested(pj, [4], []) or []:
            rcid = nested(cand, [0])
            if rcid:
                yield cid, rid, rcid, cand


def extract_generated_images(candidate):
    """[{url, alt, image_id}] from a candidate's rich-content field 7 (GENERATED_IMAGES)."""
    out = []
    rich = nested(candidate, [12])
    if not isinstance(rich, list):
        return out
    block = None
    if len(rich) > 7 and isinstance(rich[7], list):
        block = rich[7]
    elif rich and isinstance(rich[-1], dict) and "8" in rich[-1]:
        block = rich[-1]["8"]
    if not block:
        return out
    for item in nested(block, [0], []) or []:
        url = nested(item, [0, 3, 3])
        if not url:
            continue
        out.append({"url": url,
                    "alt": nested(item, [0, 3, 2], ""),
                    "image_id": nested(item, [1, 0], "")})
    return out


def full_size_image_payload(image_id, rid, rcid, cid):
    return [[[None, None, None, [None, None, None, None, None, ""]],
             [image_id, 0], None, [19, ""], None, None, None, None, None, ""],
            [rid, rcid, cid, None, ""], 1, 0, 1]


def upscale_url(url, full_size=True):
    """googleusercontent size-suffix swap (fallback when full-size RPC fails)."""
    if full_size:
        if "=s1024-rj" in url:
            return url.replace("=s1024-rj", "=s2048-rj")
        if "=s2048-rj" not in url:
            return url + "=s2048-rj"
    else:
        if "=s2048-rj" in url:
            return url.replace("=s2048-rj", "=s1024-rj")
        if "=s1024-rj" not in url:
            return url + "=s1024-rj"
    return url
