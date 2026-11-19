"""Draws the ledger as a PNG card for Telegram: one row per player, with a bar showing who is owed and who owes."""
from card import (AMBER, GREEN, MUTED, PANEL, RED, SS, TEXT, TRACK, ZERO, chain, draw_text, fit, new_card, to_png,
                  usable, width)
from money import format_cents as fmt

W, PAD, TOP, ROW, BOTTOM = 1080, 72, 196, 118, 118  # final pixel layout


def render_ledger(rows: list[tuple[str, int]], discrepancy: int = 0) -> bytes | None:
    """PNG bytes for (name, balance in cents) rows, or None if there is nothing to draw or no usable font."""
    if not rows or not usable():
        return None
    k = SS
    h = TOP + ROW * (len(rows) + (discrepancy != 0)) + BOTTOM
    img, d = new_card(W, h)

    title, pill, name_f, amt_f, small = (chain(True, 58 * k), chain(True, 28 * k), chain(False, 40 * k),
                                          chain(True, 42 * k), chain(False, 27 * k))
    draw_text(d, PAD * k, 124 * k, "Ledger", title, TEXT)
    pw = width("SGD", pill) / k + 44  # currency pill, top right
    d.rounded_rectangle(((W - PAD - pw) * k, 78 * k, (W - PAD) * k, 132 * k), radius=27 * k, outline=MUTED, width=2 * k)
    draw_text(d, (W - PAD - pw / 2) * k - width("SGD", pill) / 2, 116 * k, "SGD", pill, MUTED)

    x0, x1 = PAD, W - PAD
    mid = (x0 + x1) / 2
    biggest = max(abs(c) for _, c in rows) or 1
    lines = [(n, c, False) for n, c in rows] + ([("Discrepancy", discrepancy, True)] if discrepancy else [])
    for i, (name, cents, warn) in enumerate(lines):
        y = TOP + i * ROW
        colour = AMBER if warn else GREEN if cents > 0 else RED if cents < 0 else ZERO
        amount = fmt(cents, signed=True)
        amount_w = width(amount, amt_f) / k
        draw_text(d, x0 * k, (y + 46) * k, fit(name, name_f, (x1 - x0 - amount_w - 32) * k), name_f, AMBER if warn else TEXT)
        draw_text(d, x1 * k, (y + 46) * k, amount, amt_f, colour if cents else MUTED, right=True)
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
        draw_text(d, (dot_x + 32) * k, ly * k, label, small, MUTED)

    return to_png(img, W, h)
