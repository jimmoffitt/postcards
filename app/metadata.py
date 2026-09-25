"""Shared photo-metadata helpers used by both curate.py and poster.py.

Each account has its own metadata_<Account>.json at the project root, keyed
by bare filename:
    {"IMG_1234.jpg": {"alt_text": ..., "message": ..., "lat": ..., "lon": ...,
                       "place": ..., "date_taken": ...}, ...}

"message" holds only what the curator typed (a caption and/or extra
hashtags) -- often empty. The feed's hashtags (feeds.json) and the
place-derived ones are added at post time by compose_message, so changing
a feed's hashtags never means rewriting these files.

A photo's "posted" state isn't stored in metadata -- it's implied by
whether the file still lives directly in the account folder or has been
moved into that folder's posted/ subfolder (see move_to_posted below).
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import ExifTags, Image

import config

config.load_dotenv()  # before PHOTOS_ROOT is read, however this module is entered

APP_DIR = config.APP_DIR
PROJECT_ROOT = APP_DIR.parent
PHOTOS_ROOT = config.app_path("PHOTOS_ROOT", "../photos")
GEOCODE_CACHE_PATH = PROJECT_ROOT / "geocode_cache.json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "postcards-app/1.0"
GEOCODE_PRECISION = 4

# Every feed in feeds.json, enabled or not -- curation works on all of them.
ACCOUNTS = tuple(config.load_feeds())

# Every account photo folder has three subfolders, and a photo moves
# holding-pen -> curated -> posted, in order, and never skips a step:
#   holding-pen/  not yet reviewed / not ready to post (default landing spot)
#   curated/      reviewed and marked ready -- poster.py only posts from here
#   posted/       already posted
HOLDING_SUBDIR = "holding-pen"
CURATED_SUBDIR = "curated"
POSTED_SUBDIR = "posted"
STATUSES = (HOLDING_SUBDIR, CURATED_SUBDIR, POSTED_SUBDIR)


def account_dir(account: str) -> Path:
    return PHOTOS_ROOT / account


def holding_dir(account: str) -> Path:
    return account_dir(account) / HOLDING_SUBDIR


def curated_dir(account: str) -> Path:
    return account_dir(account) / CURATED_SUBDIR


def posted_dir(account: str) -> Path:
    return account_dir(account) / POSTED_SUBDIR


def metadata_path(account: str) -> Path:
    return PROJECT_ROOT / f"metadata_{account}.json"


def load_metadata(account: str) -> dict:
    path = metadata_path(account)
    return json.loads(path.read_text()) if path.exists() else {}


def save_metadata(account: str, data: dict) -> None:
    metadata_path(account).write_text(json.dumps(data, indent=2, sort_keys=True))


def _scan(d: Path) -> list[str]:
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg"))


def scan_holding(account: str) -> list[str]:
    return _scan(holding_dir(account))


def scan_curated(account: str) -> list[str]:
    return _scan(curated_dir(account))


def scan_posted(account: str) -> list[str]:
    return _scan(posted_dir(account))


_STATUS_DIR_FNS = {HOLDING_SUBDIR: holding_dir, CURATED_SUBDIR: curated_dir, POSTED_SUBDIR: posted_dir}


def find_photo(account: str, filename: str) -> tuple[Path, str] | tuple[None, None]:
    """Returns (path, status) for wherever this photo currently lives."""
    for status, dir_fn in _STATUS_DIR_FNS.items():
        p = dir_fn(account) / filename
        if p.exists():
            return p, status
    return None, None


def move_photo(account: str, filename: str, to_status: str) -> Path:
    if to_status not in _STATUS_DIR_FNS:
        raise ValueError(f"unknown status {to_status!r}")
    src, _ = find_photo(account, filename)
    if src is None:
        raise FileNotFoundError(f"{account}/{filename} not found in any status folder")
    dest = _STATUS_DIR_FNS[to_status](account) / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dest)
    return dest


def move_to_curated(account: str, filename: str) -> Path:
    return move_photo(account, filename, CURATED_SUBDIR)


def move_to_holding(account: str, filename: str) -> Path:
    return move_photo(account, filename, HOLDING_SUBDIR)


def move_to_posted(account: str, filename: str) -> Path:
    return move_photo(account, filename, POSTED_SUBDIR)


def delete_photo(account: str, filename: str) -> None:
    """Permanently deletes the file (wherever it currently sits) and its
    metadata entry. Only ever called for an explicit, single-photo action
    from curate.py -- never part of any bulk/automated flow."""
    path, _status = find_photo(account, filename)
    if path is None:
        raise FileNotFoundError(f"{account}/{filename} not found in any status folder")
    path.unlink()
    entries = load_metadata(account)
    entries.pop(filename, None)
    save_metadata(account, entries)


def move_to_account(account: str, filename: str, to_account: str) -> Path:
    """Moves a photo (file + metadata entry) to a different account,
    e.g. a photo curated under the wrong account. Always lands in the
    destination's holding-pen -- it needs a fresh look there (the message's
    account hashtag no longer matches, location tags may not apply the
    same way, etc.) rather than skipping straight to curated/posted."""
    if to_account not in ACCOUNTS:
        raise ValueError(f"unknown account {to_account!r}")
    if to_account == account:
        raise ValueError("source and destination account are the same")

    src_path, _status = find_photo(account, filename)
    if src_path is None:
        raise FileNotFoundError(f"{account}/{filename} not found in any status folder")

    dest_path, dest_status = find_photo(to_account, filename)
    if dest_path is not None:
        raise FileExistsError(f"{filename} already exists in {to_account} ({dest_status})")

    src_entries = load_metadata(account)
    entry = src_entries.get(filename, {})

    dest = holding_dir(to_account) / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_path.rename(dest)

    # The destination feed adds its own hashtags at post time; drop any of
    # the source feed's that were typed into the caption.
    old_tags = {t.lower() for t in config.load_feeds()[account]["hashtags"]}
    message = entry.get("message") or ""
    entry["message"] = " ".join(tok for tok in message.split(" ") if tok.lower() not in old_tags).strip()

    src_entries.pop(filename, None)
    save_metadata(account, src_entries)

    dest_entries = load_metadata(to_account)
    dest_entries[filename] = entry
    save_metadata(to_account, dest_entries)

    return dest


# ---------------------------------------------------------------------------
# EXIF extraction (GPS + capture date) -- read fresh from the file itself,
# so it's always correct regardless of how metadata_<Account>.json was built.
# ---------------------------------------------------------------------------

def extract_gps(path: Path) -> tuple[float, float] | None:
    try:
        img = Image.open(path)
        gps = img.getexif().get_ifd(ExifTags.IFD.GPSInfo)
    except Exception:
        return None
    if not gps or not all(k in gps for k in (1, 2, 3, 4)):
        return None

    def dms_to_dd(dms, ref):
        d, m, s = dms
        dd = float(d) + float(m) / 60 + float(s) / 3600
        return -dd if ref in ("S", "W") else dd

    try:
        lat = dms_to_dd(gps[2], gps[1])
        lon = dms_to_dd(gps[4], gps[3])
    except Exception:
        return None
    return round(lat, GEOCODE_PRECISION), round(lon, GEOCODE_PRECISION)


def extract_date_taken(path: Path) -> str | None:
    try:
        img = Image.open(path)
        exif = img.getexif()
        raw = exif.get(306)  # DateTime
    except Exception:
        return None
    if not raw:
        return None
    try:
        date_part = raw.split(" ")[0]  # "2024:12:24"
        return date_part.replace(":", "-")  # "2024-12-24"
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Reverse geocoding (Nominatim, cached) -- same approach as build_post_content.py
# ---------------------------------------------------------------------------

def _load_geocode_cache() -> dict:
    return json.loads(GEOCODE_CACHE_PATH.read_text()) if GEOCODE_CACHE_PATH.exists() else {}


def _save_geocode_cache(cache: dict) -> None:
    GEOCODE_CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def _fetch_address(lat: float, lon: float) -> dict | None:
    params = urllib.parse.urlencode(
        {"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 18, "addressdetails": 1}
    )
    req = urllib.request.Request(f"{NOMINATIM_URL}?{params}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def format_place(address: dict) -> str | None:
    locality = (
        address.get("city") or address.get("town") or address.get("village")
        or address.get("hamlet") or address.get("suburb") or address.get("county")
    )
    region = address.get("state")
    country = address.get("country")
    parts = []
    for p in (locality, region, country):
        if p and p not in parts:
            parts.append(p)
        if len(parts) == 2:
            break
    return ", ".join(parts) if parts else None


def to_hashtag(text: str) -> str | None:
    """"Montaña de Oro State Park" -> "#MontanaDeOroStatePark". Accents are
    folded to plain letters rather than dropped (which split words apart)."""
    folded = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    words = re.findall(r"[A-Za-z0-9]+", folded)
    return "#" + "".join((w[:1].upper() + w[1:]) for w in words) if words else None


US_STATE_ABBREVIATIONS = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


def place_component_tags(place: str) -> list[str]:
    """One hashtag per comma-separated component of place, e.g.
    "Boulder County, Colorado" -> ["#BoulderCounty", "#Colorado"].
    A bare 2-letter US state abbreviation is normalized to its full name
    first, so "Longmont, CO" and "Longmont, Colorado" both tag #Colorado."""
    tags = []
    for part in place.split(","):
        part = re.sub(r"^near\s+", "", part.strip(), flags=re.IGNORECASE)  # "Near San Luis Obispo"
        if not part:
            continue
        part = US_STATE_ABBREVIATIONS.get(part.upper(), part)
        tag = to_hashtag(part)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def reverse_geocode(lat: float, lon: float) -> str | None:
    """Returns a place name for a lat/lon, using and updating the shared cache."""
    cache = _load_geocode_cache()
    key = f"{lat},{lon}"
    entry = cache.get(key)
    if not (isinstance(entry, dict) and "address" in entry):
        data = _fetch_address(lat, lon)
        time.sleep(1.1)  # Nominatim usage policy: max 1 request/sec
        entry = {"display_name": data.get("display_name") if data else None,
                  "address": data.get("address", {}) if data else {}}
        cache[key] = entry
        _save_geocode_cache(cache)

    address = entry.get("address", {})
    return format_place(address) or entry.get("display_name")


