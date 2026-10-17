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

import datetime
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
METADATA_DIR = config.app_path("METADATA_DIR", "..")  # where metadata_<Account>.json live
GEOCODE_CACHE_PATH = PROJECT_ROOT / "geocode_cache.json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "postcards-app/1.0"
GEOCODE_PRECISION = 4

# Every feed in feeds.json, enabled or not -- curation works on all of them.
ACCOUNTS = tuple(config.load_feeds())

# Every account photo folder has four subfolders; a photo moves
# holding-pen -> curated (or up-next) -> posted:
#   holding-pen/  not yet reviewed / not ready to post (default landing spot)
#   curated/      reviewed and marked ready -- poster.py posts from here...
#   up-next/      ...and from here first: hand-picked to go out before anything in curated/
#   posted/       already posted
HOLDING_SUBDIR = "holding-pen"
CURATED_SUBDIR = "curated"
UP_NEXT_SUBDIR = "up-next"
POSTED_SUBDIR = "posted"
STATUSES = (HOLDING_SUBDIR, CURATED_SUBDIR, UP_NEXT_SUBDIR, POSTED_SUBDIR)


def account_dir(account: str) -> Path:
    return PHOTOS_ROOT / account


def holding_dir(account: str) -> Path:
    return account_dir(account) / HOLDING_SUBDIR


def curated_dir(account: str) -> Path:
    return account_dir(account) / CURATED_SUBDIR


def up_next_dir(account: str) -> Path:
    return account_dir(account) / UP_NEXT_SUBDIR


def posted_dir(account: str) -> Path:
    return account_dir(account) / POSTED_SUBDIR


def metadata_path(account: str) -> Path:
    return METADATA_DIR / f"metadata_{account}.json"


def load_metadata(account: str) -> dict:
    path = metadata_path(account)
    return json.loads(path.read_text()) if path.exists() else {}


