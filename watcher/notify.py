"""Telegram delivery for watcher alerts."""

from __future__ import annotations

import html
import logging
import re
import time
from typing import Any

import requests

from .reddit import COMMENT, Item

LOG = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
MAX_ATTEMPTS = 4
BACKOFF_BASE = 2.0
MAX_BACKOFF = 60.0
TIMEOUT = 20

# Telegram's hard cap is 4096 characters; leave headroom for the closing ellipsis.
SAFE_LIMIT = 3900
TITLE_CHARS = 160

_WHITESPACE = re.compile(r"\s+")


class TelegramError(RuntimeError):
    """Telegram refused or failed to deliver a message."""


def snippet(text: str, limit: int) -> str:
    """Collapse runs of whitespace and trim to limit characters."""
    clean = _WHITESPACE.sub(" ", (text or "").strip())
    if limit <= 0 or len(clean) <= limit:
        return clean if limit > 0 else ""
    return clean[: limit - 1].rstrip() + "\u2026"


def format_item(item: Item, snippet_chars: int = 300) -> str:
    """Build the HTML message body for one post or comment.

    Every piece of Reddit-sourced text is HTML-escaped: comment bodies routinely
    contain < and &, which Telegram would otherwise reject with a 400.
    """
    who = html.escape(item.author, quote=False)
    sub = html.escape(item.subreddit, quote=False)
    title = html.escape(snippet(item.title, TITLE_CHARS), quote=False)
    body = html.escape(snippet(item.body, snippet_chars), quote=False)

    if item.kind == COMMENT:
        lines = [f"\U0001f4ac <b>u/{who}</b> commented in r/{sub}"]
        if title:
            lines.append(f"on \u201c{title}\u201d")
    else:
        lines = [f"\U0001f195 <b>u/{who}</b> posted in r/{sub}"]
        if title:
            lines.append(f"<b>{title}</b>")

    if body:
        lines.extend(("", body))

    text = "\n".join(lines)
    if len(text) <= SAFE_LIMIT:
        return text
    return text[:SAFE_LIMIT].rstrip() + "\u2026"


class Telegram:
    """Thin sendMessage wrapper with retries and a dry-run mode."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        session: requests.Session | None = None,
        dry_run: bool = False,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._session = session or requests.Session()
        self.dry_run = dry_run

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST one Bot API method. Errors never include the URL, which holds the token."""
        url = f"{API_ROOT}/bot{self._token}/{method}"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._session.post(url, json=payload, timeout=TIMEOUT)
            except requests.RequestException as exc:
                if attempt == MAX_ATTEMPTS:
                    raise TelegramError(f"{method} failed: {exc}") from exc
                time.sleep(min(BACKOFF_BASE**attempt, MAX_BACKOFF))
                continue

            try:
                data = response.json()
            except ValueError:
                data = {}

            if response.ok and data.get("ok"):
                return data

            if response.status_code == 429:
                if attempt == MAX_ATTEMPTS:
                    raise TelegramError(f"{method} rate-limited after {attempt} attempts")
                wait = (data.get("parameters") or {}).get("retry_after")
                delay = min(float(wait or BACKOFF_BASE**attempt), MAX_BACKOFF)
                LOG.warning("Telegram rate-limited; waiting %.0fs", delay)
                time.sleep(delay)
                continue

            if response.status_code >= 500 and attempt < MAX_ATTEMPTS:
                time.sleep(min(BACKOFF_BASE**attempt, MAX_BACKOFF))
                continue

            detail = data.get("description") or response.text[:200]
            raise TelegramError(f"{method} -> {response.status_code} {detail}")

        raise TelegramError(f"{method} exhausted retries")

    def get_me(self) -> dict[str, Any]:
        """Identify the bot; the cheapest way to prove a token is valid."""
        return self._call("getMe", {}).get("result") or {}

    def get_updates(self, limit: int = 100) -> list[dict[str, Any]]:
        """Recent updates, used once to discover the chat ID."""
        return self._call("getUpdates", {"limit": limit}).get("result") or []

    def send(
        self,
        text: str,
        button_url: str | None = None,
        button_label: str = "Open on Reddit",
    ) -> None:
        if self.dry_run:
            LOG.info("[dry-run] would send:\n%s\n[dry-run] link: %s", text, button_url or "-")
            return

        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            # The snippet already carries the content, so a link card would just
            # repeat it and make the chat harder to scan.
            "link_preview_options": {"is_disabled": True},
        }
        if button_url:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": button_label, "url": button_url}]]
            }
        self._call("sendMessage", payload)

    def send_item(self, item: Item, snippet_chars: int = 300) -> None:
        self.send(format_item(item, snippet_chars), button_url=item.url)
