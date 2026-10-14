"""Cookie load/save (Netscape txt, JSON dict, or raw values). Mode 600 on save."""
import json
import os
import pathlib

KEYS = ("__Secure-1PSID", "__Secure-1PSIDTS")


def load(path):
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    txt = p.read_text(errors="ignore").strip()
    if txt.startswith("{"):
        try:
            d = json.loads(txt)
            return {k: d[k] for k in KEYS if d.get(k)}
        except Exception:
            return {}
    out = {}
    for line in txt.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and parts[5] in KEYS:
            out[parts[5]] = parts[6]
    return out


def load_any(cookies_path=None, psid=None, psidts=None):
    out = {}
    if cookies_path:
        out.update(load(cookies_path))
    if psid:
        out["__Secure-1PSID"] = psid
    if psidts:
        out["__Secure-1PSIDTS"] = psidts
    env = os.environ.get("GEMINI_PSID")
    if env and "__Secure-1PSID" not in out:
        out["__Secure-1PSID"] = env
    env2 = os.environ.get("GEMINI_PSIDTS")
    if env2 and "__Secure-1PSIDTS" not in out:
        out["__Secure-1PSIDTS"] = env2
    return out


def save(path, cookies):
    p = pathlib.Path(path)
    p.write_text(json.dumps({k: v for k, v in cookies.items() if k in KEYS}, indent=2))
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    return str(p)
