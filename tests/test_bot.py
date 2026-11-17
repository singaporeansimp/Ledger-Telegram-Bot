"""Drives the real handlers through python-telegram-bot against a fake Telegram Bot API."""
import asyncio
import csv
import io
import json
import re
import sqlite3
from pathlib import Path
from html import unescape

from telegram import Update
from telegram.request import HTTPXRequest

import bot as ledger_bot
import ledger_image
import render

CHAT = -1001
ALICE = {"id": 1, "is_bot": False, "first_name": "Alice", "username": "alice"}
BOB = {"id": 2, "is_bot": False, "first_name": "Bob", "username": "bob"}
CAROL = {"id": 3, "is_bot": False, "first_name": "Carol", "username": "carol99"}
ESGBOT = {"id": 500, "is_bot": True, "first_name": "ESG Flip Bot", "username": "esg_flip_bot"}
LINK = "https://t.me/ledgerbot?start="
# Result messages as the ESG bot's own renderer produced them (see tests/fixtures/esg_results.json)
SAMPLES = {x["label"]: x for x in json.loads((Path(__file__).parent / "fixtures" / "esg_results.json").read_text())}


class FakeTelegram:
    """Just enough of Telegram: groups and DMs, users who must /start before the bot may DM them, membership."""

    def __init__(self):
        self.messages = {}  # message_id -> {"chat", "text", "markup", "deleted"}
        self.calls = []
        self.alerts = []
        self.documents = []  # (filename, content, caption)
        self.reactions = []  # (chat, message_id, emoji)
        self.started = set()  # users who have started the bot, so it may message them
        self.not_in = set()  # (chat, user) pairs that are not group members
        self.next_id = 1000
        self.can_pin = True
        self.pinned = {}  # chat -> message_id currently pinned
        self.admins = {1}  # users who are group admins; everyone else is a plain member
        self.privacy_on = True  # Group Privacy for the bot (getMe.can_read_all_group_messages is the opposite)
        self.fail_send = None  # optional predicate on sendMessage parameters: True makes Telegram error out

    def new_id(self):
        self.next_id += 1
        return self.next_id

    def message(self, message_id, text, chat=CHAT):
        kind = {"id": chat, "type": "private", "first_name": "U"} if chat > 0 else \
            {"id": chat, "type": "supergroup", "title": "G"}
        return {"message_id": message_id, "date": 0, "text": text, "chat": kind,
                "from": {"id": 99, "is_bot": True, "first_name": "L", "username": "ledgerbot"}}

    async def do_request(self, url, method, request_data=None, *args, **kwargs):
        name = url.rsplit("/", 1)[1]
        p = request_data.parameters if request_data else {}
        self.calls.append((name, p))
        ok = lambda result: (200, json.dumps({"ok": True, "result": result}).encode())  # noqa: E731
        bad = lambda code, text: (code, json.dumps({"ok": False, "error_code": code, "description": text}).encode())  # noqa: E731
        if name == "getMe":
            return ok({"id": 99, "is_bot": True, "first_name": "L", "username": "ledgerbot",
                       "can_read_all_group_messages": not self.privacy_on})
        if name == "sendMessage":
            if self.fail_send and self.fail_send(p):
                return bad(500, "Internal Server Error")
            if p["chat_id"] > 0 and p["chat_id"] not in self.started:
                return bad(403, "Forbidden: bot can't initiate conversation with a user")
            mid = self.new_id()
            self.messages[mid] = {"chat": p["chat_id"], "text": p["text"], "markup": p.get("reply_markup"),
                                  "deleted": False}
            return ok(self.message(mid, p["text"], p["chat_id"]))
        if name == "sendPhoto":
            mid = self.new_id()
            photo = next(iter(request_data.multipart_data.values()))[1]
            self.messages[mid] = {"chat": p["chat_id"], "text": p.get("caption", ""), "markup": None,
                                  "photo": photo, "deleted": False}
            return ok(self.message(mid, p.get("caption", ""), p["chat_id"]))
        if name == "editMessageMedia":
            m = self.messages.get(p["message_id"])
            if m is None or m["deleted"] or m["chat"] != p["chat_id"]:
                return bad(400, "Bad Request: message to edit not found")
            if "photo" not in m:  # Telegram can't turn a text message into a photo
                return bad(400, "Bad Request: message can't be edited")
            photo, caption = next(iter(request_data.multipart_data.values()))[1], p["media"].get("caption", "")
            if (m["photo"], m["text"]) == (photo, caption):
                return bad(400, "Bad Request: message is not modified")
            m["photo"], m["text"] = photo, caption
            return ok(self.message(p["message_id"], caption, m["chat"]))
        if name == "editMessageText":
            m = self.messages.get(p["message_id"])
            if m is None or m["deleted"] or m["chat"] != p["chat_id"]:
                return bad(400, "Bad Request: message to edit not found")
            if "photo" in m:
                return bad(400, "Bad Request: there is no text in the message to edit")
            if (m["text"], m["markup"]) == (p["text"], p.get("reply_markup")):
                return bad(400, "Bad Request: message is not modified")
            m["text"], m["markup"] = p["text"], p.get("reply_markup")
            return ok(self.message(p["message_id"], p["text"], m["chat"]))
        if name == "sendDocument":
            filename, content, _ = next(iter(request_data.multipart_data.values()))
            self.documents.append((filename, content, p.get("caption")))
            return ok(self.message(self.new_id(), p.get("caption", "")))
        if name == "deleteMessage":
            self.messages[p["message_id"]]["deleted"] = True
            return ok(True)
        if name == "pinChatMessage":
            if not self.can_pin:
                return bad(400, "Bad Request: not enough rights to pin a message")
            self.pinned[p["chat_id"]] = p["message_id"]
            return ok(True)
        if name == "getChat":
            if p["chat_id"] != CHAT:
                return bad(400, "Bad Request: chat not found")
            info = {"id": CHAT, "type": "supergroup", "title": "G", "accent_color_id": 0, "max_reaction_count": 11}
            if CHAT in self.pinned:
                info["pinned_message"] = self.message(self.pinned[CHAT], "", CHAT)
            return ok(info)
        if name == "getChatMember":
            status = "left" if (p["chat_id"], p["user_id"]) in self.not_in else \
                "creator" if p["user_id"] in self.admins else "member"
            member = {"status": status, "user": {"id": p["user_id"], "is_bot": False, "first_name": "U"}}
            return ok({**member, "is_anonymous": False} if status == "creator" else member)
        if name == "setMessageReaction":
            self.reactions.append((p["chat_id"], p["message_id"], p["reaction"][0]["emoji"]))
            return ok(True)
        if name == "answerCallbackQuery":
            if p.get("show_alert") or p.get("text"):
                self.alerts.append(p.get("text"))
            return ok(True)
        return ok(True)

    def text(self, message_id):
        return unescape(re.sub(r"<[^>]+>", "", self.messages[message_id]["text"]))

    def buttons(self, message_id):
        rows = (self.messages[message_id]["markup"] or {}).get("inline_keyboard", [])
        return {b["text"]: b.get("callback_data") or b.get("url") for row in rows for b in row}

    def live(self, chat=None):
        return {i: m for i, m in self.messages.items() if not m["deleted"] and chat in (None, m["chat"])}


