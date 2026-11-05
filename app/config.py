"""Env loading + per-feed config (feeds.json), shared by curate.py and poster.py.

feeds.json is the one place a photo stream is defined:
    {"defaults": {"timezone": "America/Denver"},
     "feeds": {"PostcardsFromHome": {"enabled": true,
                                     "hashtags": ["#PostcardsFromHome", "#Photography"]},
               ...}}
Each feed's settings = built-in DEFAULTS, then the file's "defaults", then
the feed's own entry.

Which photo: with "order": "on_this_day" (the default), a photo taken on
today's month and day, in any year, goes first; otherwise the pick is
random. With "seasonal", photos taken within season_days of today's
calendar date come first, chosen at random among themselves; with none
that close, the window widens in season_days steps. "random" ignores
dates. In every order, a photo from the
same region (state/country) as any of the feed's last region_cooldown posts
waits while photos from elsewhere are left, and anything in up-next/ goes
before everything in curated/.

Posts go out at fixed local times: window_start, then every interval_hours
while before window_end -- with the built-in defaults (8-22, 6h) that's
08:00, 14:00 and 20:00. With the window off, slots start at midnight.
Or "times" sets them per weekday, replacing the window and interval:
    "times": {"mon-thu": ["08:00"], "fri-sun": ["16:00"]}
A day left out of "times" has no posts.

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
    "times": None,  # {"mon-thu": ["08:00"], "fri": [...]}: per-weekday times, replacing the window
    "timezone": "UTC",
    "order": "on_this_day",
    "season_days": 21,
    "region_cooldown": 2,  # posts before a region (state/country) may repeat; 0 = off
    "date_format": "Photo taken %Y-%m-%d",  # last line of each post; null to leave it off
}
ORDERS = ("on_this_day", "seasonal", "random")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
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
    if cfg["times"] is not None:
        _parse_times(account, cfg["times"])
    start, end = cfg["window_start"], cfg["window_end"]
    if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= 24):
        raise ValueError(f"{account}: window_start/window_end must be whole hours with 0 <= start < end <= 24")
    fmt = cfg["date_format"]
    if fmt is not None and not (isinstance(fmt, str) and "%" in fmt and "#" not in fmt):
        raise ValueError(f"{account}: date_format must be null or a strftime pattern like "
                         f"\"Photo taken %Y-%m-%d\" (no #), got {fmt!r}")
    if cfg["order"] not in ORDERS:
        raise ValueError(f"{account}: order must be one of {list(ORDERS)}, got {cfg['order']!r}")
    days = cfg["season_days"]
    if not (isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= 182):
        raise ValueError(f"{account}: season_days must be a whole number of days from 1 to 182, got {days!r}")
    cooldown = cfg["region_cooldown"]
    if not (isinstance(cooldown, int) and not isinstance(cooldown, bool) and 0 <= cooldown <= 50):
        raise ValueError(f"{account}: region_cooldown must be a whole number of posts from 0 to 50, got {cooldown!r}")
    try:
        ZoneInfo(cfg["timezone"])
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError(f"{account}: unknown timezone {cfg['timezone']!r}")


def _weekdays(account: str, key: str) -> list[int]:
    """'mon' -> [0]; 'mon-thu' -> [0, 1, 2, 3]; 'fri-mon' wraps -> [4, 5, 6, 0]."""
    first, _, last = key.lower().partition("-")
    last = last or first
    if first not in WEEKDAYS or last not in WEEKDAYS:
        raise ValueError(f"{account}: times key {key!r} must be a day like \"mon\" or a range like \"mon-thu\"")
    a, b = WEEKDAYS.index(first), WEEKDAYS.index(last)
    return [(a + i) % 7 for i in range((b - a) % 7 + 1)]


def _parse_times(account: str, times) -> list[list[datetime.time]]:
    """feeds.json "times" -> seven sorted lists of slot times, Monday first."""
    if not isinstance(times, dict) or not times:
        raise ValueError(f"{account}: times must be like {{\"mon-thu\": [\"08:00\"], \"fri-sun\": [\"16:00\"]}}")
    week: list[list[datetime.time] | None] = [None] * 7
    for key, clock in times.items():
        if not (isinstance(clock, list) and all(isinstance(t, str) and TIME_RE.fullmatch(t) for t in clock)):
            raise ValueError(f"{account}: times[{key!r}] must be a list of \"HH:MM\" times, got {clock!r}")
        for day in _weekdays(account, key):
            if week[day] is not None:
                raise ValueError(f"{account}: times lists {WEEKDAYS[day]} more than once")
            week[day] = sorted({datetime.time.fromisoformat(t) for t in clock})
    if not any(week):
        raise ValueError(f"{account}: times has no posting times on any day")
    return [day or [] for day in week]


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
        merged["week"] = _parse_times(account, merged["times"]) if merged["times"] is not None else None
        feeds[account] = merged
    return feeds


# ---------------------------------------------------------------------------
# Posting schedule: fixed local times each day
# ---------------------------------------------------------------------------

def slot_times(feed_cfg: dict, day: datetime.date) -> list[datetime.time]:
    """Local times a feed posts at on this day, e.g. [08:00, 14:00, 20:00]."""
    if feed_cfg["week"] is not None:
        return feed_cfg["week"][day.weekday()]
    windowed = feed_cfg["window_enabled"]
    start = feed_cfg["window_start"] * 60 if windowed else 0
    end = feed_cfg["window_end"] * 60 if windowed else 24 * 60
    step = round(feed_cfg["interval_hours"] * 60)
    return [datetime.time(m // 60, m % 60) for m in range(start, end, step)]


def week_slots(feed_cfg: dict) -> list[list[datetime.time]]:
    """slot_times for each weekday, Monday first."""
    monday = datetime.date(2024, 1, 1)
    return [slot_times(feed_cfg, monday + datetime.timedelta(days=i)) for i in range(7)]


def posts_per_day(feed_cfg: dict) -> float:
    """Average over the week."""
    return sum(len(day) for day in week_slots(feed_cfg)) / 7


def describe_week(feed_cfg: dict) -> list[str]:
    """Slot times as text, days with the same times run together:
    ["Mon-Thu 08:00", "Fri-Sun 16:00"], or just ["08:00, 14:00"] when every day matches."""
    week = week_slots(feed_cfg)
    text = [", ".join(t.strftime("%H:%M") for t in day) or "no posts" for day in week]
    if len(set(text)) == 1:
        return [text[0]]
    runs = []
    for i, times in enumerate(text):
        if runs and runs[-1][2] == times:
            runs[-1][1] = i
        else:
            runs.append([i, i, times])
    return [WEEKDAYS[a].title() + (f"-{WEEKDAYS[b].title()}" if b > a else "") + f" {times}"
            for a, b, times in runs]


def _slots_on(day: datetime.date, feed_cfg: dict) -> list[datetime.datetime]:
    return [datetime.datetime.combine(day, t, tzinfo=feed_cfg["tz"]) for t in slot_times(feed_cfg, day)]


def latest_slot(feed_cfg: dict, now: datetime.datetime) -> datetime.datetime:
    """The most recent slot at or before now."""
    local = now.astimezone(feed_cfg["tz"])
    for back in range(8):
        past = [s for s in _slots_on(local.date() - datetime.timedelta(days=back), feed_cfg) if s <= local]
        if past:
            return past[-1]
    raise AssertionError("every week has at least one slot")


def next_slot(feed_cfg: dict, now: datetime.datetime) -> datetime.datetime:
    """The first slot strictly after now."""
    local = now.astimezone(feed_cfg["tz"])
    for ahead in range(8):
        future = [s for s in _slots_on(local.date() + datetime.timedelta(days=ahead), feed_cfg) if s > local]
        if future:
            return future[0]
    raise AssertionError("every week has at least one slot")
