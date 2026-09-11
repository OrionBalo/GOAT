#!/usr/bin/env python3
"""Print the Telegram chat IDs your bot can currently see.

Run this once during setup, after sending your bot a direct message (a bot can
never start a conversation, so it has nothing to report until you write first).

    python tools/get_chat_id.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watcher import config  # noqa: E402
from watcher.notify import Telegram, TelegramError  # noqa: E402


def main() -> int:
    logging.basicConfig(level="INFO", format="%(levelname)s: %(message)s")
    config.load_env_file()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set. Put it in .env or export it first.")
        return 2

    telegram = Telegram(token, chat_id="")
    try:
        me = telegram.get_me()
    except TelegramError as exc:
        print(f"Could not reach the bot: {exc}")
        return 3
    print(f"Bot: @{me.get('username')} ({me.get('first_name')})")

    try:
        updates = telegram.get_updates()
    except TelegramError as exc:
        print(f"Could not fetch updates: {exc}")
        return 3

    chats: dict[int, dict] = {}
    for update in updates:
        message = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or {}
        )
        chat = message.get("chat") or {}
        if chat.get("id") is not None:
            chats[chat["id"]] = chat

    if not chats:
        print("\nNo chats yet. Send your bot any direct message, then re-run this.")
        return 1

    print("\nUse one of these as TELEGRAM_CHAT_ID:\n")
    for chat_id, chat in chats.items():
        label = (
            chat.get("title")
            or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            or chat.get("username")
            or "?"
        )
        print(f"  TELEGRAM_CHAT_ID={chat_id}    ({chat.get('type')}: {label})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