ESG_SCHEMA = """
CREATE TABLE flips (flip_number INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL, wager INTEGER NOT NULL,
    x INTEGER NOT NULL, y INTEGER NOT NULL, z INTEGER NOT NULL, player_count INTEGER NOT NULL, button_seat INTEGER NOT NULL,
    seed TEXT NOT NULL, commitment TEXT NOT NULL, deal TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
    y_defaulted INTEGER NOT NULL DEFAULT 0, z_defaulted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE flip_players (flip_number INTEGER NOT NULL REFERENCES flips(flip_number), user_id INTEGER NOT NULL,
    display_name TEXT NOT NULL, username TEXT, seat INTEGER NOT NULL, points TEXT NOT NULL, net_dollars INTEGER NOT NULL,
    scooped INTEGER NOT NULL, PRIMARY KEY (flip_number, seat));
"""  # the ESG bot's own tables (esgbot/storage/schema.py), reduced to what the ledger reads


def esg_money(dollars):
    return f"+${dollars}" if dollars > 0 else f"-${-dollars}" if dollars < 0 else "$0"


def esg_message(number, players):
    """A result message in the ESG bot's format (pinned against its real output by tests/fixtures/esg_results.json)."""
    top = max(players, key=lambda p: p[3])
    seats = "\n".join(f"{i}. {p[2]} — {1 if p[3] <= 0 else 2} points · {esg_money(p[3])}" for i, p in enumerate(players, 1))
    return f"🏁 Flip #{number} — {top[2]} wins\n\nSettle up\nsomeone owes {top[2]} $1\n\nPlayers\n{seats}\n\nBoards\nBoard 1: x"


class Chat:
    def __init__(self, monkeypatch, tmp_path, esg_db=True):
        self.tg = FakeTelegram()
        monkeypatch.setattr(HTTPXRequest, "do_request", lambda s, *a, **k: self.tg.do_request(*a, **k))
        self.esg_path = str(tmp_path / "esg.sqlite3")
        with sqlite3.connect(self.esg_path) as esg_db_file:
            esg_db_file.executescript(ESG_SCHEMA)
        self.app = ledger_bot.build_app("123:ABC", str(tmp_path / "ledger.db"), self.esg_path if esg_db else None)
        self.store = self.app.bot_data["store"]
        self.counter = 0

    async def __aenter__(self):
        await self.app.initialize()
        return self

    async def __aexit__(self, *exc):
        await self.app.shutdown()

    async def feed(self, **fields):
        self.counter += 1
        await self.app.process_update(Update.de_json({"update_id": self.counter, **fields}, self.app.bot))

    async def _send(self, user, chat, text, entities=()):
        self.counter += 1
        ents = [*entities]
        if text.startswith("/"):
            ents.insert(0, {"type": "bot_command", "offset": 0, "length": len(text.split()[0])})
        kind = {"id": chat, "type": "private", "first_name": user["first_name"]} if chat > 0 else \
            {"id": chat, "type": "supergroup", "title": "G"}
        msg = {"message_id": 5000 + self.counter, "date": 0, "text": text, "from": user, "chat": kind}
        if ents:
            msg["entities"] = ents
        await self.feed(message=msg)

    async def say(self, user, text, entities=()):
        """A message in the group."""
        await self._send(user, CHAT, text, entities)

    async def dm(self, user, text, entities=()):
        """A message in the user's private chat with the bot; sending one is what lets the bot message them back."""
        self.tg.started.add(user["id"])
        await self._send(user, user["id"], text, entities)

    async def start(self, user, payload=""):
        await self.dm(user, f"/start {payload}".strip())

    async def tap(self, user, message_id, data):
        chat = self.tg.messages[message_id]["chat"]
        await self.feed(callback_query={"id": str(self.counter), "from": user, "chat_instance": "x", "data": data,
                                        "message": self.tg.message(message_id, "x", chat)})

    async def flip(self, number, players, group=CHAT, record=True):
        """The ESG bot finishes a flip: it is saved in ESG's database, then the result message is posted.

        players: (telegram user id, @username, display name, net dollars) in seat order."""
        if record:
            with sqlite3.connect(self.esg_path) as db:
                db.execute("INSERT INTO flips VALUES (?, ?, 5, 6, 0, 1, ?, 0, ?, ?, '{}', 't', 't', 0, 0)",
                           (number, group, len(players), "0" * 64, "0" * 64))
                db.executemany("INSERT INTO flip_players VALUES (?, ?, ?, ?, ?, '1', ?, 0)",
                               [(number, uid, name, user, seat, net) for seat, (uid, user, name, net) in enumerate(players)])
        await self.say(ESGBOT, esg_message(number, players))

    def last_in(self, chat):
        return max(self.tg.live(chat))

    def prompt(self, chat):
        return next(i for i, m in self.tg.live(chat).items() if m["markup"] and "force_reply" in m["markup"])

    def no_prompt(self, chat):
        return not [m for m in self.tg.live(chat).values() if m["markup"] and "force_reply" in m["markup"]]

    def card(self, chat=CHAT):
        """The image the ledger should currently show, drawn independently from the stored balances."""
        rows = render.ranked(self.store.members(chat), self.store.balances(chat))
        return ledger_image.render_ledger(rows)

    def balances(self):
        names = {m.id: m.name for m in self.store.members(CHAT)}
        return {names[i]: c for i, c in self.store.balances(CHAT).items()}

    async def open_session(self, user, *players):
        """/edit in the group, then pick `players` and press Done in the user's private chat."""
        await self.say(user, "/edit")
        draft = self.last_in(user["id"])
        for who in players:
            await self.tap(user, draft, self.tg.buttons(draft)[who])
        await self.tap(user, draft, self.tg.buttons(draft)["➡ Done"])
        return draft


