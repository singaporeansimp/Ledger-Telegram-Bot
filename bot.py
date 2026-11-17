"""Telegram front end: guided edits (sessions, payments, undo) and the pinned, auto-updating ledger."""
import logging
import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime
from html import escape

from telegram import (BotCommand, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
                      MessageEntity, Update)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          filters)

import esg
import ledger_image
import render
from money import format_cents as fmt
from money import parse_cents
from store import MAX_NAME, LedgerError, Store, clean_note

log = logging.getLogger("ledger")
HTML = ParseMode.HTML
HISTORY_PAGE = 5
UNDO_CHOICES = 5

HELP = """<b>Group ledger</b> · SGD · balances always add up to zero

<b>Everyday</b>
/ledger – show the current ledger
/edit – record a session: who won, who lost
/pay – record a payout between two players
/undo – undo an entry (history is kept)
Edits, payouts and undos continue in a private chat with me, so the group stays quiet.

<b>Players</b>
/join [name] – add yourself, or change your own name
/add @friend [name] – add someone else
/rename @friend = New name – rename anyone (or: Old name = New name)

<b>Records</b>
/history – every entry, who made it and when
/settle – who should pay whom to clear everyone
/esg @bot – add the ESG Flip bot's results to the ledger automatically
/export – the full history as a CSV file

New here? Send /join first. Type / to see this list as a menu.
Admins: let me pin messages so the ledger stays pinned."""

# The "/" menu. Keep in step with the handlers in build_app; a test enforces it.
COMMANDS = [BotCommand(c, d) for c, d in [
    ("ledger", "Show the current ledger"),
    ("edit", "Record a session (wins and losses)"),
    ("pay", "Record a payout"),
    ("undo", "Undo an entry"),
    ("history", "See every entry"),
    ("settle", "Who should pay whom"),
    ("esg", "Follow the ESG Flip bot"),
    ("export", "Download history as CSV"),
    ("join", "Join, or change your name"),
    ("add", "Add a friend to the ledger"),
    ("rename", "Rename a player"),
    ("help", "How this bot works")]]


# --- in-progress edits ---------------------------------------------------------------------------------------------

@dataclass
class Draft:
    """An edit being built in a message in the owner's private chat; nothing is saved until Confirm.
    `chat_id` is the ledger (usually a group) it will change; the conversation itself happens in the owner's DM."""
    id: str
    chat_id: int
    owner_id: int
    owner_name: str
    owner_username: str | None
    kind: str  # 'session' | 'payment' | 'undo'
    ledger_name: str  # which group's ledger this edits, shown so people in several groups can tell
    msg_id: int = 0
    prompt_id: int = 0
    locked: bool = False  # session: player picking finished
    picked: list[int] = field(default_factory=list)  # session: players; payment: [payer, payee]
    amounts: dict[int, int] = field(default_factory=dict)  # session: member_id -> cents
    note: str | None = None  # session: optional short label, e.g. 'Poker Friday'
    noting: bool = False  # session: waiting for the note text
    cents: int | None = None  # payment
    target: int | None = None  # undo: seq of the entry to reverse

    @property
    def dm(self) -> int:
        return self.owner_id  # a private chat's id is the user's id

    @property
    def phase(self) -> str:
        if self.kind == "session":
            if not self.locked:
                return "pick"
            if len(self.amounts) < len(self.picked):
                return "amount"
            return "note" if self.noting else "review"
        if self.kind == "payment":
            if len(self.picked) < 2:
                return "pick"
            return "review" if self.cents is not None else "amount"
        return "pick" if self.target is None else "review"

    def reset(self) -> None:
        self.amounts.clear()
        self.noting = False
        self.cents = None
        self.target = None

    def lines(self) -> dict[int, int]:
        if self.kind == "payment":
            payer, payee = self.picked
            return {payer: self.cents, payee: -self.cents}
        return dict(self.amounts)


def store_of(context) -> Store:
    return context.application.bot_data["store"]


def drafts_of(context) -> dict[str, Draft]:
    return context.application.bot_data.setdefault("drafts", {})


