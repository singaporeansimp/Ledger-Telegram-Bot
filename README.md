# Group ledger bot

[![tests](https://github.com/singaporeansimp/Ledger-Telegram-Bot/actions/workflows/tests.yml/badge.svg)](https://github.com/singaporeansimp/Ledger-Telegram-Bot/actions/workflows/tests.yml)

Zero-sum SGD ledger for a Telegram group. Every entry sums to zero, entries are append-only
(undo adds a reversing entry), and the ledger is pinned as a picture (a card with a bar per player) that is
replaced in place after every change. If a picture can't be drawn (no fonts), it falls back to a text table.

## Set up
1. In Telegram, message @BotFather: `/newbot`, copy the token.
2. Add the bot to your group and make it an admin with **Pin messages** (it needs nothing else).
   Leave BotFather's privacy mode on: commands and replies to the bot are all it needs.
3. Put the token in `.env` and start the service (already installed in systemd, just not started):
   ```
   echo 'BOT_TOKEN=123456:ABC...' > /root/TelegramBotLedger/.env && chmod 600 /root/TelegramBotLedger/.env
   systemctl enable --now ledger-bot
   journalctl -u ledger-bot -f      # watch the log
   ```
   To run it by hand instead: `set -a; . ./.env; set +a; .venv/bin/python bot.py`.
4. In the group, everyone sends `/join` (or `/join My Nickname`), then `/ledger`.
5. `/edit`, `/pay` and `/undo` run as a conversation in each person's **private chat** with the bot, so the group
   isn't spammed with buttons and prompts. The first time, Telegram won't let the bot message someone who hasn't
   started it, so the bot replies in the group with an "Open private chat" button. They press it and Start, and
   their edit begins there. The group only sees the result: one line saying what was recorded and by whom, plus the
   pinned ledger updating.

All data is the single file `ledger.db` (override with `LEDGER_DB`). `/export` sends the full history as a CSV
(one row per player per entry, with editor, @username, note and undo links); copy `ledger.db` too if you want a
full backup.

## Use
`/edit` record a session (optional note on the review screen) · `/pay` record a payout · `/undo` · `/history` · `/export` · `/settle` · `/ledger`
`/add @friend [name]` · `/rename @friend = New name` · `/join [name]`

`/graph` draws profit as a picture: one stepped line per player, or one player (shaded green while ahead, red while
behind). Options go in any order: a player (`Jared`, `me`, `@friend`), how far back (`24h`, `7d`, `2w`, `1m`, or `all`),
and the horizontal axis: time (default) or `entries`, which spaces the sessions evenly and labels them with their entry
numbers (`#81`), so it lines up with `/history`. For example `/graph me 7d entries`. Buttons on the picture change the
player, window and axis in place, keeping the other choices. A window shows the profit made *within* it, starting from
zero. Profit counts sessions only: payouts settle a balance without being winnings, and a session that was undone
disappears from the history as if it never happened.

Drafts (an edit in progress) live in memory only: a restart discards them, and nothing is
ever recorded until someone taps Confirm. Each person has one edit open at a time; starting another replaces it.

## Owner-only: /purge
`/purge 81 82` permanently deletes entries from the ledger, for cleaning up test or joke entries. It is the one
way past the append-only rule, so it is locked down: only the Telegram **user id** in `OWNER_USER_ID` (in `.env`; a
number, not a username, since usernames can be changed or reclaimed) can use it, and everyone else is ignored without
a reply. It is not in `/help` or the command menu. The bot shows exactly what would go and how balances would change,
and nothing is deleted until the owner taps Delete. An entry and the undo of it are always deleted together. A copy of
the database is saved to `backups/` first, and the deletion is a single all-or-nothing step that is rolled back unless
the ledger still sums to zero and the append-only guards are back. Without `OWNER_USER_ID`, `/purge` is off.

## ESG Flip bot results
The ledger can add the ESG Flip bot's flip results by itself. In the group, a group
admin sends `/esg @the_esg_bot_username` (`/esg off` stops it; `/esg` shows the state). Then, whenever that bot posts
a result, the ledger records it as a session: each player's net, noted "ESG Flip #42" and attributed to the ESG bot.
The group gets the usual one-line announcement and the pinned ledger updates.

**Telegram only delivers one bot's messages to another if you allow it.** For the ledger bot, in @BotFather: turn on
*Bot-to-Bot Communication Mode*, turn *Group Privacy* off, and keep it a group admin
([Telegram's docs](https://core.telegram.org/api/bots/bot-to-bot)). `/esg` warns while Group Privacy is still on.

The result message only carries display names, and two people can share one. So the
message is just the trigger: the bot looks the flip up by number in the ESG bot's own SQLite database
(`ESG_DB_PATH` in `.env`, read-only, the file the running ESG bot uses) to get each player's Telegram id and @username
and the exact amounts, and refuses to record if the two disagree. A player is matched by Telegram id, or by the
@username they were `/add`ed with. The first time an account is unknown the bot asks once, with buttons (a ledger player
who has no account yet, or "New player"), and ties the account to that player for good. Each ESG flip number is added
once, ever. Ties and flips ESG couldn't save aren't recorded (the latter is reported); ESG's `$` amounts are taken as SGD.
If ESG's database can't be read, nothing is recorded by guessing from names.

## Fonts
The ledger picture is drawn with Pillow using DejaVu Sans (Latin) and Droid Sans Fallback (Chinese, Japanese,
Korean) from `/usr/share/fonts`; see the paths at the top of `ledger_image.py`. Emoji in names are left out of the
picture (no emoji font), but they still show in the text receipts.

## Running the tests
The tests need no bot token and no network: the bot tests run the real handlers against a fake Telegram API.
Developed and tested on Python 3.14.

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest
```

The ledger picture tests draw real images, so they need the fonts the picture uses (see "Fonts"). On Debian or
Ubuntu: `sudo apt install fonts-dejavu-core fonts-droid-fallback`.

## License
[MIT](LICENSE).
