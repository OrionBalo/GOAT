"""Opaque persistence of what the user has already been told about.

The watcher only ever needs *equality* checks against past activity, never the
original values, so nothing is stored in the clear: usernames and Reddit
fullnames are reduced to truncated SHA-256 digests. A leaked state blob
therefore reveals neither which accounts are watched nor which items were seen.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

LOG = logging.getLogger(__name__)

STATE_VERSION = 1
_DIGEST_CHARS = 16


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


def user_key(username: str) -> str:
    """Stable, non-reversible key for a username (case-insensitive)."""
    return _digest(username.strip().lower())


def item_key(fullname: str) -> str:
    """Stable, non-reversible key for a Reddit fullname such as ``t3_1abc234``."""
    return _digest(fullname)


@dataclass
class UserState:
    """High-water mark plus a short ring of recently-seen item digests."""

    last_seen_utc: float = 0.0
    seen: list[str] = field(default_factory=list)


@dataclass
class State:
    users: dict[str, UserState] = field(default_factory=dict)
    bootstrapped_at: float = 0.0
    last_heartbeat_utc: float = 0.0
    last_error_notice_utc: float = 0.0
    alerts_since_heartbeat: int = 0
    existed: bool = False
    """False when no usable state file was found -- a first run, or a lost cache."""

    # ------------------------------------------------------------------ reads

    def get(self, username: str) -> UserState | None:
        return self.users.get(user_key(username))

    def is_new(self, username: str, fullname: str, created_utc: float) -> bool:
        """True when this item is at/past the watermark and not in the seen ring.

        The comparison is deliberately ``>=`` rather than ``>``. Reddit timestamps
        have one-second granularity, so two items can share a ``created_utc``; a
        strict ``>`` would silently drop the second one once the first advanced
        the watermark. The ring is what actually prevents repeats, and the config
        keeps it larger than the fetch window so it always covers everything a
        single poll can return.
        """
        user = self.users.get(user_key(username))
        if user is None:
            # Un-baselined users are handled by baseline(), never by is_new().
            return False
        if item_key(fullname) in user.seen:
            return False
        return created_utc >= user.last_seen_utc

    # ----------------------------------------------------------------- writes

    def baseline(
        self, username: str, items: Sequence[tuple[str, float]], ring_size: int
    ) -> None:
        """Adopt the current activity as already-seen, announcing none of it.

        ``items`` is a sequence of ``(fullname, created_utc)``. The watermark is
        set from the newest item rather than from the clock: an item posted
        between this fetch and the next run would otherwise be skipped.
        """
        user = UserState()
        if items:
            user.last_seen_utc = max(created for _, created in items)
            user.seen = [item_key(fullname) for fullname, _ in items][-ring_size:]
        else:
            user.last_seen_utc = time.time()
        self.users[user_key(username)] = user

    def record(
        self, username: str, fullname: str, created_utc: float, ring_size: int
    ) -> None:
        """Mark one item as delivered. Only ever called after a successful send."""
        user = self.users.setdefault(user_key(username), UserState())
        key = item_key(fullname)
        if key not in user.seen:
            user.seen.append(key)
        if ring_size > 0:
            del user.seen[:-ring_size]
        user.last_seen_utc = max(user.last_seen_utc, created_utc)

    # -------------------------------------------------------------- throttles

    def should_notify_error(self, interval: float, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return (now - self.last_error_notice_utc) >= interval

    def mark_error_notified(self, now: float | None = None) -> None:
        self.last_error_notice_utc = time.time() if now is None else now

    def should_heartbeat(self, every_days: float, now: float | None = None) -> bool:
        if every_days <= 0:
            return False
        now = time.time() if now is None else now
        return (now - self.last_heartbeat_utc) >= every_days * 86400

    def mark_heartbeat(self, now: float | None = None) -> None:
        self.last_heartbeat_utc = time.time() if now is None else now


def load(path: str | os.PathLike[str]) -> State:
    """Read state from disk, degrading to a clean re-baseline on any problem.

    A missing or corrupt file is not an error: it means either a genuine first
    run or an evicted Actions cache. Either way the caller re-baselines and says
    so, which is far better than replaying a backlog of stale notifications.
    """
    state_path = Path(path)
    if not state_path.is_file():
        LOG.info("no state file at %s -- treating this as a first run", state_path)
        return State(bootstrapped_at=time.time())

    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOG.warning("state file %s is unreadable (%s) -- re-baselining", state_path, exc)
        return State(bootstrapped_at=time.time())

    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        LOG.warning(
            "state version %r is not %r -- re-baselining",
            (raw or {}).get("version") if isinstance(raw, dict) else None,
            STATE_VERSION,
        )
        return State(bootstrapped_at=time.time())

    users: dict[str, UserState] = {}
    for key, data in (raw.get("users") or {}).items():
        if not isinstance(data, dict):
            continue
        users[str(key)] = UserState(
            last_seen_utc=float(data.get("last_seen_utc") or 0.0),
            seen=[str(entry) for entry in (data.get("seen") or [])],
        )

    return State(
        users=users,
        bootstrapped_at=float(raw.get("bootstrapped_at") or 0.0),
        last_heartbeat_utc=float(raw.get("last_heartbeat_utc") or 0.0),
        last_error_notice_utc=float(raw.get("last_error_notice_utc") or 0.0),
        alerts_since_heartbeat=int(raw.get("alerts_since_heartbeat") or 0),
        existed=True,
    )


def save(path: str | os.PathLike[str], state: State) -> None:
    """Write state atomically, so an interrupted run cannot truncate the file."""
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STATE_VERSION,
        "bootstrapped_at": state.bootstrapped_at or time.time(),
        "last_heartbeat_utc": state.last_heartbeat_utc,
        "last_error_notice_utc": state.last_error_notice_utc,
        "alerts_since_heartbeat": state.alerts_since_heartbeat,
        "users": {
            key: {"last_seen_utc": user.last_seen_utc, "seen": user.seen}
            for key, user in state.users.items()
        },
    }
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, state_path)
    LOG.info("state saved to %s (%d account(s))", state_path, len(state.users))
