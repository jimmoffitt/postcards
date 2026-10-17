#!/usr/bin/env python3
"""Local curation web app: browse photos, edit alt text/message/geo, save.

Run locally (Mac + VS Code, or the Pi) with:
    python3 curate.py
then open http://127.0.0.1:5001 (host/port configurable via .local.env).

Read-only over the image files themselves; all edits are saved to
metadata_<Account>.json at the project root via metadata.py.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_file, abort

import config
import metadata as md

config.load_dotenv()

app = Flask(__name__)


def _photo_path(account: str, filename: str) -> Path | None:
    path, _status = md.find_photo(account, filename)
    return path


@app.get("/")
def index():
    return send_file(Path(__file__).resolve().parent / "static" / "curate.html")


def _preview(account: str, entry: dict, feeds: dict = None, location_tags: dict = None) -> str:
    """Exactly what poster.py would post for this entry right now."""
    feeds = feeds or config.load_feeds()
    location_tags = location_tags if location_tags is not None else md.load_location_tags()
    return md.compose_message(entry, feeds[account], location_tags)


@app.get("/api/accounts")
def api_accounts():
    return jsonify(sorted(md.ACCOUNTS))


def _describe_interval(hours: float) -> str:
    minutes = round(hours * 60)
    if minutes % 60:
        return f"{minutes} minutes" if minutes < 60 else f"{minutes / 60:g} hours"
    return "hour" if minutes == 60 else f"{minutes // 60} hours"


def _count_posts(filenames: list[str], groups: dict) -> int:
    """How many posts these photos make: a group's photos are one post."""
    return len({md.group_of(groups, f) or f for f in filenames})


@app.get("/api/stream/<account>")
def api_stream(account):
    """The feed's posting schedule (from feeds.json, read fresh) and how many
    days of photos are left at that rate, for the banner."""
    if account not in md.ACCOUNTS:
        abort(404)
    cfg = config.load_feeds()[account]
    slots = config.slot_times(cfg)
    per_day = len(slots)
    groups = md.load_groups(account)
    up_next = _count_posts(md.scan_up_next(account), groups)
    curated = up_next + _count_posts(md.scan_curated(account), groups)
    holding = _count_posts(md.scan_holding(account), groups)
    every = _describe_interval(cfg["interval_hours"])
    if cfg["window_enabled"]:
        when = f"every {every} between {cfg['window_start']:02d}:00 and {cfg['window_end']:02d}:00"
    else:
        when = f"every {every}, around the clock"
    remaining = (f"{curated / per_day:.1f} days remain in the \u2018Curated\u2019 and \u2018Up next\u2019 folders and "
                 f"{holding / per_day:.1f} days in the \u2018Holding pen\u2019 folder")
    if up_next:
        remaining += f" ({up_next} up next, posted first)"
    if cfg["enabled"]:
        summary = f"{account} posts {when}. At this rate, {remaining}."
    else:
        summary = (f"{account} is paused (\"enabled\": false in feeds.json). "
                   f"When enabled, it posts {when}; at that rate, {remaining}.")
    return jsonify({
        "summary": summary,
        "enabled": cfg["enabled"],
        "platform": cfg["platform"],
        "limits": cfg["limits"],  # photos per post, length, hashtags: for grouping and the preview
        "posts_per_day": per_day,
        "slots": [t.strftime("%H:%M") for t in slots],
        "timezone": cfg["timezone"],
        "curated": curated, "up_next": up_next, "holding_pen": holding,
        "curated_days": round(curated / per_day, 1), "holding_pen_days": round(holding / per_day, 1),
    })


@app.get("/api/places/<account>")
def api_places(account):
    """Distinct place values already used in this account, for the Place
    field's suggestion list -- deliberately NOT the browser's own
    autocomplete (which can silently pre-fill a field with a remembered
    value with no explicit user action, and our auto-save-on-change would
    then commit that over whatever was really there)."""
    if account not in md.ACCOUNTS:
        abort(404)
    entries = md.load_metadata(account)
    places = sorted({v["place"] for v in entries.values() if v.get("place")})
    return jsonify(places)


