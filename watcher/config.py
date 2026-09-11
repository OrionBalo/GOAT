"""Configuration for the Reddit activity watcher.

Every setting arrives through the environment, so the same code runs unchanged
locally (from a ``.env`` file) and on GitHub Actions (from repository secrets).
Secret values are never logged verbatim -- see :meth:`Config.describe` for the
redacted summary used by ``tools/check_setup.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REQUIRED = (
    "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET",
    "REDDIT_USER_AGENT",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "WATCH_USERS",
)

_SECRETS = {
    "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
}


class ConfigError(RuntimeError):
    """Required configuration is missing or malformed."""


def load_env_file(path: str | os.PathLike[str] = ".env") -> None:
    """Populate ``os.environ`` from a ``KEY=VALUE`` file without overriding real vars.

    Deliberately tiny -- a full dotenv parser is not worth an extra dependency.
    Blank lines and ``#`` comments are skipped and surrounding quotes stripped.
    """
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_watchlist(raw: str) -> tuple[str, ...]:
    """Split a watchlist string into unique usernames, preserving first-seen order.

    Commas, spaces and newlines all separate entries, and the ``u/name`` /
    ``/u/name`` forms people paste straight out of Reddit are tolerated.
    Duplicates are dropped case-insensitively, since Reddit usernames are.
    """
    names: list[str] = []
    seen: set[str] = set()
    for chunk in raw.replace(",", " ").split():
        name = chunk.strip().strip("/")
        for prefix in ("user/", "u/"):
            if name.lower().startswith(prefix):
                name = name[len(prefix) :]
                break
        name = name.strip("/")
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        names.append(name)
    return tuple(names)


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in {"1", "true", "yes", "on"} if raw else default


@dataclass(frozen=True)
class Config:
    reddit_client_id: str
    reddit_client_secret: str
    reddit_user_agent: str
    telegram_bot_token: str
    telegram_chat_id: str
    watch_users: tuple[str, ...]
    state_path: Path
    dry_run: bool
    fetch_limit: int
    snippet_chars: int
    seen_ring_size: int
    heartbeat_days: int
    error_notice_interval: int

    def describe(self) -> str:
        """A readable summary in which every secret is reduced to a length."""
        lines = []
        for key in REQUIRED:
            value = os.environ.get(key, "")
            if key in _SECRETS:
                lines.append(f"  {key:<22} set ({len(value)} chars)")
            else:
                lines.append(f"  {key:<22} {value}")
        lines.append(f"  {'watching':<22} {len(self.watch_users)} account(s)")
        lines.append(f"  {'state file':<22} {self.state_path}")
        lines.append(f"  {'dry run':<22} {self.dry_run}")
        return "\n".join(lines)


def load(env_file: str = ".env") -> Config:
    """Build a :class:`Config` from the environment, failing loudly if incomplete."""
    load_env_file(env_file)

    missing = [key for key in REQUIRED if not os.environ.get(key, "").strip()]
    if missing:
        raise ConfigError(
            "missing required environment variables: " + ", ".join(missing)
        )

    users = parse_watchlist(os.environ["WATCH_USERS"])
    if not users:
        raise ConfigError("WATCH_USERS is set but contains no usable usernames")

    user_agent = os.environ["REDDIT_USER_AGENT"].strip()
    if "by /u/" not in user_agent:
        # Not fatal, but Reddit throttles or blocks generic User-Agents, so a
        # malformed one tends to show up later as confusing intermittent 429s.
        print(
            "WARNING: REDDIT_USER_AGENT should look like "
            "'github-actions:goat-notification:v1 (by /u/yourname)' -- "
            "Reddit throttles generic agents.",
            flush=True,
        )

    fetch_limit = _env_int("FETCH_LIMIT", 25, minimum=1)
    # The seen-ring is what stops repeats, so it must always be able to hold a
    # whole fetch window -- otherwise same-second items could slip out of it and
    # be announced twice.
    seen_ring_size = max(_env_int("SEEN_RING_SIZE", 50, minimum=1), fetch_limit * 2)

    return Config(
        reddit_client_id=os.environ["REDDIT_CLIENT_ID"].strip(),
        reddit_client_secret=os.environ["REDDIT_CLIENT_SECRET"].strip(),
        reddit_user_agent=user_agent,
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"].strip(),
        telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"].strip(),
        watch_users=users,
        state_path=Path(os.environ.get("STATE_PATH", ".state/state.json")),
        dry_run=_env_bool("DRY_RUN"),
        fetch_limit=fetch_limit,
        snippet_chars=_env_int("SNIPPET_CHARS", 300, minimum=0),
        seen_ring_size=seen_ring_size,
        heartbeat_days=_env_int("HEARTBEAT_DAYS", 7),
        error_notice_interval=_env_int("ERROR_NOTICE_INTERVAL", 6 * 3600),
    )
