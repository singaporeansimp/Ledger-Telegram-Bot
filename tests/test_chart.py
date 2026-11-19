import io
from datetime import datetime, timedelta, timezone

from PIL import Image

import profit_chart as pc
from store import ProfitEvent

T0 = datetime(2026, 11, 2, 13, 0, tzinfo=timezone.utc)
NAMES = {1: "Alice", 2: "Bob", 3: "Caz"}


def event(seq, day, deltas, minutes=0):
    return ProfitEvent(seq, T0 + timedelta(days=day, minutes=minutes), deltas)


EVENTS = [event(1, 0, {1: 4000, 2: -1500, 3: -2500}), event(3, 2, {1: -2000, 3: 2000}), event(7, 5, {2: 3500, 1: -1500, 3: -2000})]
NOW = T0 + timedelta(days=6)


def png_size(png):
    return Image.open(io.BytesIO(png)).size


def points(events):
    return [(float(i), e.deltas) for i, e in enumerate(events, 1)]


def test_steps_are_a_running_total_over_only_the_sessions_a_player_was_in():
    assert pc.steps(points(EVENTS), 1) == [(1.0, 4000), (2.0, 2000), (3.0, 500)]
    assert [v for _, v in pc.steps(points(EVENTS), 2)] == [-1500, 2000]  # Bob skipped the second session
    assert pc.steps(points(EVENTS), 99) == []
    assert pc.steps(points([event(1, 0, {1: 0, 2: 0})]), 1) == []  # breaking even moves nothing


def test_axis_ticks_are_round_numbers_that_cover_the_data():
    for lo, hi in [(-4950, 8000), (0, 100), (-30, 20), (-1_200_000, 3_400_000), (0, 5)]:
        ticks = pc.nice_ticks(lo, hi)
        assert ticks and ticks[0] >= lo and ticks[-1] <= hi + 1
        assert len({b - a for a, b in zip(ticks, ticks[1:])}) <= 1  # evenly spaced
    assert pc.nice_ticks(-4950, 8000) == [-4000, -2000, 0, 2000, 4000, 6000, 8000]
    assert 0 in pc.nice_ticks(-4950, 8000)


def test_axis_labels_use_whole_dollars_unless_the_step_is_smaller():
    assert pc.axis_label(0, 500) == "0" and pc.axis_label(-2000, 1000) == "-20" and pc.axis_label(123_400_000, 100) == "1,234,000"
    assert pc.axis_label(250, 50) == "2.50"


def test_time_labels_show_the_hour_only_for_short_spans():
    assert pc.time_label(T0, timedelta(days=9)) == "2 Nov"
    assert pc.time_label(T0, timedelta(hours=1)) == "2 Nov 21:00"  # Singapore time


def test_windows_parse_hours_days_weeks_and_months_and_reject_the_rest():
    assert pc.Window.parse("24h") == pc.Window("24h", timedelta(hours=24), "last 24 hours")
    assert pc.Window.parse(" 7D ").token == "7d" and pc.Window.parse("7d").delta == timedelta(days=7)
    assert pc.Window.parse("1d").label == "last day" and pc.Window.parse("2w").label == "last 2 weeks"
    assert pc.Window.parse("1m").delta == timedelta(days=30) and pc.Window.parse("3m").label == "last 3 months"
    for bad in ["", "0d", "d", "7", "7x", "-7d", "1.5d", "7 d", "1000d", "all", "week"]:
        assert pc.Window.parse(bad) is None


def test_a_chart_is_a_png_and_the_legend_gains_a_row_for_every_two_more_players():
    group = pc.render_profit(EVENTS, NAMES, None, NOW)
    assert group[:8] == b"\x89PNG\r\n\x1a\n"
    assert pc.render_profit(EVENTS, NAMES, None, NOW) == group  # deterministic for a given moment
    four = pc.render_profit([*EVENTS, event(8, 6, {4: 100, 1: -100})], {**NAMES, 4: "Dee"}, None, NOW)
    (w, h), (_, h4) = png_size(group), png_size(four)
    assert w == pc.W and h4 == h  # the legend has two columns: 3 and 4 players both need two rows
    five = pc.render_profit([*EVENTS, event(8, 6, {4: 100, 5: 200, 1: -300})], {**NAMES, 4: "Dee", 5: "Eve"}, None, NOW)
    assert png_size(five)[1] - h == 58


def test_one_player_gets_their_own_chart():
    alice = pc.render_profit(EVENTS, NAMES, 1, NOW)
    assert alice != pc.render_profit(EVENTS, NAMES, None, NOW) and alice != pc.render_profit(EVENTS, NAMES, 2, NOW)
    elsewhere = EVENTS + [event(8, 5, {2: 100, 3: -100}, minutes=5)]  # a later session Alice wasn't in
    assert pc.render_profit(elsewhere, NAMES, 1, NOW) != alice  # it shows in her "played 3 of 4"


def test_nothing_to_draw_gives_none():
    assert pc.render_profit([], NAMES, None, NOW) is None
    assert pc.render_profit(EVENTS, NAMES, 99, NOW) is None  # not a player
    assert pc.render_profit([event(1, 0, {1: 100, 2: -100})], NAMES, 3, NOW) is None  # Caz never played