@app.get("/api/photos/<account>")
def api_list_photos(account):
    if account not in md.ACCOUNTS:
        abort(404)

    status = request.args.get("status", md.HOLDING_SUBDIR)  # holding-pen | curated | up-next | posted | all
    q = request.args.get("q", "").strip().lower()
    has_geo = request.args.get("has_geo", "")  # "true" | "false" | ""
    has_alt = request.args.get("has_alt", "")  # "true" | "false" | ""
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")

    entries = md.load_metadata(account)
    groups = md.load_groups(account)
    feeds = config.load_feeds()
    location_tags = md.load_location_tags()
    by_status = {
        md.HOLDING_SUBDIR: set(md.scan_holding(account)),
        md.CURATED_SUBDIR: set(md.scan_curated(account)),
        md.UP_NEXT_SUBDIR: set(md.scan_up_next(account)),
        md.POSTED_SUBDIR: set(md.scan_posted(account)),
    }

    if status == "all":
        candidates = {f: s for s, fs in by_status.items() for f in fs}
    elif status in by_status:
        candidates = {f: status for f in by_status[status]}
    else:
        abort(400, f"unknown status {status!r}")

    def photo_item(filename):
        meta = entries.get(filename, {})
        return {
            "kind": "photo",
            "filename": filename,
            "status": candidates[filename],
            **{k: meta.get(k) for k in ("alt_text", "message", "lat", "lon", "place", "date_taken")},
            "post_preview": _preview(account, meta, feeds, location_tags),
        }

    # A group is listed once, where its first matching photo would be, if
    # any of its photos matches the filters.
    results, listed_groups = [], set()
    for filename in sorted(candidates):
        meta = entries.get(filename, {})
        if q and q not in filename.lower() and q not in (meta.get("alt_text") or "").lower():
            continue
        if has_geo == "true" and not meta.get("place") and meta.get("lat") is None:
            continue
        if has_geo == "false" and (meta.get("place") or meta.get("lat") is not None):
            continue
        if has_alt == "true" and not meta.get("alt_text"):
            continue
        if has_alt == "false" and meta.get("alt_text"):
            continue
        date_taken = meta.get("date_taken") or ""
        if date_from and date_taken and date_taken < date_from:
            continue
        if date_to and date_taken and date_taken > date_to:
            continue
        gid = md.group_of(groups, filename)
        if gid is None:
            results.append(photo_item(filename))
        elif gid not in listed_groups:
            listed_groups.add(gid)
            results.append(_group_item(account, gid, groups, entries, feeds, location_tags))
    if status == md.UP_NEXT_SUBDIR:
        queue = md.up_next_queue(account, groups)
        for item in results:
            item["queue_pos"] = queue.index(_item_key(item)) + 1
        results.sort(key=lambda item: item["queue_pos"])
    return jsonify(results)


def _item_key(item: dict) -> str:
    return item["id"] if item["kind"] == "group" else item["filename"]


def _group_item(account, gid, groups, entries, feeds=None, location_tags=None) -> dict:
    group = groups[gid]
    photos = []
    for f in group["photos"]:
        _path, status = md.find_photo(account, f)
        meta = entries.get(f, {})
        photos.append({"filename": f, "status": status,
                       **{k: meta.get(k) for k in ("alt_text", "place", "date_taken")}})
    entry = md.group_entry(group, entries)
    return {
        "kind": "group",
        "id": gid,
        "status": photos[0]["status"],
        "photos": photos,
        "message": group.get("message") or "",
        "place": group.get("place") or "",
        "date_taken": entry["date_taken"],
        "post_preview": _preview(account, entry, feeds, location_tags),
    }


