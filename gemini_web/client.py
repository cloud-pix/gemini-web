"""Sync Gemini web client: init (SNlM0e) -> upload -> StreamGenerate.

Bot-detection notes (from upstream + practice):
- impersonate chrome145 (avoids Device Bound Session Credentials prompts)
- Origin/Referer gemini.google.com + X-Same-Domain on generate
- full cookie jar on .google.com; RotateCookies keeps 1PSIDTS fresh
- HTTP 429 from Google = IP flagged; surface it, don't hammer
"""
import mimetypes
import pathlib
import random
import time
import uuid

from curl_cffi import requests as _cr

from . import protocol as P


class GeminiError(Exception):
    pass


class AuthError(GeminiError):
    pass


class BlockedError(GeminiError):
    pass


class Client:
    def __init__(self, cookies, timeout=300, verbose=False):
        self.cookies = dict(cookies or {})
        self.timeout = timeout
        self.verbose = verbose
        self.s = _cr.Session(impersonate=P.IMPERSONATE)
        for k, v in self.cookies.items():
            self.s.cookies.set(k, v, domain=".google.com", path="/")
        self.access_token = None
        self.build_label = None
        self.session_id = None
        self.language = "en"
        self.push_id = None
        self._reqid = random.randint(10000, 99999)
        self._sessionid = str(uuid.uuid4()).upper()
        self._models = None

    def _log(self, msg):
        if self.verbose:
            print(msg, flush=True)

    def init(self):
        """GET /app -> SNlM0e + bl + session + push_id. Raises AuthError/BlockedError."""
        try:
            r = self.s.get(P.INIT_URL, headers=P.BASE_HEADERS, timeout=30)
        except Exception as e:
            raise GeminiError(f"init request failed: {type(e).__name__}: {e}")
        if r.status_code == 429:
            raise BlockedError("Google rate-limited this IP (HTTP 429). Change network/VPN or wait.")
        if r.status_code != 200:
            raise AuthError(f"init HTTP {r.status_code}: cookies likely expired/invalid")
        vals = P.extract_init_values(r.text)
        if not vals["access_token"]:
            # merge anonymous google.com cookies (consent/NID) and retry once,
            # mirroring upstream preflight; user cookies always win
            try:
                g = self.s.get("https://www.google.com/", timeout=20)
                if g.status_code == 200:
                    r = self.s.get(P.INIT_URL, headers=P.BASE_HEADERS, timeout=30)
                    if r.status_code == 200:
                        vals = P.extract_init_values(r.text)
            except Exception as e:
                self._log(f"preflight failed: {e}")
        # merge freshly issued cookies; user values win
        for c in self.s.cookies.jar:
            if c.name not in self.cookies and c.value:
                self.cookies[c.name] = c.value
        if not vals["access_token"]:
            raise AuthError("no SNlM0e in /app response: not logged in (export fresh __Secure-1PSID)")
        self.access_token = vals["access_token"]
        self.build_label = vals["build_label"]
        self.session_id = vals["session_id"]
        self.language = vals["language"] or "en"
        self.push_id = vals["push_id"]
        self._log(f"init ok (bl={self.build_label} sid={bool(self.session_id)} push={bool(self.push_id)})")
        return vals

    def rotate(self):
        """Refresh __Secure-1PSIDTS via accounts.google.com/RotateCookies (best effort)."""
        try:
            r = self.s.post(P.ROTATE_URL, headers={"Content-Type": "application/json",
                                                   "Origin": "https://accounts.google.com"},
                            json={}, timeout=20)
            fresh = {c.name: c.value for c in self.s.cookies.jar}
            if fresh.get(P.COOKIE_1PSIDTS):
                self.cookies[P.COOKIE_1PSIDTS] = fresh[P.COOKIE_1PSIDTS]
                return True
        except Exception as e:
            self._log(f"rotate failed: {e}")
        return False

    def upload(self, path, filename=None):
        """Multipart upload -> file id (/contrib_service/...)."""
        p = pathlib.Path(path)
        if not p.is_file():
            raise GeminiError(f"not a file: {path}")
        return self.upload_bytes(p.read_bytes(), filename or p.name)

    def upload_bytes(self, data, filename):
        """Multipart upload of in-memory bytes -> file id."""
        name = filename or "upload.bin"
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        headers = dict(P.UPLOAD_HEADERS)
        if self.push_id:
            headers["Push-ID"] = self.push_id
        from curl_cffi import CurlMime
        mime = CurlMime()
        mime.addpart(name="file", content_type=ctype, filename=name, data=data)
        try:
            r = self.s.post(P.UPLOAD_URL, headers=headers, multipart=mime, timeout=120)
        except Exception as e:
            raise GeminiError(f"upload failed: {type(e).__name__}: {e}")
        finally:
            try:
                mime.close()
            except Exception:
                pass
        if r.status_code == 429:
            raise BlockedError("upload rate-limited (HTTP 429)")
        if r.status_code != 200:
            raise GeminiError(f"upload HTTP {r.status_code}: {r.text[:150]}")
        fid = (r.text or "").strip()
        if not fid.startswith("/"):
            raise GeminiError(f"unexpected upload response: {fid[:150]}")
        self._log(f"uploaded {name} -> {fid[:60]}...")
        return fid

    def batch(self, rpcid, payload="[]", source_path="/", timeout=60):
        """One batchexecute call -> parsed parts list."""
        import json as _json
        if not self.access_token:
            raise AuthError("call init() first")
        params = {"rpcids": rpcid, "hl": self.language, "_reqid": self._reqid,
                  "rt": "c", "source-path": source_path}
        self._reqid += 100000
        if self.build_label:
            params["bl"] = self.build_label
        if self.session_id:
            params["f.sid"] = self.session_id
        base = _json.loads(P.BATCH_EXEC_HEADER_BASE)
        base.append(self._sessionid)
        headers = dict(P.GENERATE_HEADERS)
        headers[P.MODEL_HEADER_KEY] = _json.dumps(base)
        headers["x-goog-ext-73010989-jspb"] = "[0]"
        headers.update(P.SAME_DOMAIN)
        try:
            r = self.s.post(P.BATCH_EXEC_URL, params=params, headers=headers,
                            data={"at": self.access_token,
                                  "f.req": P.batch_f_req(rpcid, payload)},
                            timeout=timeout)
        except Exception as e:
            raise GeminiError(f"batch failed: {type(e).__name__}: {e}")
        if r.status_code == 429:
            raise BlockedError("batch rate-limited (HTTP 429)")
        if r.status_code != 200:
            raise GeminiError(f"batch HTTP {r.status_code}: {r.text[:150]}")
        return P.parse_frames(r.text)

    def list_models(self, refresh=False):
        """Available models for this account (from GetUserStatus RPC)."""
        if self._models is not None and not refresh:
            return self._models
        parts = self.batch(P.RPC_GET_USER_STATUS, "[]")
        self._models = P.parse_models(parts)
        return self._models

    def resolve_model(self, name):
        if not name:
            return None
        models = self.list_models()
        key = name.strip().lower()
        for m in models:
            if key == m["model_name"].lower() or key in m["aliases"]:
                if not m["is_available"]:
                    raise GeminiError(f"model '{name}' is not available on this account/session")
                return m
        avail = ", ".join(m["model_name"] for m in models) or "none listed"
        raise GeminiError(f"unknown model '{name}'. Available: {avail}")

    def generate(self, prompt, file_ids=(), filenames=(), timeout=None, model=None,
                   thinking="low"):
        text, _, _, _ = self._generate_parts(prompt, file_ids, filenames, timeout, model,
                                             thinking)
        if not text:
            raise GeminiError("empty reply (model returned no text; possibly blocked prompt)")
        return text

    def _generate_parts(self, prompt, file_ids=(), filenames=(), timeout=None, model=None,
                        thinking="low"):
        """One StreamGenerate call -> full reply text (non-streaming collect)."""
        if not self.access_token:
            raise AuthError("call init() first")
        selected = self.resolve_model(model) if model else None
        req_file_data = P.build_file_data(file_ids, filenames) if file_ids else None
        inner = P.build_inner_request(prompt, req_file_data, self.language, thinking=thinking)
        if selected:
            inner[79] = selected["model_number"]
        body = P.build_generate_body(self.access_token, inner)
        params = P.build_generate_params(self.language, self._reqid,
                                         self.build_label, self.session_id)
        self._reqid += 100000
        headers = dict(P.GENERATE_HEADERS)
        headers.update(P.SAME_DOMAIN)
        if selected:
            import json as _json
            mh = dict(selected["header"])
            parsed = _json.loads(mh[P.MODEL_HEADER_KEY])
            lvl = thinking.lower() if isinstance(thinking, str) else None
            parsed.append({"low": 1, "medium": 4, "high": 2}.get(lvl, 2 if thinking else 1))
            parsed.append(self._sessionid)
            mh[P.MODEL_HEADER_KEY] = _json.dumps(parsed)
            headers.update(mh)
        headers["x-goog-ext-525005358-jspb"] = f'["{inner[59]}",1]'
        try:
            r = self.s.post(P.GENERATE_URL, params=params, headers=headers,
                            data=body, timeout=timeout or self.timeout)
        except Exception as e:
            raise GeminiError(f"generate failed: {type(e).__name__}: {e}")
        if r.status_code == 429:
            raise BlockedError("generate rate-limited (HTTP 429). Wait before retrying.")
        if r.status_code != 200:
            raise GeminiError(f"generate HTTP {r.status_code}: {r.text[:200]}")
        try:
            parts = P.parse_frames(r.text)
        except Exception as e:
            raise GeminiError(f"unparseable stream: {e}")
        try:
            text = P.best_text(parts)
        except RuntimeError as e:
            raise GeminiError(str(e))
        cid, rid = "", ""
        for c, r, _, _ in P.iter_candidate_full(parts):
            cid, rid = c or cid, r or rid
        if not text:
            # image-only replies may carry no text; keep parts for media extraction
            pass
        return text, parts, cid, rid

    def generate_images(self, prompt, timeout=None, model=None, thinking="low"):
        """Ask for image generation -> [{url, alt, image_id, cid, rid}]."""
        _, parts, cid, rid = self._generate_parts(prompt, timeout=timeout, model=model,
                                                  thinking=thinking)
        found = []
        for c, r, rcid, cand in P.iter_candidate_full(parts):
            for img in P.extract_generated_images(cand):
                img.update({"cid": c or cid, "rid": r or rid, "rcid": rcid})
                found.append(img)
        return found

    def full_size_url(self, image_id, rid, rcid, cid):
        """Resolve original-size image URL via DOWNLOAD_GENERATED_IMAGE RPC."""
        import json as _json
        payload = _json.dumps(P.full_size_image_payload(image_id, rid, rcid, cid))
        parts = self.batch("c8o8Fe", payload, source_path="/app")
        for part in parts or []:
            if not isinstance(part, list) or len(part) < 3:
                continue
            if P.nested(part, [1]) != "c8o8Fe":
                continue
            body = P.nested(part, [2])
            if isinstance(body, str):
                try:
                    body = _json.loads(body)
                except Exception:
                    continue
            url = P.nested(body, [0])
            if url:
                return url
        return None

    def download_image(self, url, dest, timeout=120):
        """GET image bytes (authed session + Referer) -> dest path."""
        try:
            r = self.s.get(url, headers=dict(P.BASE_HEADERS), timeout=timeout)
        except Exception as e:
            raise GeminiError(f"image download failed: {type(e).__name__}: {e}")
        if r.status_code != 200 or not r.content:
            raise GeminiError(f"image download HTTP {r.status_code}")
        p = pathlib.Path(dest)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(r.content)
        return str(p)

    def generate_image(self, prompt, dest, timeout=None, model=None, full_size=True,
                       thinking="low"):
        """Generate one image and save it. Returns dest path."""
        found = self.generate_images(prompt, timeout=timeout, model=model, thinking=thinking)
        if not found:
            raise GeminiError("no generated image in reply (prompt may have been refused)")
        img = found[0]
        url = img["url"]
        if full_size and img.get("image_id") and img.get("cid"):
            try:
                orig = self.full_size_url(img["image_id"], img["rid"], img["rcid"], img["cid"])
                if orig:
                    t = self.s.get(orig + "=d-I?alr=yes", headers=dict(P.BASE_HEADERS),
                                   timeout=60).text.strip()
                    if t.startswith("http"):
                        url = self.s.get(t, headers=dict(P.BASE_HEADERS),
                                         timeout=60).text.strip() or url
            except Exception:
                url = P.upscale_url(url, True)
        else:
            url = P.upscale_url(url, full_size)
        return self.download_image(url, dest)

    def ask(self, prompt, timeout=None, model=None, thinking="low"):
        return self.generate(prompt, timeout=timeout, model=model, thinking=thinking)

    def analyze_video(self, path, prompt, timeout=None, model=None, thinking="low"):
        fid = self.upload(path)
        return self.generate(prompt, [fid], [pathlib.Path(path).name], timeout, model,
                             thinking)