def test_a_window_keeps_only_recent_sessions_and_starts_profit_from_zero():
    two_days = pc.Window.parse("2d")  # NOW is day 6: only the day-5 session is inside it
    assert pc.render_profit(EVENTS, NAMES, None, NOW, two_days) == pc.render_profit(EVENTS[2:], NAMES, None, NOW, two_days)
    assert pc.render_profit(EVENTS, NAMES, None, NOW, two_days) != pc.render_profit(EVENTS, NAMES, None, NOW)
    assert pc.render_profit(EVENTS, NAMES, None, NOW, pc.Window.parse("30d")) is not None  # covers everything
    assert pc.render_profit(EVENTS, NAMES, None, NOW + timedelta(days=30), pc.Window.parse("7d")) is None  # a quiet month


def test_a_player_with_no_sessions_inside_the_window_has_nothing_to_draw():
    early_caz = [event(1, 0, {3: 100, 1: -100}), event(2, 5, {1: 50, 2: -50})]  # Caz only played on day 0
    two_days = pc.Window.parse("2d")
    assert pc.render_profit(early_caz, NAMES, 3, NOW, two_days) is None
    assert pc.render_profit(early_caz, NAMES, 1, NOW, two_days) is not None  # Alice was in the recent one
    assert pc.render_profit(early_caz, NAMES, 3, NOW) is not None  # without the window Caz has a line


def test_the_window_axis_covers_the_whole_window_even_if_sessions_started_later():
    short = [event(5, 5, {1: 100, 2: -100})]  # one session, a day before NOW
    assert pc.render_profit(short, NAMES, None, NOW, pc.Window.parse("30d")) != pc.render_profit(short, NAMES, None, NOW, pc.Window.parse("7d"))


def test_by_entries_spaces_sessions_evenly_whenever_they_happened():
    close = [event(10, 0, {1: 500, 2: -500}), event(11, 0, {1: -200, 2: 200}, minutes=1), event(12, 0, {1: 300, 2: -300}, minutes=2)]
    far = [event(10, 0, {1: 500, 2: -500}), event(11, 40, {1: -200, 2: 200}), event(12, 400, {1: 300, 2: -300})]
    now = T0 + timedelta(days=500)
    by_time = pc.render_profit(far, NAMES, None, now)
    assert pc.render_profit(close, NAMES, None, now, by_entries=True) == pc.render_profit(far, NAMES, None, now, by_entries=True)
    assert pc.render_profit(far, NAMES, None, now, by_entries=True) != by_time  # but by time they differ
    assert pc.render_profit(close, NAMES, None, now, by_entries=True) != pc.render_profit(close, NAMES, None, now)
    shifted = [event(20, 0, {1: 500, 2: -500}), event(21, 0, {1: -200, 2: 200}, minutes=1), event(22, 0, {1: 300, 2: -300}, minutes=2)]
    assert pc.render_profit(shifted, NAMES, None, now, by_entries=True) != pc.render_profit(close, NAMES, None, now, by_entries=True)  # entry numbers are on the axis


def test_by_entries_combines_with_a_window_and_with_one_player():
    for only in (None, 1):
        assert pc.render_profit(EVENTS, NAMES, only, NOW, pc.Window.parse("3d"), by_entries=True)
    assert pc.render_profit(EVENTS, NAMES, 1, NOW, by_entries=True) != pc.render_profit(EVENTS, NAMES, 1, NOW)
    assert pc.render_profit(EVENTS, NAMES, 3, NOW + timedelta(days=60), pc.Window.parse("7d"), by_entries=True) is None


def test_awkward_data_still_draws():
    now = T0 + timedelta(minutes=5)
    assert pc.render_profit([event(1, 0, {1: 2500, 2: -2500})], NAMES, None, now)  # one session, moments ago
    assert pc.render_profit([event(1, 0, {1: 2500, 2: -2500})], NAMES, None, now, by_entries=True)  # one entry: one tick
    assert pc.render_profit([event(1, 0, {1: 2500, 2: -2500}), event(2, 0, {1: 1, 2: -1})], NAMES, 1, now, by_entries=True)  # two entries
    assert pc.render_profit([event(1, 0, {1: 2500, 2: -2500}), event(2, 0, {1: 1, 2: -1}, minutes=0)], NAMES, 1, now)  # same instant
    assert pc.render_profit([event(1, 0, {1: 123_456_789, 2: -123_456_789})], NAMES, None, now)  # a million-dollar night
    assert pc.render_profit([event(1, 0, {1: 5, 2: -5})], NAMES, None, now)  # a few cents: sub-dollar axis
    assert pc.render_profit([event(1, 0, {1: -700, 2: 700})], NAMES, 1, now)  # a player who only ever lost
    assert pc.render_profit([event(1, 0, {1: -123_456_789, 2: 123_456_789}), event(2, 1, {1: 500, 2: -500})], NAMES, 1, now)  # wide stats
    assert pc.render_profit(EVENTS, {1: "王小明", 2: "x" * 40, 3: "🎲"}, None, T0 + timedelta(days=9))  # CJK, long, emoji
    many = [event(i, 0, {1: 100 * (i % 7 - 3), 2: -100 * (i % 7 - 3)}, minutes=i) for i in range(1, 300)]
    assert pc.render_profit(many, NAMES, None, T0 + timedelta(days=1), by_entries=True)  # hundreds of sessions
