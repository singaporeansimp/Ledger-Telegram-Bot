"""Draws profit as a PNG: one line per player on one chart, or a single player with wins and losses shaded.

Profit only moves when a session is recorded, so lines are steps: flat between sessions, a jump at each one.
The horizontal axis is either time, or the sessions themselves (evenly spaced, labelled with their entry numbers).
A time window shows the profit made within it, starting from zero.
"""
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from card import (AMBER, GREEN, MUTED, RED, SS, TEXT, TRACK, chain, draw_text, fit, new_card, to_png, usable, width)
from money import format_cents as fmt
from store import ProfitEvent

SGT = ZoneInfo("Asia/Singapore")
W, PAD, PLOT_LEFT, PLOT_H = 1080, 72, 166, 440
ZERO_LINE = (84, 100, 128)
# One colour per player, assigned by their place in the ledger so a player keeps theirs from chart to chart.
PALETTE = [(52, 211, 153), (96, 165, 250), (251, 191, 36), (244, 114, 182), (167, 139, 250),
           (251, 146, 60), (163, 230, 53), (34, 211, 238), (248, 113, 113), (148, 163, 184)]


_UNITS = {"h": (timedelta(hours=1), "hour"), "d": (timedelta(days=1), "day"),
          "w": (timedelta(weeks=1), "week"), "m": (timedelta(days=30), "month")}


@dataclass(frozen=True)
class Window:
    """The last 24 hours, 7 days, 2 weeks, a month ...: how far back a chart looks."""
    token: str  # canonical text, e.g. "7d", which is also how it travels in a button
    delta: timedelta
    label: str  # "last 7 days"

    @classmethod
    def parse(cls, text: str) -> "Window | None":
        found = re.fullmatch(r"(\d{1,3})([hdwm])", text.strip().lower())
        if not found or not int(found[1]):
            return None
        amount, (unit, name) = int(found[1]), _UNITS[found[2]]
        if unit * amount > timedelta(days=3650):
            return None
        return cls(f"{amount}{found[2]}", unit * amount, f"last {name}" if amount == 1 else f"last {amount} {name}s")


def within(events: list[ProfitEvent], now: datetime, window: Window | None) -> list[ProfitEvent]:
    """The sessions inside the window (all of them if there is none)."""
    return [e for e in events if e.at >= now - window.delta] if window else events


def steps(points: Sequence[tuple[float, dict[int, int]]], member_id: int) -> list[tuple[float, int]]:
    """(x, profit just after) for each session that moved this player's profit; points are (x, deltas) in order."""
    total, out = 0, []
    for x, deltas in points:
        if delta := deltas.get(member_id, 0):
            total += delta
            out.append((x, total))
    return out


def nice_ticks(lo: float, hi: float, target: int = 7) -> list[int]:
    """Round-numbered values (cents) covering lo..hi: steps of 1, 2, 2.5 or 5 times a power of ten dollars."""
    raw = max(hi - lo, 100) / target
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    step = max(int(round(step)), 1)
    first = math.ceil(lo / step) * step
    return list(range(first, int(hi) + 1, step))


def axis_label(cents: int, step: int) -> str:
    if cents == 0:
        return "0"
    return f"{cents / 100:,.0f}" if step % 100 == 0 else f"{cents / 100:,.2f}"


def time_label(at: datetime, span: timedelta) -> str:
    local = at.astimezone(SGT)
    return f"{local.day} {local:%b}" if span >= timedelta(days=2) else f"{local.day} {local:%b %H:%M}"