# ---------------------------------------------------------------------------
# Location -> alt text / hashtag sync. Rerunnable and idempotent: safe to
# call again any time more photos get a place set, only touches entries
# that need it.
# ---------------------------------------------------------------------------

LOCATION_TAGS_PATH = PROJECT_ROOT / "location_tags.json"


def load_location_tags() -> dict:
    """{"substring to match in place (case-insensitive)": ["#Tag", ...]}
    Hand-editable -- add a new key any time you want more places to pick up
    extra hashtags on the next sync."""
    return json.loads(LOCATION_TAGS_PATH.read_text()) if LOCATION_TAGS_PATH.exists() else {}


def already_mentions_place(text: str, place: str) -> bool:
    """True only if the specific locality (place's first comma-separated
    part, e.g. "Highland" in "Highland, Colorado") is already mentioned --
    not just the state/country, which is generic enough that almost every
    AI-generated description already names it regardless of locality."""
    locality = place.split(",")[0].strip().lower()
    return bool(locality) and locality in (text or "").lower()


def tags_for_place(place: str, location_tags: dict) -> list[str]:
    place_lower = place.lower()
    tags = []
    for substring, extra_tags in location_tags.items():
        if substring.lower() in place_lower:
            for tag in extra_tags:
                if tag not in tags:
                    tags.append(tag)
    return tags


