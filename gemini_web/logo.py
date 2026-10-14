"""Gemini sparkle-watermark remover (PIL + numpy, no cv2).

The web app stamps a semi-transparent white 4-point sparkle near the
bottom-right (~0.926w, ~0.874h, ~2% of width). Detection runs on a downscaled
copy (max 640px wide, <10MB peak) via normalized cross-correlation against an
ideal concave 4-point star; removal = iterative neighbor-mean inpaint on the
small bbox region only, mapped back to full resolution.

Memory-safe by construction: full-res pixels are only touched for a final
crop/paste of the bbox area. Never processes whole large images with
windowed ops.

This removes the VISIBLE mark only. Gemini also embeds an invisible SynthID
signal which this does not (and cannot) remove.
"""
import math
import pathlib

import numpy as np
from PIL import Image, ImageFilter

ZONE = (0.85, 0.78, 1.0, 1.0)
SCALES = (0.014, 0.018, 0.023, 0.030, 0.040)
THRESHOLD = 0.45
STAR_P = 0.55
MAX_DETECT_WIDTH = 640


def star_template(size):
    """Float mask of an ideal 4-point sparkle, `size` px square."""
    ax = np.linspace(-1, 1, size)
    xx, yy = np.meshgrid(ax, ax)
    m = (np.abs(xx) ** STAR_P + np.abs(yy) ** STAR_P) <= 1.0
    return m.astype(np.float64)


def _contrast_small(gray):
    """Local contrast vs median background on a SMALL array."""
    bg = np.asarray(Image.fromarray(gray).filter(ImageFilter.MedianFilter(21)),
                    dtype=np.float64)
    return np.clip(gray.astype(np.float64) - bg, 0, None)


def _ncc_best(search, templ):
    """Best NCC of templ over search (both small). Returns (score, x, y)."""
    sh, sw = search.shape
    th, tw = templ.shape
    if th > sh or tw > sw or th < 5:
        return -1.0, 0, 0
    t = templ - templ.mean()
    tss = (t ** 2).sum()
    if tss <= 1e-9:
        return -1.0, 0, 0
    # integral images for box mean/variance (low memory)
    I = np.zeros((sh + 1, sw + 1))
    I[1:, 1:] = search
    S = I.cumsum(0).cumsum(1)
    I2 = np.zeros((sh + 1, sw + 1))
    I2[1:, 1:] = search ** 2
    S2 = I2.cumsum(0).cumsum(1)
    n = th * tw
    # cross-correlation via FFT (small arrays only)
    F1 = np.fft.fft2(search, s=(sh + th - 1, sw + tw - 1))
    F2 = np.fft.fft2(t[::-1, ::-1], s=(sh + th - 1, sw + tw - 1))
    xcorr = np.real(np.fft.ifft2(F1 * F2))[th - 1:sh, tw - 1:sw]
    s00 = S[th:, tw:] - S[:-th, tw:] - S[th:, :-tw] + S[:-th, :-tw]
    s200 = S2[th:, tw:] - S2[:-th, tw:] - S2[th:, :-tw] + S2[:-th, :-tw]
    num = xcorr - s00 * t.mean()
    den = np.sqrt(np.maximum(s200 - s00 ** 2 / n, 0) * tss)
    with np.errstate(divide="ignore", invalid="ignore"):
        ncc = np.where(den > 1e-9, num / den, 0.0)
    j, i = np.unravel_index(int(np.argmax(ncc)), ncc.shape)
    return float(ncc[j, i]), int(i), int(j)


def locate(arr):
    """Find sparkle in RGB/gray numpy array. Returns (score, x, y, size) or None."""
    if arr.ndim == 3:
        gray = (0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2])
    else:
        gray = arr.astype(np.float64)
    h, w = gray.shape
    # downscale for detection (memory cap)
    scale = min(1.0, MAX_DETECT_WIDTH / w)
    if scale < 1.0:
        small = np.asarray(Image.fromarray(np.clip(gray, 0, 255).astype(np.uint8))
                           .resize((int(w * scale), int(h * scale)), Image.BILINEAR))
    else:
        small = np.clip(gray, 0, 255).astype(np.uint8)
    sh, sw = small.shape
    x0, y0 = int(sw * ZONE[0]), int(sh * ZONE[1])
    zone = small[y0:, x0:]
    con = _contrast_small(zone)
    if con.max() < 8:
        return None
    best = (-1.0, 0, 0, 0)
    for s in SCALES:
        size = max(9, int(sw * s))
        if size % 2 == 0:
            size += 1
        score, ix, iy = _ncc_best(con, star_template(size))
        if score > best[0]:
            best = (score, ix, iy, size)
    if best[0] < THRESHOLD:
        return None
    _, ix, iy, size = best
    inv = 1.0 / scale
    return best[0], int((x0 + ix) * inv), int((y0 + iy) * inv), int(size * inv)


