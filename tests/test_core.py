import sqlite3

import pytest

import csv
import io
import re

from money import format_cents, parse_cents, settle
import render
from store import LedgerError, ProfitEvent, Store

CHAT = -100


@pytest.fixture
def store():
    s = Store(":memory:")
    s.ids = {n: s.add_member(CHAT, n).id for n in ("Alice", "Bob", "Carol")}
    return s


def test_parse_cents():
    assert parse_cents("50") == 5000
    assert parse_cents("-30.5") == -3050
    assert parse_cents("+S$1,200.00") == 120000
    assert parse_cents("−$.75") == -75
    for bad in ("", "abc", "1.234", "1e3", "--5", "99999999"):
        with pytest.raises(ValueError):
            parse_cents(bad)


def test_format_cents():
    assert format_cents(123456, signed=True) == "+1,234.56"
    assert format_cents(-5) == "-0.05"
    assert format_cents(0, signed=True) == "0.00"


def test_settle_zeroes_everyone():
    balances = {1: 8000, 2: -3000, 3: -5000, 4: 0}
    after = dict(balances)
    for debtor, creditor, cents in settle(balances):
        after[debtor] += cents
        after[creditor] -= cents
    assert all(v == 0 for v in after.values())


def test_entry_must_sum_to_zero(store):
    a, b, c = store.ids["Alice"], store.ids["Bob"], store.ids["Carol"]
    with pytest.raises(LedgerError):
        store.add_entry(CHAT, "session", {a: 5000, b: -3000}, 1, "Alice")
    with pytest.raises(LedgerError):
        store.add_entry(CHAT, "session", {a: 0, b: 0}, 1, "Alice")
    assert store.count(CHAT) == 0
    store.add_entry(CHAT, "session", {a: 8000, b: -3000, c: -5000}, 1, "Alice")
    assert store.balances(CHAT) == {a: 8000, b: -3000, c: -5000}
    assert sum(store.balances(CHAT).values()) == 0


def test_undo_appends_reversal_and_is_once_only(store):
    a, b = store.ids["Alice"], store.ids["Bob"]
    first = store.add_entry(CHAT, "session", {a: 1000, b: -1000}, 1, "Alice")
    undo = store.undo(CHAT, first.seq, 2, "Bob")
    assert (undo.kind, undo.reverses, undo.editor_name) == ("undo", first.seq, "Bob")
    assert store.balances(CHAT)[a] == 0
    assert store.entry(CHAT, first.seq).reversed_by == undo.seq
    assert store.count(CHAT) == 2  # history kept, nothing overwritten
    with pytest.raises(LedgerError):
        store.undo(CHAT, first.seq, 2, "Bob")
    assert [e.seq for e in store.undoable(CHAT, 5)] == [undo.seq]
    store.undo(CHAT, undo.seq, 1, "Alice")  # redo
    assert store.balances(CHAT)[a] == 1000