def render_profit(events: list[ProfitEvent], names: dict[int, str], only: int | None, now: datetime,
                  window: Window | None = None, by_entries: bool = False) -> bytes | None:
    """PNG of profit up to `now`. only=None draws everyone who has played; only=a player draws just them.
    window keeps only the sessions in that stretch (profit then starts from zero at its start); by_entries spaces
    the sessions evenly instead of by when they happened. None if there is nothing to draw or no font can be found."""
    if not usable():
        return None
    events = within(events, now, window)
    if not events:
        return None
    xs = [float(i) for i in range(1, len(events) + 1)] if by_entries else [e.at.timestamp() for e in events]
    points = [(x, e.deltas) for x, e in zip(xs, events)]
    ids = [only] if only is not None else list(names)
    series = {m: steps(points, m) for m in ids if m in names}
    series = {m: pts for m, pts in series.items() if pts}
    if not series:
        return None
    colour_of = {m: PALETTE[i % len(PALETTE)] for i, m in enumerate(sorted(names))}
    solo = only is not None
    final = {m: pts[-1][1] for m, pts in series.items()}
    ranking = sorted(series, key=lambda m: (-final[m], names[m].lower()))

    # --- axes
    if by_entries:
        first, last = xs[0], xs[-1]
        pad = max((last - first) * 0.04, 0.5)
    else:
        if window:
            first, last = (now - window.delta).timestamp(), now.timestamp()  # the whole window, not just where sessions were
        else:
            first = min(pts[0][0] for pts in series.values())
            last = max(max(pts[-1][0] for pts in series.values()), now.timestamp())
            last = max(last, first + 3600)
        pad = (last - first) * 0.04
    t0, t1 = first - pad, last + pad
    values = [0] + [v for pts in series.values() for _, v in pts]
    lo, hi = min(values), max(values)
    margin = max(hi - lo, 100) * 0.08
    ticks = nice_ticks(lo - margin, hi + margin)
    y_lo, y_hi = min(lo - margin, ticks[0]), max(hi + margin, ticks[-1])
    step = ticks[1] - ticks[0] if len(ticks) > 1 else 100

    k = SS
    title, pill, big, label, name_f, num_f = (chain(True, 56 * k), chain(True, 28 * k), chain(True, 76 * k),
                                              chain(False, 26 * k), chain(False, 30 * k), chain(True, 30 * k))
    top = 262 if solo else 196
    # leave room on the left for the widest axis label (a million-dollar figure needs more than a small one)
    left = max(PLOT_LEFT, 62 + max(width(axis_label(t, step), label) for t in ticks) / k + 18)
    right = W - PAD
    bottom = top + PLOT_H
    legend_rows = 0 if solo else math.ceil(len(ranking) / 2)
    h = bottom + 84 + (112 if solo else legend_rows * 58) + 44
    X = lambda t: left + (t - t0) / (t1 - t0) * (right - left)  # noqa: E731
    Y = lambda v: bottom - (v - y_lo) / (y_hi - y_lo) * (bottom - top)  # noqa: E731

    def path(pts: list[tuple[float, int]]) -> list[tuple[float, float]]:
        value, out = 0, [(X(t0), Y(0))]
        for x, after in pts:
            out += [(X(x), Y(value)), (X(x), Y(after))]
            value = after
        return [*out, (X(t1), Y(value))]

    img, d = new_card(W, h)
    if solo:  # shade between the line and zero: green while up, red while down
        shade = Image.new("RGBA", img.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(shade)
        pts = path(series[only])
        for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
            if ya == yb and ya != Y(0):
                sd.rectangle((xa * k, min(ya, Y(0)) * k, xb * k, max(ya, Y(0)) * k),
                             fill=(*(GREEN if ya < Y(0) else RED), 64))
        img = Image.alpha_composite(img.convert("RGBA"), shade).convert("RGB")
        d = ImageDraw.Draw(img)

    pw = width("SGD", pill) / k + 44
    d.rounded_rectangle(((W - PAD - pw) * k, 78 * k, (W - PAD) * k, 132 * k), radius=27 * k, outline=MUTED, width=2 * k)
    draw_text(d, (W - PAD - pw / 2) * k - width("SGD", pill) / 2, 116 * k, "SGD", pill, MUTED)
    room = (W - 2 * PAD - pw - 30) * k
    if solo:
        draw_text(d, PAD * k, 124 * k, fit(names[only], title, room), title, TEXT)
        now_profit = final[only]
        colour = GREEN if now_profit > 0 else RED if now_profit < 0 else MUTED
        draw_text(d, PAD * k, 214 * k, fmt(now_profit, signed=True), big, colour)
        since = f"profit in the {window.label}" if window else "profit so far"
        draw_text(d, (PAD + width(fmt(now_profit, signed=True), big) / k + 18) * k, 214 * k, since, label, MUTED)
    else:
        draw_text(d, PAD * k, 124 * k, "Profit by entry" if by_entries else "Profit over time", title, TEXT)
        bits = [f"{len(series)} players"]
        if by_entries:
            bits.append(f"{len(events)} sessions")
        if window:
            bits.append(window.label)
        elif not by_entries:
            a, b = (datetime.fromtimestamp(x, tz=timezone.utc).astimezone(SGT) for x in (first, last))
            bits.append(f"{a.day} {a:%b %Y}" if a.date() == b.date() else f"{a.day} {a:%b} – {b.day} {b:%b %Y}")
        draw_text(d, PAD * k, 170 * k, " · ".join(bits), label, MUTED)

    for tick in ticks:  # horizontal grid, the zero line stronger
        y = Y(tick)
        d.line([(left * k, y * k), (right * k, y * k)], fill=ZERO_LINE if tick == 0 else TRACK, width=(3 if tick == 0 else 2) * k)
        draw_text(d, (left - 18) * k, (y + 9) * k, axis_label(tick, step), label, MUTED, right=True)
    span_t = timedelta(seconds=last - first)

    def tick_labels(count: int) -> list[tuple[float, str]]:  # (x, text), spread from `first` to `last`
        at = [first + (last - first) * i / (count - 1) for i in range(count)] if count > 1 else [first]
        if by_entries:  # snap to a real session and name it by its entry number
            nearest = [min(max(round(x), 1), len(events)) for x in at]
            return [(float(n), f"#{events[n - 1].seq}") for n in nearest]
        return [(x, time_label(datetime.fromtimestamp(x, tz=timezone.utc), span_t)) for x in at]

    counts = [c for c in (5, 4, 3, 2) if not by_entries or c <= len(events)] or [1]
    for count in counts:  # as many labels as fit without touching
        labels = tick_labels(count)
        half = [width(text, label) / k / 2 for _, text in labels]
        centres = [min(max(X(x), PAD + half[i]), right + 10 - half[i]) for i, (x, _) in enumerate(labels)]
        if all(centres[i + 1] - half[i + 1] - (centres[i] + half[i]) >= 24 for i in range(count - 1)):
            break
    for (_, text), centre, w in zip(labels, centres, half):
        draw_text(d, (centre - w) * k, (bottom + 46) * k, text, label, MUTED)

    for m in reversed(ranking):  # the leaders last, so they sit on top
        colour = TEXT if solo else colour_of[m]
        pts = [(x * k, y * k) for x, y in path(series[m])]
        d.line(pts, fill=colour, width=(6 if solo else 5) * k, joint="curve")
        ex, ey = pts[-1]
        d.ellipse((ex - 9 * k, ey - 9 * k, ex + 9 * k, ey + 9 * k), fill=colour, outline=(18, 28, 48), width=3 * k)

    if solo:
        profit = [v for _, v in series[only]]
        latest = profit[-1] - (profit[-2] if len(profit) > 1 else 0)
        stats = [("Peak", fmt(max(profit + [0]), signed=True)), ("Low", fmt(min(profit + [0]), signed=True)),
                 ("Last session", fmt(latest, signed=True)), ("Played", f"{len(series[only])} of {len(events)}")]
        size = 40
        while size > 24:  # shrink the figures until the three blocks fit side by side with room between them
            value_f = chain(True, size * k)
            blocks = [max(width(n, label), width(v, value_f)) / k for n, v in stats]
            if sum(blocks) + 40 * (len(stats) - 1) <= W - 2 * PAD:
                break
            size -= 2
        x = PAD
        for (name, value), block in zip(stats, blocks):
            draw_text(d, x * k, (bottom + 138) * k, name, label, MUTED)
            c = GREEN if value.startswith("+") else RED if value.startswith("-") else TEXT
            draw_text(d, x * k, (bottom + 182) * k, value, value_f, c)
            x += block + (W - 2 * PAD - sum(blocks)) / (len(stats) - 1)  # spread what is left evenly
    else:
        col_w = (W - 2 * PAD) / 2
        for i, m in enumerate(ranking):
            x, y = PAD + (i % 2) * col_w, bottom + 128 + (i // 2) * 58
            d.rounded_rectangle((x * k, (y - 18) * k, (x + 30) * k, (y - 8) * k), radius=5 * k, fill=colour_of[m])
            value = fmt(final[m], signed=True)
            vw = width(value, num_f) / k
            draw_text(d, (x + 44) * k, y * k, fit(names[m], name_f, (col_w - 44 - vw - 40) * k), name_f, TEXT)
            draw_text(d, (x + col_w - 36) * k, y * k, value, num_f, GREEN if final[m] > 0 else RED if final[m] < 0 else MUTED, right=True)
    return to_png(img, W, h)