def _inpaint_mean(arr, mask):
    out = arr.astype(np.float64).copy()
    m = mask.copy()
    padded = np.zeros((m.shape[0] + 2, m.shape[1] + 2), bool)
    for _ in range(500):
        if not m.any():
            break
        padded[1:-1, 1:-1] = ~m
        known = np.zeros_like(m, dtype=np.int32)
        acc = np.zeros(out.shape, dtype=np.float64)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                k = padded[1 + dy:m.shape[0] + 1 + dy, 1 + dx:m.shape[1] + 1 + dx]
                known += k
                if out.ndim == 3:
                    acc += np.roll(np.roll(out, dy, 0), dx, 1) * k[..., None]
                else:
                    acc += np.roll(np.roll(out, dy, 0), dx, 1) * k
        fillable = m & (known > 0)
        if not fillable.any():
            break
        if out.ndim == 3:
            out[fillable] = acc[fillable] / known[fillable][..., None]
        else:
            out[fillable] = acc[fillable] / known[fillable]
        m[fillable] = False
    return np.clip(out, 0, 255).astype(np.uint8)


def remove_from_array(arr):
    """Returns (cleaned_uint8, info_dict). info has found/score/bbox."""
    found = locate(arr)
    if not found:
        return arr, {"found": False}
    score, x, y, size = found
    h, w = arr.shape[:2]
    pad = max(2, size // 12)
    t = star_template(size)
    mask = np.zeros((h, w), bool)
    x1, y1 = max(0, x - pad), max(0, y - pad)
    x2, y2 = min(w, x + size + pad), min(h, y + size + pad)
    sub = t[:y2 - y1, :x2 - x1]
    region = np.zeros((y2 - y1, x2 - x1), bool)
    region[:sub.shape[0], :sub.shape[1]] = sub > 0.25
    mask[y1:y2, x1:x2] = region
    d = mask.copy()
    d[1:, :] |= mask[:-1, :]
    d[:-1, :] |= mask[1:, :]
    d[:, 1:] |= mask[:, :-1]
    d[:, :-1] |= mask[:, 1:]
    # inpaint on the bbox crop only (small), paste back
    cx1, cy1 = max(0, x1 - pad * 2), max(0, y1 - pad * 2)
    cx2, cy2 = min(w, x2 + pad * 2), min(h, y2 + pad * 2)
    crop = arr[cy1:cy2, cx1:cx2]
    crop_mask = d[cy1:cy2, cx1:cx2]
    filled = _inpaint_mean(crop, crop_mask)
    out = arr.copy()
    out[cy1:cy2, cx1:cx2] = filled
    img = Image.fromarray(out if out.ndim == 3 else out)
    blur = img.crop((cx1, cy1, cx2, cy2)).filter(ImageFilter.GaussianBlur(1.5))
    img.paste(blur, (cx1, cy1))
    return np.asarray(img), {"found": True, "score": round(score, 3),
                             "bbox": [x1, y1, x2, y2]}


def remove_logo(src, dest=None, keep_original=True):
    """Remove sparkle from file. Returns info dict (found/score/bbox/dest)."""
    p = pathlib.Path(src)
    im = Image.open(p).convert("RGB")
    arr = np.asarray(im)
    clean, info = remove_from_array(arr)
    if not info.get("found"):
        info["dest"] = str(p)
        return info
    out = pathlib.Path(dest) if dest else p
    if keep_original and out.resolve() == p.resolve():
        backup = p.with_name(p.stem + ".orig" + p.suffix)
        if not backup.exists():
            p.replace(backup)
            info["original"] = str(backup)
    fmt = (out.suffix or ".png").lstrip(".").upper()
    if fmt == "JPG":
        fmt = "JPEG"
    Image.fromarray(clean).save(out, fmt)
    info["dest"] = str(out)
    return info