@app.post("/api/groups/<account>")
def api_create_group(account):
    """Group 2 or more (up to the feed's platform limit: 4 on Bluesky, 10
    on Instagram) unposted, ungrouped photos into one post. They keep their
    status if they share one; otherwise they all go to the holding pen. The
    group starts with the first message and place set on any of them."""
    if account not in md.ACCOUNTS:
        abort(404)
    filenames = request.get_json(force=True).get("photos") or []
    limits = config.load_feeds()[account]["limits"]
    if not (md.MIN_GROUP_PHOTOS <= len(filenames) <= limits["max_photos"]) or len(set(filenames)) != len(filenames):
        abort(400, f"a {limits['name']} group needs {md.MIN_GROUP_PHOTOS} to {limits['max_photos']} different photos")
    groups = md.load_groups(account)
    statuses = set()
    for f in filenames:
        _path, status = md.find_photo(account, f)
        if status is None:
            abort(404, f"{f} not found")
        if status == md.POSTED_SUBDIR:
            abort(400, f"{f} was already posted")
        if md.group_of(groups, f):
            abort(409, f"{f} is already in a group -- ungroup it first")
        statuses.add(status)
    if len(statuses) > 1:
        for f in filenames:
            md.move_photo(account, f, md.HOLDING_SUBDIR)

    entries = md.load_metadata(account)
    first = lambda key: next((entries.get(f, {}).get(key) for f in filenames if entries.get(f, {}).get(key)), "")
    gid = f"g-{uuid.uuid4().hex[:8]}"
    groups[gid] = {"photos": filenames, "message": first("message"), "place": first("place")}
    md.save_groups(account, groups)
    # In up-next, the group takes the place of its highest photo; anywhere
    # else, its photos just leave the order.
    md.replace_in_up_next(account, filenames, [gid] if statuses == {md.UP_NEXT_SUBDIR} else [])
    return jsonify(_group_item(account, gid, groups, entries))


@app.put("/api/groups/<account>/<gid>")
def api_update_group(account, gid):
    """Edit a group's message and place, or reorder or drop its photos
    ("photos": a reordering of some of its current photos). Left with fewer
    than 2 photos, the group is dissolved."""
    if account not in md.ACCOUNTS:
        abort(404)
    groups = md.load_groups(account)
    group = groups.get(gid)
    if group is None:
        abort(404)
    body = request.get_json(force=True)
    for key in ("message", "place"):
        if key in body:
            group[key] = body[key] or ""
    if "photos" in body:
        photos = body["photos"]
        if len(set(photos)) != len(photos) or not set(photos) <= set(group["photos"]):
            abort(400, "photos must be some of the group's current photos")
        group["photos"] = photos
        if len(photos) < md.MIN_GROUP_PHOTOS:
            del groups[gid]
            md.save_groups(account, groups)
            md.replace_in_up_next(account, [gid], photos)
            return jsonify({"dissolved": gid})
    md.save_groups(account, groups)
    return jsonify(_group_item(account, gid, groups, md.load_metadata(account)))


@app.delete("/api/groups/<account>/<gid>")
def api_ungroup(account, gid):
    """Ungroup: the photos become singles again, where they are. Deletes no photos."""
    if account not in md.ACCOUNTS:
        abort(404)
    groups = md.load_groups(account)
    group = groups.pop(gid, None)
    if group is None:
        abort(404)
    md.save_groups(account, groups)
    md.replace_in_up_next(account, [gid], group["photos"])
    return jsonify({"ungrouped": gid})


@app.put("/api/up-next/<account>")
def api_move_in_up_next(account):
    """Move one item (a photo's filename or a group's id) within the up-next
    order: {"key": k, "before": other} or {"after": other}, or "to": "top" /
    "bottom". Relative moves keep working while the list is filtered."""
    if account not in md.ACCOUNTS:
        abort(404)
    body = request.get_json(force=True)
    queue = md.up_next_queue(account)
    key = body.get("key")
    if key not in queue:
        abort(404, f"{key!r} isn't in up-next")
    queue.remove(key)
    if body.get("to") == "top":
        queue.insert(0, key)
    elif body.get("to") == "bottom":
        queue.append(key)
    elif body.get("before") in queue:
        queue.insert(queue.index(body["before"]), key)
    elif body.get("after") in queue:
        queue.insert(queue.index(body["after"]) + 1, key)
    else:
        abort(400, "give before/after (an item in up-next) or to: top/bottom")
    md.save_up_next_order(account, queue)
    return jsonify(queue)


@app.get("/api/photos/<account>/<filename>/image")
def api_photo_image(account, filename):
    if account not in md.ACCOUNTS:
        abort(404)
    path = _photo_path(account, filename)
    if path is None:
        abort(404)
    return send_file(path)


