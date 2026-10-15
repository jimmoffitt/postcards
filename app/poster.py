#!/usr/bin/env python3
"""Multi-feed Bluesky posting daemon.

One process, one thread per enabled feed in feeds.json, each posting at
fixed local times (config.slot_times -- by default 08:00, 14:00, 20:00).
At each slot, its worker picks a photo -- from up-next/ if there are any,
else from curated/; one taken on today's date in an earlier year if there is
one, else not from the region of the feed's last few posts (see order_candidates) -- posts it (alt text from
metadata_<Account>.json, post text from compose_message: caption + feed
hashtags + place hashtags), and moves the file into posted/. A photo in a
group (groups_<Account>.json) is posted together with the rest of its group,
as one post of up to 4 images with the group's message and place.

feeds.json is re-read continuously, so hashtag and schedule edits apply to
the next post without a restart. Enabling a feed needs a restart.

Each feed's last post time is kept in state/<Account>.json, so a restart
never posts a slot twice. A slot missed while the Pi was down is still
posted if it's no more than LATE_GRACE late; otherwise it's skipped. Every
post is also recorded in state/posted_log.jsonl; a photo in that log or in
posted/ is never posted again, even if a sync copies it back into curated/.

Usage:
    python3 poster.py                             # run all enabled feeds, loop forever
    python3 poster.py --workers PostcardsFromHome # only these (must be enabled)
    python3 poster.py --check                     # validate config/logins, show what's queued
    python3 poster.py --once                      # post one photo per feed NOW (ignores the schedule)
    python3 poster.py --once --dry-run            # ...pick + log only, post/move nothing
    python3 poster.py --once --workers PostcardsFromHome --photo IMG_1234.jpg  # post that photo now
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import logging
import os
import random
import re
import signal
import sys
import threading
from pathlib import Path

import config
import metadata as md
import upload_prep

logger = logging.getLogger(__name__)

POLL_SECONDS = 30
LATE_GRACE = datetime.timedelta(hours=1)  # how late a missed slot may still be posted
PLACEHOLDER_PASSWORD = "xxxx-xxxx-xxxx-xxxx"  # as in .local.env.example

STATE_DIR = config.app_path("STATE_DIR", "state")
POSTED_LOG = STATE_DIR / "posted_log.jsonl"
_log_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Bluesky
# ---------------------------------------------------------------------------

def build_hashtag_facets(text: str):
    from atproto import models as atproto_models

    facets = []
    for match in re.finditer(r"#\w+", text):
        tag = match.group(0)[1:]
        byte_start = len(text[: match.start()].encode("utf-8"))
        byte_end = byte_start + len(match.group(0).encode("utf-8"))
        facets.append(
            atproto_models.AppBskyRichtextFacet.Main(
                index=atproto_models.AppBskyRichtextFacet.ByteSlice(byte_start=byte_start, byte_end=byte_end),
                features=[atproto_models.AppBskyRichtextFacet.Tag(tag=tag)],
            )
        )
    return facets


def post_to_bluesky(handle: str, app_password: str, images: list[upload_prep.PreparedImage],
                    alt_texts: list[str], message: str) -> str:
    from atproto import Client, models

    client = Client()
    client.login(handle, app_password)
    resp = client.send_images(
        text=message,
        images=[image.data for image in images],
        image_alts=alt_texts,
        facets=build_hashtag_facets(message),
        # Without an aspect ratio Bluesky falls back to a square crop.
        image_aspect_ratios=[models.AppBskyEmbedDefs.AspectRatio(width=image.width, height=image.height)
                             for image in images],
    )
    return getattr(resp, "uri", "")


# ---------------------------------------------------------------------------
# Persistent state: last post time per feed + a log of every post
# ---------------------------------------------------------------------------

def _state_path(account: str) -> Path:
    return STATE_DIR / f"{account}.json"


def load_state(account: str) -> dict:
    path = _state_path(account)
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(account: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _state_path(account)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def record_post(account: str, filename: str, uri: str, when: datetime.datetime) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"account": account, "filename": filename, "uri": uri, "posted_at": when.isoformat()})
    with _log_lock, open(POSTED_LOG, "a") as f:
        f.write(line + "\n")


def recent_regions(account: str, entries: dict, groups: dict, count: int) -> list[str]:
    """Regions (see md.region) of the feed's last `count` posts, newest
    first; posts with no place are skipped over, not counted. A group's
    photos share one post (one uri), so they count once."""
    if count <= 0 or not POSTED_LOG.exists():
        return []
    with _log_lock, open(POSTED_LOG) as f:
        lines = f.readlines()
    regions, seen_uris = [], set()
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("account") != account:
            continue
        uri = rec.get("uri")
        if uri:
            if uri in seen_uris:
                continue
            seen_uris.add(uri)
        r = md.region(md.effective_entry(rec.get("filename"), entries, groups).get("place"))
        if r:
            regions.append(r)
            if len(regions) == count:
                break
    return regions


def posted_filenames(account: str) -> set[str]:
    done = set(md.scan_posted(account))
    if POSTED_LOG.exists():
        with _log_lock, open(POSTED_LOG) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("account") == account:
                    done.add(rec["filename"])
    return done


NEVER = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def last_attempt(state: dict) -> datetime.datetime:
    """Most recent post or attempt (failure / empty queue count too)."""
    stamps = [state.get("last_posted_at"), state.get("last_attempt_at")]
    stamps = [datetime.datetime.fromisoformat(s) for s in stamps if s]
    return max(stamps) if stamps else NEVER


def due_slot(state: dict, feed_cfg: dict, now: datetime.datetime) -> datetime.datetime | None:
    """The slot to post for right now, or None: the latest slot counts if it
    hasn't been attempted yet and is no more than LATE_GRACE old."""
    slot = config.latest_slot(feed_cfg, now)
    if last_attempt(state) >= slot or now - slot > LATE_GRACE:
        return None
    return slot


