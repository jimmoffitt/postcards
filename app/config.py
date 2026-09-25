"""Env loading + per-feed config (feeds.json), shared by curate.py and poster.py.

feeds.json is the one place a photo stream is defined:
    {"defaults": {"timezone": "America/Denver"},
     "feeds": {"PostcardsFromHome": {"enabled": true,
                                     "hashtags": ["#PostcardsFromHome", "#Photography"]},
               ...}}
Each feed's settings = built-in DEFAULTS, then the file's "defaults", then
the feed's own entry.

Which photo: with "order": "seasonal" (the default), photos taken within
season_days of today's calendar date -- any year -- come first, chosen at
random among themselves; with none that close, the window widens in
season_days steps. "random" ignores dates.

Posts go out at fixed local times: window_start, then every interval_hours
while before window_end -- with the built-in defaults (8-22, 6h) that's
08:00, 14:00 and 20:00. With the window off, slots start at midnight.

poster.py re-reads it every few seconds, so edits to hashtags and schedule
apply to the next post without a restart. Starting a newly enabled feed
does need a restart (its worker thread is created at startup).
"""

from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

APP_DIR = Path(__file__).resolve().parent
FEEDS_PATH = APP_DIR / "feeds.json"

MIN_INTERVAL_HOURS = 5 / 60  # 5 minutes
MAX_INTERVAL_HOURS = 24
DEFAULTS = {
    "enabled": False,
    "hashtags": [],
    "interval_hours": 6,
    "window_enabled": True,
    "window_start": 8,
    "window_end": 22,
    "timezone": "UTC",
    "order": "seasonal",
    "season_days": 21,
}
ORDERS = ("seasonal", "random")
HASHTAG_RE = re.compile(r"#\w+")


def load_dotenv(env_file: str | Path = ".local.env") -> None:
    path = APP_DIR / env_file
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv as _load
        _load(dotenv_path=path, override=False)
    except ImportError:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def app_path(env_var: str, default: str) -> Path:
    """A path from the environment, resolved against app/ when relative --
    so it means the same thing whether run by hand or under systemd."""
    p = Path(os.environ.get(env_var) or default).expanduser()
    return p if p.is_absolute() else (APP_DIR / p).resolve()


def credentials(account: str) -> tuple[str, str]:
    prefix = account.upper()
    handle = os.environ.get(f"{prefix}_BSKY_HANDLE", "").strip()
    app_password = os.environ.get(f"{prefix}_BSKY_APP_PASSWORD", "").strip()
    return handle, app_password


def _validate(account: str, cfg: dict) -> None:
    unknown = set(cfg) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"{account}: unknown setting(s) {sorted(unknown)} -- valid: {sorted(DEFAULTS)}")
    if not isinstance(cfg["enabled"], bool):
        raise ValueError(f"{account}: enabled must be true or false")
    tags = cfg["hashtags"]
    if not isinstance(tags, list) or not all(isinstance(t, str) and HASHTAG_RE.fullmatch(t) for t in tags):
        raise ValueError(f"{account}: hashtags must be a list like [\"#Tag\", ...] (letters/digits/_ only), got {tags!r}")
    interval = cfg["interval_hours"]
    if not (isinstance(interval, (int, float)) and MIN_INTERVAL_HOURS <= interval <= MAX_INTERVAL_HOURS):
        raise ValueError(f"{account}: interval_hours={interval!r} out of range "
                         f"[{MIN_INTERVAL_HOURS:.4f}, {MAX_INTERVAL_HOURS}]")
    if abs(interval * 60 - round(interval * 60)) > 1e-9:
        raise ValueError(f"{account}: interval_hours={interval!r} must be a whole number of minutes")
    start, end = cfg["window_start"], cfg["window_end"]
    if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= 24):
        raise ValueError(f"{account}: window_start/window_end must be whole hours with 0 <= start < end <= 24")
    if cfg["order"] not in ORDERS:
        raise ValueError(f"{account}: order must be one of {list(ORDERS)}, got {cfg['order']!r}")
    days = cfg["season_days"]
    if not (isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= 182):
        raise ValueError(f"{account}: season_days must be a whole number of days from 1 to 182, got {days!r}")
    try:
        ZoneInfo(cfg["timezone"])
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError(f"{account}: unknown timezone {cfg['timezone']!r}")


def load_feeds(path: str | Path = None) -> dict:
    """{account: {enabled, hashtags, interval_hours, window_*, timezone, tz}}
    for every feed, enabled or not. Raises ValueError on any bad setting."""
    path = Path(path) if path else FEEDS_PATH
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict) or set(raw) - {"defaults", "feeds"} or not isinstance(raw.get("feeds"), dict):
        raise ValueError('feeds.json must look like {"defaults": {...}, "feeds": {"Name": {...}, ...}}')
    file_defaults = raw.get("defaults", {})
    feeds = {}
    for account, cfg in raw["feeds"].items():
        merged = {**DEFAULTS, **file_defaults, **cfg}
        _validate(account, merged)
        merged["tz"] = ZoneInfo(merged["timezone"])
        feeds[account] = merged
    return feeds


# ---------------------------------------------------------------------------
# Posting schedule: fixed local times each day
# ---------------------------------------------------------------------------

def slot_times(feed_cfg: dict) -> list[datetime.time]:
    """Local times of day a feed posts at, e.g. [08:00, 14:00, 20:00]."""
    windowed = feed_cfg["window_enabled"]
    start = feed_cfg["window_start"] * 60 if windowed else 0
    end = feed_cfg["window_end"] * 60 if windowed else 24 * 60
    step = round(feed_cfg["interval_hours"] * 60)
    return [datetime.time(m // 60, m % 60) for m in range(start, end, step)]


def _slots_on(day: datetime.date, feed_cfg: dict) -> list[datetime.datetime]:
    return [datetime.datetime.combine(day, t, tzinfo=feed_cfg["tz"]) for t in slot_times(feed_cfg)]


def latest_slot(feed_cfg: dict, now: datetime.datetime) -> datetime.datetime:
    """The most recent slot at or before now."""
    local = now.astimezone(feed_cfg["tz"])
    for day in (local.date(), local.date() - datetime.timedelta(days=1)):
        past = [s for s in _slots_on(day, feed_cfg) if s <= local]
        if past:
            return past[-1]
    raise AssertionError("every day has at least one slot")


def next_slot(feed_cfg: dict, now: datetime.datetime) -> datetime.datetime:
    """The first slot strictly after now."""
    local = now.astimezone(feed_cfg["tz"])
    for day in (local.date(), local.date() + datetime.timedelta(days=1)):
        future = [s for s in _slots_on(day, feed_cfg) if s > local]
        if future:
            return future[0]
    raise AssertionError("every day has at least one slot")
