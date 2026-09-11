#!/usr/bin/env python3
"""Validate every credential and watched username, then send one test message.

Run this before pushing anything, and any time alerts stop arriving:

    python tools/check_setup.py

Exits non-zero if anything is wrong. Secret values are never printed.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watcher import config  # noqa: E402
from watcher.notify import Telegram, TelegramError  # noqa: E402
from watcher.reddit import RedditClient, RedditError  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "WARNING").upper(),
        format="%(levelname)s: %(message)s",
    )

    try:
        cfg = config.load()
    except config.ConfigError as exc:
        print(f"FAIL  configuration: {exc}")
        return 2

    print("Configuration:")
    print(cfg.describe())
    print()

    problems: list[str] = []

    reddit = RedditClient(
        cfg.reddit_client_id, cfg.reddit_client_secret, cfg.reddit_user_agent
    )
    try:
        reddit.token()
        print("OK    Reddit OAuth token acquired")
    except RedditError as exc:
        print(f"FAIL  Reddit OAuth: {exc}")
        return 3

    for username in cfg.watch_users:
        try:
            data = (reddit.about(username) or {}).get("data") or {}
        except RedditError as exc:
            print(f"FAIL  u/{username}: {exc}")
            problems.append(f"u/{username}: {exc}")
            continue

        joined = datetime.fromtimestamp(
            float(data.get("created_utc") or 0), tz=timezone.utc
        ).date()
        print(
            f"OK    u/{data.get('name') or username}: exists "
            f"(karma {data.get('total_karma', '?')}, joined {joined})"
        )
        if data.get("is_suspended"):
            print(f"WARN  u/{username} is suspended -- no activity will appear")

        try:
            items = reddit.overview(username, min(5, cfg.fetch_limit))
            newest = (
                datetime.fromtimestamp(items[-1].created_utc, tz=timezone.utc)
                .strftime("%Y-%m-%d %H:%M UTC")
                if items
                else "none visible"
            )
            print(f"OK    u/{username}: overview readable, latest activity {newest}")
        except RedditError as exc:
            print(f"FAIL  u/{username}: overview unreadable: {exc}")
            problems.append(f"u/{username} overview: {exc}")

    telegram = Telegram(cfg.telegram_bot_token, cfg.telegram_chat_id)
    try:
        me = telegram.get_me()
        print(f"OK    Telegram bot @{me.get('username')}")
    except TelegramError as exc:
        print(f"FAIL  Telegram token: {exc}")
        return 4

    try:
        telegram.send(
            "\U0001f9ea <b>Reddit watcher setup check</b>\n"
            "If you can read this, delivery works."
        )
        print("OK    test message delivered")
    except TelegramError as exc:
        print(f"FAIL  Telegram delivery: {exc}")
        problems.append(f"telegram: {exc}")

    print()
    if problems:
        print(f"{len(problems)} problem(s) found.")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
