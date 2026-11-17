"""Telegram-HTML text for the ledger, entries, history and settlement suggestions, plus the CSV export."""
import csv
import io
from html import escape
from zoneinfo import ZoneInfo

from money import format_cents as fmt
from money import settle
from store import Entry, Member

SGT = ZoneInfo("Asia/Singapore")
# Balances always sum to zero, so this row only ever appears if something has gone badly wrong.
DISCREPANCY = "Discrepancy"
KINDS = {"session": "Session", "payment": "Payout", "undo": "Undo"}


def who(name: str, username: str | None) -> str:
    """'Alice (@alice)': the name people know them by plus the Telegram account that made the edit."""
    return escape(name) + (f" (@{escape(username)})" if username else "")


def when(entry: Entry) -> str:
    return entry.created_at.astimezone(SGT).strftime("%d %b %Y %H:%M") + " SGT"


def lines_text(entry: Entry, names: dict[int, str]) -> str:
    if entry.kind == "payment":
        payee, payer = sorted(entry.lines, key=entry.lines.get)
        return f"{escape(names[payer])} paid {escape(names[payee])} S${fmt(entry.lines[payer])}"
    ordered = sorted(entry.lines.items(), key=lambda kv: (-kv[1], names[kv[0]].lower()))
    return " · ".join(f"{escape(names[m])} {fmt(c, signed=True)}" for m, c in ordered)


def label(entry: Entry) -> str:
    return f"{KINDS[entry.kind]} of #{entry.reverses}" if entry.reverses else KINDS[entry.kind]


def entry_text(entry: Entry, names: dict[int, str]) -> str:
    note = f"\n📝 {escape(entry.note)}" if entry.note else ""
    text = (f"<b>#{entry.seq}</b> {label(entry)} · {when(entry)}\nby {who(entry.editor_name, entry.editor_username)}{note}\n"
            f"{lines_text(entry, names)}")
    if entry.reversed_by:
        text += f"\n↩ undone by #{entry.reversed_by}"
    return text


def ranked(members: list[Member], balances: dict[int, int]) -> list[tuple[str, int]]:
    """(name, cents) for everyone, biggest winner first; shared by the text table and the image."""
    return sorted(((m.name, balances.get(m.id, 0)) for m in members), key=lambda r: (-r[1], r[0].lower()))


def ledger_footer(last: Entry | None) -> str:
    if last is None:
        return "No entries yet. Use /edit to record a session."
    return f"Last edit: #{last.seq} {label(last)} by {who(last.editor_name, last.editor_username)} · {when(last)}"


def ledger_text(members: list[Member], balances: dict[int, int], last: Entry | None) -> str:
    head = "📒 <b>Ledger</b> · SGD"
    if not members:
        return f"{head}\nNo players yet. Use /join to add yourself."
    rows = ranked(members, balances)
    total = sum(balances.values())
    name_w = max([len(n) for n, _ in rows] + [len(DISCREPANCY)] * (total != 0))
    values = [fmt(c, signed=True) for _, c in rows]
    value_w = max(len(v) for v in values + [fmt(total, signed=True)] * (total != 0))
    body = [f"{escape(n.ljust(name_w))}  {v.rjust(value_w)}" for (n, _), v in zip(rows, values)]
    if total != 0:
        body += ["-" * (name_w + 2 + value_w), f"{DISCREPANCY.ljust(name_w)}  {fmt(total, signed=True).rjust(value_w)}"]
    return f"{head}\n<pre>{chr(10).join(body)}</pre>\n{ledger_footer(last)}"


def history_text(entries: list[Entry], names: dict[int, str], offset: int, total: int) -> str:
    if not entries:
        return "No entries yet. Record one with /edit."
    shown = f"{offset + 1}-{offset + len(entries)} of {total}"
    return f"🧾 <b>History</b> (newest first, {shown})\n\n" + "\n\n".join(entry_text(e, names) for e in entries)


def settle_text(balances: dict[int, int], names: dict[int, str]) -> str:
    transfers = settle(balances)
    if not transfers:
        return "✅ Everyone is settled. Nobody owes anything."
    rows = [f"{escape(names[d])} pays {escape(names[c])} S${fmt(cents)}" for d, c, cents in transfers]
    return "💸 <b>Suggested settlement</b>\n" + "\n".join(rows) + "\n\nRecord each one with /pay once it's done."


def _safe(text: str) -> str:
    """Stops spreadsheets from running a name or note that starts like a formula."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def history_csv(entries: list[Entry], names: dict[int, str]) -> str:
    """One row per player per entry, oldest first, so the sheet can be summed or filtered directly."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["entry", "time", "kind", "editor", "editor_username", "note", "undoes_entry",
                     "undone_by_entry", "player", "amount_sgd"])
    for e in entries:
        for member_id, cents in sorted(e.lines.items(), key=lambda kv: (-kv[1], names[kv[0]].lower())):
            writer.writerow([e.seq, e.created_at.astimezone(SGT).isoformat(timespec="seconds"), e.kind,
                             _safe(e.editor_name), e.editor_username or "", _safe(e.note or ""),
                             e.reverses or "", e.reversed_by or "", _safe(names[member_id]),
                             fmt(cents, commas=False)])
    return out.getvalue()
