#!/usr/bin/env python3
"""One-time/rerunnable seeding of metadata_<Account>.json from photos on disk.

Idempotent: never overwrites an existing entry (so curator edits made via
curate.py are safe) -- only adds entries for photos that don't have one yet,
seeding alt_text from a legacy alt_text_<Account>.json if given (from the
single-folder pipeline's migration) and always computing GPS/date fresh
from the file itself.

Usage:
    python3 build_metadata.py [--account PostcardsFromHome] [--legacy-alt-text ../alt_text_PostcardsFromHome.json] [--legacy-overrides ../location_overrides_PostcardsFromHome.json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import metadata as md


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, choices=list(md.ACCOUNTS))
    parser.add_argument("--legacy-alt-text", default=None)
    parser.add_argument("--legacy-overrides", default=None)
    args = parser.parse_args()

    account = args.account
    legacy_alt = json.loads(Path(args.legacy_alt_text).read_text()) if args.legacy_alt_text else {}
    legacy_overrides = json.loads(Path(args.legacy_overrides).read_text()) if args.legacy_overrides else {"files": {}}

    existing = md.load_metadata(account)
    filenames = sorted(set(md.scan_holding(account)) | set(md.scan_curated(account)) | set(md.scan_posted(account)))

    added = 0
    for filename in filenames:
        if filename in existing:
            continue

        path, _status = md.find_photo(account, filename)

        gps = md.extract_gps(path)
        place = None
        if gps:
            lat, lon = gps
            place = md.reverse_geocode(lat, lon)
            print(f"{filename}: GPS {lat},{lon} -> {place}")
        else:
            override = legacy_overrides.get("files", {}).get(filename)
            if override:
                place = override.get("place")
                print(f"{filename}: manual override -> {place}")
            else:
                lat = lon = None
                print(f"{filename}: no location")

        existing[filename] = {
            "alt_text": legacy_alt.get(filename, ""),
            "message": "",  # hashtags are added at post time from feeds.json + place
            "lat": gps[0] if gps else None,
            "lon": gps[1] if gps else None,
            "place": place,
            "date_taken": md.extract_date_taken(path),
        }
        added += 1

    md.save_metadata(account, existing)
    print(f"\n{account}: {added} new entries added, {len(existing)} total in metadata_{account}.json")


if __name__ == "__main__":
    main()
