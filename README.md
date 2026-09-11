# Reddit activity watcher

Watches a list of Reddit accounts and sends a Telegram message whenever any of
them makes a post or a comment. It runs on GitHub Actions every five minutes, so
it keeps working while your computer is off.

- **One message per item**, with the subreddit, a text snippet, and an
  *Open on Reddit* button.
- **Add accounts later** by editing a single secret -- no code change.
- **Nothing identifying lives in this repo.** The watched usernames are stored as
  an encrypted secret, and the "already seen" state is hashed and kept in the
  Actions cache rather than committed. That is what makes a *public* repo safe
  here, and a public repo is what makes the 5-minute schedule free.

---

## Setup

### 1. Reddit app (2 minutes)

Reddit's unauthenticated endpoints return 403 and its RSS feeds rate-limit almost
immediately, so the watcher uses the official API.

Go to <https://www.reddit.com/prefs/apps> -> **create another app**:

- type: **script**
- redirect uri: `http://localhost:8080` (unused, but required)

Copy the **client ID** (the short string under the app name) and the **secret**.

### 2. Telegram bot (2 minutes)

Message **@BotFather** -> `/newbot` -> copy the **token**.

Then **send your new bot any message**. A bot cannot open a conversation, so it
has no chat to reply to until you write first.

### 3. Local check before deploying

```bash
cp .env.example .env      # then fill it in
pip install -r requirements.txt
python tools/get_chat_id.py      # prints your TELEGRAM_CHAT_ID
python tools/check_setup.py      # validates everything, sends a test message
```

`check_setup.py` is the first real confirmation that each watched username
resolves and is publicly readable.

### 4. Push to a public GitHub repo

```bash
git init -b main
git add .
git commit -m "Reddit activity watcher"
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

Then add these under **Settings -> Secrets and variables -> Actions**:

| Secret | Value |
| --- | --- |
| `REDDIT_CLIENT_ID` | from step 1 |
| `REDDIT_CLIENT_SECRET` | from step 1 |
| `REDDIT_USER_AGENT` | `github-actions:goat-notification:v1 (by /u/yourname)` |
| `TELEGRAM_BOT_TOKEN` | from step 2 |
| `TELEGRAM_CHAT_ID` | from `get_chat_id.py` |
| `WATCH_USERS` | `Novel_Calendar5168,CSmith20001` |

Finally, open **Actions** -> **reddit-watch** -> **Run workflow** to try it by
hand. You should get one "Now watching" message per account.

> Scheduled workflows only run from the repository's **default branch**, and only
> once the workflow file is actually on it.

---

## Adding or removing an account later

Edit the **`WATCH_USERS`** secret and save the full comma-separated list:

```
Novel_Calendar5168,CSmith20001,SomeNewAccount
```

That is the whole procedure. On its next run the watcher baselines the new name
silently -- it will not dump their back catalogue at you -- and confirms with a
single *Now watching u/SomeNewAccount* message.

GitHub secrets **cannot be read back** once saved, only overwritten, so keep your
canonical list in `watchlist.local.txt` (gitignored). The weekly check-in message
also lists everyone currently being watched, so the live configuration is always
visible in Telegram.

---

## How it works

```
GitHub Actions cron (*/5)
  -> OAuth token from reddit.com
  -> GET oauth.reddit.com/user/<name>/overview     posts + comments in one call
  -> compare against the hashed state in the Actions cache
  -> POST api.telegram.org/.../sendMessage         one message per new item
  -> save state back to the cache
```

Roughly three API calls per run against a limit of 100 per minute.

A few decisions worth knowing:

- **First sight of an account sets a baseline and announces nothing.** Otherwise
  adding an account would fire 25 stale alerts.
- **The watermark never advances past a failed send.** If Telegram breaks
  mid-batch the run stops there and exits non-zero; the undelivered items are
  retried next time rather than being lost.
- **Same-second items still arrive.** Reddit timestamps have one-second
  granularity, so the comparison is `>=` and a ring of recent item hashes does
  the actual de-duplication.
- **A broken account does not silence the others.** Each profile is checked
  independently.
- **A weekly check-in message** means silence is unambiguous: no message for over
  a week means something is wrong, not that nobody posted.

---

## Testing

```bash
python -m unittest discover -s tests -v     # 22 behavioural tests, no network
DRY_RUN=1 python -m watcher.main            # log what would be sent
```

Local runs keep their state in `.state/`, which is separate from the copy CI
holds in its cache, so experimenting locally cannot suppress real alerts.

---

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| No alerts at all, ever | Secrets missing or misspelled. Run `python tools/check_setup.py`. |
| Reddit 401 | Bad client ID/secret, or the app is not type **script**. |
| Reddit 403 on a profile | Account suspended, shadowbanned, or profile hidden. |
| Intermittent 429 | `REDDIT_USER_AGENT` is generic. Use the `(by /u/name)` form. |
| Telegram 400 | Usually a bad `TELEGRAM_CHAT_ID`. Re-run `get_chat_id.py`. |
| Alerts stopped after ~2 months | See the 60-day note below. |

---

## Limits worth knowing

- **Not real-time.** GitHub's scheduler is best-effort: a 5-minute cron commonly
  lands 5-20 minutes late, and under heavy load runs can be **dropped entirely**.
  The cron is offset to `:02, :07, ...` because GitHub's docs name the top of the
  hour as the worst window, but that only reduces the problem.
- **60-day inactivity.** GitHub disables scheduled workflows in public repos after
  60 days with no repository activity. Push any commit, or click *Enable
  workflow*, to reset it. The weekly check-in going quiet is your warning.
- **Cache eviction** would reset tracking once. The watcher re-baselines and says
  so in the message, rather than replaying a backlog.
- **Public activity only.** Posts and comments in private or quarantined
  subreddits never appear, and anything deleted or removed disappears
  retroactively, so an alert can point at something already gone.
- **Reddit's free API tier** is for personal, non-commercial use at 100 queries
  per minute. This uses about three per run.