def describe_next(state: dict, feed_cfg: dict) -> str:
    now = datetime.datetime.now(feed_cfg["tz"])
    slot = due_slot(state, feed_cfg, now)
    if slot:
        return f"now (the {slot:%H:%M} slot)"
    return f"{config.next_slot(feed_cfg, now):%Y-%m-%d %H:%M %Z}"


def describe_schedule(feed_cfg: dict) -> str:
    slots = [t.strftime("%H:%M") for t in config.slot_times(feed_cfg)]
    if len(slots) > 6:
        return (f"every {round(feed_cfg['interval_hours'] * 60)} min, {slots[0]}-{slots[-1]} "
                f"{feed_cfg['timezone']} ({len(slots)}/day)")
    return ", ".join(slots) + f" {feed_cfg['timezone']}"


# ---------------------------------------------------------------------------
# Feed config, re-read whenever feeds.json changes
# ---------------------------------------------------------------------------

_feeds_lock = threading.Lock()
_feeds_cache: dict = {"mtime": None, "feeds": None}


def current_feeds() -> dict:
    with _feeds_lock:
        try:
            mtime = config.FEEDS_PATH.stat().st_mtime
            if mtime != _feeds_cache["mtime"]:
                feeds = config.load_feeds()
                if _feeds_cache["feeds"] is not None:
                    logger.info("feeds.json changed -- reloaded")
                _feeds_cache.update(mtime=mtime, feeds=feeds)
        except (OSError, ValueError) as exc:
            if _feeds_cache["feeds"] is None:
                raise
            logger.error("feeds.json is invalid, keeping previous settings: %s", exc)
            _feeds_cache["mtime"] = config.FEEDS_PATH.stat().st_mtime if config.FEEDS_PATH.exists() else None
        return _feeds_cache["feeds"]


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

