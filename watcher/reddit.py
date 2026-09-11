"""Read-only Reddit client built on application-only OAuth.

Reddit's unauthenticated JSON endpoints now return 403, and the ``.rss`` fallback
rate-limits within seconds of a second request, so the watcher always
authenticates. A registered "script" app plus the ``client_credentials`` grant is
enough to read public profiles and lifts the quota to 100 queries per minute --
this watcher uses roughly three per run.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

LOG = logging.getLogger(__name__)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_ROOT = "https://oauth.reddit.com"
WEB_ROOT = "https://www.reddit.com"

MAX_ATTEMPTS = 4
BACKOFF_BASE = 2.0
MAX_BACKOFF = 60.0
TIMEOUT = 20

POST = "post"
COMMENT = "comment"


class RedditError(RuntimeError):
    """Any Reddit API failure."""


class RedditAuthError(RedditError):
    """Credentials were rejected -- retrying will not help."""


class UserNotFound(RedditError):
    """The profile does not exist, is suspended, or is not publicly visible."""


@dataclass(frozen=True)
class Item:
    """One post or comment, normalised across the t1/t3 payload shapes."""

    fullname: str
    kind: str
    author: str
    subreddit: str
    created_utc: float
    title: str
    body: str
    permalink: str

    @property
    def url(self) -> str:
        return f"{WEB_ROOT}{self.permalink}"


def _retry_delay(response: requests.Response, attempt: int) -> float:
    """Honour Retry-After when Reddit sends it, else exponential backoff."""
    header = (response.headers.get("Retry-After") or "").strip()
    if header:
        try:
            return min(float(header), MAX_BACKOFF)
        except ValueError:
            pass
    return min(BACKOFF_BASE**attempt, MAX_BACKOFF)


def _comment_permalink(data: dict[str, Any]) -> str:
    """Rebuild a comment permalink for the rare payload that omits one."""
    link_id = str(data.get("link_id") or "")
    post_id = link_id.split("_", 1)[1] if "_" in link_id else ""
    subreddit, comment_id = data.get("subreddit"), data.get("id")
    if not (post_id and subreddit and comment_id):
        return ""
    return f"/r/{subreddit}/comments/{post_id}/_/{comment_id}/"


def _parse_child(child: Any, fallback_author: str) -> Item | None:
    """Turn one listing child into an Item, or None if it is unusable."""
    if not isinstance(child, dict):
        return None
    data = child.get("data")
    if not isinstance(data, dict):
        return None

    code = child.get("kind")
    if code == "t3":
        kind, title, body = POST, data.get("title") or "", data.get("selftext") or ""
    elif code == "t1":
        # For a comment, link_title is the title of the post it sits under.
        kind, title, body = COMMENT, data.get("link_title") or "", data.get("body") or ""
    else:
        return None

    fullname = str(data.get("name") or "")
    permalink = str(data.get("permalink") or "") or _comment_permalink(data)
    if not fullname or not permalink:
        LOG.debug("skipping child with no fullname/permalink: kind=%r", code)
        return None

    return Item(
        fullname=fullname,
        kind=kind,
        author=str(data.get("author") or fallback_author),
        subreddit=str(data.get("subreddit") or ""),
        created_utc=float(data.get("created_utc") or 0.0),
        title=title,
        body=body,
        permalink=permalink,
    )


class RedditClient:
    """Minimal client covering the two endpoints the watcher needs."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        user_agent: str,
        session: requests.Session | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._session = session or requests.Session()
        self._session.headers["User-Agent"] = user_agent
        self._token: str | None = None

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Issue a request, retrying transport errors, 429s and 5xx responses."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._session.request(method, url, timeout=TIMEOUT, **kwargs)
            except requests.RequestException as exc:
                if attempt == MAX_ATTEMPTS:
                    raise RedditError(f"{method} {url} failed: {exc}") from exc
                delay = min(BACKOFF_BASE**attempt, MAX_BACKOFF)
                LOG.warning("%s %s errored (%s); retry in %.0fs", method, url, exc, delay)
                time.sleep(delay)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == MAX_ATTEMPTS:
                    raise RedditError(
                        f"{method} {url} still returned {response.status_code} "
                        f"after {attempt} attempts"
                    )
                delay = _retry_delay(response, attempt)
                LOG.warning(
                    "%s %s -> %s; retry in %.0fs",
                    method,
                    url,
                    response.status_code,
                    delay,
                )
                time.sleep(delay)
                continue

            return response

        raise RedditError(f"{method} {url} exhausted retries")

    def _log_quota(self, response: requests.Response) -> None:
        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining is not None:
            LOG.debug(
                "reddit quota: %s remaining, resets in %ss",
                remaining,
                response.headers.get("x-ratelimit-reset", "?"),
            )

    def token(self) -> str:
        """Fetch (and cache for this run) an application-only bearer token."""
        if self._token:
            return self._token

        response = self._request(
            "POST",
            TOKEN_URL,
            auth=(self._client_id, self._client_secret),
            data={"grant_type": "client_credentials"},
        )
        if response.status_code == 401:
            raise RedditAuthError(
                "Reddit rejected REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET (401). "
                "Check the app is type 'script' and the secret was copied whole."
            )
        if not response.ok:
            raise RedditError(
                f"token request failed: {response.status_code} {response.text[:200]}"
            )

        token = (response.json() or {}).get("access_token")
        if not token:
            raise RedditError("token response contained no access_token")
        self._token = str(token)
        return self._token

    def _api_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = dict(params or {})
        query.setdefault("raw_json", 1)  # stop Reddit HTML-escaping text for us
        response = self._request(
            "GET",
            f"{API_ROOT}{path}",
            headers={"Authorization": f"bearer {self.token()}"},
            params=query,
        )
        self._log_quota(response)

        if response.status_code == 401:
            self._token = None
            raise RedditAuthError(f"Reddit returned 401 for {path}")
        if response.status_code == 404:
            raise UserNotFound(f"Reddit returned 404 for {path} -- no such profile")
        if response.status_code == 403:
            raise UserNotFound(
                f"Reddit returned 403 for {path} -- suspended, private or blocked"
            )
        if not response.ok:
            raise RedditError(
                f"GET {path} -> {response.status_code} {response.text[:200]}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise RedditError(f"GET {path} returned a non-JSON body") from exc

    def about(self, username: str) -> dict[str, Any]:
        """Profile metadata. Used by check_setup to prove a username resolves."""
        return self._api_get(f"/user/{username}/about") or {}

    def overview(self, username: str, limit: int = 25) -> list[Item]:
        """Recent posts and comments together, oldest first.

        /overview returns both kinds in one listing (t3 posts, t1 comments),
        which is why it is preferred over polling /submitted and /comments
        separately. Sorted oldest-first so callers notify in chronological order.
        """
        payload = self._api_get(
            f"/user/{username}/overview", {"limit": limit, "sort": "new"}
        )
        children = ((payload or {}).get("data") or {}).get("children") or []
        items = [
            item
            for item in (_parse_child(child, username) for child in children)
            if item is not None
        ]
        items.sort(key=lambda item: item.created_utc)
        return items