def sync_locations(account: str) -> dict:
    """For every entry with a place: append '<place>.' to alt_text if the
    locality isn't already mentioned (never removes text there). Hashtags
    aren't stored, so there's nothing to sync for them -- compose_message
    derives them from the current place at post time."""
    entries = load_metadata(account)
    alt_updated = 0
    checked = 0
    for entry in entries.values():
        place = entry.get("place")
        if not place:
            continue
        checked += 1
        alt_text = entry.get("alt_text", "")
        if alt_text and not already_mentions_place(alt_text, place):
            entry["alt_text"] = f"{alt_text} {place}."
            alt_updated += 1
    save_metadata(account, entries)
    return {"checked": checked, "alt_text_updated": alt_updated}


# ---------------------------------------------------------------------------
# Post text, built fresh at post time (and for the curation preview).
# ---------------------------------------------------------------------------

MAX_POST_CHARS = 300  # Bluesky counts graphemes; len() is never smaller, so this is safe


def post_hashtags(entry: dict, feed_cfg: dict, location_tags: dict) -> tuple[list[str], list[str]]:
    """(location tags, feed tags). Location = one per place component, then
    location_tags.json extras; feed = feeds.json hashtags. Tags already typed
    into the caption, or repeated, are dropped (case-insensitive)."""
    seen = {t.lower() for t in config.HASHTAG_RE.findall(entry.get("message") or "")}

    def fresh(candidates):
        out = []
        for t in candidates:
            if t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out

    place = entry.get("place") or ""
    location = fresh(place_component_tags(place) + tags_for_place(place, location_tags)) if place else []
    return location, fresh(feed_cfg["hashtags"])


def compose_message(entry: dict, feed_cfg: dict, location_tags: dict) -> str:
    """The caption as typed, then a blank line, the location hashtags, and the
    feed's hashtags. Over the length limit, location tags are dropped first
    (from the end), then feed tags; the caption itself is only cut if it's
    too long on its own."""
    caption = (entry.get("message") or "").strip()
    location, feed = post_hashtags(entry, feed_cfg, location_tags)

    def join():
        line = " ".join(location + feed)
        return f"{caption}\n\n{line}" if caption and line else (caption or line)

    while (location or feed) and len(join()) > MAX_POST_CHARS:
        (location or feed).pop()
    text = join()
    return text if len(text) <= MAX_POST_CHARS else text[: MAX_POST_CHARS - 1] + "…"