def run(coro):
    asyncio.run(coro)


async def join_everyone(c):
    await c.say(ALICE, "/join")
    await c.say(BOB, "/join Bobby")
    await c.say(ALICE, "/add @carol99 Caz", entities=[{"type": "mention", "offset": 5, "length": 8}])


def test_full_flow(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            await join_everyone(c)
            assert sorted(m.name for m in c.store.members(CHAT)) == ["Alice", "Bobby", "Caz"]
            pinned = c.store.pinned_message(CHAT)
            assert pinned and tg.messages[pinned]["photo"] == c.card()  # a pinned picture of the ledger, kept current
            assert any(n == "pinChatMessage" for n, _ in tg.calls)

            # Carol uses the bot later: her @username placeholder links to her Telegram id
            await c.say(CAROL, "/join")
            assert c.store.find(CHAT, user_id=3).name == "Caz"
            for user in (ALICE, BOB, CAROL):
                await c.start(user)  # each opens a private chat with the bot once

            # --- a session: starts in the group, runs in Alice's private chat ---
            quiet = len(tg.live(CHAT))
            await c.say(ALICE, "/edit")
            assert len(tg.live(CHAT)) == quiet  # the bot posts nothing in the group while she edits
            assert tg.reactions == [(CHAT, tg.reactions[0][1], "👌")]  # acknowledged without a new message
            draft = c.last_in(1)
            assert "who played" in tg.text(draft) and "Ledger: G" in tg.text(draft)
            for who in ("Alice", "Bobby", "Caz"):
                await c.tap(ALICE, draft, tg.buttons(draft)[who])
            await c.tap(ALICE, draft, tg.buttons(draft)["➡ Done"])
            first = c.prompt(1)
            assert "Alice" in tg.text(first)
            await c.dm(ALICE, "abc")
            assert "Couldn't read" in tg.text(c.last_in(1)) and c.prompt(1) == first  # same question stays open
            await c.dm(ALICE, "+80")
            second = c.prompt(1)
            assert second != first and tg.messages[first]["deleted"] and "Bobby" in tg.text(second)
            await c.dm(ALICE, "-30.50")
            assert "Caz -49.50" in tg.text(draft) and "remainder" in tg.text(draft)
            await c.tap(ALICE, draft, tg.buttons(draft)["📝 Add note"])
            assert "Note for this session" in tg.text(c.prompt(1))
            await c.dm(ALICE, "x" * 61)
            assert "up to 60" in tg.text(c.last_in(1))  # too long: asked again
            await c.dm(ALICE, "Poker Friday")
            assert "📝 Poker Friday" in tg.text(draft) and "📝 Edit note" in tg.buttons(draft)
            assert c.balances() == {"Alice": 0, "Bobby": 0, "Caz": 0}  # nothing saved before Confirm
            confirm = tg.buttons(draft)["✅ Confirm"]
            await c.tap(ALICE, draft, confirm)
            assert c.balances() == {"Alice": 8000, "Bobby": -3050, "Caz": -4950}
            assert "Alice  +80.00" in tg.text(draft) and "Total" not in tg.text(draft)  # her receipt includes the ledger
            announced = tg.text(c.last_in(CHAT))  # the group hears only the result, attributed
            for needle in ("Recorded", "#1 Session", "by Alice (@alice)", "📝 Poker Friday", "+80.00"):
                assert needle in announced
            assert tg.messages[pinned]["photo"] == c.card() and "#1 Session by Alice" in tg.text(pinned)
            assert c.no_prompt(1)
            await c.tap(ALICE, draft, confirm)
            assert "expired" in tg.alerts[-1] and c.store.count(CHAT) == 1  # a stale button never double-records

            # --- Bobby pays Alice 20, in his own private chat ---
            await c.say(BOB, "/pay")
            pay = c.last_in(2)
            await c.tap(BOB, pay, tg.buttons(pay)["Bobby"])
            assert "Bobby" not in tg.buttons(pay)  # can't pay yourself
            await c.tap(BOB, pay, tg.buttons(pay)["Alice"])
            await c.dm(BOB, "0")
            assert "more than zero" in tg.text(c.last_in(2)) and c.prompt(2)
            await c.dm(BOB, "20")
            await c.tap(BOB, pay, tg.buttons(pay)["✅ Confirm"])
            assert c.balances() == {"Alice": 6000, "Bobby": -1050, "Caz": -4950}
            assert "Bobby paid Alice S$20.00" in tg.text(c.last_in(CHAT))

            # --- Carol undoes the session ---
            await c.say(CAROL, "/undo")
            undo = c.last_in(3)
            assert "#1" in tg.text(undo) and "#2" in tg.text(undo)  # the choices are described, not just numbered
            await c.tap(CAROL, undo, next(d for t, d in tg.buttons(undo).items() if t.startswith("Undo #1 ")))
            await c.tap(CAROL, undo, tg.buttons(undo)["✅ Confirm"])
            assert c.balances() == {"Alice": -2000, "Bobby": 2000, "Caz": 0}
            assert sum(c.store.balances(CHAT).values()) == 0
            assert tg.messages[pinned]["photo"] == c.card()
            assert "#3 Undo of #1" in tg.text(pinned) and "by Caz" in tg.text(pinned)

            # --- history is complete and shows who did what ---
            await c.say(ALICE, "/history")
            history = tg.text(c.last_in(CHAT))
            for needle in ("📝 Poker Friday", "#1 Session", "#2 Payout", "#3 Undo of #1", "undone by #3",
                           "by Alice (@alice)", "by Bobby (@bob)", "by Caz (@carol99)"):
                assert needle in history

            # --- export: the whole history as a CSV, oldest first ---
            await c.say(BOB, "/export")
            filename, content, caption = tg.documents[-1]
            rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
            assert filename.startswith("ledger-history-") and filename.endswith(".csv") and "3 entries" in caption
            assert [r["entry"] for r in rows] == ["1"] * 3 + ["2"] * 2 + ["3"] * 3
            assert {r["editor_username"] for r in rows} == {"alice", "bob", "carol99"}
            assert any(r["note"] == "Poker Friday" for r in rows)

            # --- rename flows into the pinned ledger ---
            await c.say(ALICE, "/rename Caz = Carol")
            assert tg.messages[pinned]["photo"] == c.card()  # the picture uses the new name
            assert "by Caz" in tg.text(pinned)  # but the caption keeps the editor's name as recorded
            await c.say(ALICE, "/settle")
            assert "Alice pays Bobby S$20.00" in tg.text(c.last_in(CHAT))

    run(scenario())


def test_people_who_never_started_the_bot_get_a_deep_link_that_begins_their_edit(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            await join_everyone(c)
            await c.say(ALICE, "/edit")  # Alice never messaged the bot privately
            reply = c.last_in(CHAT)
            assert "can't message you privately" in tg.text(reply)
            assert tg.buttons(reply) == {"Open private chat": f"{LINK}edit{CHAT}"}
            assert not tg.live(1) and not tg.reactions and not c.app.bot_data.get("drafts")

            await c.start(ALICE, f"edit{CHAT}")  # she taps the link and presses Start
            draft = c.last_in(1)
            assert "who played" in tg.text(draft) and "Ledger: G" in tg.text(draft)
            assert len(c.app.bot_data["drafts"]) == 1

            await c.say(BOB, "/pay")  # each command makes its own link
            assert tg.buttons(c.last_in(CHAT)) == {"Open private chat": f"{LINK}pay{CHAT}"}
            await c.start(BOB, f"pay{CHAT}")
            assert "who paid" in tg.text(c.last_in(2))

            await c.say(CAROL, "/undo")  # nothing to undo: answered in the group, no private chat needed
            assert "Nothing to undo" in tg.text(c.last_in(CHAT)) and not tg.live(3)
            await c.start(CAROL, f"undo{CHAT}")  # a stale or hand-made link says the same, privately
            assert "Nothing to undo" in tg.text(c.last_in(3))

    run(scenario())


def test_start_links_are_checked_before_they_open_anything(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            await join_everyone(c)
            await c.start(ALICE, "edit-999")  # a group the bot doesn't know
            assert "can't find that group" in tg.text(c.last_in(1))
            tg.not_in.add((CHAT, 2))
            await c.start(BOB, f"edit{CHAT}")  # real group, but Bob isn't in it
            assert "can't find that group" in tg.text(c.last_in(2))
            await c.start(CAROL, "hello")  # not one of ours: just help
            assert "Group ledger" in tg.text(c.last_in(3))
            await c.start(CAROL)
            assert "Group ledger" in tg.text(c.last_in(3))
            assert not c.app.bot_data.get("drafts")

    run(scenario())


def test_one_edit_at_a_time_per_person(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            await join_everyone(c)
            await c.start(ALICE)
            await c.say(ALICE, "/edit")
            first = c.last_in(1)
            old_button = tg.buttons(first)["Alice"]
            await c.say(ALICE, "/pay")
            assert "Replaced by your newer edit" in tg.text(first) and not tg.buttons(first)
            second = c.last_in(1)
            assert second != first and "who paid" in tg.text(second)
            await c.tap(ALICE, first, old_button)
            assert "expired" in tg.alerts[-1]

    run(scenario())


def test_someone_who_left_the_group_cannot_confirm(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            await join_everyone(c)
            await c.start(ALICE)
            draft = await c.open_session(ALICE, "Alice", "Bobby")
            await c.dm(ALICE, "10")
            tg.not_in.add((CHAT, 1))
            before = len(tg.live(CHAT))
            await c.tap(ALICE, draft, tg.buttons(draft)["✅ Confirm"])
            assert "no longer in that group" in tg.text(draft)
            assert c.store.count(CHAT) == 0 and len(tg.live(CHAT)) == before

    run(scenario())


def test_cancel_and_stray_text_leave_the_ledger_untouched(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            await join_everyone(c)
            await c.start(ALICE)
            await c.dm(ALICE, "hello?")  # nothing is waiting for an answer
            assert "not waiting for anything" in tg.text(c.last_in(1))
            draft = await c.open_session(ALICE, "Alice", "Bobby")
            assert c.prompt(1)
            await c.tap(ALICE, draft, tg.buttons(draft)["✖ Cancel"])
            assert "cancelled" in tg.text(draft) and c.store.count(CHAT) == 0 and c.no_prompt(1)
            await c.dm(ALICE, "50")  # the question is gone, so a late answer does nothing
            assert c.store.count(CHAT) == 0 and "not waiting" in tg.text(c.last_in(1))

    run(scenario())


def test_note_is_optional_and_can_be_skipped_or_removed(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            await c.say(ALICE, "/join")
            await c.say(BOB, "/join")
            await c.start(ALICE)
            draft = await c.open_session(ALICE, "Alice", "Bob")
            await c.dm(ALICE, "5")
            await c.tap(ALICE, draft, tg.buttons(draft)["📝 Add note"])
            await c.tap(ALICE, draft, tg.buttons(draft)["↩ Back"])  # changed my mind
            assert c.no_prompt(1)
            await c.tap(ALICE, draft, tg.buttons(draft)["📝 Add note"])
            await c.dm(ALICE, "Oops")
            await c.tap(ALICE, draft, tg.buttons(draft)["📝 Edit note"])
            await c.dm(ALICE, "-")  # remove it again
            assert "📝 Oops" not in tg.text(draft) and "📝 Add note" in tg.buttons(draft)
            await c.tap(ALICE, draft, tg.buttons(draft)["✅ Confirm"])
            assert c.store.entry(CHAT, 1).note is None and c.balances() == {"Alice": 500, "Bob": -500}

    run(scenario())


def test_a_private_chat_ledger_works_in_place_without_announcements(monkeypatch, tmp_path):
    """/edit typed to the bot itself: the ledger is that private chat, so there is no group to tell."""
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            await c.dm(ALICE, "/join")
            await c.dm(ALICE, "/add Dave")
            await c.dm(ALICE, "/edit")
            draft = c.last_in(1)
            assert "Ledger: your private ledger" in tg.text(draft)
            for who in ("Alice", "Dave"):
                await c.tap(ALICE, draft, tg.buttons(draft)[who])
            await c.tap(ALICE, draft, tg.buttons(draft)["➡ Done"])
            await c.dm(ALICE, "12.50")
            before = len(tg.live(1))
            await c.tap(ALICE, draft, tg.buttons(draft)["✅ Confirm"])
            assert c.store.balances(1) == {1: 1250, 2: -1250}
            assert len(tg.live(1)) == before  # receipt replaces the draft in place; nothing else is posted

    run(scenario())


def test_pinned_ledger_is_reposted_when_deleted_and_pin_failure_is_explained(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            c.tg.can_pin = False
            await c.say(ALICE, "/join")
            assert "couldn't pin" in c.tg.text(c.last_in(CHAT))
            old = c.store.pinned_message(CHAT)
            c.tg.messages[old]["deleted"] = True
            await c.say(BOB, "/join")
            new = c.store.pinned_message(CHAT)
            assert new != old and c.tg.messages[new]["photo"] == c.card()

    run(scenario())


def test_ledger_command_sends_the_picture_with_the_last_edit_as_caption(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            await c.say(ALICE, "/ledger")
            assert "No players yet" in tg.text(c.last_in(CHAT)) and "photo" not in tg.messages[c.last_in(CHAT)]
            await join_everyone(c)
            e = c.store.add_entry(CHAT, "session", {1: 100, 2: -100}, 1, "Alice", editor_username="alice")
            await c.say(BOB, "/ledger")
            sent = c.last_in(CHAT) if "photo" in tg.messages[c.last_in(CHAT)] else None
            assert sent and tg.messages[sent]["photo"] == c.card()
            assert tg.text(sent) == f"Last edit: #{e.seq} Session by Alice (@alice) · {render.when(e)}"
            assert tg.messages[c.store.pinned_message(CHAT)]["photo"] == c.card()  # and the pin is brought up to date
            assert tg.messages[sent]["photo"][:8] == b"\x89PNG\r\n\x1a\n"

    run(scenario())


def test_without_a_drawable_picture_the_ledger_falls_back_to_text(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            await join_everyone(c)
            pinned = c.store.pinned_message(CHAT)
            assert "photo" in tg.messages[pinned]
            monkeypatch.setattr(ledger_image, "render_ledger", lambda *a, **k: None)  # e.g. fonts went missing
            await c.say(ALICE, "/ledger")
            texts = [tg.text(i) for i in tg.live(CHAT) if "photo" not in tg.messages[i]]
            assert any("Alice  " in t and "Bobby" in t and "Caz" in t for t in texts)  # the text table
            new_pin = c.store.pinned_message(CHAT)
            assert new_pin != pinned and "photo" not in tg.messages[new_pin]  # a photo can't become text: reposted

    run(scenario())


def test_add_other_people_by_name_by_mention_and_without_username(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            reply = lambda: tg.text(c.last_in(CHAT))  # noqa: E731
            await c.say(ALICE, "/add")
            assert "/add @friend Nickname" in reply()
            await c.say(ALICE, "/add Dave")  # plain name, no Telegram account attached
            assert c.store.find(CHAT, name="Dave").tg_user_id is None
            await c.say(BOB, "/add dave")  # names are unique, ignoring case
            assert "already taken" in reply()
            # someone with no @username: picked from Telegram's member list, which carries their user id
            eve = {"type": "text_mention", "offset": 5, "length": 3, "user": {"id": 9, "is_bot": False, "first_name": "Eve"}}
            await c.say(ALICE, "/add Eve", entities=[eve])
            assert c.store.find(CHAT, user_id=9).name == "Eve"
            await c.say(BOB, "/add Eve", entities=[eve])  # same person again, even under another name
            assert "already in" in reply() and len(c.store.members(CHAT)) == 2
            await c.say(BOB, "/add @bobby Bobby", entities=[{"type": "mention", "offset": 5, "length": 6}])
            assert c.store.find(CHAT, username="bobby").name == "Bobby"
            assert tg.messages[c.store.pinned_message(CHAT)]["photo"] == c.card()

    run(scenario())


def test_export_with_no_entries_says_so(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            await c.say(ALICE, "/export")
            assert "Nothing to export" in c.tg.text(c.last_in(CHAT)) and not c.tg.documents

    run(scenario())


def test_command_menu_is_published_valid_and_matches_the_handlers(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            await ledger_bot.publish_commands(c.app)  # what run_polling does at startup
            call = next(p for n, p in c.tg.calls if n == "setMyCommands")
            published = call["commands"]
            assert [x["command"] for x in published] == [x.command for x in ledger_bot.COMMANDS]
            for x in published:  # Telegram's rules for the menu
                assert re.fullmatch(r"[a-z0-9_]{1,32}", x["command"]) and 3 <= len(x["description"]) <= 256
            handled = {cmd for h in c.app.handlers[0] if hasattr(h, "commands") for cmd in h.commands}
            assert {x.command for x in ledger_bot.COMMANDS} <= handled  # nothing in the menu is dead
            # everything the help text advertises is a real command too
            assert set(re.findall(r"^/(\w+)", ledger_bot.HELP, re.M)) <= handled

    run(scenario())


def test_a_telegram_error_while_publishing_the_menu_does_not_stop_the_bot(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            real = c.tg.do_request

            async def flaky(url, *args, **kwargs):
                if url.endswith("setMyCommands"):
                    return 429, b'{"ok": false, "error_code": 429, "description": "Too Many Requests: retry later"}'
                return await real(url, *args, **kwargs)

            monkeypatch.setattr(HTTPXRequest, "do_request", lambda s, *a, **k: flaky(*a, **k))
            await ledger_bot.publish_commands(c.app)  # logs and carries on

    run(scenario())


def pins(tg):
    return [p["message_id"] for n, p in tg.calls if n == "pinChatMessage"]


def test_ledger_command_does_not_repin_a_pin_that_worked_or_fight_an_admin(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            await join_everyone(c)
            ours = c.store.pinned_message(CHAT)
            assert tg.pinned[CHAT] == ours and c.store.pin_ok(CHAT)
            await c.say(ALICE, "/ledger")
            assert pins(tg) == [ours]  # already pinned successfully: no needless pin (each one posts a service message)
            tg.pinned[CHAT] = 7777  # an admin pins something of their own
            await c.say(ALICE, "/ledger")
            assert pins(tg) == [ours] and tg.pinned[CHAT] == 7777  # their choice stands
            c.store.add_entry(CHAT, "session", {1: 100, 2: -100}, 1, "Alice")
            await ledger_bot.refresh_pinned(_Ctx(c), CHAT)
            assert "#1" in tg.text(ours) and tg.messages[ours]["photo"] == c.card()  # edits still land on our message

    run(scenario())


class _Ctx:
    """The little of a handler context that refresh_pinned uses."""
    def __init__(self, chat):
        self.bot, self.application = chat.app.bot, chat.app


def test_a_pin_that_failed_for_lack_of_permission_is_retried_by_ledger_as_the_hint_promises(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            tg.can_pin = False
            await c.say(ALICE, "/join")
            assert "send /ledger again" in tg.text(c.last_in(CHAT)) and CHAT not in tg.pinned
            tg.can_pin = True  # the admin grants the permission
            await c.say(ALICE, "/ledger")
            assert tg.pinned[CHAT] == c.store.pinned_message(CHAT)

    run(scenario())


def test_a_failed_group_announcement_does_not_stop_the_pinned_ledger_updating(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            await join_everyone(c)
            await c.start(ALICE)
            draft = await c.open_session(ALICE, "Alice", "Bobby")
            await c.dm(ALICE, "10")
            tg.fail_send = lambda p: p["chat_id"] == CHAT and "Recorded" in p["text"]  # Telegram times out
            await c.tap(ALICE, draft, tg.buttons(draft)["✅ Confirm"])
            assert c.balances()["Alice"] == 1000  # the entry is saved regardless
            assert tg.messages[c.store.pinned_message(CHAT)]["photo"] == c.card()  # and the pin still updated
            assert "couldn't fully update the group" in tg.text(c.last_in(1))

    run(scenario())


def esg_text(label="scoop", names=("Alice", "Bobby", "Caz"), flip=42):
    """A real ESG result message (from tests/fixtures) with other names and flip number swapped in."""
    text = SAMPLES[label]["text"]
    for old, new in zip(("Player 11", "Player 12", "Player 13"), names):
        text = text.replace(old, new)
    return text.replace("Flip #42", f"Flip #{flip}").replace("/verify 42", f"/verify {flip}")


async def follow_esg(c):
    await join_everyone(c)
    await c.say(ALICE, "/esg @esg_flip_bot")  # Alice is the group admin


# the ESG bot's view of our three players: same people, deliberately different names
A, B, C = (1, "alice", "Totally Different", 0), (2, "bob", "Bob the Builder", 0), (3, "carol99", "c", 0)


def seat(player, net):
    return (*player[:3], net)


def test_only_admins_choose_the_bot_and_the_status_explains_telegrams_settings(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            reply = lambda: tg.text(c.last_in(CHAT))  # noqa: E731
            await c.say(BOB, "/esg")
            assert "not following" in reply()
            await c.say(BOB, "/esg @esg_flip_bot")
            assert "Only group admins" in reply() and c.store.esg_bot(CHAT) is None
            await c.say(ALICE, "/esg @esg_flip_bot")
            assert "Following ESG flip results from @esg_flip_bot" in reply() and c.store.esg_bot(CHAT) == "esg_flip_bot"
            assert "Bot-to-Bot Communication Mode" in reply() and "Group Privacy is still on" in reply()
            tg.privacy_on = False
            await c.say(BOB, "/esg")  # anyone may look
            assert "Group Privacy is still on" not in reply() and "@esg_flip_bot" in reply()
            await c.say(BOB, "/esg")
            assert "haven't received any message from it" in reply()  # nothing heard yet: points at Telegram's settings
            await c.say(ESGBOT, esg_text())
            await c.say(BOB, "/esg")
            assert "Last message I received from it" in reply() and "a flip result" in reply()
            await c.say(ALICE, "/esg nonsense words")
            assert "Usage" in reply()
            await c.say(ALICE, "/esg off")
            assert c.store.esg_bot(CHAT) is None and "not following" in reply()
            await c.dm(ALICE, "/esg @esg_flip_bot")
            assert "in your group" in tg.text(c.last_in(1))

    run(scenario())


def test_esg_status_says_whether_players_can_be_looked_up(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path, esg_db=False) as c:
            await c.say(ALICE, "/esg @esg_flip_bot")
            assert "can't read ESG's database" in c.tg.text(c.last_in(CHAT))

    run(scenario())


def test_esg_results_are_matched_by_telegram_account_not_by_name(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            await follow_esg(c)  # ledger: Alice (@alice), Bobby (added as @bob), Caz (added as @carol99)
            await c.flip(42, [seat(A, 500), seat(B, -300), seat(C, -200)])  # ESG calls them other things entirely
            assert c.balances() == {"Alice": 50000, "Bobby": -30000, "Caz": -20000}  # whole dollars -> cents
            assert not any(tg.buttons(i) for i in tg.live(CHAT))  # nobody had to be asked
            e = c.store.entry(CHAT, 1)
            assert (e.kind, e.note, e.editor_name, e.editor_username) == ("session", "ESG Flip #42", "ESG Flip Bot", "esg_flip_bot")
            said = tg.text(c.last_in(CHAT))
            assert "by ESG Flip Bot (@esg_flip_bot)" in said and "📝 ESG Flip #42" in said
            assert tg.messages[c.store.pinned_message(CHAT)]["photo"] == c.card()  # the pinned ledger followed
            assert c.store.find(CHAT, user_id=2).name == "Bobby"  # @bob placeholder is now tied to their account

            messages = len(tg.live(CHAT))
            await c.say(ESGBOT, esg_message(42, [seat(A, 500), seat(B, -300), seat(C, -200)]))  # delivered twice
            assert c.store.count(CHAT) == 1 and len(tg.live(CHAT)) == messages

            renamed = (1, "alice_new", "Alice again", 0)  # same person, new username and display name: still Alice
            await c.flip(43, [seat(renamed, -100), seat(B, 100)])
            assert c.balances() == {"Alice": 40000, "Bobby": -20000, "Caz": -20000}
            assert c.store.find(CHAT, user_id=1).tg_username == "alice_new"
            before = len(tg.live(CHAT))
            await c.flip(44, [seat(A, 0), seat(B, 0)])  # tie: nothing moved, so nothing to record
            assert c.store.count(CHAT) == 2 and len(tg.live(CHAT)) == before
            assert sum(c.store.balances(CHAT).values()) == 0

    run(scenario())


def test_two_people_with_the_same_name_stay_separate(monkeypatch, tmp_path):
    """The bug this design exists for: ESG players who share a display name used to be merged by name."""
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            await follow_esg(c)
            await c.say(ALICE, "/add @sam_one Sam Tan", entities=[{"type": "mention", "offset": 5, "length": 8}])
            sam1, sam2 = (618, "sam_one", "sam", 0), (600, "sam_two", "sam", 0)  # two people, both "sam"
            await c.flip(50, [seat(A, 1000), seat(sam1, -500), seat(sam2, -500)])  # both in the same flip
            ask = c.last_in(CHAT)
            assert "sam (@sam_two)" in tg.text(ask) and "@sam_one" not in tg.text(ask)  # only the stranger is asked about
            assert "Sam Tan" not in tg.buttons(ask)  # and Sam Tan is already the other sam
            await c.tap(BOB, ask, next(d for t, d in tg.buttons(ask).items() if t.startswith("➕ New player")))
            assert c.balances() == {"Alice": 100000, "Bobby": 0, "Caz": 0, "Sam Tan": -50000, "sam": -50000}
            assert c.store.find(CHAT, user_id=600).name == "sam" and c.store.find(CHAT, user_id=618).name == "Sam Tan"

            await c.flip(51, [seat(A, 300), seat(sam2, -300)])  # only the second sam: not Sam Tan
            assert c.balances()["sam"] == -80000 and c.balances()["Sam Tan"] == -50000
            await c.flip(52, [seat(A, 200), seat(sam1, -200)])  # and only the first
            assert c.balances()["Sam Tan"] == -70000 and c.balances()["sam"] == -80000

            third = (700, "someone_else", "sam", 0)  # a third "sam": the name is taken, so it gets a distinguishing name
            await c.flip(53, [seat(A, 100), seat(third, -100)])
            await c.tap(ALICE, c.last_in(CHAT), next(d for t, d in tg.buttons(c.last_in(CHAT)).items() if t.startswith("➕")))
            assert c.store.find(CHAT, user_id=700).name == "sam (@someone_else)"
            assert sum(c.store.balances(CHAT).values()) == 0

    run(scenario())


def test_an_unknown_account_can_be_tied_to_an_existing_player_who_has_no_account_yet(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            await follow_esg(c)
            await c.say(ALICE, "/add Dave")  # added by name only, so nothing to match on
            dave = (9, "dave_tg", "Dave Lim", 0)
            await c.flip(60, [seat(A, 400), seat(dave, -400)])
            ask = c.last_in(CHAT)
            assert "Dave Lim (@dave_tg)" in tg.text(ask)
            shown = set(tg.buttons(ask))
            assert {"Caz", "Dave"} <= shown and not {"Alice", "Bobby"} & shown  # Alice and Bobby already have accounts
            pending_id = tg.buttons(ask)["✖ Skip this flip"].split(":")[1]
            await c.tap(BOB, ask, f"e:{pending_id}:{c.store.find(CHAT, name='Alice').id}")  # a forged button for Alice
            assert "already tied" in tg.alerts[-1] and c.store.count(CHAT) == 0
            await c.tap(BOB, ask, tg.buttons(ask)["Dave"])
            assert c.balances()["Dave"] == -40000 and c.store.find(CHAT, user_id=9).name == "Dave"
            assert "Recorded" in tg.text(ask) and not tg.buttons(ask)  # the question became the receipt
            await c.flip(61, [seat(A, 100), seat(dave, -100)])  # remembered
            assert c.balances()["Dave"] == -50000 and c.store.count(CHAT) == 2

    run(scenario())


def test_a_flip_can_be_skipped_and_stale_buttons_are_refused(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            await follow_esg(c)
            stranger = (77, "stranger", "Someone New", 0)
            await c.flip(70, [seat(A, 100), seat(stranger, -100)])
            ask = c.last_in(CHAT)
            pending_id = tg.buttons(ask)["✖ Skip this flip"].split(":")[1]
            await c.tap(ALICE, ask, tg.buttons(ask)["✖ Skip this flip"])
            assert "wasn't added" in tg.text(ask) and c.store.count(CHAT) == 0 and not tg.buttons(ask)
            await c.tap(ALICE, ask, f"e:{pending_id}:1")
            assert "expired" in tg.alerts[-1]

    run(scenario())


def test_only_the_followed_bot_in_a_followed_chat_is_listened_to(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            await join_everyone(c)
            players = [seat(A, 100), seat(B, -100)]
            await c.flip(80, players)  # not following anyone yet
            await c.say(ALICE, "/esg @esg_flip_bot")
            before = len(tg.live(CHAT))
            other_bot = {"id": 501, "is_bot": True, "first_name": "Other", "username": "otherbot"}
            await c.say(other_bot, esg_message(80, players))  # another bot, same text
            await c.say(BOB, esg_message(80, players))  # a person typing the same text
            assert c.store.count(CHAT) == 0 and len(tg.live(CHAT)) == before
            await c.say({**ESGBOT, "username": "ESG_Flip_Bot"}, esg_message(80, players))  # username case doesn't matter
            assert c.store.count(CHAT) == 1 and c.store.entry(CHAT, 1).note == "ESG Flip #80"
            await c.say(ESGBOT, "🏁 something else entirely\n\nPlayers\n1. nope")  # not a result we understand
            await c.say(ESGBOT, "Flip #53 was abandoned.")
            assert c.store.count(CHAT) == 1

    run(scenario())


def test_esg_results_that_cannot_be_trusted_are_reported_not_recorded(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path) as c:
            tg = c.tg
            reply = lambda: tg.text(c.last_in(CHAT))  # noqa: E731
            await follow_esg(c)
            players = [seat(A, 500), seat(B, -500)]
            await c.say(ESGBOT, esg_message(1, players).replace("Flip #1 — ", "Flip result — "))
            assert "couldn't save that flip" in reply()
            await c.say(ESGBOT, esg_message(2, players).replace("+$500", "+$510"))
            assert "don't add up to zero" in reply() and "#2" in reply()
            await c.flip(3, players, record=False)  # the message arrives but ESG's database has no such flip
            assert "no such flip for this group" in reply()
            await c.flip(4, players, group=-999)  # that flip belongs to a different group
            assert "no such flip for this group" in reply()
            await c.flip(5, players, record=False)
            with sqlite3.connect(c.esg_path) as db:  # the database says something else than the message did
                db.execute("INSERT INTO flips VALUES (5, ?, 5, 6, 0, 1, 2, 0, ?, ?, '{}', 't', 't', 0, 0)", (CHAT, "0" * 64, "0" * 64))
                db.executemany("INSERT INTO flip_players VALUES (5, ?, ?, ?, ?, '1', ?, 0)", [(1, "x", "x", 0, 400), (2, "y", "y", 1, -400)])
            await c.say(ESGBOT, esg_message(5, players))
            assert "disagree about the amounts" in reply()
            assert c.store.count(CHAT) == 0 and not c.store.esg_recorded(CHAT, 2)

    run(scenario())


def test_without_a_readable_esg_database_nothing_is_recorded_by_guessing(monkeypatch, tmp_path):
    async def scenario():
        async with Chat(monkeypatch, tmp_path, esg_db=False) as c:
            tg = c.tg
            await follow_esg(c)
            players = [seat(A, 500), seat(B, -500)]
            await c.flip(9, players)
            assert "without ESG's database" in tg.text(c.last_in(CHAT)) and c.store.count(CHAT) == 0
            c.app.bot_data["esg_db"] = str(tmp_path / "garbage.sqlite3")  # configured, but not a database
            Path(c.app.bot_data["esg_db"]).write_text("not sqlite")
            await c.say(ESGBOT, esg_message(9, players))
            assert "couldn't read ESG's database" in tg.text(c.last_in(CHAT)) and c.store.count(CHAT) == 0

    run(scenario())
