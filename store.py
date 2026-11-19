"""SQLite ledger. Entries are append-only and every entry sums to zero, so the ledger always tallies."""
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    name TEXT NOT NULL COLLATE NOCASE,
    tg_user_id INTEGER,
    tg_username TEXT COLLATE NOCASE,
    UNIQUE (chat_id, name)
);
CREATE UNIQUE INDEX IF NOT EXISTS members_user ON members (chat_id, tg_user_id) WHERE tg_user_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS members_username ON members (chat_id, tg_username) WHERE tg_username IS NOT NULL;

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('session', 'payment', 'undo')),
    created_at TEXT NOT NULL,
    editor_user_id INTEGER,
    editor_name TEXT NOT NULL,
    editor_username TEXT,
    note TEXT,
    reverses INTEGER UNIQUE REFERENCES entries (id),
    UNIQUE (chat_id, seq)
);
CREATE TABLE IF NOT EXISTS entry_lines (
    entry_id INTEGER NOT NULL REFERENCES entries (id),
    member_id INTEGER NOT NULL REFERENCES members (id),
    cents INTEGER NOT NULL CHECK (cents != 0),
    PRIMARY KEY (entry_id, member_id)
);
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    pinned_message_id INTEGER,
    pin_ok INTEGER,  -- did pinning that message succeed? NULL until tried
    esg_bot TEXT  -- @username (no @) of the ESG Flip bot whose results this chat's ledger follows
);
CREATE TABLE IF NOT EXISTS esg_flips (  -- ESG flips already in the ledger, so none is ever added twice
    chat_id INTEGER NOT NULL,
    flip_number INTEGER NOT NULL,
    entry_id INTEGER NOT NULL REFERENCES entries (id),
    PRIMARY KEY (chat_id, flip_number)
);

CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS lines_no_update BEFORE UPDATE ON entry_lines
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS lines_no_delete BEFORE DELETE ON entry_lines
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
"""

# The four statements that make the ledger append-only. purge() lifts and restores exactly these.
TRIGGER_NAMES = re.findall(r"CREATE TRIGGER IF NOT EXISTS (\w+)", SCHEMA)
TRIGGER_SQL = re.findall(r"CREATE TRIGGER IF NOT EXISTS \w+ .*?END;", SCHEMA, re.S)

MAX_NAME = 24
MAX_PURGE = 20
MAX_NOTE = 60


class LedgerError(Exception):
    """A user-facing reason an operation was refused."""


@dataclass(frozen=True)
class Member:
    id: int
    name: str
    tg_user_id: int | None
    tg_username: str | None


@dataclass(frozen=True)
class Entry:
    id: int
    seq: int
    kind: str
    created_at: datetime
    editor_name: str
    editor_username: str | None  # Telegram @username when they made the edit, if they had one
    reverses: int | None  # seq of the entry this one undoes
    reversed_by: int | None  # seq of the entry that undid this one
    note: str | None = None
    lines: dict[int, int] = field(default_factory=dict)  # member_id -> cents


@dataclass(frozen=True)
class Purge:
    """What removing some entries would do: every entry that goes (an entry and the undo of it go together)."""
    entries: list[Entry]
    effect: dict[int, int]  # member_id -> change to their balance, cents (empty if balances stay as they are)
    esg_flips: list[int]  # ESG flip numbers that would no longer count as recorded


@dataclass(frozen=True)
class ProfitEvent:
    seq: int  # the ledger entry number (#81), so a chart can be matched up with /history
    at: datetime
    deltas: dict[int, int]  # member_id -> cents won (+) or lost (-)


def clean_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        raise LedgerError("A name is required.")
    if len(name) > MAX_NAME or "=" in name or name.startswith("@"):
        raise LedgerError(f"Names are up to {MAX_NAME} characters, can't contain '=' or start with '@'.")
    return name


def clean_note(raw: str) -> str | None:
    note = " ".join(raw.split())
    if len(note) > MAX_NOTE:
        raise LedgerError(f"Notes are up to {MAX_NOTE} characters. That one is {len(note)}.")
    return note or None


class Store:
    def __init__(self, path: str):
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None)  # autocommit; transactions are explicit
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self._add_column("chats", "pin_ok", "INTEGER")  # databases created before these columns existed
        self._add_column("chats", "esg_bot", "TEXT")

    def _add_column(self, table: str, column: str, decl: str) -> None:
        if column not in {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    @contextmanager
    def _tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    # --- members -----------------------------------------------------------

    def members(self, chat: int) -> list[Member]:
        rows = self.db.execute(
            "SELECT id, name, tg_user_id, tg_username FROM members WHERE chat_id = ? ORDER BY name", (chat,))
        return [Member(*r) for r in rows]

    def find(self, chat: int, *, user_id: int | None = None, username: str | None = None,
             name: str | None = None) -> Member | None:
        for column, value in (("tg_user_id", user_id), ("tg_username", username), ("name", name)):
            if value is None:
                continue
            row = self.db.execute(
                f"SELECT id, name, tg_user_id, tg_username FROM members WHERE chat_id = ? AND {column} = ?",
                (chat, value)).fetchone()
            if row:
                return Member(*row)
        return None

    def add_member(self, chat: int, name: str, user_id: int | None = None,
                   username: str | None = None) -> Member:
        name = clean_name(name)
        if self.find(chat, name=name):
            raise LedgerError(f"The name '{name}' is already taken.")
        cur = self.db.execute(
            "INSERT INTO members (chat_id, name, tg_user_id, tg_username) VALUES (?, ?, ?, ?)",
            (chat, name, user_id, username))
        return Member(cur.lastrowid, name, user_id, username)

    def rename(self, chat: int, member_id: int, name: str) -> None:
        name = clean_name(name)
        clash = self.find(chat, name=name)
        if clash and clash.id != member_id:
            raise LedgerError(f"The name '{name}' is already taken.")
        self.db.execute("UPDATE members SET name = ? WHERE id = ? AND chat_id = ?", (name, member_id, chat))

    def identify(self, chat: int, user_id: int, username: str | None) -> Member | None:
        """The member behind a Telegram user, linking a name-only placeholder added via @username."""
        member = self.find(chat, user_id=user_id)
        if member is None and username:
            placeholder = self.find(chat, username=username)
            if placeholder and placeholder.tg_user_id is None:
                self.db.execute("UPDATE members SET tg_user_id = ? WHERE id = ?", (user_id, placeholder.id))
                member = self.find(chat, user_id=user_id)
        if member and member.tg_username != username:
            try:
                self.db.execute("UPDATE members SET tg_username = ? WHERE id = ?", (username, member.id))
            except sqlite3.IntegrityError:  # another entry already claims that @username
                pass
        return member

    # --- entries -----------------------------------------------------------

    def add_esg_entry(self, chat: int, flip_number: int, lines: dict[int, int], editor_user_id: int | None,
                      editor_name: str, editor_username: str | None) -> Entry:
        """An ESG flip as a session entry, atomically with the record that it has been added."""
        with self._tx():
            if self.db.execute("SELECT 1 FROM esg_flips WHERE chat_id = ? AND flip_number = ?",
                               (chat, flip_number)).fetchone():
                raise LedgerError(f"ESG flip #{flip_number} is already in the ledger.")
            seq = self._insert(chat, "session", lines, (editor_user_id, editor_name, editor_username), None,
                               f"ESG Flip #{flip_number}")
            self.db.execute("INSERT INTO esg_flips (chat_id, flip_number, entry_id) "
                            "SELECT ?, ?, id FROM entries WHERE chat_id = ? AND seq = ?", (chat, flip_number, chat, seq))
        return self.entry(chat, seq)

    def esg_recorded(self, chat: int, flip_number: int) -> bool:
        return self.db.execute("SELECT 1 FROM esg_flips WHERE chat_id = ? AND flip_number = ?",
                               (chat, flip_number)).fetchone() is not None

    def esg_bot(self, chat: int) -> str | None:
        row = self.db.execute("SELECT esg_bot FROM chats WHERE chat_id = ?", (chat,)).fetchone()
        return row[0] if row else None

    def set_esg_bot(self, chat: int, username: str | None) -> None:
        self.db.execute("INSERT INTO chats (chat_id, esg_bot) VALUES (?, ?) "
                        "ON CONFLICT (chat_id) DO UPDATE SET esg_bot = excluded.esg_bot", (chat, username))

    def link_member(self, chat: int, member_id: int, user_id: int, username: str | None) -> None:
        """Record that this ledger player is that Telegram account (what /join does for yourself)."""
        try:
            self.db.execute("UPDATE members SET tg_user_id = ?, tg_username = COALESCE(?, tg_username) "
                            "WHERE id = ? AND chat_id = ? AND tg_user_id IS NULL", (user_id, username, member_id, chat))
        except sqlite3.IntegrityError:
            raise LedgerError("That Telegram account or username is already linked to another player.") from None

    def add_entry(self, chat: int, kind: str, lines: dict[int, int], editor_user_id: int | None,
                  editor_name: str, *, editor_username: str | None = None, note: str | None = None) -> Entry:
        with self._tx():
            seq = self._insert(chat, kind, lines, (editor_user_id, editor_name, editor_username), None,
                               clean_note(note or ""))
        return self.entry(chat, seq)

    def undo(self, chat: int, seq: int, editor_user_id: int | None, editor_name: str, *,
             editor_username: str | None = None) -> Entry:
        with self._tx():
            target = self.entry(chat, seq)
            if target is None:
                raise LedgerError(f"There is no entry #{seq}.")
            if target.reversed_by:
                raise LedgerError(f"#{seq} was already undone by #{target.reversed_by}.")
            seq = self._insert(chat, "undo", {m: -c for m, c in target.lines.items()},
                               (editor_user_id, editor_name, editor_username), target.id, None)
        return self.entry(chat, seq)

    def _insert(self, chat, kind, lines, editor, reverses, note) -> int:
        """Validates and inserts inside the caller's transaction; returns the new entry's seq."""
        lines = {m: c for m, c in lines.items() if c != 0}
        if len(lines) < 2:
            raise LedgerError("An entry needs at least two people with non-zero amounts.")
        if sum(lines.values()) != 0:
            raise LedgerError("Amounts don't add up to zero, so nothing was recorded.")
        marks = ",".join("?" * len(lines))
        known = self.db.execute(
            f"SELECT COUNT(*) FROM members WHERE chat_id = ? AND id IN ({marks})", (chat, *lines)).fetchone()[0]
        if known != len(lines):
            raise LedgerError("Unknown player in that entry.")
        seq = self.db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM entries WHERE chat_id = ?", (chat,)).fetchone()[0]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = self.db.execute(
            "INSERT INTO entries (chat_id, seq, kind, created_at, editor_user_id, editor_name, editor_username, "
            "note, reverses) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (chat, seq, kind, now, *editor, note, reverses))
        self.db.executemany("INSERT INTO entry_lines (entry_id, member_id, cents) VALUES (?, ?, ?)",
                            [(cur.lastrowid, m, c) for m, c in lines.items()])
        return seq

    _ENTRY_SQL = """
        SELECT e.id, e.seq, e.kind, e.created_at, e.editor_name, e.editor_username, e.note, t.seq AS reverses, r.seq AS reversed_by
        FROM entries e
        LEFT JOIN entries t ON t.id = e.reverses
        LEFT JOIN entries r ON r.reverses = e.id
        WHERE e.chat_id = ? {where}
        ORDER BY e.seq DESC LIMIT ? OFFSET ?"""

    def _entries(self, where: str, params: tuple, limit: int, offset: int = 0) -> list[Entry]:
        rows = self.db.execute(self._ENTRY_SQL.format(where=where), (*params, limit, offset)).fetchall()
        lines: dict[int, dict[int, int]] = {r["id"]: {} for r in rows}
        marks = ",".join("?" * len(lines))
        for entry_id, member_id, cents in self.db.execute(
                f"SELECT entry_id, member_id, cents FROM entry_lines WHERE entry_id IN ({marks})", tuple(lines)):
            lines[entry_id][member_id] = cents
        return [Entry(r["id"], r["seq"], r["kind"], datetime.fromisoformat(r["created_at"]), r["editor_name"],
                      r["editor_username"], r["reverses"], r["reversed_by"], r["note"], lines[r["id"]])
                for r in rows]

    def entries(self, chat: int, limit: int, offset: int = 0) -> list[Entry]:
        """Newest first."""
        return self._entries("", (chat,), limit, offset)

    def entry(self, chat: int, seq: int) -> Entry | None:
        found = self._entries("AND e.seq = ?", (chat, seq), 1)
        return found[0] if found else None

    def profit_events(self, chat: int) -> list[ProfitEvent]:
        """Each session that still stands, as its result per player, oldest first: the raw material for profit over time.

        A session someone undid is left out altogether (at the time it was recorded, not just from the undo onwards),
        so a curve never shows a result that was taken back. Undoing the undo brings it back where it was.
        Payouts settle a balance rather than win or lose anything, so they never appear.
        """
        entries = self.db.execute(
            "SELECT id, seq, kind, reverses, created_at FROM entries WHERE chat_id = ? ORDER BY seq", (chat,)).fetchall()
        undone_by = {row["reverses"]: row["id"] for row in entries if row["reverses"]}
        kinds = {row["id"]: row["kind"] for row in entries}

        def stands(entry_id: int) -> bool:  # standing unless a standing entry reversed it
            return entry_id not in undone_by or not stands(undone_by[entry_id])

        lines: dict[int, dict[int, int]] = {}
        for entry_id, member_id, cents in self.db.execute(
                "SELECT l.entry_id, l.member_id, l.cents FROM entry_lines l JOIN entries e ON e.id = l.entry_id "
                "WHERE e.chat_id = ?", (chat,)):
            lines.setdefault(entry_id, {})[member_id] = cents
        return [ProfitEvent(row["seq"], datetime.fromisoformat(row["created_at"]), lines[row["id"]])
                for row in entries if kinds[row["id"]] == "session" and stands(row["id"])]

    def purge_plan(self, chat: int, seqs: list[int]) -> Purge:
        """What purge(seqs) would remove. An entry and the undo of it can't be separated, so both go."""
        found: dict[int, Entry] = {}
        todo = list(seqs)
        while todo:
            seq = todo.pop()
            if seq in found:
                continue
            entry = self.entry(chat, seq)
            if entry is None:
                raise LedgerError(f"There is no entry #{seq}.")
            found[seq] = entry
            todo += [n for n in (entry.reverses, entry.reversed_by) if n]
        entries = [found[n] for n in sorted(found)]
        effect: dict[int, int] = {}
        for entry in entries:
            for member_id, cents in entry.lines.items():
                effect[member_id] = effect.get(member_id, 0) - cents
        marks = ",".join("?" * len(entries))
        flips = [r[0] for r in self.db.execute(
            f"SELECT flip_number FROM esg_flips WHERE chat_id = ? AND entry_id IN ({marks}) ORDER BY 1",
            (chat, *(e.id for e in entries)))]
        return Purge(entries, {m: c for m, c in effect.items() if c}, flips)

    def backup(self, directory: str | None = None) -> str:
        """A consistent copy of the whole database (readable only by its owner); returns where it went."""
        directory = directory or os.path.join(os.path.dirname(os.path.abspath(self.path)), "backups")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        dest = os.path.join(directory, f"ledger-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-before-purge.db")
        copy = sqlite3.connect(dest)
        try:
            self.db.backup(copy)
        finally:
            copy.close()
        os.chmod(dest, 0o600)
        return dest

    def purge(self, chat: int, seqs: list[int]) -> Purge:
        """Permanently delete entries (and the undos tied to them). The one way past the append-only rule.

        All or nothing: the guards are lifted and put back inside one transaction, which is rolled back unless the
        ledger still sums to zero, every balance changed by exactly what the plan said, nothing dangles, and all
        four guards are back. Take a backup() first; this cannot be undone.
        """
        if not 1 <= len(seqs) <= MAX_PURGE:
            raise LedgerError(f"Purge between 1 and {MAX_PURGE} entries at a time.")
        with self._tx():
            plan = self.purge_plan(chat, seqs)
            before = self.balances(chat)
            marks = ",".join("?" * len(plan.entries))
            ids = [e.id for e in plan.entries]
            for name in TRIGGER_NAMES:
                self.db.execute(f"DROP TRIGGER {name}")
            self.db.execute(f"DELETE FROM esg_flips WHERE entry_id IN ({marks})", ids)
            self.db.execute(f"DELETE FROM entry_lines WHERE entry_id IN ({marks})", ids)
            self.db.execute(f"DELETE FROM entries WHERE id IN ({marks})", ids)
            for statement in TRIGGER_SQL:
                self.db.execute(statement)
            after = self.balances(chat)
            sound = (sum(after.values()) == 0
                     and after == {m: before[m] + plan.effect.get(m, 0) for m in before}
                     and not self.db.execute("PRAGMA foreign_key_check").fetchall()
                     and self.db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0] == len(TRIGGER_NAMES))
            if not sound:
                raise LedgerError("The ledger failed its checks after the purge, so nothing was changed.")
        return plan

    def undoable(self, chat: int, limit: int) -> list[Entry]:
        return self._entries("AND r.id IS NULL", (chat,), limit)

    def count(self, chat: int) -> int:
        return self.db.execute("SELECT COUNT(*) FROM entries WHERE chat_id = ?", (chat,)).fetchone()[0]

    def balances(self, chat: int) -> dict[int, int]:
        rows = self.db.execute("""
            SELECT m.id, COALESCE(SUM(l.cents), 0) FROM members m
            LEFT JOIN entry_lines l ON l.member_id = m.id WHERE m.chat_id = ? GROUP BY m.id""", (chat,))
        return dict(rows.fetchall())

    # --- pinned ledger message ---------------------------------------------

    def pinned_message(self, chat: int) -> int | None:
        row = self.db.execute("SELECT pinned_message_id FROM chats WHERE chat_id = ?", (chat,)).fetchone()
        return row[0] if row else None

    def set_pinned_message(self, chat: int, message_id: int) -> None:
        """Remember the ledger message; it counts as not pinned until set_pin_ok says it was."""
        self.db.execute(
            "INSERT INTO chats (chat_id, pinned_message_id) VALUES (?, ?) "
            "ON CONFLICT (chat_id) DO UPDATE SET pinned_message_id = excluded.pinned_message_id, pin_ok = NULL",
            (chat, message_id))

    def pin_ok(self, chat: int) -> bool:
        row = self.db.execute("SELECT pin_ok FROM chats WHERE chat_id = ?", (chat,)).fetchone()
        return bool(row and row[0])

    def set_pin_ok(self, chat: int, ok: bool) -> None:
        self.db.execute("UPDATE chats SET pin_ok = ? WHERE chat_id = ?", (int(ok), chat))