def queue(account: str) -> tuple[dict, list[str], set[str]]:
    """(metadata entries, filenames in up-next/ and curated/ that are safe
    to post, the subset of those in up-next/)."""
    entries = md.load_metadata(account)
    done = posted_filenames(account)
    up_next = set(md.scan_up_next(account))
    candidates = []
    for f in md.scan_up_next(account) + md.scan_curated(account):
        if f in done:
            logger.warning("[%s] %s is queued but was already posted -- skipping", account, f)
        elif f not in entries:
            logger.warning("[%s] %s has no metadata entry -- skipping", account, f)
        else:
            candidates.append(f)
    return entries, candidates, up_next & set(candidates)


def post_units(candidates: list[str], groups: dict) -> dict[str, list[str]]:
    """The queue as posts: {first photo: [every photo in that post]}. A
    single photo is a post of its own; a group's queued photos go together,
    in the group's order."""
    queued, units, taken = set(candidates), {}, set()
    for f in candidates:
        if f in taken:
            continue
        gid = md.group_of(groups, f)
        photos = [p for p in groups[gid]["photos"] if p in queued][:md.MAX_GROUP_PHOTOS] if gid else [f]
        taken.update(photos)
        units[photos[0]] = photos
    return units


def _day_of_year(month: int, day: int) -> int:
    # A non-leap year, so Feb 29 counts as Feb 28.
    return datetime.date(2001, month, min(day, 28) if month == 2 else day).timetuple().tm_yday


def season_distance(date_taken: str | None, today: datetime.date) -> int | None:
    """Days between the photo's calendar date (any year) and today's, going
    around the year the short way: Dec 30 and Jan 2 are 3 days apart."""
    try:
        _, month, day = (int(x) for x in date_taken.split("-"))
        taken = _day_of_year(month, day)
    except (AttributeError, ValueError):
        return None
    diff = abs(taken - _day_of_year(today.month, today.day))
    return min(diff, 365 - diff)