@app.put("/api/photos/<account>/<filename>/account")
def api_move_account(account, filename):
    """Move a photo to a different account -- it lands in that account's
    holding-pen for a fresh review."""
    if account not in md.ACCOUNTS:
        abort(404)
    body = request.get_json(force=True)
    to_account = body.get("to_account")
    if to_account not in md.ACCOUNTS:
        abort(400, f"to_account must be one of {list(md.ACCOUNTS)}")
    try:
        md.move_to_account(account, filename, to_account)
    except FileNotFoundError:
        abort(404)
    except FileExistsError as exc:
        abort(409, str(exc))
    return jsonify({"filename": filename, "account": to_account, "status": md.HOLDING_SUBDIR})


@app.delete("/api/photos/<account>/<filename>")
def api_delete_photo(account, filename):
    """Permanently delete a photo and its metadata. Explicit, single-photo,
    user-initiated only -- never called from any bulk/automated route."""
    if account not in md.ACCOUNTS:
        abort(404)
    if _photo_path(account, filename) is None:
        abort(404)
    md.delete_photo(account, filename)
    return jsonify({"deleted": filename})


@app.put("/api/photos/<account>/<filename>")
def api_update_photo(account, filename):
    if account not in md.ACCOUNTS:
        abort(404)
    if _photo_path(account, filename) is None:
        abort(404)

    body = request.get_json(force=True)
    entries = md.load_metadata(account)
    entry = entries.setdefault(filename, {})
    for key in ("alt_text", "message", "place"):
        if key in body:
            entry[key] = body[key]
    for key in ("lat", "lon"):
        if key in body:
            entry[key] = float(body[key]) if body[key] not in (None, "") else None
    md.save_metadata(account, entries)
    return jsonify({**entry, "post_preview": _preview(account, entry)})


@app.put("/api/photos/<account>/<filename>/status")
def api_set_status(account, filename):
    """Move a photo between holding-pen, curated and up-next (the status
    buttons), along with the rest of its group. Moving to/from posted isn't
    exposed here -- that's poster.py's job."""
    if account not in md.ACCOUNTS:
        abort(404)
    body = request.get_json(force=True)
    to_status = body.get("status")
    allowed = (md.HOLDING_SUBDIR, md.CURATED_SUBDIR, md.UP_NEXT_SUBDIR)
    if to_status not in allowed:
        abort(400, f"status must be one of {list(allowed)}")

    _path, current_status = md.find_photo(account, filename)
    if current_status is None:
        abort(404)
    if current_status == md.POSTED_SUBDIR:
        abort(400, "already posted, can't move back to holding-pen/curated/up-next")

    # A group's photos move together.
    groups = md.load_groups(account)
    gid = md.group_of(groups, filename)
    for f in groups[gid]["photos"] if gid else [filename]:
        if md.find_photo(account, f)[1] not in (None, md.POSTED_SUBDIR):
            md.move_photo(account, f, to_status)
    # Newly up next goes to the bottom of the order; leaving takes it out.
    key = gid or filename
    order = [k for k in md.load_up_next_order(account) if k != key]
    md.save_up_next_order(account, order + [key] if to_status == md.UP_NEXT_SUBDIR else order)
    return jsonify({"filename": filename, "status": to_status})


@app.post("/api/sync-locations/<account>")
def api_sync_locations(account):
    """Append place into alt_text (if not already mentioned) for every
    entry with a place set. Safe to call repeatedly as more photos get a
    place. (Hashtags need no sync -- they're derived at post time.)"""
    if account not in md.ACCOUNTS:
        abort(404)
    summary = md.sync_locations(account)
    return jsonify(summary)


@app.post("/api/photos/<account>/<filename>/geocode")
def api_geocode_photo(account, filename):
    """Look up place for the entry's current lat/lon and save it."""
    if account not in md.ACCOUNTS:
        abort(404)
    entries = md.load_metadata(account)
    entry = entries.get(filename)
    if entry is None or entry.get("lat") is None or entry.get("lon") is None:
        abort(400, "lat/lon required before geocoding")
    entry["place"] = md.reverse_geocode(entry["lat"], entry["lon"])
    md.save_metadata(account, entries)
    return jsonify({**entry, "post_preview": _preview(account, entry)})


if __name__ == "__main__":
    host = os.environ.get("CURATE_HOST", "127.0.0.1")
    port = int(os.environ.get("CURATE_PORT", "5001"))
    app.run(host=host, port=port, debug=True)
