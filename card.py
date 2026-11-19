"""Shared drawing toolkit for the pictures the bot sends: fonts (with a CJK fallback), text, palette, the card canvas."""
import io
import os
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

# Latin fonts first, then a CJK font so Chinese/Japanese/Korean names (common in Singapore) don't turn into boxes.
LATIN = {
    False: ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"],
    True: ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
           "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
}
CJK = ["/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
       "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]

SS = 2  # draw at 2x and shrink: smooth edges on the rounded shapes and text
BG, PANEL, TRACK = (11, 18, 32), (18, 28, 48), (34, 46, 68)
TEXT, MUTED = (232, 236, 244), (140, 150, 170)
GREEN, RED, AMBER, ZERO = (52, 211, 153), (248, 113, 113), (251, 191, 36), (100, 116, 139)


@lru_cache(maxsize=None)
def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def chain(bold: bool, size: int) -> list[ImageFont.FreeTypeFont]:
    paths = [next((p for p in LATIN[bold] if os.path.exists(p)), None), next((p for p in CJK if os.path.exists(p)), None)]
    return [font(p, size) for p in paths if p]


@lru_cache(maxsize=None)
def has_glyph(font: ImageFont.FreeTypeFont, ch: str) -> bool:
    """A character the font lacks is drawn as its 'missing glyph' box, so compare against that box."""
    if ch.isspace():
        return True
    mask, missing = font.getmask(ch), font.getmask("￿")
    return (mask.size, bytes(mask)) != (missing.size, bytes(missing))


def runs(text: str, chain: list[ImageFont.FreeTypeFont]) -> list[tuple[ImageFont.FreeTypeFont, str]]:
    """Splits text into runs that each use the first font able to draw them; characters no font has (emoji) are dropped."""
    runs: list[tuple[ImageFont.FreeTypeFont, str]] = []
    for ch in text:
        font = next((f for f in chain if has_glyph(f, ch)), None)
        if font is None:
            continue
        if runs and runs[-1][0] is font:
            runs[-1] = (font, runs[-1][1] + ch)
        else:
            runs.append((font, ch))
    return runs


def width(text: str, chain) -> float:
    return sum(f.getlength(s) for f, s in runs(text, chain))


def fit(text: str, chain, max_width: float) -> str:
    text = " ".join("".join(run for _, run in runs(text, chain)).split()) or "?"  # drop unsupported chars, tidy gaps
    while width(text, chain) > max_width and len(text) > 1:
        text = text[:-2].rstrip() + "…" if text.endswith("…") else text[:-1].rstrip() + "…"
    return text


def draw_text(d: ImageDraw.ImageDraw, x: float, y: float, text: str, chain, fill, right: bool = False) -> None:
    """Draws on a shared baseline at y; `right` makes x the right edge."""
    if right:
        x -= width(text, chain)
    for font, run in runs(text, chain):
        d.text((x, y), run, font=font, fill=fill, anchor="ls")
        x += font.getlength(run)


def usable() -> bool:
    """False when no font can be found at all, so callers fall back to text."""
    return bool(chain(False, 10))


def new_card(w: int, h: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """A dark rounded panel on a darker background, w x h final pixels, drawn at SS times that size."""
    img = Image.new("RGB", (w * SS, h * SS), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((28 * SS, 28 * SS, (w - 28) * SS, (h - 28) * SS), radius=44 * SS, fill=PANEL)
    return img, d


def to_png(img: Image.Image, w: int, h: int) -> bytes:
    out = io.BytesIO()
    img.resize((w, h), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()