def button(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def view(s: Store, d: Draft) -> tuple[str, InlineKeyboardMarkup]:
    members = s.members(d.chat_id)
    names = {m.id: m.name for m in members}
    nm = lambda i: f"<b>{escape(names[i])}</b>"  # noqa: E731
    cancel = [button("✖ Cancel", f"d:{d.id}:x")]
    confirm = [button("✅ Confirm", f"d:{d.id}:ok"), button("↺ Redo", f"d:{d.id}:redo")]
    footer = f"\n\n<i>Ledger: {escape(d.ledger_name)}</i>"

    def member_rows(prefix, skip=(), marked=()):
        buttons = [button(("✅ " if m.id in marked else "") + m.name, f"d:{d.id}:{prefix}:{m.id}")
                   for m in members if m.id not in skip]
        return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]

    if d.kind == "session":
        if d.phase == "pick":
            chosen = ", ".join(escape(names[i]) for i in d.picked) or "nobody yet"
            text = f"🎲 <b>New session</b> · who played?\nTap everyone who played, then press Done.\nSelected: {chosen}"
            rows = member_rows("t", marked=d.picked) + [[button("➡ Done", f"d:{d.id}:go"), *cancel]]
        else:
            so_far = "\n".join(f"{nm(i)} {fmt(c, signed=True)}" for i, c in d.amounts.items())
            if d.phase == "amount":
                waiting = next(i for i in d.picked if i not in d.amounts)
                text = f"🎲 <b>New session</b>\n{so_far}\nWaiting for {nm(waiting)}'s amount. Reply to my question below."
                rows = [cancel]
            elif d.phase == "note":
                text = f"🎲 <b>New session</b>\n{so_far}\nWaiting for the note. Reply to my question below."
                rows = [[button("↩ Back", f"d:{d.id}:back")]]
            else:
                rest = f"\n<i>{escape(names[d.picked[-1]])} gets the remainder so the session adds up to zero.</i>"
                note = f"\n📝 {escape(d.note)}" if d.note else ""
                text = f"🎲 <b>Check this session</b>\n{so_far}{rest}{note}\nConfirm to record it?"
                rows = [confirm, [button("📝 Edit note" if d.note else "📝 Add note", f"d:{d.id}:n"), *cancel]]
    elif d.kind == "payment":
        if d.phase == "pick":
            if not d.picked:
                text = "💵 <b>Payout</b> · who paid?\nPick the person who handed over the money."
                rows = member_rows("p") + [cancel]
            else:
                text = f"💵 <b>Payout</b> · {nm(d.picked[0])} paid whom?"
                rows = member_rows("p", skip=d.picked) + [cancel]
        else:
            payer, payee = d.picked
            if d.phase == "amount":
                text = f"💵 {nm(payer)} paid {nm(payee)}. Waiting for the amount. Reply to my question below."
                rows = [cancel]
            else:
                text = f"💵 <b>Check this payout</b>\n{nm(payer)} paid {nm(payee)} S${fmt(d.cents)}\nConfirm to record it?"
                rows = [confirm, cancel]
    else:
        if d.phase == "pick":
            choices = s.undoable(d.chat_id, UNDO_CHOICES)
            text = "↩️ <b>Undo</b> · which entry? Newest first:\n\n" + "\n\n".join(
                render.entry_text(e, names) for e in choices)
            rows = [[button(f"Undo #{e.seq} ({render.label(e)})", f"d:{d.id}:u:{e.seq}")] for e in choices] + [cancel]
        else:
            text = (f"↩️ <b>Undo this entry?</b>\n{render.entry_text(s.entry(d.chat_id, d.target), names)}\n"
                    "A reversing entry is added. Nothing is erased.")
            rows = [confirm[:1] + [button("↺ Pick another", f"d:{d.id}:redo")], cancel]
    return text + footer, InlineKeyboardMarkup(rows)


async def edit(bot, chat_id: int, message_id: int, text: str, markup=None) -> None:
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode=HTML,
                                    reply_markup=markup)
    except BadRequest as err:
        if "not modified" not in str(err).lower():
            raise


async def drop_prompt(context, d: Draft) -> None:
    if d.prompt_id:
        try:
            await context.bot.delete_message(d.dm, d.prompt_id)
        except TelegramError:
            pass
        d.prompt_id = 0


async def show(context, d: Draft) -> None:
    """Re-render the draft message and, when an amount is needed, ask for it with a forced reply."""
    s = store_of(context)
    await drop_prompt(context, d)
    text, markup = view(s, d)
    await edit(context.bot, d.dm, d.msg_id, text, markup)
    if d.phase not in ("amount", "note"):
        return
    names = {m.id: m.name for m in s.members(d.chat_id)}
    if d.phase == "note":
        ask = "Note for this session, up to 60 characters? Send - to remove it."
    elif d.kind == "session":
        who = next(i for i in d.picked if i not in d.amounts)
        ask = f"Amount for <b>{escape(names[who])}</b>? Winnings like 50, losses like -30.50."
    else:
        ask = f"How much did <b>{escape(names[d.picked[0]])}</b> pay <b>{escape(names[d.picked[1]])}</b>? e.g. 20.50"
    placeholder = "e.g. Poker Friday" if d.phase == "note" else "e.g. 50 or -30.50"
    prompt = await context.bot.send_message(d.dm, ask, parse_mode=HTML,
                                            reply_markup=ForceReply(input_field_placeholder=placeholder))
    d.prompt_id = prompt.message_id


