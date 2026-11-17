import io

from PIL import Image

import ledger_image as li

ROWS = [("Alice", 8000), ("Bobby", -3050), ("Caz", -4950)]


def size(png):
    return Image.open(io.BytesIO(png)).size


def test_renders_a_png_that_grows_with_the_number_of_players():
    png = li.render_ledger(ROWS)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    (w, h), (_, h_more) = size(png), size(li.render_ledger(ROWS + [("Dave", 0)]))
    assert w == li.W and h_more - h == li.ROW
    assert li.render_ledger(ROWS) == png  # deterministic, so an unchanged ledger gives an identical image


def test_nothing_to_draw_gives_none_so_the_bot_falls_back_to_text():
    assert li.render_ledger([]) is None


def test_a_discrepancy_adds_a_row():
    assert size(li.render_ledger(ROWS, discrepancy=50))[1] - size(li.render_ledger(ROWS))[1] == li.ROW


def test_each_player_gets_a_distinct_picture_of_their_balance():
    base = li.render_ledger([("Alice", 1000), ("Bob", -1000)])
    assert li.render_ledger([("Alice", 1000), ("Bob", -500)]) != base  # bar length follows the amount
    assert li.render_ledger([("Alice", 1000), ("Bobby", -1000)]) != base  # and the name is drawn


def test_chinese_names_are_drawn_not_replaced_by_missing_glyph_boxes():
    latin = li._chain(False, 40)[0]
    assert not li._has_glyph(latin, "王") and any(li._has_glyph(f, "王") for f in li._chain(False, 40))
    assert li.render_ledger([("王小明", 1000), ("李", -1000)]) != li.render_ledger([("?", 1000), ("?", -1000)])


def test_emoji_and_very_long_names_do_not_break_the_card():
    chain = li._chain(False, 40)
    assert li._fit("Bobby 🎲 Gambler", chain, 10_000) == "Bobby Gambler"  # unsupported characters dropped, gaps tidied
    assert li._fit("🎲", chain, 10_000) == "?"
    fitted = li._fit("x" * 200, chain, 300)
    assert li._width(fitted, chain) <= 300 and fitted.endswith("…")
    assert li.render_ledger([("🎲", 100), ("y" * 24, -100)])
