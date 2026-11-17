"""Draws the ledger as a PNG card for Telegram: one row per player, with a bar showing who is owed and who owes."""
import io
import os
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from money import format_cents as fmt

# Latin fonts first, then a CJK font so Chinese/Japanese/Korean names (common in Singapore) don't turn into boxes.
LATIN = {
    False: ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"],
    True: ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
           "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
}
CJK = ["/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
       "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]

W, PAD, TOP, ROW, BOTTOM = 1080, 72, 196, 118, 118  # final pixel layout
SS = 2  # draw at 2x and shrink: smooth edges on the rounded shapes and text
BG, PANEL, TRACK = (11, 18, 32), (18, 28, 48), (34, 46, 68)
TEXT, MUTED = (232, 236, 244), (140, 150, 170)
GREEN, RED, AMBER, ZERO = (52, 211, 153), (248, 113, 113), (251, 191, 36), (100, 116, 139)


@lru_cache(maxsize=None)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _chain(bold: bool, size: int) -> list[ImageFont.FreeTypeFont]:
    paths = [next((p for p in LATIN[bold] if os.path.exists(p)), None), next((p for p in CJK if os.path.exists(p)), None)]
    return [_font(p, size) for p in paths if p]


@lru_cache(maxsize=None)
def _has_glyph(font: ImageFont.FreeTypeFont, ch: str) -> bool:
    """A character the font lacks is drawn as its 'missing glyph' box, so compare against that box."""
    if ch.isspace():
        return True
    mask, missing = font.getmask(ch), font.getmask("￿")
    return (mask.size, bytes(mask)) != (missing.size, bytes(missing))


def _runs(text: str, chain: list[ImageFont.FreeTypeFont]) -> list[tuple[ImageFont.FreeTypeFont, str]]:
    """Splits text into runs that each use the first font able to draw them; characters no font has (emoji) are dropped."""
    runs: list[tuple[ImageFont.FreeTypeFont, str]] = []
    for ch in text:
        font = next((f for f in chain if _has_glyph(f, ch)), None)
        if font is None:
            continue
        if runs and runs[-1][0] is font:
            runs[-1] = (font, runs[-1][1] + ch)
        else:
            runs.append((font, ch))
    return runs


def _width(text: str, chain) -> float:
    return sum(f.getlength(s) for f, s in _runs(text, chain))


def _fit(text: str, chain, max_width: float) -> str:
    text = " ".join("".join(run for _, run in _runs(text, chain)).split()) or "?"  # drop unsupported chars, tidy gaps
    while _width(text, chain) > max_width and len(text) > 1:
        text = text[:-2].rstrip() + "…" if text.endswith("…") else text[:-1].rstrip() + "…"
    return text


def _draw_text(d: ImageDraw.ImageDraw, x: float, y: float, text: str, chain, fill, right: bool = False) -> None:
    """Draws on a shared baseline at y; `right` makes x the right edge."""
    if right:
        x -= _width(text, chain)
    for font, run in _runs(text, chain):
        d.text((x, y), run, font=font, fill=fill, anchor="ls")
        x += font.getlength(run)


def render_ledger(rows: list[tuple[str, int]], discrepancy: int = 0) -> bytes | None:
    """PNG bytes for (name, balance in cents) rows, or None if there is nothing to draw or no usable font."""
    if not rows or not _chain(False, 10):
        return None
    k = SS
    h = TOP + ROW * (len(rows) + (discrepancy != 0)) + BOTTOM
    img = Image.new("RGB", (W * k, h * k), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((28 * k, 28 * k, (W - 28) * k, (h - 28) * k), radius=44 * k, fill=PANEL)

    title, pill, name_f, amt_f, small = (_chain(True, 58 * k), _chain(True, 28 * k), _chain(False, 40 * k),
                                          _chain(True, 42 * k), _chain(False, 27 * k))
    _draw_text(d, PAD * k, 124 * k, "Ledger", title, TEXT)
    pw = _width("SGD", pill) / k + 44  # currency pill, top right
    d.rounded_rectangle(((W - PAD - pw) * k, 78 * k, (W - PAD) * k, 132 * k), radius=27 * k, outline=MUTED, width=2 * k)
    _draw_text(d, (W - PAD - pw / 2) * k - _width("SGD", pill) / 2, 116 * k, "SGD", pill, MUTED)

    x0, x1 = PAD, W - PAD
    mid = (x0 + x1) / 2
    biggest = max(abs(c) for _, c in rows) or 1
    lines = [(n, c, False) for n, c in rows] + ([("Discrepancy", discrepancy, True)] if discrepancy else [])
    for i, (name, cents, warn) in enumerate(lines):
        y = TOP + i * ROW
        colour = AMBER if warn else GREEN if cents > 0 else RED if cents < 0 else ZERO
        amount = fmt(cents, signed=True)
        amount_w = _width(amount, amt_f) / k
        _draw_text(d, x0 * k, (y + 46) * k, _fit(name, name_f, (x1 - x0 - amount_w - 32) * k), name_f, AMBER if warn else TEXT)
        _draw_text(d, x1 * k, (y + 46) * k, amount, amt_f, colour if cents else MUTED, right=True)
        by = y + 70  # bar: runs right from the centre line for money owed, left for money owing
        d.rounded_rectangle((x0 * k, by * k, x1 * k, (by + 12) * k), radius=6 * k, fill=TRACK)
        if cents:
            reach = max(abs(cents) / biggest * (x1 - x0) / 2, 12)
            left, right = (mid, mid + reach) if cents > 0 else (mid - reach, mid)
            d.rounded_rectangle((left * k, by * k, right * k, (by + 12) * k), radius=6 * k, fill=colour)
        d.rectangle(((mid - 1) * k, (by - 6) * k, (mid + 1) * k, (by + 18) * k), fill=PANEL)  # centre line

    ly = h - BOTTOM + 52
    for dot_x, colour, label in ((x0, GREEN, "Owed money"), (x0 + 270, RED, "Owes money")):
        d.ellipse((dot_x * k, (ly - 20) * k, (dot_x + 18) * k, (ly - 2) * k), fill=colour)
        _draw_text(d, (dot_x + 32) * k, ly * k, label, small, MUTED)

    out = io.BytesIO()
    img.resize((W, h), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()
