#!/usr/bin/env python3
"""Prepare photos under photos/ for posting to Bluesky.

Walks the source folder (including all subfolders), and for every photo
(.heic, .heif, .jpg, .jpeg, .png) produces a JPEG, mirroring the source
folder structure. By default it converts in place (--dest defaults to
--src), so photos/ ends up containing only JPGs. Along the way it:

  - decodes HEIC/HEIF (via pillow-heif) and any other supported format
  - by default, preserves EXIF metadata as-is, including GPS location --
    pass --strip-exif to drop all metadata instead (recommended for a
    final pass right before public posting)
  - shrinks each file to fit Bluesky's ~976.56 KB (1,000,000 byte) image
    blob limit, first by stepping down JPEG quality, then -- if still too
    large -- by downscaling resolution
  - with --delete-originals, removes the source file once its .jpg has
    been written, but only when the source had a different filename (e.g.
    .HEIC -> .jpg); a source that was already a same-named .jpg is just
    overwritten in place, nothing to delete

EXIF is carried through as the original raw bytes (not re-serialized via
Pillow's high-level Exif object), which preserves nested IFDs like GPSInfo
exactly and avoids a Pillow bug where re-encoding certain cameras' EXIF
blocks raises "argument out of range".

Videos (.mov, .mp4) are left alone; Bluesky video posting has its own
separate API and limits.

Usage:
    python3 prepare_photos.py [--src photos] [--dest photos] [--quality 88] [--delete-originals] [--strip-exif]
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path

from PIL import Image
import pillow_heif

pillow_heif.register_heif_opener()

PHOTO_SUFFIXES = {".heic", ".heif", ".jpg", ".jpeg", ".png"}
BLUESKY_MAX_BYTES = 976_560  # AT Protocol image blob limit (~976.56 KB)
MIN_QUALITY = 40
QUALITY_STEP = 8
RESIZE_FACTOR = 0.9
MIN_DIMENSION = 800


def encode_jpeg(img: Image.Image, quality: int, exif: bytes | None) -> bytes:
    buf = io.BytesIO()
    kwargs = {"quality": quality, "optimize": True}
    if exif:
        kwargs["exif"] = exif
    img.save(buf, "JPEG", **kwargs)
    return buf.getvalue()


def fit_under_limit(
    img: Image.Image, quality: int, max_bytes: int, exif: bytes | None
) -> tuple[bytes, int, tuple[int, int]]:
    """Return (jpeg_bytes, quality_used, dimensions) that fit under max_bytes."""
    q = quality
    working = img
    data = encode_jpeg(working, q, exif)

    while len(data) > max_bytes and q > MIN_QUALITY:
        q -= QUALITY_STEP
        data = encode_jpeg(working, q, exif)

    while len(data) > max_bytes and min(working.size) > MIN_DIMENSION:
        new_size = (int(working.width * RESIZE_FACTOR), int(working.height * RESIZE_FACTOR))
        working = working.resize(new_size, Image.LANCZOS)
        q = quality
        data = encode_jpeg(working, q, exif)
        while len(data) > max_bytes and q > MIN_QUALITY:
            q -= QUALITY_STEP
            data = encode_jpeg(working, q, exif)

    return data, q, working.size


def convert_file(src_path: Path, dest_path: Path, quality: int, max_bytes: int, strip_exif: bool) -> tuple[int, int]:
    with Image.open(src_path) as img:
        exif = None if strip_exif else img.info.get("exif")
        img = img.convert("RGB")

        data, used_quality, dims = fit_under_limit(img, quality, max_bytes, exif)

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(data)

    return src_path.stat().st_size, len(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--src", default="photos", help="Source folder to scan (default: photos)")
    parser.add_argument(
        "--dest",
        default=None,
        help="Output folder for prepared JPEGs (default: same as --src, i.e. convert in place)",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=88,
        help="Starting JPEG quality 1-95 (default: 88, a good size/quality balance)",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=BLUESKY_MAX_BYTES,
        help=f"Max output file size in bytes (default: {BLUESKY_MAX_BYTES}, Bluesky's blob limit)",
    )
    parser.add_argument(
        "--delete-originals",
        action="store_true",
        help="Delete the source file after a successful conversion, when its filename "
             "differs from the output (e.g. .HEIC -> .jpg). Never deletes a source that "
             "was already the same-named .jpg being overwritten in place.",
    )
    parser.add_argument(
        "--strip-exif",
        action="store_true",
        help="Drop all EXIF metadata (including GPS) instead of preserving it. Use for "
             "a final pass right before public posting.",
    )
    args = parser.parse_args()

    src_root = Path(args.src)
    dest_root = Path(args.dest) if args.dest else src_root

    source_files = sorted(
        p for p in src_root.rglob("*") if p.suffix.lower() in PHOTO_SUFFIXES
    )

    if not source_files:
        print(f"No photos found under {src_root}")
        return

    total_src = 0
    total_dest = 0
    failures = []
    deleted = 0

    for src_path in source_files:
        rel = src_path.relative_to(src_root)
        dest_path = dest_root / rel.with_suffix(".jpg")

        try:
            src_size, dest_size = convert_file(src_path, dest_path, args.quality, args.max_bytes, args.strip_exif)
        except Exception as exc:
            failures.append((src_path, exc))
            print(f"FAILED  {rel}: {exc}")
            continue

        total_src += src_size
        total_dest += dest_size

        note = ""
        if args.delete_originals and not src_path.samefile(dest_path):
            src_path.unlink()
            deleted += 1
            note = "  [deleted original]"

        print(f"OK      {rel} -> {dest_path.relative_to(dest_root)}  "
              f"({src_size/1024:.0f} KB -> {dest_size/1024:.0f} KB){note}")

    print()
    print(f"Prepared {len(source_files) - len(failures)}/{len(source_files)} files")
    if args.delete_originals:
        print(f"Deleted {deleted} original source file(s)")
    if total_src:
        pct = (1 - total_dest / total_src) * 100
        print(f"Total size: {total_src/1024/1024:.1f} MB -> {total_dest/1024/1024:.1f} MB "
              f"({pct:.1f}% smaller)")
    if failures:
        print(f"\n{len(failures)} file(s) failed:")
        for path, exc in failures:
            print(f"  {path}: {exc}")


if __name__ == "__main__":
    main()
