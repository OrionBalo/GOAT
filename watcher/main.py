"""Entry point: poll each watched profile and push anything new to Telegram."""

from __future__ import annotations

import html
import logging
import os
import sys

from . import config
from . import state as state_mod
from .notify import Telegram, TelegramError
from .reddit import Item, RedditAuthError, RedditClient

LOG = logging.getLogger("watcher")


def _setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _announce_baseline(
    telegram: Telegram, username: str, count: int, state_was_lost: bool
) -> None:
    """Confirm a newly watched account, so silence later means 'no activity'."""
    note = ""
    if state_was_lost:
        note = (
            "\n\n<i>Previous tracking state was lost, so anything posted while it "
            "was missing will not be announced \u2014 worth a glance at the profile.</i>"
        )
    telegram.send(
        f"\U0001f440 Now watching <b>u/{html.escape(username)}</b> \u2014 baseline "
        f"set from {count} recent item(s). You will hear about anything new from "
        f"here on.{note}"
    )


def process_user(
    reddit: RedditClient,
    telegram: Telegram,
    state: state_mod.State,
    cfg: config.Config,
    username: str,
    state_was_lost: bool,
) -> int:
    """Check one profile and deliver anything new. Returns the number sent."""
    items: list[Item] = reddit.overview(username, cfg.fetch_limit)
    LOG.info("u/%s: fetched %d recent item(s)", username, len(items))

    if state.get(username) is None:
        # First sight of this account: adopt its current activity as the
        # baseline rather than firing off a backlog of old posts.
        state.baseline(
            username, [(item.fullname, item.created_utc) for item in items],
            cfg.seen_ring_size,
        )
        _announce_baseline(telegram, username, len(items), state_was_lost)
        LOG.info("u/%s: baselined, nothing announced", username)
        return 0

    new = [
        item
        for item in items
        if state.is_new(username, item.fullname, item.created_utc)
    ]
    if not new:
        LOG.info("u/%s: nothing new", username)
        return 0

    LOG.info("u/%s: %d new item(s)", username, len(new))
    sent = 0
    for item in new:  # reddit.overview() already returns oldest-first
        try:
            telegram.send_item(item, cfg.snippet_chars)
        except TelegramError:
            # Stop rather than skip. Recording this item would advance the
            # watermark past something the user was never actually told about,
            # and it would never be retried.
            LOG.error(
                "u/%s: delivery failed on %s; leaving it and everything newer unseen",
                username,
                item.fullname,
            )
            raise
        state.record(username, item.fullname, item.created_utc, cfg.seen_ring_size)
        sent += 1
    return sent


def _maybe_heartbeat(
    state: state_mod.State, cfg: config.Config, telegram: Telegram
) -> None:
    """Periodic proof of life, so a broken watcher cannot look like a quiet one."""
    if not state.should_heartbeat(cfg.heartbeat_days):
        return
    watching = ", ".join(f"u/{html.escape(name)}" for name in cfg.watch_users)
    try:
        telegram.send(
            f"\u2705 <b>Watcher alive.</b>\nWatching: {watching}\n"
            f"Alerts sent since the last check-in: {state.alerts_since_heartbeat}."
        )
    except TelegramError:
        LOG.exception("could not deliver the heartbeat")
        return
    state.mark_heartbeat()
    state.alerts_since_heartbeat = 0


def _notify_failures(
    state: state_mod.State,
    cfg: config.Config,
    telegram: Telegram,
    failures: list[tuple[str, Exception]],
) -> None:
    """Report breakage to Telegram, at most once per ERROR_NOTICE_INTERVAL."""
    if not state.should_notify_error(cfg.error_notice_interval):
        LOG.info("suppressing error notice; one was sent recently")
        return
    lines = ["\u26a0\ufe0f <b>Reddit watcher hit a problem.</b>"]
    for username, exc in failures:
        lines.append(
            f"\u2022 u/{html.escape(username)}: {html.escape(str(exc)[:200])}"
        )
    lines.append("\nChecks continue. See the GitHub Actions log for detail.")
    try:
        telegram.send("\n".join(lines))
    except TelegramError:
        LOG.exception("could not deliver the error notice")
        return
    state.mark_error_notified()


def run() -> int:
    cfg = config.load()
    state = state_mod.load(cfg.state_path)
    state_was_lost = not state.existed

    if cfg.dry_run:
        LOG.warning(
            "DRY RUN: nothing will be sent to Telegram, but state at %s is still updated",
            cfg.state_path,
        )

    reddit = RedditClient(
        cfg.reddit_client_id, cfg.reddit_client_secret, cfg.reddit_user_agent
    )
    telegram = Telegram(
        cfg.telegram_bot_token, cfg.telegram_chat_id, dry_run=cfg.dry_run
    )

    failures: list[tuple[str, Exception]] = []
    total = 0
    for username in cfg.watch_users:
        try:
            total += process_user(
                reddit, telegram, state, cfg, username, state_was_lost
            )
        except Exception as exc:  # one bad profile must not silence the others
            LOG.exception("u/%s: check failed", username)
            failures.append((username, exc))

    state.alerts_since_heartbeat += total
    if state.last_heartbeat_utc <= 0:
        # Start the clock instead of pinging on the very first run, which already
        # sends a "now watching" message per account.
        state.mark_heartbeat()
    else:
        _maybe_heartbeat(state, cfg, telegram)

    if failures:
        _notify_failures(state, cfg, telegram, failures)

    try:
        state_mod.save(cfg.state_path, state)
    except OSError:
        LOG.exception("could not persist state to %s", cfg.state_path)
        return 1

    LOG.info("done: %d alert(s) sent, %d failure(s)", total, len(failures))
    return 1 if failures else 0


def main() -> int:
    _setup_logging()
    try:
        return run()
    except config.ConfigError as exc:
        LOG.error("configuration problem: %s", exc)
        return 2
    except RedditAuthError as exc:
        LOG.error("Reddit authentication failed: %s", exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