def order_candidates(candidates: list[str], entries: dict, feed_cfg: dict, today: datetime.date,
                     recent: list[str] = (), up_next: set[str] = frozenset()) -> list[str]:
    """Candidates in the order to try them, sorted on these keys in turn:
    1. up-next/ photos before curated/ ones;
    2. "on_this_day": photos taken on today's month and day (any year) first;
    3. photos from a region in `recent` (the regions of the last few posts)
       after those from anywhere else, including photos with no place;
    4. "seasonal": taken within season_days of today's date (any year)
       first, then within 2x season_days, and so on; undated photos last.
    Random among photos that tie on all of them."""
    order, window = feed_cfg["order"], feed_cfg["season_days"]
    recent = set(recent)

    def key(filename):
        d = season_distance(entries[filename].get("date_taken"), today)
        on_this_day = order == "on_this_day" and d == 0
        band = 0 if order != "seasonal" else 10_000 if d is None else max(0, -(-d // window) - 1)  # ceil(d/window)-1
        return (filename not in up_next, not on_this_day,
                md.region(entries[filename].get("place")) in recent, band)

    return sorted(random.sample(candidates, len(candidates)), key=key)  # stable, so ties stay shuffled


def run_cycle(account: str, feed_cfg: dict, handle: str, app_password: str, dry_run: bool,
              photo: str = None) -> bool:
    """Posts one photo, or one group -- `photo` (or its group) if given,
    else per the feed's order. Returns True if something was posted."""
    entries, candidates, up_next = queue(account)
    groups = md.load_groups(account)
    units = post_units(candidates, groups)
    queued = len(units)
    if photo:
        if photo not in candidates:
            logger.error("[%s] %s isn't an unposted photo in curated/ or up-next/ with metadata -- nothing posted", account, photo)
            return False
        units = {first: photos for first, photos in units.items() if photo in photos}
    if not units:
        logger.info("[%s] no curated photos ready to post -- nothing to do", account)
        return False

    # Seasonal (or random) order; a post with a photo that can't be made safe
    # to upload (see upload_prep) is skipped in favour of the next, not posted as-is.
    today = datetime.datetime.now(feed_cfg["tz"]).date()
    recent = recent_regions(account, entries, groups, feed_cfg["region_cooldown"])
    unit_entries = {first: md.effective_entry(first, entries, groups) for first in units}
    for first in order_candidates(list(units), unit_entries, feed_cfg, today, recent, up_next):
        try:
            images = [upload_prep.prepare(md.find_photo(account, f)[0]) for f in units[first]]
            break
        except Exception as exc:
            logger.error("[%s] skipping %s: %s", account, ", ".join(units[first]), exc)
    else:
        logger.error("[%s] none of the %d queued posts could be prepared -- nothing posted",
                     account, len(units))
        return False

    photos = units[first]
    meta = unit_entries[first]
    message = md.compose_message(meta, feed_cfg, md.load_location_tags())
    distance = season_distance(meta.get("date_taken"), today)
    region = md.region(meta.get("place"))
    what = first if len(photos) == 1 else f"a group of {len(photos)} ({', '.join(photos)})"
    logger.info("[%s] selected %s%s, taken %s%s, in %s%s (%d queued; %s bytes, metadata stripped, %s)",
                account, what, " from up-next/" if first in up_next else "",
                meta.get("date_taken") or "on an unknown date",
                "" if distance is None else f", {distance} days from today's date",
                region or "no known region",
                f" -- repeats a recent region ({', '.join(dict.fromkeys(recent))}): nothing else was left"
                if region in recent and first not in up_next and distance != 0 else "",
                queued, " + ".join(str(len(i.data)) for i in images), ", ".join(i.method for i in images))

    alt_texts = [entries[f].get("alt_text", "") for f in photos]
    for f, alt in zip(photos, alt_texts):
        if not alt:
            logger.warning("[%s] %s has no alt_text -- posting anyway", account, f)

    if dry_run:
        logger.info("[%s] [dry-run] would post %s\n%s\n  text: %s", account, what,
                    "\n".join(f"  alt ({f}, {i.width}x{i.height}): {alt}"
                              for f, i, alt in zip(photos, images, alt_texts)), message)
        return False

    uri = post_to_bluesky(handle, app_password, images, alt_texts, message)
    now = datetime.datetime.now(datetime.timezone.utc)
    # Log before moving: if the move fails, the log alone still stops a repost.
    for f in photos:
        record_post(account, f, uri, now)
    logger.info("[%s] posted %s -> %s", account, what, uri)

    for f in photos:
        dest = md.move_to_posted(account, f)
        logger.info("[%s] moved %s -> %s", account, f, dest)
    return True


def attempt(account: str, feed_cfg: dict, handle: str, app_password: str, dry_run: bool,
            photo: str = None) -> None:
    state = load_state(account)
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        if run_cycle(account, feed_cfg, handle, app_password, dry_run, photo):
            state["last_posted_at"] = now.isoformat()
    except Exception:
        logger.exception("[%s] post cycle failed, will try again at the next slot", account)
    if not dry_run:
        state["last_attempt_at"] = now.isoformat()
        save_state(account, state)


def worker_loop(account: str, dry_run: bool, once: bool, stop_event: threading.Event,
                photo: str = None) -> None:
    handle, app_password = config.credentials(account)
    if not dry_run and (not handle or not app_password):
        logger.error("[%s] missing %s_BSKY_HANDLE / %s_BSKY_APP_PASSWORD in .local.env",
                     account, account.upper(), account.upper())
        return

    if once:
        attempt(account, current_feeds()[account], handle, app_password, dry_run, photo)
        return

    cfg = current_feeds()[account]
    logger.info("[%s] starting -- posts at %s, next post %s",
                account, describe_schedule(cfg), describe_next(load_state(account), cfg))

    was_disabled = False
    schedule = describe_schedule(cfg)
    missed_logged = None
    while not stop_event.is_set():
        cfg = current_feeds().get(account)
        if not cfg or not cfg["enabled"]:
            if not was_disabled:
                logger.info("[%s] disabled in feeds.json -- idle until re-enabled", account)
            was_disabled = True
        else:
            was_disabled = False
            if describe_schedule(cfg) != schedule:
                schedule = describe_schedule(cfg)
                logger.info("[%s] schedule is now %s", account, schedule)
            now = datetime.datetime.now(cfg["tz"])
            state = load_state(account)
            if due_slot(state, cfg, now):
                attempt(account, cfg, handle, app_password, dry_run)
            else:
                latest = config.latest_slot(cfg, now)
                if last_attempt(state) < latest and latest != missed_logged:
                    logger.warning("[%s] missed the %s slot (more than %s ago) -- next post %s",
                                   account, f"{latest:%Y-%m-%d %H:%M}", LATE_GRACE,
                                   f"{config.next_slot(cfg, now):%H:%M}")
                    missed_logged = latest
        stop_event.wait(POLL_SECONDS)


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------

def check(accounts: list[str], feeds: dict) -> bool:
    ok = True
    location_tags = md.load_location_tags()
    print(f"feeds.json OK -- {len(feeds)} feed(s): "
          + ", ".join(f"{a} ({'enabled' if c['enabled'] else 'disabled'})" for a, c in feeds.items()))
    print(f"photos: {md.PHOTOS_ROOT}\nstate:  {STATE_DIR}\n")

    for account in accounts:
        cfg = feeds[account]
        print(f"== {account}")
        print(f"   hashtags: {' '.join(cfg['hashtags']) or '(none)'}")
        handle, app_password = config.credentials(account)
        if not handle or not app_password or app_password == PLACEHOLDER_PASSWORD:
            # Don't attempt it: Bluesky allows only 10 failed logins a day.
            print(f"   ✗ {account.upper()}_BSKY_HANDLE / _APP_PASSWORD not set in .local.env")
            ok = False
        else:
            try:
                from atproto import Client
                Client().login(handle, app_password)
                print(f"   ✓ login as {handle}")
            except Exception as exc:
                content = getattr(getattr(exc, "response", None), "content", None)
                print(f"   ✗ login as {handle} failed: {getattr(content, 'message', None) or exc}")
                ok = False

        if not md.curated_dir(account).exists():
            print(f"   ✗ no folder {md.curated_dir(account)}")
            ok = False
        entries, candidates, up_next = queue(account)
        groups = md.load_groups(account)
        units = post_units(candidates, groups)
        unit_entries = {first: md.effective_entry(first, entries, groups) for first in units}
        # Run every queued photo through the same prep as a real post.
        unpostable, reencoded = [], 0
        for f in candidates:
            try:
                reencoded += upload_prep.prepare(md.find_photo(account, f)[0]).method == "re-encoded"
            except Exception as exc:
                unpostable.append(f"{f} ({exc})")
        no_alt = [f for f in candidates if not entries[f].get("alt_text")]
        per_day = len(config.slot_times(cfg))
        print(f"   schedule: {describe_schedule(cfg)}")
        if cfg["order"] == "seasonal":
            today = datetime.datetime.now(cfg["tz"]).date()
            dists = [season_distance(e.get("date_taken"), today) for e in unit_entries.values()]
            in_season = sum(1 for d in dists if d is not None and d <= cfg["season_days"])
            undated = sum(1 for d in dists if d is None)
            print(f"   order: seasonal -- {in_season} posts taken within ±{cfg['season_days']} days of "
                  f"{today:%b %d} (any year)" + (f", {undated} undated (posted last)" if undated else ""))
        elif cfg["order"] == "on_this_day":
            today = datetime.datetime.now(cfg["tz"]).date()
            on_day = sum(1 for e in unit_entries.values() if season_distance(e.get("date_taken"), today) == 0)
            print(f"   order: on_this_day -- {on_day} posts taken on {today:%b %d} in an earlier year "
                  f"(posted first); otherwise random")
        else:
            print("   order: random")
        recent = recent_regions(account, entries, groups, cfg["region_cooldown"])
        if cfg["region_cooldown"]:
            print(f"   regions: none repeated within {cfg['region_cooldown']} posts while others are queued; "
                  f"waiting now: {', '.join(dict.fromkeys(recent)) or '(none)'}")
            by_region = collections.Counter(md.region(e.get("place")) or "(no place)" for e in unit_entries.values())
            print("   queued by region: " + ", ".join(f"{r} {n}" for r, n in by_region.most_common()))
        up_next_posts = sum(1 for first in units if first in up_next)
        grouped = sum(1 for photos in units.values() if len(photos) > 1)
        print(f"   queue: {len(units)} posts ready ({up_next_posts} up next, {len(units) - up_next_posts} curated"
              + (f"; {grouped} of them groups, {len(candidates)} photos in all" if grouped else "")
              + f"; ~{len(units) / per_day:.0f} days at {per_day}/day), {len(md.scan_posted(account))} photos posted")
        if unpostable:
            print(f"   ✗ can't be prepared for upload (will be skipped):\n      " + "\n      ".join(unpostable))
            ok = False
        else:
            print(f"   ✓ all {len(candidates)} upload clean: metadata stripped "
                  f"({len(candidates) - reencoded} lossless, {reencoded} rotated + re-encoded)")
        if no_alt:
            print(f"   ! no alt text: {', '.join(no_alt)}")
        print(f"   next post: {describe_next(load_state(account), cfg)}")
        if units:
            sample = order_candidates(list(units), unit_entries, cfg, datetime.datetime.now(cfg["tz"]).date(),
                                      recent, up_next)[0]
            text = md.compose_message(unit_entries[sample], cfg, location_tags)
            print(f"   a likely next post ({', '.join(units[sample])}, "
                  f"taken {unit_entries[sample].get('date_taken') or 'undated'}):\n      "
                  + text.replace("\n", "\n      "))
        print()
    print("All checks passed." if ok else "Some checks FAILED.")
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workers", default=None, help="Comma-separated feed names (default: all enabled in feeds.json)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true", help="Post one photo per feed now, then exit")
    parser.add_argument("--check", action="store_true", help="Validate config and logins, show queues, then exit")
    parser.add_argument("--photo", default=None, metavar="FILENAME",
                        help="With --once and one --workers feed: post this photo (from curated/ or up-next/) instead of picking one")
    args = parser.parse_args()

    log_path = config.app_path("LOG_PATH", "logs/poster.log")
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path))
    except OSError as exc:
        print(f"Warning: could not open log file {log_path}: {exc}", file=sys.stderr)
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO),
                        format="%(asctime)s %(levelname)-8s %(message)s", handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per Bluesky request otherwise

    feeds = current_feeds()
    if args.workers:
        selected = args.workers.split(",")
        unknown = [a for a in selected if a not in feeds]
        if unknown:
            parser.error(f"unknown feed(s) {unknown} -- feeds.json has {list(feeds)}")
        disabled = [a for a in selected if not feeds[a]["enabled"]]
        if disabled and not (args.check or args.once):
            parser.error(f"{disabled} disabled in feeds.json -- set \"enabled\": true to run the daemon for it")
        accounts = selected
    else:
        accounts = [a for a, c in feeds.items() if c["enabled"]]

    if args.photo and not (args.once and len(accounts) == 1):
        parser.error("--photo needs --once and exactly one feed in --workers")

    if args.check:
        sys.exit(0 if check(accounts, feeds) else 1)

    if not accounts:
        logger.error("No feeds enabled/selected -- check feeds.json and --workers")
        sys.exit(1)

    stop_event = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info("Received signal %d, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    threads = [
        threading.Thread(target=worker_loop, args=(account, args.dry_run, args.once, stop_event, args.photo),
                         name=f"worker-{account}", daemon=True)
        for account in accounts
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    logger.info("Stopped")


if __name__ == "__main__":
    main()
