#!/usr/bin/env python3
"""Regenerate alt text for photos that don't have any yet.

Finds every metadata entry with alt_text empty/missing, wherever the photo
currently sits (holding-pen/curated/posted), and runs the same vision
batch pass as ../generate_alt_text.py against just those files, merging
results back into metadata_<Account>.json. Safe to rerun any time -- only
touches entries that are still missing alt text.

Requires an Anthropic API key: set ANTHROPIC_API_KEY, or run `ant auth
login` beforehand (same requirement as generate_alt_text.py).

Usage:
    python3 regenerate_missing_alt_text.py [--account PostcardsFromHome] [--model claude-opus-5]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import metadata as md

sys.path.insert(0, str(md.PROJECT_ROOT))
import generate_alt_text as gat  # noqa: E402


def find_missing(account: str) -> list[tuple[str, Path]]:
    entries = md.load_metadata(account)
    missing = []
    for filename, meta in entries.items():
        if meta.get("alt_text"):
            continue
        path, _status = md.find_photo(account, filename)
        if path is None:
            print(f"  {account}/{filename}: file missing on disk, skipping")
            continue
        missing.append((filename, path))
    return missing


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", choices=list(md.ACCOUNTS), default=None,
                         help="Only this account (default: both)")
    parser.add_argument("--model", default="claude-opus-5")
    args = parser.parse_args()

    accounts = [args.account] if args.account else list(md.ACCOUNTS)

    cache_path = md.PROJECT_ROOT / gat.GEOCODE_CACHE_FILE
    cache = gat.load_cache(cache_path)

    import anthropic
    client = anthropic.Anthropic()

    for account in accounts:
        missing = find_missing(account)
        if not missing:
            print(f"{account}: nothing missing alt text")
            continue
        print(f"{account}: {len(missing)} photo(s) missing alt text")

        id_map = {}
        chunks = [[]]
        chunk_bytes = 0

        for i, (filename, path) in enumerate(missing):
            cid = f"img-{i:05d}"
            id_map[cid] = filename

            gps = gat.extract_gps(path)
            place = None
            if gps:
                lat, lon = gps
                place = gat.reverse_geocode(lat, lon, cache, cache_path)
                print(f"  [{i + 1}/{len(missing)}] {filename}: GPS -> {place}")
            else:
                print(f"  [{i + 1}/{len(missing)}] {filename}: no GPS")

            b64 = gat.downscale_for_vision(path)
            request = gat.build_request(cid, b64, place, args.model)

            if chunk_bytes + len(b64) > gat.MAX_BATCH_BYTES and chunks[-1]:
                chunks.append([])
                chunk_bytes = 0
            chunks[-1].append(request)
            chunk_bytes += len(b64)

        all_results = {}
        for i, chunk in enumerate(chunks):
            print(f"  batch {i + 1}/{len(chunks)} ({len(chunk)} requests)")
            all_results.update(gat.run_batch(client, chunk))

        entries = md.load_metadata(account)
        updated = 0
        for cid, text in all_results.items():
            filename = id_map[cid]
            if text:
                entries[filename]["alt_text"] = text
                updated += 1
            else:
                print(f"  {filename}: generation failed")
        md.save_metadata(account, entries)
        print(f"{account}: updated {updated}/{len(missing)}\n")


if __name__ == "__main__":
    main()
