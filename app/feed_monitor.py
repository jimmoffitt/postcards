"""Alert by email/text when a feed misses a scheduled post.

Runs off the Pi (as a GitHub Actions job, .github/workflows/feed-monitor.yml),
because when the Pi's network is down it can't report anything itself. It
reads the schedule from feeds.json and the newest posts from Bluesky's public
API, so it needs no Bluesky login.

A feed is overdue when its latest slot (taken GRACE before now) has no post at
or after it. One alert goes out per outage, a reminder every REMIND while it
lasts, and an "all clear" once the feed posts again. Which outages were already
reported is kept in a small JSON state file that the workflow carries from run
to run in the Actions cache.

Environment:
    GMAIL_ADDRESS, GMAIL_APP_PASSWORD  sender (a Gmail app password, not the account password)
    ALERT_TO                           comma-separated recipients (email and/or phone@vtext.com)
    MONITOR_STATE                      state file path (default monitor_state.json)

    python feed_monitor.py           check and alert
    python feed_monitor.py --dry-run check and print, send nothing
    python feed_monitor.py --test    send a test alert
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import smtplib
import sys
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

from config import latest_slot, load_feeds

HANDLES = {
    "PostcardsFromHome": "athome-postcards.bsky.social",
    "SomePostcards": "somepostcards.bsky.social",
}
GRACE = datetime.timedelta(minutes=90)   # a post this late still counts as on time
EARLY = datetime.timedelta(minutes=10)   # a post this far before its slot counts for it
REMIND = datetime.timedelta(hours=24)
API = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
UTC = datetime.timezone.utc


def newest_post(handle: str) -> datetime.datetime | None:
    query = urllib.parse.urlencode({"actor": handle, "limit": 5, "filter": "posts_no_replies"})
    req = urllib.request.Request(f"{API}?{query}", headers={"User-Agent": "postcards-feed-monitor"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        feed = json.load(resp)["feed"]
    times = [datetime.datetime.fromisoformat(item["post"]["record"]["createdAt"].replace("Z", "+00:00"))
             for item in feed if "reason" not in item]          # skip reposts
    return max(times) if times else None


def check(now: datetime.datetime) -> dict[str, dict]:
    """{feed: {"overdue": bool, "slot": iso, "last_post": iso|None}} for each enabled feed."""
    status = {}
    for feed, cfg in load_feeds().items():
        if not cfg["enabled"]:
            continue
        handle = HANDLES.get(feed)
        if not handle:
            print(f"warning: no Bluesky handle for {feed}; add it to HANDLES", file=sys.stderr)
            continue
        slot = latest_slot(cfg, now - GRACE)
        last = newest_post(handle)
        status[feed] = {
            "overdue": last is None or last < slot - EARLY,
            "slot": slot.isoformat(),
            "last_post": last.isoformat() if last else None,
        }
    return status


def ago(iso: str | None, now: datetime.datetime) -> str:
    if not iso:
        return "never"
    hours = (now - datetime.datetime.fromisoformat(iso)).total_seconds() / 3600
    return f"{hours:.0f}h ago" if hours < 48 else f"{hours / 24:.1f} days ago"


def send(subject: str, body: str) -> None:
    sender = os.environ["GMAIL_ADDRESS"]
    to = [a.strip() for a in os.environ["ALERT_TO"].split(",") if a.strip()]
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, ", ".join(to), subject
    msg.set_content(body)       # kept short: the phone gateway turns it into a text
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(sender, os.environ["GMAIL_APP_PASSWORD"])
        smtp.send_message(msg)
    print(f"sent: {subject}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()

    if args.test:
        send("Postcards monitor test", "Test alert from the postcards feed monitor. Alerts are working.")
        return 0

    now = datetime.datetime.now(UTC)
    state_path = Path(os.environ.get("MONITOR_STATE", "monitor_state.json"))
    try:
        state = json.loads(state_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    status = check(now)
    for feed, s in status.items():
        print(f"{feed}: {'OVERDUE' if s['overdue'] else 'ok'} -- last post {ago(s['last_post'], now)}, "
              f"latest slot {s['slot']}")

    down, up = [], []
    for feed, s in status.items():
        alerted = state.get(feed)                                # {"last_post", "alerted_at"} while down
        if s["overdue"]:
            stale = alerted is None or alerted["last_post"] != s["last_post"]
            due = stale or now - datetime.datetime.fromisoformat(alerted["alerted_at"]) >= REMIND
            if due:
                down.append(feed)
                state[feed] = {"last_post": s["last_post"], "alerted_at": now.isoformat()}
        elif alerted:
            up.append(feed)
            del state[feed]

    messages = []
    if down:
        lines = [f"{f}: last post {ago(status[f]['last_post'], now)}" for f in down]
        messages.append(("Postcards: missed posts", "\n".join(lines) + "\nCheck the Pi (ssh pi)."))
    if up:
        messages.append(("Postcards: posting again", ", ".join(up) + " posted again."))

    for subject, body in messages:
        if args.dry_run:
            print(f"[dry run] would send: {subject}\n{body}")
        else:
            send(subject, body)

    if not args.dry_run:
        state_path.write_text(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