async def finish(context, d: Draft) -> None:
    s = store_of(context)
    drafts_of(context).pop(d.id, None)
    await drop_prompt(context, d)
    if d.chat_id != d.dm and not await is_member(context.bot, d.chat_id, d.owner_id):
        await edit(context.bot, d.dm, d.msg_id, "⚠️ Not recorded: you're no longer in that group.")
        return
    try:
        if d.kind == "undo":
            entry = s.undo(d.chat_id, d.target, d.owner_id, d.owner_name, editor_username=d.owner_username)
        else:
            entry = s.add_entry(d.chat_id, d.kind, d.lines(), d.owner_id, d.owner_name,
                                editor_username=d.owner_username, note=d.note)
    except LedgerError as err:
        await edit(context.bot, d.dm, d.msg_id, f"⚠️ Not recorded: {escape(str(err))}")
        return
    names = {m.id: m.name for m in s.members(d.chat_id)}
    receipt = render.entry_text(entry, names)
    await edit(context.bot, d.dm, d.msg_id, f"✅ <b>Recorded</b>\n{receipt}\n\n{ledger_text(s, d.chat_id)}")
    if d.chat_id == d.dm:  # a private ledger: the receipt above is the ledger, nothing else to tell
        await refresh_pinned(context, d.chat_id)
    elif not await publish_entry(context, d.chat_id, entry, names):
        await context.bot.send_message(d.dm, "⚠️ Recorded, but I couldn't fully update the group. Send /ledger "
                                             "there to refresh the pinned ledger.")


async def on_draft_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, draft_id, action, *rest = query.data.split(":")
    d = drafts_of(context).get(draft_id)
    if d is None:
        await query.answer("This edit has expired. Start a new one.", show_alert=True)
        return
    arg = int(rest[0]) if rest else 0
    known = {m.id for m in store_of(context).members(d.chat_id)}
    if action == "x":
        drafts_of(context).pop(d.id)
        await drop_prompt(context, d)
        await edit(context.bot, d.dm, d.msg_id, "✖ Edit cancelled. Nothing was recorded.")
        await query.answer()
        return
    if action == "ok":
        if d.phase != "review":
            await query.answer("Not ready to confirm yet.")
            return
        await query.answer()
        await finish(context, d)
        return
    if action == "t" and d.kind == "session" and not d.locked and arg in known:
        d.picked.remove(arg) if arg in d.picked else d.picked.append(arg)
    elif action == "go" and d.kind == "session" and not d.locked:
        if len(d.picked) < 2:
            await query.answer("Pick at least two players.")
            return
        d.locked = True
    elif action == "p" and d.kind == "payment" and len(d.picked) < 2 and arg in known and arg not in d.picked:
        d.picked.append(arg)
    elif action == "u" and d.kind == "undo" and d.target is None:
        d.target = arg
    elif action == "n" and d.phase == "review" and d.kind == "session":
        d.noting = True
    elif action == "back" and d.phase == "note":
        d.noting = False
    elif action == "redo" and d.phase in ("amount", "review"):
        d.reset()
    await query.answer()
    await show(context, d)


async def on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A typed answer (an amount or a note) in the private chat, for the draft that is waiting for one."""
    msg = update.effective_message
    d = next((x for x in drafts_of(context).values() if x.owner_id == msg.from_user.id and x.prompt_id), None)
    if d is None:
        await msg.reply_text("I'm not waiting for anything from you. Use the buttons above, "
                             "or send /edit, /pay or /undo in your group to start.")
        return
    if d.phase == "note":
        try:
            d.note = clean_note("" if msg.text.strip() == "-" else msg.text)
        except LedgerError as err:
            await msg.reply_text(str(err))
            return
        d.noting = False
        await show(context, d)
        return
    try:
        cents = parse_cents(msg.text)
        if d.kind == "payment" and cents <= 0:
            raise ValueError("A payout must be more than zero.")
    except ValueError as err:
        await msg.reply_text(str(err))
        return
    if d.kind == "session":
        d.amounts[next(i for i in d.picked if i not in d.amounts)] = cents
        if len(d.amounts) == len(d.picked) - 1:  # last player takes the remainder, so it always tallies
            d.amounts[d.picked[-1]] = -sum(d.amounts.values())
    else:
        d.cents = cents
    await show(context, d)


LINK_KINDS = {"edit": "session", "pay": "payment", "undo": "undo"}
LINK_NAMES = {kind: name for name, kind in LINK_KINDS.items()}


async def is_member(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    return member.status in ("creator", "administrator", "member") or (
        member.status == "restricted" and member.is_member)


def precheck(s: Store, chat: int, kind: str) -> str | None:
    if len(s.members(chat)) < 2:
        return "I need at least two players first. Use /join, or /add @friend."
    if kind == "undo" and not s.undoable(chat, 1):
        return "Nothing to undo yet."
    return None


async def open_draft(context, user, chat: int, chat_title: str | None, kind: str) -> None:
    """Starts the guided edit in the user's private chat. Raises Forbidden if they never started the bot."""
    s = store_of(context)
    d = Draft(secrets.token_hex(3), chat, user.id, editor_name(s, chat, user), user.username, kind,
              chat_title or "your private ledger")
    text, markup = view(s, d)
    d.msg_id = (await context.bot.send_message(d.dm, text, parse_mode=HTML, reply_markup=markup)).message_id
    for old in [x for x in drafts_of(context).values() if x.owner_id == user.id]:  # one edit at a time per person
        drafts_of(context).pop(old.id)
        await drop_prompt(context, old)
        await edit(context.bot, old.dm, old.msg_id, "↪ Replaced by your newer edit. Nothing was recorded.")
    drafts_of(context)[d.id] = d