def save_metadata(account: str, data: dict) -> None:
    metadata_path(account).write_text(json.dumps(data, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Groups: 2-4 photos posted together as one Bluesky post. Kept in
# groups_<Account>.json next to the metadata file:
#     {"g-20261013-101500": {"photos": ["a.jpg", "b.jpg"], "message": ..., "place": ...}}
# The group's message and place are used for the post instead of any one
# photo's; each photo keeps its own alt text. A group's photos always share
# a status folder -- curate.py moves them together.
# ---------------------------------------------------------------------------

MIN_GROUP_PHOTOS = 2  # the most is the feed's platform limit: cfg["limits"]["max_photos"]


def groups_path(account: str) -> Path:
    return METADATA_DIR / f"groups_{account}.json"


def load_groups(account: str) -> dict:
    path = groups_path(account)
    return json.loads(path.read_text()) if path.exists() else {}


def save_groups(account: str, groups: dict) -> None:
    groups_path(account).write_text(json.dumps(groups, indent=2, sort_keys=True))


def group_of(groups: dict, filename: str) -> str | None:
    """The id of the group this photo belongs to, if any."""
    return next((gid for gid, g in groups.items() if filename in g["photos"]), None)


def group_entry(group: dict, entries: dict) -> dict:
    """The group as a single metadata-like entry, for compose_message and
    the poster's ordering: its own message and place, and the earliest date
    any of its photos was taken."""
    dates = sorted(d for d in (entries.get(f, {}).get("date_taken") for f in group["photos"]) if d)
    return {"message": group.get("message") or "", "place": group.get("place") or "",
            "date_taken": dates[0] if dates else None}


def effective_entry(filename: str, entries: dict, groups: dict) -> dict:
    """What a post of this photo is built from: its group's entry if it's in
    one, else its own."""
    gid = group_of(groups, filename)
    return group_entry(groups[gid], entries) if gid else entries.get(filename, {})


def remove_from_group(account: str, filename: str) -> None:
    """Takes a photo out of its group; a group left with one photo is
    dissolved (that photo just becomes a single again)."""
    groups = load_groups(account)
    gid = group_of(groups, filename)
    if gid is None:
        return
    groups[gid]["photos"].remove(filename)
    if len(groups[gid]["photos"]) < MIN_GROUP_PHOTOS:
        replace_in_up_next(account, [gid], groups.pop(gid)["photos"])
    save_groups(account, groups)


# ---------------------------------------------------------------------------
# Up next order: the curator's posting order for up-next/, top first, kept in
# up_next_<Account>.json as a list of keys -- a group's id, or a single
# photo's filename. Only curate.py writes it; poster.py reads it. Keys that
# aren't in up-next/ any more are ignored, and up-next photos missing from it
# go after the listed ones.
# ---------------------------------------------------------------------------

def up_next_order_path(account: str) -> Path:
    return METADATA_DIR / f"up_next_{account}.json"


def load_up_next_order(account: str) -> list[str]:
    path = up_next_order_path(account)
    return json.loads(path.read_text()) if path.exists() else []


def save_up_next_order(account: str, order: list[str]) -> None:
    up_next_order_path(account).write_text(json.dumps(order, indent=2))


def replace_in_up_next(account: str, old_keys: list[str], new_keys: list[str]) -> None:
    """Puts new_keys where the first of old_keys stood in the up-next order
    (e.g. a new group takes its photos' place, an ungrouped group's photos
    take the group's), dropping the rest of old_keys. No-op if none of
    old_keys is in the order."""
    order = load_up_next_order(account)
    positions = [order.index(k) for k in old_keys if k in order]
    if not positions:
        return
    at = min(positions)
    head = [k for k in order[:at] if k not in old_keys and k not in new_keys]
    tail = [k for k in order[at:] if k not in old_keys and k not in new_keys]
    save_up_next_order(account, head + list(new_keys) + tail)


def unit_key(filename: str, groups: dict) -> str:
    """What a photo is posted as: its group's id, or itself."""
    return group_of(groups, filename) or filename


def up_next_queue(account: str, groups: dict = None) -> list[str]:
    """The keys (see unit_key) of everything in up-next/, in posting order."""
    groups = load_groups(account) if groups is None else groups
    present = list(dict.fromkeys(unit_key(f, groups) for f in scan_up_next(account)))
    order = [k for k in load_up_next_order(account) if k in present]
    return order + [k for k in present if k not in order]


def _scan(d: Path) -> list[str]:
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg"))


def scan_holding(account: str) -> list[str]:
    return _scan(holding_dir(account))


def scan_curated(account: str) -> list[str]:
    return _scan(curated_dir(account))


def scan_up_next(account: str) -> list[str]:
    return _scan(up_next_dir(account))


def scan_posted(account: str) -> list[str]:
    return _scan(posted_dir(account))


_STATUS_DIR_FNS = {HOLDING_SUBDIR: holding_dir, CURATED_SUBDIR: curated_dir,
                   UP_NEXT_SUBDIR: up_next_dir, POSTED_SUBDIR: posted_dir}


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
    remove_from_group(account, filename)


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
    remove_from_group(account, filename)

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


EXIF_DATETIME_ORIGINAL = 36867   # when the shutter fired (Exif sub-IFD)
EXIF_DATETIME_DIGITIZED = 36868
EXIF_DATETIME = 306              # when the file was last changed; usually, not always, the same


def extract_date_taken(path: Path) -> str | None:
    """Capture date as "YYYY-MM-DD", preferring the capture timestamp over
    the file-modified one (which an editing app can change, or omit)."""
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            sub = exif.get_ifd(ExifTags.IFD.Exif)
            raw = sub.get(EXIF_DATETIME_ORIGINAL) or sub.get(EXIF_DATETIME_DIGITIZED) or exif.get(EXIF_DATETIME)
    except Exception:
        return None
    if not raw or not isinstance(raw, str):
        return None
    date_part = raw.strip().split(" ")[0].replace(":", "-")  # "2024:12:24 10:01:02" -> "2024-12-24"
    return date_part if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_part) and date_part != "0000-00-00" else None


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


def place_component_tags(place: str, location_tags: dict = None) -> list[str]:
    """One hashtag per comma-separated component of place, in the place's
    own order -- most specific first, out to the state/country:
    "Boulder County, Colorado" -> ["#BoulderCounty", "#Colorado"].
    A bare 2-letter US state abbreviation is normalized to its full name
    first, so "Longmont, CO" and "Longmont, Colorado" both tag #Colorado.

    A component matching a location_tags.json key (case-insensitive
    substring) gets that entry's tags instead, in the same position:
    with {"McIntosh": ["#McIntosh"]}, "McIntosh Reservoir, Longmont, CO"
    -> ["#McIntosh", "#Longmont", "#Colorado"]. An empty list drops it."""
    tags = []
    for part in place.split(","):
        part = re.sub(r"^near\s+", "", part.strip(), flags=re.IGNORECASE)  # "Near San Luis Obispo"
        if not part:
            continue
        part = US_STATE_ABBREVIATIONS.get(part.upper(), part)
        overrides = [t for key, key_tags in (location_tags or {}).items()
                     if key.lower() in part.lower() for t in key_tags]
        matched = any(key.lower() in part.lower() for key in (location_tags or {}))
        for tag in overrides if matched else [to_hashtag(part)]:
            if tag and tag not in tags:
                tags.append(tag)
    return tags


def region(place: str | None) -> str | None:
    """The broad area a place is in -- its last comma-separated part (state
    or country), with US state abbreviations expanded: "Near San Luis
    Obispo, CA" and "Montaña de Oro State Park, California" are both
    "California". None for no place."""
    last = (place or "").split(",")[-1].strip()
    return US_STATE_ABBREVIATIONS.get(last.upper(), last) or None


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
    """{"text to match in one part of a place (case-insensitive)": ["#Tag", ...]}
    Replaces the auto-generated tag for the matching part of the place --
    see place_component_tags. Hand-editable; applies to the next post."""
    data = json.loads(LOCATION_TAGS_PATH.read_text()) if LOCATION_TAGS_PATH.exists() else {}
    for key, tags in data.items():
        if not (isinstance(tags, list) and all(isinstance(t, str) and config.HASHTAG_RE.fullmatch(t) for t in tags)):
            raise ValueError(f"location_tags.json: {key!r} must map to a list like [\"#Tag\"], got {tags!r}")
    return data


def already_mentions_place(text: str, place: str) -> bool:
    """True only if the specific locality (place's first comma-separated
    part, e.g. "Highland" in "Highland, Colorado") is already mentioned --
    not just the state/country, which is generic enough that almost every
    AI-generated description already names it regardless of locality."""
    locality = place.split(",")[0].strip().lower()
    return bool(locality) and locality in (text or "").lower()


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

# A post's length limit is the feed's platform's: cfg["limits"]["max_chars"].
# Bluesky counts graphemes and Instagram characters; len() is never smaller
# than either, so measuring with it is safe.


def post_hashtags(entry: dict, feed_cfg: dict, location_tags: dict) -> tuple[list[str], list[str]]:
    """(location tags, feed tags). Location = one per place component, most
    specific first, with location_tags.json substitutions; feed = feeds.json
    hashtags. Tags already typed
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
    location = fresh(place_component_tags(place, location_tags)) if place else []
    return location, fresh(feed_cfg["hashtags"])


def date_line(entry: dict, feed_cfg: dict) -> str:
    """e.g. "Photo taken 2020-09-13", per the feed's date_format; "" if the
    feed has date_format null or the photo has no date."""
    fmt, taken = feed_cfg.get("date_format"), entry.get("date_taken")
    if not fmt or not taken:
        return ""
    try:
        return datetime.date.fromisoformat(taken).strftime(fmt)
    except ValueError:
        return ""


def compose_message(entry: dict, feed_cfg: dict, location_tags: dict) -> str:
    """The caption as typed, then a blank line, the location hashtags and the
    feed's hashtags, then a blank line and the date line. Over the feed's
    platform limits (length, and number of hashtags where there is one),
    location tags are dropped first (from the end), then feed tags; the
    caption itself is only cut if it's too long on its own."""
    limits = feed_cfg["limits"]
    max_chars, max_tags = limits["max_chars"], limits["max_hashtags"]
    caption = (entry.get("message") or "").strip()
    location, feed = post_hashtags(entry, feed_cfg, location_tags)
    dated = date_line(entry, feed_cfg)
    caption_tags = len(config.HASHTAG_RE.findall(caption))

    def join():
        tail = "\n\n".join(x for x in (" ".join(location + feed), dated) if x)
        return f"{caption}\n\n{tail}" if caption and tail else (caption or tail)

    def too_many_tags():
        return max_tags is not None and caption_tags + len(location) + len(feed) > max_tags

    while (location or feed) and (len(join()) > max_chars or too_many_tags()):
        (location or feed).pop()
    text = join()
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"