def test_ledger_rows_cannot_be_edited_or_deleted(store):
    a, b = store.ids["Alice"], store.ids["Bob"]
    store.add_entry(CHAT, "session", {a: 1000, b: -1000}, 1, "Alice")
    for sql in ("UPDATE entry_lines SET cents = 1", "DELETE FROM entry_lines",
                "UPDATE entries SET editor_name = 'x'", "DELETE FROM entries"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            store.db.execute(sql)


def test_players_must_belong_to_the_chat(store):
    other = store.add_member(-200, "Dave").id
    with pytest.raises(LedgerError):
        store.add_entry(CHAT, "session", {store.ids["Alice"]: 100, other: -100}, 1, "Alice")


def test_names_unique_case_insensitive_and_cleaned(store):
    with pytest.raises(LedgerError):
        store.add_member(CHAT, "alice")
    for bad in ("", "  ", "a=b", "@bob", "x" * 25):
        with pytest.raises(LedgerError):
            store.add_member(CHAT, bad)
    store.rename(CHAT, store.ids["Alice"], "ALICE")  # case change of own name is fine
    with pytest.raises(LedgerError):
        store.rename(CHAT, store.ids["Alice"], "bob")


def test_identify_links_username_placeholder(store):
    ghost = store.add_member(CHAT, "Gizmo", username="gizmo99")
    assert store.identify(CHAT, 777, "Gizmo99").id == ghost.id
    assert store.find(CHAT, user_id=777).name == "Gizmo"
    assert store.identify(CHAT, 888, "nobody") is None


def test_note_is_stored_cleaned_and_limited(store):
    a, b = store.ids["Alice"], store.ids["Bob"]
    plain = store.add_entry(CHAT, "session", {a: 100, b: -100}, 1, "Alice")
    noted = store.add_entry(CHAT, "session", {a: 100, b: -100}, 1, "Alice", note="  Poker \n Friday ")
    assert (plain.note, noted.note) == (None, "Poker Friday")
    assert store.entry(CHAT, noted.seq).note == "Poker Friday"
    assert store.undo(CHAT, noted.seq, 1, "Alice").note is None
    with pytest.raises(LedgerError):
        store.add_entry(CHAT, "session", {a: 100, b: -100}, 1, "Alice", note="x" * 61)
    assert store.count(CHAT) == 3


def test_editor_username_is_recorded_for_edits_and_undos(store):
    a, b = store.ids["Alice"], store.ids["Bob"]
    e = store.add_entry(CHAT, "session", {a: 100, b: -100}, 1, "Alice", editor_username="alice")
    u = store.undo(CHAT, e.seq, 2, "Bob")  # Bob has no @username
    assert (e.editor_username, u.editor_username) == ("alice", None)
    names = {a: "Alice", b: "Bob"}
    assert "by Alice (@alice)" in render.entry_text(store.entry(CHAT, e.seq), names)
    assert "by Bob\n" in render.entry_text(u, names)


def test_history_csv_has_one_row_per_player_oldest_first_and_is_formula_safe(store):
    a, b, c = store.ids["Alice"], store.ids["Bob"], store.ids["Carol"]
    store.rename(CHAT, c, "+cmd")
    first = store.add_entry(CHAT, "session", {a: 123456, b: -3050, c: -120406}, 1, "Alice",
                            editor_username="alice", note="+Poker, \"Friday\"")
    store.undo(CHAT, first.seq, 2, "Bob")
    names = {m.id: m.name for m in store.members(CHAT)}
    rows = list(csv.DictReader(io.StringIO(render.history_csv(store.entries(CHAT, 10)[::-1], names))))
    assert [r["entry"] for r in rows] == ["1"] * 3 + ["2"] * 3
    assert rows[0]["player"] == "Alice" and rows[0]["amount_sgd"] == "1234.56"  # plain number, no commas
    assert rows[0]["editor_username"] == "alice" and rows[0]["undone_by_entry"] == "2"
    assert rows[0]["note"] == "'+Poker, \"Friday\"" and rows[2]["player"] == "'+cmd"
    assert rows[5]["undoes_entry"] == "1" and rows[5]["amount_sgd"] == "-1234.56"
    assert rows[0]["time"].endswith("+08:00")
    assert sum(int(r["amount_sgd"].replace(".", "")) for r in rows) == 0


def test_ledger_table_has_no_total_row_unless_balances_are_off(store):
    a, b, c = store.ids["Alice"], store.ids["Bob"], store.ids["Carol"]
    e = store.add_entry(CHAT, "session", {a: 8000, b: -3050, c: -4950}, 1, "Alice")
    members = store.members(CHAT)
    text = render.ledger_text(members, store.balances(CHAT), e)
    assert "Total" not in text and "Discrepancy" not in text and "----" not in text
    assert "Alice  +80.00" in text and "Carol  -49.50" in text
    # not reachable through the store, which refuses unbalanced entries; shown only as a safety net
    broken = {**store.balances(CHAT), b: -3000}
    text = render.ledger_text(members, broken, e)
    assert re.search(r"Discrepancy\s+\+0\.50", text) and "-----" in text
    assert "Total" not in text


def test_a_database_from_before_pin_tracking_is_migrated_in_place(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE chats (chat_id INTEGER PRIMARY KEY, pinned_message_id INTEGER);
        INSERT INTO chats VALUES (-5, 123);""")
    old.commit()
    old.close()
    s = Store(path)  # adds the column, keeps the row
    assert s.pinned_message(-5) == 123 and s.pin_ok(-5) is False
    s.set_pin_ok(-5, True)
    assert s.pin_ok(-5) is True
    s.set_pinned_message(-5, 456)  # a new ledger message isn't pinned until its pin succeeds
    assert s.pinned_message(-5) == 456 and s.pin_ok(-5) is False
    Store(path)  # opening again is a no-op, not an error


def test_profit_events_are_sessions_only_so_payouts_dont_erase_winnings(store):
    a, b = store.ids["Alice"], store.ids["Bob"]
    store.add_entry(CHAT, "session", {a: 5000, b: -5000}, 1, "Alice")
    store.add_entry(CHAT, "payment", {b: 5000, a: -5000}, 1, "Alice")  # Bob pays Alice back: balances reset, profit doesn't
    store.add_entry(CHAT, "session", {a: -1000, b: 1000}, 1, "Alice")
    events = store.profit_events(CHAT)
    assert [e.deltas for e in events] == [{a: 5000, b: -5000}, {a: -1000, b: 1000}]
    assert [e.seq for e in events] == [1, 3]  # entry numbers, so the payout (#2) leaves a gap
    assert all(isinstance(e, ProfitEvent) and e.at.tzinfo for e in events)
    assert events[0].at <= events[1].at
    assert store.profit_events(-999) == []  # another chat's results are not ours


def test_an_undone_session_vanishes_from_profit_and_undoing_the_undo_restores_it(store):
    a, b = store.ids["Alice"], store.ids["Bob"]
    keep = store.add_entry(CHAT, "session", {a: 300, b: -300}, 1, "Alice")
    session = store.add_entry(CHAT, "session", {a: 700, b: -700}, 1, "Alice")
    payout = store.add_entry(CHAT, "payment", {b: 300, a: -300}, 1, "Alice")
    store.undo(CHAT, payout.seq, 1, "Alice")  # a payout reversed: not profit either way
    assert [e.deltas for e in store.profit_events(CHAT)] == [{a: 300, b: -300}, {a: 700, b: -700}]
    undo = store.undo(CHAT, session.seq, 1, "Alice")
    assert [e.deltas for e in store.profit_events(CHAT)] == [{a: 300, b: -300}]  # as if it never happened
    redo = store.undo(CHAT, undo.seq, 1, "Alice")  # undoing the undo: the session stands again, at its own time
    events = store.profit_events(CHAT)
    assert [e.deltas for e in events] == [{a: 300, b: -300}, {a: 700, b: -700}] and events[0].at <= events[1].at
    store.undo(CHAT, redo.seq, 1, "Alice")  # and undone once more
    assert [e.deltas for e in store.profit_events(CHAT)] == [{a: 300, b: -300}]
    assert keep.seq == 1
