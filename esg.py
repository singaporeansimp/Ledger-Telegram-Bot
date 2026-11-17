"""Reads result messages posted by the ESG Flip bot (its source: esgbot/telegram/render.py).

A result looks like this (the players' nets always add up to zero):

    🏁 Flip #42 — Sam wins

    Settle up
    Dana owes Sam $50

    Players
    1. Sam — 2 points · +$50
    2. Dana — 1 point · -$50
    ...

Only the title and the Players section are read. Names are Telegram display names, which are not the ledger's
nicknames, so matching them to ledger players is the caller's job.
"""
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

_TITLE = re.compile(r"Flip #(\d+) — ")
_SEAT = re.compile(r"(\d+)\. (.+)")
_NET = re.compile(r"([+-]?)\$([\d,]+)")


@dataclass(frozen=True)
class EsgResult:
    flip_number: int | None  # None: the ESG bot could not save the flip, so it has no number
    nets: tuple[tuple[str, int], ...]  # (display name, cents) in seat order

    @property
    def balanced(self) -> bool:
        return sum(cents for _, cents in self.nets) == 0


def parse_result(text: str) -> EsgResult | None:
    """The flip number and each player's net, or None if this is not a result message."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("🏁 ") or "Players" not in lines:
        return None
    nets: list[tuple[str, int]] = []
    for line in lines[lines.index("Players") + 1:]:
        if not line.strip():
            continue  # ESG puts none between players, but a copy of its message might
        seat = _SEAT.fullmatch(line)
        if nets and not seat:
            break  # the next section (Boards) starts here
        left, _, net = seat[2].rpartition(" · ") if seat else ("", "", "")
        name, _, _points = left.rpartition(" — ")
        amount = _NET.fullmatch(net)
        if not (seat and int(seat[1]) == len(nets) + 1 and name and amount):
            return None  # not the shape we know: better to ignore it than to guess at money
        cents = int(amount[2].replace(",", "")) * 100
        nets.append((name, -cents if amount[1] == "-" else cents))
    if len(nets) < 2:
        return None
    title = _TITLE.match(lines[0][2:])
    return EsgResult(int(title[1]) if title else None, tuple(nets))


def ledger_name(esg_name: str, limit: int) -> str:
    """An ESG display name as a ledger player name: ledger names are shorter and can't contain '=' or start with '@'."""
    name = " ".join(esg_name.replace("=", " ").split()).lstrip("@")
    return name[:limit].rstrip() or "Player"


class EsgDbError(Exception):
    """The ESG bot's database can't be read (missing, or not laid out as expected)."""


@dataclass(frozen=True)
class EsgPlayer:
    user_id: int  # Telegram's id: the one thing that identifies a person (names and usernames can repeat or change)
    username: str | None
    display_name: str
    cents: int


def flip_players(db_path: str, group_id: int, flip_number: int) -> list[EsgPlayer] | None:
    """Who played `flip_number` in `group_id` and what each won or lost, in seat order; None if there is no such flip.

    Read-only: this is another program's database. The result message only carries display names, which two people
    can share, so identity comes from here instead.
    """
    try:
        with closing(sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=5)) as db:
            if not db.execute("SELECT 1 FROM flips WHERE flip_number = ? AND group_id = ?",
                              (flip_number, group_id)).fetchone():
                return None
            rows = db.execute("SELECT user_id, username, display_name, net_dollars FROM flip_players "
                              "WHERE flip_number = ? ORDER BY seat", (flip_number,)).fetchall()
    except sqlite3.Error as err:
        raise EsgDbError(f"{db_path}: {err}") from err
    return [EsgPlayer(user_id, username, name, dollars * 100) for user_id, username, name, dollars in rows]
