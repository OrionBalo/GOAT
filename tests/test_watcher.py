"""Behavioural tests for the watcher's notification logic.

These cover the parts that are easy to get subtly wrong -- baselining,
idempotency, same-second items, and never advancing the watermark past a failed
delivery -- using fake Reddit and Telegram clients, so no network access or
credentials are involved.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watcher import config  # noqa: E402
from watcher import main as main_mod  # noqa: E402
from watcher.notify import TelegramError, format_item, snippet  # noqa: E402
from watcher.reddit import COMMENT, POST, Item, UserNotFound, _parse_child  # noqa: E402

logging.disable(logging.CRITICAL)  # keep expected error paths off the test output

ENV = {
    "REDDIT_CLIENT_ID": "id",
    "REDDIT_CLIENT_SECRET": "secret",
    "REDDIT_USER_AGENT": "test:watcher:v1 (by /u/tester)",
    "TELEGRAM_BOT_TOKEN": "token",
    "TELEGRAM_CHAT_ID": "12345",
    "WATCH_USERS": "Alice",
    "DRY_RUN": "0",
}


def make_item(
    fullname: str,
    created: float,
    kind: str = POST,
    author: str = "Alice",
    sub: str = "testsub",
    title: str = "Title",
    body: str = "Body",
) -> Item:
    return Item(
        fullname=fullname,
        kind=kind,
        author=author,
        subreddit=sub,
        created_utc=float(created),
        title=title,
        body=body,
        permalink=f"/r/{sub}/comments/{fullname}/x/",
    )


class Harness:
    """Fake Reddit and Telegram clients, wired into watcher.main."""

    def __init__(self) -> None:
        self.feed: dict[str, list[Item]] = {}
        self.errors: dict[str, Exception] = {}
        self.sent: list[tuple[str, str | None]] = []
        self.fail_at: int | None = None  # raise once this many sends have landed

    def deliver(self, text: str, url: str | None) -> None:
        if self.fail_at is not None and len(self.sent) >= self.fail_at:
            raise TelegramError("simulated Telegram outage")
        self.sent.append((text, url))

    def patches(self):
        harness = self

        class FakeReddit:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def overview(self, username, limit=25):
                if username in harness.errors:
                    raise harness.errors[username]
                items = sorted(harness.feed.get(username, []), key=lambda i: i.created_utc)
                return items[-limit:]

        class FakeTelegram:
            def __init__(self, *args, **kwargs) -> None:
                self.dry_run = False

            def send(self, text, button_url=None, button_label="Open on Reddit"):
                harness.deliver(text, button_url)

            def send_item(self, item, snippet_chars=300):
                harness.deliver(format_item(item, snippet_chars), item.url)

        return (
            mock.patch.object(main_mod, "RedditClient", FakeReddit),
            mock.patch.object(main_mod, "Telegram", FakeTelegram),
        )


class WatcherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_path = Path(tmp.name) / "state.json"

        env_patch = mock.patch.dict(os.environ, dict(ENV, STATE_PATH=str(self.state_path)))
        env_patch.start()
        self.addCleanup(env_patch.stop)

        self.harness = Harness()
        for patch in self.harness.patches():
            patch.start()
            self.addCleanup(patch.stop)

    def run_watcher(self):
        """Run one cycle; return (exit code, messages sent during this cycle)."""
        before = len(self.harness.sent)
        code = main_mod.run()
        return code, self.harness.sent[before:]


class BaselineTests(WatcherTestCase):
    def test_first_run_announces_no_backlog(self):
        self.harness.feed["Alice"] = [
            make_item("t3_a", 100),
            make_item("t3_b", 200),
            make_item("t1_c", 300, kind=COMMENT),
        ]
        code, sent = self.run_watcher()
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1, "only the baseline note should be sent")
        self.assertIn("Now watching", sent[0][0])
        self.assertTrue(self.state_path.is_file())

    def test_second_run_is_silent(self):
        self.harness.feed["Alice"] = [make_item("t3_a", 100), make_item("t3_b", 200)]
        self.run_watcher()
        code, sent = self.run_watcher()
        self.assertEqual(code, 0)
        self.assertEqual(sent, [], "re-running with no new activity must send nothing")

    def test_new_post_is_announced_exactly_once(self):
        self.harness.feed["Alice"] = [make_item("t3_a", 100)]
        self.run_watcher()

        self.harness.feed["Alice"].append(
            make_item("t3_new", 500, title="Fresh post", body="Hello there")
        )
        code, sent = self.run_watcher()
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        text, url = sent[0]
        self.assertIn("posted in r/testsub", text)
        self.assertIn("Fresh post", text)
        self.assertEqual(url, "https://www.reddit.com/r/testsub/comments/t3_new/x/")

        _, again = self.run_watcher()
        self.assertEqual(again, [], "the same item must not be announced twice")

    def test_comment_is_announced_with_parent_title(self):
        self.harness.feed["Alice"] = [make_item("t3_a", 100)]
        self.run_watcher()
        self.harness.feed["Alice"].append(
            make_item("t1_c", 500, kind=COMMENT, title="Parent thread", body="my reply")
        )
        _, sent = self.run_watcher()
        self.assertEqual(len(sent), 1)
        self.assertIn("commented in r/testsub", sent[0][0])
        self.assertIn("Parent thread", sent[0][0])
        self.assertIn("my reply", sent[0][0])

    def test_items_sharing_a_timestamp_all_arrive(self):
        self.harness.feed["Alice"] = [make_item("t3_a", 100)]
        self.run_watcher()
        self.harness.feed["Alice"] += [
            make_item("t3_x", 400, title="same second one"),
            make_item("t1_y", 400, kind=COMMENT, title="same second two"),
        ]
        _, sent = self.run_watcher()
        self.assertEqual(len(sent), 2, "items sharing a created_utc must both arrive")
        _, again = self.run_watcher()
        self.assertEqual(again, [], "and neither may repeat")

    def test_late_arriving_item_from_the_same_second_is_not_missed(self):
        """Reddit indexes with a lag, so an item can surface after a peer that
        shares its created_utc has already advanced the watermark. A strict >
        comparison drops it silently; this is the case that forces >=."""
        self.harness.feed["Alice"] = [make_item("t3_a", 100)]
        self.run_watcher()

        self.harness.feed["Alice"].append(make_item("t3_first", 400, title="first"))
        _, sent = self.run_watcher()
        self.assertEqual(len(sent), 1)

        # Same second, but only visible to the next poll.
        self.harness.feed["Alice"].append(
            make_item("t1_late", 400, kind=COMMENT, title="late twin")
        )
        _, sent = self.run_watcher()
        self.assertEqual(len(sent), 1, "the late same-second item must still arrive")
        self.assertIn("late twin", sent[0][0])

        _, again = self.run_watcher()
        self.assertEqual(again, [], "and it must not repeat afterwards")

    def test_alerts_arrive_oldest_first(self):
        self.harness.feed["Alice"] = [make_item("t3_a", 100)]
        self.run_watcher()
        self.harness.feed["Alice"] += [
            make_item("t3_mid", 300, title="middle"),
            make_item("t3_late", 400, title="latest"),
        ]
        _, sent = self.run_watcher()
        self.assertIn("middle", sent[0][0])
        self.assertIn("latest", sent[1][0])


class DeliveryFailureTests(WatcherTestCase):
    def test_watermark_never_passes_a_failed_send(self):
        self.harness.feed["Alice"] = [make_item("t3_base", 100)]
        self.run_watcher()

        self.harness.feed["Alice"] += [
            make_item("t3_1", 201, title="one"),
            make_item("t3_2", 202, title="two"),
            make_item("t3_3", 203, title="three"),
        ]
        self.harness.fail_at = len(self.harness.sent) + 1

        code, sent = self.run_watcher()
        self.assertEqual(code, 1, "a failed delivery must make the run exit non-zero")
        self.assertEqual(len(sent), 1)
        self.assertIn("one", sent[0][0])

        self.harness.fail_at = None
        _, recovered = self.run_watcher()
        texts = " ".join(text for text, _ in recovered)
        self.assertEqual(len(recovered), 2, "exactly the undelivered items are retried")
        self.assertIn("two", texts)
        self.assertIn("three", texts)


class MultiAccountTests(WatcherTestCase):
    def test_adding_an_account_later_baselines_only_that_one(self):
        self.harness.feed["Alice"] = [make_item("t3_a", 100)]
        self.run_watcher()

        # The documented "add an account" flow: rewrite WATCH_USERS, nothing else.
        os.environ["WATCH_USERS"] = "Alice, u/Bob"
        self.harness.feed["Bob"] = [make_item("t3_b", 900, author="Bob")]

        code, sent = self.run_watcher()
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1, "only the new account should be announced")
        self.assertIn("Now watching", sent[0][0])
        self.assertIn("Bob", sent[0][0])

    def test_one_broken_account_does_not_silence_the_other(self):
        os.environ["WATCH_USERS"] = "Alice,Bob"
        self.harness.feed["Alice"] = [make_item("t3_a", 100)]
        self.harness.feed["Bob"] = [make_item("t3_b", 100, author="Bob")]
        self.run_watcher()

        self.harness.errors["Bob"] = UserNotFound("Reddit returned 404")
        self.harness.feed["Alice"].append(make_item("t3_new", 500, title="Still working"))

        code, sent = self.run_watcher()
        self.assertEqual(code, 1)
        texts = " ".join(text for text, _ in sent)
        self.assertIn("Still working", texts, "a broken account must not block a healthy one")
        self.assertIn("hit a problem", texts, "the failure should reach Telegram")

    def test_repeated_failures_are_throttled(self):
        os.environ["WATCH_USERS"] = "Bob"
        self.harness.feed["Bob"] = [make_item("t3_b", 100, author="Bob")]
        self.run_watcher()
        self.harness.errors["Bob"] = UserNotFound("Reddit returned 404")

        _, first = self.run_watcher()
        _, second = self.run_watcher()
        self.assertEqual(len(first), 1, "the first failure is reported")
        self.assertEqual(second, [], "a second failure in the window is suppressed")


class StatePrivacyTests(WatcherTestCase):
    def test_state_file_names_nothing_identifying(self):
        self.harness.feed["Alice"] = [make_item("t3_secret", 100, sub="somesub")]
        self.run_watcher()
        raw = self.state_path.read_text(encoding="utf-8")
        for leak in ("Alice", "alice", "t3_secret", "somesub"):
            self.assertNotIn(leak, raw, f"{leak} must not appear in the state file")


class FormattingTests(unittest.TestCase):
    def test_reddit_text_is_html_escaped(self):
        item = make_item(
            "t1_x", 1, kind=COMMENT, title="A & B",
            body="<script>alert(1)</script> & more",
        )
        text = format_item(item)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("A &amp; B", text)
        self.assertIn("<b>", text, "our own markup must survive escaping")

    def test_long_body_stays_under_telegram_limit(self):
        text = format_item(make_item("t3_x", 1, body="x" * 9000), snippet_chars=300)
        self.assertLess(len(text), 4096)
        self.assertTrue(text.rstrip().endswith("\u2026"))

    def test_snippet_collapses_whitespace(self):
        self.assertEqual(snippet("a\n\n  b\tc ", 100), "a b c")


class ConfigTests(unittest.TestCase):
    def test_watchlist_accepts_the_forms_people_paste(self):
        self.assertEqual(
            config.parse_watchlist("Alice, u/Bob\n/u/Carol  dave"),
            ("Alice", "Bob", "Carol", "dave"),
        )

    def test_watchlist_dedupes_case_insensitively(self):
        self.assertEqual(config.parse_watchlist("Alice,alice,ALICE"), ("Alice",))

    def test_seen_ring_always_covers_the_fetch_window(self):
        env = dict(ENV, FETCH_LIMIT="100", SEEN_RING_SIZE="10")
        with mock.patch.dict(os.environ, env):
            cfg = config.load()
        self.assertGreaterEqual(cfg.seen_ring_size, cfg.fetch_limit)

    def test_missing_secrets_are_reported(self):
        env = {k: v for k, v in ENV.items() if k != "TELEGRAM_BOT_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(config.ConfigError) as ctx:
                config.load()
        self.assertIn("TELEGRAM_BOT_TOKEN", str(ctx.exception))


class ParseTests(unittest.TestCase):
    def test_comment_permalink_is_rebuilt_when_absent(self):
        child = {
            "kind": "t1",
            "data": {
                "name": "t1_zz", "link_id": "t3_post1", "subreddit": "news",
                "id": "zz", "body": "hi", "created_utc": 10,
                "author": "Alice", "link_title": "Parent",
            },
        }
        item = _parse_child(child, "Alice")
        self.assertEqual(item.kind, COMMENT)
        self.assertEqual(item.permalink, "/r/news/comments/post1/_/zz/")
        self.assertEqual(item.url, "https://www.reddit.com/r/news/comments/post1/_/zz/")

    def test_post_is_parsed_from_t3(self):
        child = {
            "kind": "t3",
            "data": {
                "name": "t3_aa", "subreddit": "news",
                "permalink": "/r/news/comments/aa/t/", "title": "Headline",
                "selftext": "Body", "created_utc": 20, "author": "Alice",
            },
        }
        item = _parse_child(child, "Alice")
        self.assertEqual((item.kind, item.title), (POST, "Headline"))

    def test_unknown_kinds_are_ignored(self):
        self.assertIsNone(_parse_child({"kind": "t5", "data": {"name": "x"}}, "Alice"))
        self.assertIsNone(_parse_child({"kind": "t3", "data": None}, "Alice"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
