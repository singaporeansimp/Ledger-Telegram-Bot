import json
from pathlib import Path

import pytest

import esg
from store import LedgerError, Store

SAMPLES = json.loads((Path(__file__).parent / "fixtures" / "esg_results.json").read_text())


@pytest.mark.parametrize("sample", SAMPLES, ids=[s["label"] for s in SAMPLES])
def test_every_real_esg_result_is_read_exactly(sample):
    result = esg.parse_result(sample["text"])
    assert result.flip_number == sample["flip_number"] and result.balanced
    assert [cents for _, cents in result.nets] == [dollars * 100 for _, dollars in sample["nets"]]
    for (shown, _), (real, _) in zip(result.nets, sample["nets"]):
        assert shown == real or (shown.endswith("…") and real.startswith(shown[:-1]))  # ESG clips long names


def test_things_that_are_not_results_are_ignored():
    for text in ["", "hello", "🏁 Flip #1 — x wins", "Flip #1 — x wins\n\nPlayers\n1. A — 1 point · +$5",
                 "🏁 Flip #1 — x\n\nPlayers\n1. A — 1 point · +$5\n2. B — 1 point · maybe",  # unreadable amount
                 "🏁 Flip #1 — x\n\nPlayers\n1. A — 1 point · +$5\n3. B — 0 points · -$5",  # seat 2 is missing
                 "🏁 Flip #1 — x\n\nPlayers\n1. A — 1 point · +$5"]:  # one player is not a flip
        assert esg.parse_result(text) is None


def test_amounts_names_and_unsaved_flips():
    text = "🏁 Flip result — Ann — the Great wins\n\nPlayers\n1. Ann — the · Great — 2 points · +$1,250\n2. Bo — 0 points · -$1,250\n\nBoards"
    result = esg.parse_result(text)
    assert result.flip_number is None  # ESG couldn't save it
    assert result.nets == (("Ann — the · Great", 125000), ("Bo", -125000))


def test_ledger_names_are_made_safe_and_short():
    assert esg.ledger_name("  Dr. @Mo = Great  ", 24) == "Dr. @Mo Great"
    assert esg.ledger_name("@bob", 24) == "bob" and esg.ledger_name("=", 24) == "Player"
    assert esg.ledger_name("x" * 40, 24) == "x" * 24


def test_an_esg_flip_can_only_be_recorded_once_even_across_restarts(tmp_path):
    path = str(tmp_path / "l.db")
    s = Store(path)
    a, b = s.add_member(1, "A").id, s.add_member(1, "B").id
    first = s.add_esg_entry(1, 42, {a: 500, b: -500}, 7, "ESG Flip Bot", "esgbot")
    assert first.note == "ESG Flip #42" and s.esg_recorded(1, 42) and not s.esg_recorded(1, 43)
    with pytest.raises(LedgerError):
        Store(path).add_esg_entry(1, 42, {a: 500, b: -500}, 7, "ESG Flip Bot", "esgbot")  # after a restart
    assert s.count(1) == 1 and s.balances(1) == {a: 500, b: -500}
    assert s.add_esg_entry(1, 43, {a: 100, b: -100}, 7, "ESG Flip Bot", "esgbot").seq == 2


def test_esg_bot_setting_is_per_chat_and_can_be_cleared(tmp_path):
    s = Store(str(tmp_path / "l.db"))
    s.set_esg_bot(1, "esgbot")
    assert s.esg_bot(1) == "esgbot" and s.esg_bot(2) is None
    s.set_esg_bot(1, None)
    assert s.esg_bot(1) is None


def test_a_player_can_be_tied_to_a_telegram_account_once(tmp_path):
    s = Store(str(tmp_path / "l.db"))
    dave, eve = s.add_member(1, "Dave").id, s.add_member(1, "Eve", username="eve").id
    s.link_member(1, dave, 9, "dave_tg")
    assert s.find(1, user_id=9).name == "Dave" and s.find(1, username="dave_tg").name == "Dave"
    s.link_member(1, dave, 10, "other")  # already tied to an account: left alone
    assert s.find(1, user_id=9).name == "Dave" and s.find(1, user_id=10) is None
    with pytest.raises(LedgerError):
        s.link_member(1, eve, 9, None)  # that account is Dave's
    s.link_member(1, eve, 11, None)  # no username given: keeps the one it had
    assert s.find(1, user_id=11).tg_username == "eve"


def _esg_db(path, group=-5):
    import sqlite3
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE flips (flip_number INTEGER PRIMARY KEY, group_id INTEGER NOT NULL);
        CREATE TABLE flip_players (flip_number INTEGER, user_id INTEGER, display_name TEXT, username TEXT, seat INTEGER,
            net_dollars INTEGER);""")
    db.execute("INSERT INTO flips VALUES (42, ?)", (group,))
    db.executemany("INSERT INTO flip_players VALUES (42, ?, ?, ?, ?, ?)",
                   [(600, "sam", "sam_two", 1, -5), (618, "sam", "sam_one", 0, 5)])  # inserted out of seat order
    db.commit()
    return db


def test_players_are_read_from_the_esg_database_by_flip_and_group_in_seat_order(tmp_path):
    path = str(tmp_path / "esg.sqlite3")
    _esg_db(path)
    assert esg.flip_players(path, -5, 42) == [esg.EsgPlayer(618, "sam_one", "sam", 500), esg.EsgPlayer(600, "sam_two", "sam", -500)]
    assert esg.flip_players(path, -6, 42) is None and esg.flip_players(path, -5, 43) is None
    with pytest.raises(esg.EsgDbError):
        esg.flip_players(str(tmp_path / "missing.sqlite3"), -5, 42)
    (tmp_path / "bad.sqlite3").write_text("not a database")
    with pytest.raises(esg.EsgDbError):
        esg.flip_players(str(tmp_path / "bad.sqlite3"), -5, 42)


def test_the_esg_database_can_be_read_while_the_esg_bot_is_writing_to_it(tmp_path):
    path = str(tmp_path / "esg.sqlite3")
    writer = _esg_db(path)
    writer.execute("PRAGMA journal_mode = WAL")  # how the real one runs
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE flips SET group_id = -5")  # a write transaction is open
    assert esg.flip_players(path, -5, 42)[0].user_id == 618
    writer.rollback()
