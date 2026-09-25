#!/usr/bin/env python3
"""Generate Bluesky accessibility alt text for photos under photos/.

For each JPG found under --src (skipping the original zips backup folder):

  1. Reads GPS EXIF, if present, and reverse-geocodes it to a place name via
     OpenStreetMap Nominatim (free, no API key; results are cached locally
     in geocode_cache.json and lookups are rate-limited to Nominatim's
     usage policy of 1 request/sec).
  2. Sends the photo -- plus the place name, if any -- to Claude's vision
     API asking for concise, accessibility-focused alt text.
  3. Writes filename -> alt text pairs to a JSON file.

Uses the Message Batches API (50% cheaper than synchronous calls, and this
isn't latency-sensitive), split into chunks to stay under the batch size
limit (base64-encoded images inflate request size by ~33%).

Requires an Anthropic API key: set ANTHROPIC_API_KEY, or run `ant auth
login` beforehand.

Usage:
    python3 generate_alt_text.py [--src photos] [--out alt_text.json] [--model claude-opus-5]
    python3 generate_alt_text.py --src photos --list ambiguous.txt --out alt_text_patch.json
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from PIL import ExifTags, Image, ImageOps

GEOCODE_CACHE_FILE = "geocode_cache.json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "postcards-alt-text-script/1.0"
MAX_BATCH_BYTES = 180_000_000  # stay safely under the Batches API's 256MB cap
GEOCODE_PRECISION = 4  # ~11m -- enough to dedupe nearby shots without over-fetching
VISION_MAX_DIM = 1500  # downscale a temporary copy for the API call only; saved JPGs are untouched
VISION_QUALITY = 85

ALT_TEXT_SYSTEM = """You write concise accessibility alt text for photos posted publicly on a \
"postcards from home" Bluesky account. Describe what's actually visible -- subject, scene, \
notable colors, mood -- in one or two plain sentences, under 250 characters. Don't start with \
"Image of" or "Photo of". If location context is given, you may naturally weave in the place \
name only if it adds real value, but the visual description always comes first. Never hedge with \
words like "likely", "possibly", "probably", "perhaps", or "maybe" -- if you're not confident \
enough to state a place plainly, leave it out of the description entirely rather than guessing at \
it with a qualifier. When people are visible, \
mention their presence and general activity (e.g. "a person walking along the shore") without \
describing their appearance, clothing, gender, age, or other identifying details."""


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


def downscale_for_vision(path: Path, max_dim: int = VISION_MAX_DIM, quality: int = VISION_QUALITY) -> str:
    """Return a base64 JPEG, resized for the vision API call only -- the saved file on disk is untouched."""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)  # normalize orientation before the model sees it
    img = img.convert("RGB")
    if max(img.size) > max_dim:
        scale = max_dim / max(img.size)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def load_cache(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def reverse_geocode(lat: float, lon: float, cache: dict, cache_path: Path) -> str | None:
    key = f"{lat},{lon}"
    if key in cache:
        return cache[key]

    params = urllib.parse.urlencode(
        {"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 18, "addressdetails": 1}
    )
    req = urllib.request.Request(f"{NOMINATIM_URL}?{params}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        place = data.get("display_name")
    except Exception as exc:
        print(f"    geocode failed for {key}: {exc}")
        place = None

    cache[key] = place
    cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True))
    time.sleep(1.1)  # Nominatim usage policy: max 1 request/sec
    return place


def build_request(custom_id: str, image_b64: str, place: str | None, model: str) -> Request:
    text = "Write alt text for this photo."
    if place:
        text += f"\n\nLocation context (from GPS metadata, may be imprecise): {place}"
    return Request(
        custom_id=custom_id,
        params=MessageCreateParamsNonStreaming(
            model=model,
            max_tokens=200,
            system=ALT_TEXT_SYSTEM,
            output_config={"effort": "low"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64},
                        },
                        {"type": "text", "text": text},
                    ],
                }
            ],
        ),
    )


def run_batch(client: anthropic.Anthropic, requests: list) -> dict:
    batch = client.messages.batches.create(requests=requests)
    print(f"    batch {batch.id}: {len(requests)} requests submitted, waiting...")
    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        time.sleep(20)
    print(f"    done: succeeded={batch.request_counts.succeeded} errored={batch.request_counts.errored}")

    results = {}
    for result in client.messages.batches.results(batch.id):
        if result.result.type == "succeeded":
            msg = result.result.message
            text = next((b.text for b in msg.content if b.type == "text"), "")
            results[result.custom_id] = text.strip()
        else:
            results[result.custom_id] = None
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--src", default="photos")
    parser.add_argument("--out", default="alt_text.json")
    parser.add_argument("--model", default="claude-opus-5")
    parser.add_argument("--list", default=None,
                         help="Text file of relative photo paths, one per line -- process only these "
                              "instead of scanning --src")
    args = parser.parse_args()

    src_root = Path(args.src)
    cache_path = Path(GEOCODE_CACHE_FILE)
    cache = load_cache(cache_path)

    if args.list:
        files = [src_root / line.strip() for line in Path(args.list).read_text().splitlines() if line.strip()]
        files = sorted(p for p in files if p.exists())
    else:
        files = sorted(p for p in src_root.rglob("*.jpg") if "original zips" not in p.parts)
    print(f"Found {len(files)} photos under {src_root}")

    client = anthropic.Anthropic()

    id_map = {}
    chunks = [[]]
    chunk_bytes = 0

    for i, path in enumerate(files):
        rel = str(path.relative_to(src_root))
        cid = f"img-{i:05d}"
        id_map[cid] = rel

        gps = extract_gps(path)
        place = None
        if gps:
            lat, lon = gps
            place = reverse_geocode(lat, lon, cache, cache_path)
            print(f"[{i + 1}/{len(files)}] {rel}: GPS {lat},{lon} -> {place}")
        else:
            print(f"[{i + 1}/{len(files)}] {rel}: no GPS")

        b64 = downscale_for_vision(path)
        request = build_request(cid, b64, place, args.model)

        if chunk_bytes + len(b64) > MAX_BATCH_BYTES and chunks[-1]:
            chunks.append([])
            chunk_bytes = 0
        chunks[-1].append(request)
        chunk_bytes += len(b64)

    print(f"\nSubmitting {len(chunks)} batch(es) covering {len(files)} photos")

    all_results = {}
    for i, chunk in enumerate(chunks):
        print(f"Batch {i + 1}/{len(chunks)} ({len(chunk)} requests)")
        all_results.update(run_batch(client, chunk))

    out = {id_map[cid]: text for cid, text in all_results.items() if text}
    failed = [id_map[cid] for cid, text in all_results.items() if not text]

    Path(args.out).write_text(json.dumps(out, indent=2, sort_keys=True))
    print(f"\nWrote {len(out)} alt texts to {args.out}")
    if failed:
        print(f"{len(failed)} failed: {failed[:10]}{'...' if len(failed) > 10 else ''}")


if __name__ == "__main__":
    main()
