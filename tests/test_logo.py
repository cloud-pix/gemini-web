"""Logo remover tests — tiny synthetic images only (no real files, KBs of RAM)."""
import numpy as np
from PIL import Image

from gemini_web import logo


def _synth(w=200, h=150, star=True):
    rng = np.random.default_rng(7)
    arr = (rng.integers(0, 40, (h, w, 3))).astype(np.uint8)
    if star:
        # draw ideal sparkle centered at (0.926w, 0.874h), width ~2.2% of w
        t = logo.star_template(9)
        cx, cy = int(w * 0.926), int(h * 0.874)
        x0, y0 = cx - 4, cy - 4
        patch = arr[y0:y0 + 9, x0:x0 + 9].astype(np.float64)
        m = t > 0.25
        for c in range(3):
            patch[m] = patch[m] * 0.25 + 235 * 0.75
        arr[y0:y0 + 9, x0:x0 + 9] = patch.astype(np.uint8)
    return arr


def test_locate_finds_synth_star():
    arr = _synth()
    found = logo.locate(arr)
    assert found is not None, "star not found"
    score, x, y, size = found
    assert score >= logo.THRESHOLD
    # within a few px of planted center (185, 131)
    assert abs(x + size // 2 - 185) < 8 and abs(y + size // 2 - 131) < 8


def test_locate_clean_image_finds_nothing():
    assert logo.locate(_synth(star=False)) is None


def test_remove_clears_star_keeps_rest():
    arr = _synth()
    before = float(arr[131, 185].mean())
    clean, info = logo.remove_from_array(arr)
    assert info["found"]
    # star center clearly dimmed toward background (was ~230, bg ~20)
    assert float(clean[131, 185].mean()) < before - 60
    # far-away pixels untouched
    assert np.array_equal(clean[5, 5], arr[5, 5])
    assert clean.shape == arr.shape


def test_remove_logo_file_roundtrip(tmp_path):
    src = tmp_path / "a.png"
    Image.fromarray(_synth()).save(src)
    info = logo.remove_logo(str(src))
    assert info["found"] and info["dest"] == str(src)
    assert (tmp_path / "a.orig.png").exists()
    # backup still has the bright star
    orig = np.asarray(Image.open(tmp_path / "a.orig.png"))
    assert orig[131, 185].mean() > 150
    cleaned = np.asarray(Image.open(src))
    assert float(cleaned[131, 185].mean()) < float(orig[131, 185].mean()) - 60


def test_remove_logo_no_star_leaves_file(tmp_path):
    src = tmp_path / "b.png"
    Image.fromarray(_synth(star=False)).save(src)
    before = src.read_bytes()
    info = logo.remove_logo(str(src))
    assert not info["found"] and src.read_bytes() == before