async def start_draft(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
    """/edit, /pay and /undo: the conversation moves to a private chat so the group stays quiet."""
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if problem := precheck(store_of(context), chat.id, kind):
        await msg.reply_text(problem)
        return
    try:
        await open_draft(context, user, chat.id, chat.title, kind)
    except Forbidden:  # Telegram only lets a bot message people who have started it
        link = f"https://t.me/{context.bot.username}?start={LINK_NAMES[kind]}{chat.id}"
        await msg.reply_text("I can't message you privately yet. Tap the button, press Start, and your edit "
                             "will begin there.",
                             reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open private chat", url=link)]]))
        return
    if chat.type != "private":  # a reaction acknowledges without adding a message to the group
        try:
            await context.bot.set_message_reaction(chat.id, msg.message_id, "👌")
        except TelegramError:
            pass


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain /start shows help; the deep link from start_draft begins the edit that was asked for."""
    link = re.fullmatch(r"(edit|pay|undo)(-?\d+)", context.args[0]) if context.args else None
    if not link or update.effective_chat.type != "private":
        await cmd_help(update, context)
        return
    msg, user = update.effective_message, update.effective_user
    kind, chat = LINK_KINDS[link[1]], int(link[2])
    title = None
    if chat != user.id:  # anyone can type a link, so check they really belong to that group
        try:
            title = (await context.bot.get_chat(chat)).title
        except TelegramError:
            pass
        if title is None or not await is_member(context.bot, chat, user.id):
            await msg.reply_text("I can't find that group, or you're not in it. Send /edit in your group chat.")
            return
    if problem := precheck(store_of(context), chat, kind):
        await msg.reply_text(problem)
        return
    await open_draft(context, user, chat, title, kind)


async def cmd_edit(update, context):
    await start_draft(update, context, "session")


async def cmd_pay(update, context):
    await start_draft(update, context, "payment")


async def cmd_undo(update, context):
    await start_draft(update, context, "undo")


# --- ledger views --------------------------------------------------------------------------------------------------

def ledger_text(s: Store, chat: int) -> str:
    last = s.entries(chat, 1)
    return render.ledger_text(s.members(chat), s.balances(chat), last[0] if last else None)


def editor_name(s: Store, chat: int, user) -> str:
    member = s.identify(chat, user.id, user.username)
    return member.name if member else user.full_name


@dataclass
class LedgerView:
    """The ledger as a picture with a caption, or as text alone when no picture can be drawn (no players, no fonts)."""
    png: bytes | None
    text: str  # the caption under the picture, or the whole ledger when there is no picture


def ledger_view(s: Store, chat: int) -> LedgerView:
    members, balances = s.members(chat), s.balances(chat)
    last = s.entries(chat, 1)
    last = last[0] if last else None
    png = ledger_image.render_ledger(render.ranked(members, balances), discrepancy=sum(balances.values()))
    if png is None:
        return LedgerView(None, render.ledger_text(members, balances, last))
    return LedgerView(png, render.ledger_footer(last))


async def send_ledger(bot, chat: int, view: LedgerView):
    if view.png is None:
        return await bot.send_message(chat, view.text, parse_mode=HTML)
    return await bot.send_photo(chat, view.png, caption=view.text, parse_mode=HTML)


async def update_in_place(bot, chat: int, message_id: int, view: LedgerView) -> bool:
    """Edit the ledger message to match `view`. False means it can't be edited (gone, or not ours any more)."""
    try:
        if view.png is None:
            await bot.edit_message_text(view.text, chat_id=chat, message_id=message_id, parse_mode=HTML)
        else:
            await bot.edit_message_media(InputMediaPhoto(view.png, caption=view.text, parse_mode=HTML),
                                         chat_id=chat, message_id=message_id)
    except BadRequest as err:
        return "not modified" in str(err).lower()  # already up to date is fine
    return True


async def pin_ledger(context, chat: int, message_id: int) -> None:
    """Pin the ledger message and remember whether that worked, so /ledger can retry after a permission fix."""
    try:
        await context.bot.pin_chat_message(chat, message_id, disable_notification=True)
    except TelegramError:
        store_of(context).set_pin_ok(chat, False)
        await context.bot.send_message(chat, "I couldn't pin the ledger. Make me an admin with the "
                                             "'Pin messages' permission, then send /ledger again.")
    else:
        store_of(context).set_pin_ok(chat, True)


async def refresh_pinned(context, chat: int, repin: bool = False) -> None:
    """Update the ledger message in place; if it is gone, post a fresh one and pin it.

    In-place edits never touch the pin, so a pin that failed (the bot lacked permission) would stay failed.
    With `repin`, retry it. A pin that succeeded is left alone, even if an admin later pins something else:
    Telegram can't reliably tell us what is pinned, and the admins' choice is theirs.
    """
    s = store_of(context)
    view = ledger_view(s, chat)
    message_id = s.pinned_message(chat)
    if message_id and await update_in_place(context.bot, chat, message_id, view):
        if repin and not s.pin_ok(chat):
            await pin_ledger(context, chat, message_id)
        return
    sent = await send_ledger(context.bot, chat, view)
    s.set_pinned_message(chat, sent.message_id)
    await pin_ledger(context, chat, sent.message_id)


async def publish_entry(context, chat: int, entry, names: dict[int, str], edit_message_id: int | None = None) -> bool:
    """Tell the group about a recorded entry (in `edit_message_id` if given) and bring the pinned ledger up to date.

    The two are independent and best effort: the entry is already saved, so a Telegram hiccup on one must not
    skip the other. Returns False if either failed.
    """
    ok = True
    receipt = f"✅ <b>Recorded</b>\n{render.entry_text(entry, names)}"
    announce = (lambda: edit(context.bot, chat, edit_message_id, receipt)) if edit_message_id else \
        (lambda: context.bot.send_message(chat, receipt, parse_mode=HTML))
    for step in (announce, lambda: refresh_pinned(context, chat)):
        try:
            await step()
        except TelegramError:
            ok = False
            log.warning("Recorded #%s but a group update failed (chat %s)", entry.seq, chat, exc_info=True)
    return ok


async def cmd_ledger(update, context):
    chat = update.effective_chat.id
    editor_name(store_of(context), chat, update.effective_user)
    view = ledger_view(store_of(context), chat)
    if view.png is None:
        await update.effective_message.reply_html(view.text)
    else:
        await update.effective_message.reply_photo(view.png, caption=view.text, parse_mode=HTML)
    await refresh_pinned(context, chat, repin=True)


def history_view(s: Store, chat: int, offset: int):
    total = s.count(chat)
    names = {m.id: m.name for m in s.members(chat)}
    text = render.history_text(s.entries(chat, HISTORY_PAGE, offset), names, offset, total)
    nav = []
    if offset > 0:
        nav.append(button("◀ Newer", f"h:{max(0, offset - HISTORY_PAGE)}"))
    if offset + HISTORY_PAGE < total:
        nav.append(button("Older ▶", f"h:{offset + HISTORY_PAGE}"))
    return text, InlineKeyboardMarkup([nav] if nav else [])


async def cmd_history(update, context):
    text, markup = history_view(store_of(context), update.effective_chat.id, 0)
    await update.effective_message.reply_html(text, reply_markup=markup)


async def on_history_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    text, markup = history_view(store_of(context), query.message.chat_id, int(query.data.split(":")[1]))
    await query.answer()
    await edit(context.bot, query.message.chat_id, query.message.message_id, text, markup)


async def cmd_export(update, context):
    """The complete history as a spreadsheet file, e.g. for backup or settling a dispute."""
    s, chat = store_of(context), update.effective_chat.id
    entries = s.entries(chat, max(s.count(chat), 1))[::-1]  # oldest first
    if not entries:
        await update.effective_message.reply_text("Nothing to export yet.")
        return
    names = {m.id: m.name for m in s.members(chat)}
    stamp = datetime.now(render.SGT).strftime("%Y-%m-%d")
    await update.effective_message.reply_document(
        render.history_csv(entries, names).encode("utf-8-sig"),  # BOM so Excel reads names correctly
        filename=f"ledger-history-{stamp}.csv", caption=f"{len(entries)} entries, oldest first.")


async def cmd_settle(update, context):
    s, chat = store_of(context), update.effective_chat.id
    names = {m.id: m.name for m in s.members(chat)}
    await update.effective_message.reply_html(render.settle_text(s.balances(chat), names))


async def cmd_help(update, context):
    await update.effective_message.reply_html(HELP)


# --- players -------------------------------------------------------------------------------------------------------

@dataclass
class Mention:
    token: str
    user_id: int | None
    username: str | None
    label: str


def mention_of(msg) -> Mention | None:
    for ent in msg.entities:
        if ent.type == MessageEntity.TEXT_MENTION and ent.user:  # people without a @username
            return Mention(msg.parse_entity(ent), ent.user.id, ent.user.username, ent.user.first_name)
        if ent.type == MessageEntity.MENTION:
            token = msg.parse_entity(ent)
            return Mention(token, None, token.lstrip("@"), token.lstrip("@"))
    return None


def args_of(msg) -> str:
    parts = msg.text.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


async def cmd_join(update, context):
    """Registers the sender, or renames them if they are already a player (also /nick)."""
    msg, chat, user, s = update.effective_message, update.effective_chat.id, update.effective_user, store_of(context)
    requested = args_of(msg)
    member = s.identify(chat, user.id, user.username)
    try:
        if member is None:
            member = s.add_member(chat, requested or user.first_name, user.id, user.username)
            await msg.reply_html(f"Welcome, <b>{escape(member.name)}</b>! Send /help to see what I can do.")
        elif requested:
            s.rename(chat, member.id, requested)
            await msg.reply_html(f"Done. You're now <b>{escape(requested)}</b>.")
        else:
            await msg.reply_html(f"You're already in as <b>{escape(member.name)}</b>. Use /join New name to change it.")
            return
    except LedgerError as err:
        await msg.reply_text(f"{err} Try /join Another name.")
        return
    await refresh_pinned(context, chat)


async def cmd_add(update, context):
    msg, chat, s = update.effective_message, update.effective_chat.id, store_of(context)
    text, mention = args_of(msg), mention_of(msg)
    name = (text.replace(mention.token, "", 1) if mention else text).strip() or (mention.label if mention else "")
    if not name:
        await msg.reply_text("Add a player with /add @friend, /add @friend Nickname, or just /add Name.")
        return
    if mention and (found := s.find(chat, user_id=mention.user_id, username=mention.username)):
        await msg.reply_html(f"<b>{escape(found.name)}</b> is already in. Use /rename to change their name.")
        return
    try:
        member = s.add_member(chat, name, mention and mention.user_id, mention and mention.username)
    except LedgerError as err:
        await msg.reply_text(str(err))
        return
    await msg.reply_html(f"Added <b>{escape(member.name)}</b> to the ledger.")
    await refresh_pinned(context, chat)


async def cmd_rename(update, context):
    msg, chat, s = update.effective_message, update.effective_chat.id, store_of(context)
    text, mention = args_of(msg), mention_of(msg)
    if "=" not in text:
        await msg.reply_text("Usage: /rename @friend = New name  or  /rename Old name = New name")
        return
    who, new = text.split("=", 1)
    member = (s.find(chat, user_id=mention.user_id, username=mention.username) if mention
              else s.find(chat, name=" ".join(who.split())))
    if member is None:
        await msg.reply_text("I don't know that player. Check /ledger, or /add them first.")
        return
    try:
        s.rename(chat, member.id, new)
    except LedgerError as err:
        await msg.reply_text(str(err))
        return
    await msg.reply_html(f"<b>{escape(member.name)}</b> is now <b>{escape(' '.join(new.split()))}</b>.")
    await refresh_pinned(context, chat)


# --- ESG Flip bot results ------------------------------------------------------------------------------------------

@dataclass
class EsgPending:
    """An ESG result waiting for someone to say which ledger player one of its Telegram accounts is."""
    id: str
    chat_id: int
    flip_number: int
    players: tuple[esg.EsgPlayer, ...]  # who moved money in the flip (not those who broke even)
    bot_id: int
    bot_name: str
    bot_username: str | None
    links: dict[int, int] = field(default_factory=dict)  # Telegram user id -> ledger member id
    msg_id: int = 0  # the question in the group

    def unknown(self) -> list[esg.EsgPlayer]:
        return [p for p in self.players if p.user_id not in self.links]


def who_is(p: esg.EsgPlayer) -> str:
    return escape(p.display_name) + (f" (@{escape(p.username)})" if p.username else "")


async def is_admin(bot, chat_id: int, user_id: int) -> bool:
    try:
        return (await bot.get_chat_member(chat_id, user_id)).status in ("creator", "administrator")
    except TelegramError:
        return False


async def cmd_esg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/esg @bot starts following that bot's flip results, /esg off stops, /esg shows where things stand."""
    msg, chat, user, s = update.effective_message, update.effective_chat, update.effective_user, store_of(context)
    if chat.type == "private":
        await msg.reply_text("Send /esg in your group, where the ESG Flip bot plays.")
        return
    arg = args_of(msg)
    if arg:
        if not await is_admin(context.bot, chat.id, user.id):
            await msg.reply_text("Only group admins can change which bot I follow.")
            return
        named = re.fullmatch(r"@?([A-Za-z0-9_]{3,32})", arg)
        if arg.lower() != "off" and not named:
            await msg.reply_text("Usage: /esg @esg_bot_username   or   /esg off")
            return
        s.set_esg_bot(chat.id, None if arg.lower() == "off" else named[1])
    following = s.esg_bot(chat.id)
    text = f"Following ESG flip results from @{escape(following)}." if following else \
        "I'm not following any ESG Flip bot here. Send /esg @username to start."
    if following:
        text += ("\nWhen it posts a result, I add it to the ledger. The first time I meet a player name I'll ask who "
                 "it is.\n\nTelegram only shows me another bot's messages if <b>Bot-to-Bot Communication Mode</b> is on "
                 "for me in @BotFather, Group Privacy is off, and I'm a group admin.")
        if not (await context.bot.get_me()).can_read_all_group_messages:
            text += "\n⚠️ Group Privacy is still <b>on</b> for me, so I probably can't see its results yet."
        esg_db = context.application.bot_data.get("esg_db")
        text += ("\nI look players up in ESG's database, so two people with the same name stay separate." if esg_db
                 and os.path.exists(esg_db) else
                 "\n⚠️ I can't read ESG's database (ESG_DB_PATH), so I can't record its flips safely yet.")
        seen = esg_seen_of(context).get(chat.id)
        text += (f"\n\nLast message I received from it: {seen[0]:%d %b %H:%M} SGT, {seen[1]}." if seen else
                 "\n\nI haven't received any message from it since I last started. If it has posted since, Telegram "
                 "isn't passing its messages on to me yet, so check the settings above.")
    await msg.reply_html(text)


async def on_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Only the one bot this chat follows is listened to; anything else posted in a group is none of our business."""
    msg, chat = update.effective_message, update.effective_chat
    sender, following = msg.from_user, store_of(context).esg_bot(chat.id)
    if sender and sender.is_bot and sender.id != context.bot.id:  # Telegram only delivers these if bot-to-bot is on
        log.info("Message from bot @%s in chat %s (following @%s): %.50r", sender.username, chat.id, following, msg.text)
    if not (sender and sender.is_bot and following and (sender.username or "").lower() == following.lower()):
        return
    result = esg.parse_result(msg.text or "")
    esg_seen_of(context)[chat.id] = (datetime.now(render.SGT), "a flip result" if result else "a message that isn't a flip result")
    if result is not None:
        await take_esg_result(context, chat.id, result, sender)


async def take_esg_result(context, chat: int, result: esg.EsgResult, sender) -> None:
    """A result message arrived: look the flip up in ESG's database (names can repeat, Telegram ids can't), then record it."""
    s, say = store_of(context), context.bot.send_message
    if result.flip_number is None:
        await say(chat, "⚠️ The ESG bot couldn't save that flip, so I haven't added it to the ledger. "
                        "Record it yourself with /edit if the money changed hands.")
        return
    if s.esg_recorded(chat, result.flip_number) or not any(cents for _, cents in result.nets):
        return  # seen already, or a tie where nothing moved

    async def refuse(why: str) -> None:
        await say(chat, f"⚠️ I haven't added ESG Flip #{result.flip_number} to the ledger: {why} Record it with /edit.")

    db = context.application.bot_data.get("esg_db")
    if not result.balanced:
        return await refuse("its amounts don't add up to zero.")
    if not db:
        return await refuse("I can't tell players with the same name apart without ESG's database (ESG_DB_PATH).")
    try:
        players = esg.flip_players(db, chat, result.flip_number)
    except esg.EsgDbError:
        log.warning("Cannot read the ESG database", exc_info=True)
        return await refuse("I couldn't read ESG's database.")
    if players is None:
        return await refuse("ESG's database has no such flip for this group.")
    if [p.cents for p in players] != [cents for _, cents in result.nets]:
        return await refuse("the message and ESG's database disagree about the amounts.")
    pending = EsgPending(secrets.token_hex(3), chat, result.flip_number, tuple(p for p in players if p.cents),
                         sender.id, sender.full_name, sender.username)
    for p in pending.players:  # a known account, or a player added by @username who has now shown up
        if member := s.identify(chat, p.user_id, p.username):
            pending.links[p.user_id] = member.id
    await esg_advance(context, pending)


async def esg_advance(context, p: EsgPending) -> None:
    """Record the flip once every account is known; until then ask about the next unknown one."""
    s = store_of(context)
    unknown = p.unknown()
    if not unknown:
        esg_pending_of(context).pop(p.id, None)
        try:
            entry = s.add_esg_entry(p.chat_id, p.flip_number, {p.links[x.user_id]: x.cents for x in p.players},
                                    p.bot_id, p.bot_name, p.bot_username)
        except LedgerError as err:
            await context.bot.send_message(p.chat_id, f"⚠️ ESG Flip #{p.flip_number} wasn't added: {escape(str(err))}")
            return
        names = {m.id: m.name for m in s.members(p.chat_id)}
        await publish_entry(context, p.chat_id, entry, names, edit_message_id=p.msg_id or None)
        return
    taken = set(p.links.values())
    buttons = [button(m.name, f"e:{p.id}:{m.id}") for m in s.members(p.chat_id)
               if m.tg_user_id is None and m.id not in taken]  # someone already tied to an account isn't this one
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([button(f"➕ New player “{esg.ledger_name(unknown[0].display_name, 12)}”", f"e:{p.id}:new"),
                 button("✖ Skip this flip", f"e:{p.id}:x")])
    text = (f"📒 <b>ESG Flip #{p.flip_number}</b> is ready to add to the ledger, but I don't know "
            f"<b>{who_is(unknown[0])}</b> yet. Who is that on the ledger?")
    markup = InlineKeyboardMarkup(rows)
    if p.msg_id:
        await edit(context.bot, p.chat_id, p.msg_id, text, markup)
    else:
        p.msg_id = (await context.bot.send_message(p.chat_id, text, parse_mode=HTML, reply_markup=markup)).message_id
        esg_pending_of(context)[p.id] = p


def new_player_name(s: Store, chat: int, player: esg.EsgPlayer) -> str:
    """A free ledger name for someone new: their display name, else with their @username, else numbered."""
    base = esg.ledger_name(player.display_name, MAX_NAME)
    suffixes = ([f" (@{player.username})"] if player.username else []) + [f" {n}" for n in range(2, 100)]
    for suffix in ["", *suffixes]:
        name = base[:MAX_NAME - len(suffix)].rstrip() + suffix
        if s.find(chat, name=name) is None:
            return name
    raise LedgerError("I couldn't find a free name for them. Add them with /add first.")


def esg_seen_of(context) -> dict[int, tuple[datetime, str]]:
    """When each chat last heard from its ESG bot (since this process started), to show in /esg."""
    return context.application.bot_data.setdefault("esg_seen", {})


def esg_pending_of(context) -> dict[str, EsgPending]:
    return context.application.bot_data.setdefault("esg_pending", {})


async def on_esg_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Anyone in the group can answer. The account is then tied to that ledger player, so it is asked once."""
    query = update.callback_query
    _, pending_id, choice = query.data.split(":")
    p, s = esg_pending_of(context).get(pending_id), store_of(context)
    if p is None:
        await query.answer("That question has expired. Record the flip with /edit.", show_alert=True)
        return
    if choice == "x":
        esg_pending_of(context).pop(p.id)
        await edit(context.bot, p.chat_id, p.msg_id, f"↪ ESG Flip #{p.flip_number} wasn't added. Record it with /edit.")
        await query.answer()
        return
    who = p.unknown()[0]
    try:
        if choice == "new":
            username = who.username if who.username and s.find(p.chat_id, username=who.username) is None else None
            member = s.add_member(p.chat_id, new_player_name(s, p.chat_id, who), who.user_id, username)
        else:
            member = next((m for m in s.members(p.chat_id) if m.id == int(choice)), None)
            if member is None or member.tg_user_id is not None or member.id in p.links.values():
                raise LedgerError("That player is already tied to someone else.")
            s.link_member(p.chat_id, member.id, who.user_id, who.username)
    except LedgerError as err:
        await query.answer(str(err), show_alert=True)
        return
    p.links[who.user_id] = member.id
    await query.answer(f"{who.display_name} is {member.name}")
    await esg_advance(context, p)


# --- wiring --------------------------------------------------------------------------------------------------------

async def on_error(update, context) -> None:
    log.error("Unhandled error", exc_info=context.error)


async def publish_commands(app: Application) -> None:
    """Fills Telegram's "/" menu. Best effort: a hiccup here must not stop the bot starting."""
    try:
        await app.bot.set_my_commands(COMMANDS)
    except TelegramError:
        log.warning("Could not publish the command menu", exc_info=True)


def build_app(token: str, db_path: str, esg_db_path: str | None = None) -> Application:
    app = (Application.builder().token(token).post_init(publish_commands)
           .connect_timeout(10).read_timeout(30).write_timeout(30).pool_timeout(10).build())
    app.bot_data["store"] = Store(db_path)
    app.bot_data["esg_db"] = esg_db_path
    for names, handler in [(["help"], cmd_help), (["start"], cmd_start), (["esg"], cmd_esg), (["join", "nick"], cmd_join), (["add"], cmd_add),
                           (["rename"], cmd_rename), (["edit", "session"], cmd_edit), (["pay"], cmd_pay),
                           (["undo"], cmd_undo), (["ledger"], cmd_ledger), (["history"], cmd_history), (["export"], cmd_export),
                           (["settle"], cmd_settle)]:
        app.add_handler(CommandHandler(names, handler))
    app.add_handler(CallbackQueryHandler(on_draft_button, pattern=r"^d:"))
    app.add_handler(CallbackQueryHandler(on_history_page, pattern=r"^h:"))
    app.add_handler(CallbackQueryHandler(on_esg_button, pattern=r"^e:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_private_text))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS & filters.UpdateType.MESSAGE, on_group_text))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request lines contain the bot token
    here = os.path.dirname(os.path.abspath(__file__))
    token = os.environ.get("BOT_TOKEN")
    if not token:
        sys.exit("BOT_TOKEN is not set. Put it in .env (see README.md).")
    build_app(token, os.environ.get("LEDGER_DB", os.path.join(here, "ledger.db")),
              os.environ.get("ESG_DB_PATH")).run_polling()


if __name__ == "__main__":
    main()
