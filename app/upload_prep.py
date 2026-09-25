"""Make a photo safe to upload: no EXIF/XMP/IPTC metadata (GPS, camera,
timestamps), correct orientation, and within Bluesky's size limit.

Bluesky stores uploaded image blobs as-is, so anything embedded in the file
-- including the GPS position it was taken at -- is downloadable by anyone.
The files on disk are never changed; this only affects the bytes uploaded.

Two paths:
  - Upright photos (no orientation tag, or orientation 1): the metadata
    segments are cut out of the JPEG byte stream. Lossless -- the image data
    itself is copied untouched.
  - Photos that rely on an EXIF orientation tag to display the right way up
    (a phone held sideways): the pixels are rotated and re-encoded, since
    dropping the tag would otherwise show them sideways.

Either way the result is re-read and verified to carry no EXIF at all before
it's returned; if that ever fails, prepare() raises rather than upload.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, JpegImagePlugin

MAX_IMAGE_BYTES = 976_560  # AT Protocol image blob limit (~976.56 KB)
ORIENTATION_TAG = 0x0112

# JPEG marker segments to keep before the image data: APP0 (JFIF header),
# APP2 (ICC colour profile), APP14 (Adobe colour transform -- dropping it can
# invert CMYK/YCCK colours), plus the structural ones (DQT, SOF, DHT, DRI...).
# Every other APPn (APP1 = EXIF and XMP, APP13 = IPTC, maker data in APP3-15)
# and COM comments are dropped.
_KEEP_APP = {0xE0, 0xE2, 0xEE}
_COM = 0xFE
_SOS = 0xDA
_EOI = b"\xff\xd9"
_DOWNSCALE_STEPS = (1.0, 0.9, 0.8, 0.7, 0.6)


@dataclass
class PreparedImage:
    data: bytes
    width: int
    height: int
    method: str  # "lossless" or "re-encoded"


def strip_jpeg_metadata(data: bytes) -> bytes:
    """Drop metadata segments from a JPEG without touching the image data.
    Also drops anything after the end-of-image marker (phones append extra
    images, depth maps, etc. there, which can carry their own EXIF)."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG (no SOI marker)")
    out = bytearray(data[:2])
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            raise ValueError(f"malformed JPEG: expected a marker at byte {i}")
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == _SOS:
            # Entropy-coded data can't contain FF D9 (0xFF is byte-stuffed),
            # so the first one after here is the end of this image.
            end = data.find(_EOI, i)
            if end < 0:
                raise ValueError("malformed JPEG: no EOI marker")
            out += data[i:end + 2]
            return bytes(out)
        length = int.from_bytes(data[i + 2:i + 4], "big")
        segment = data[i:i + 2 + length]
        is_app = 0xE0 <= marker <= 0xEF
        if (is_app and marker in _KEEP_APP) or (not is_app and marker != _COM):
            out += segment
        i += 2 + length
    raise ValueError("malformed JPEG: no image data (SOS) found")


def _reencode_upright(img: Image.Image) -> bytes:
    """Rotate upright and re-encode with the original's own quantization
    tables and chroma subsampling: prepare_photos.py already tuned those to
    fit the size limit, so reusing them keeps the size about the same and
    adds very little loss (a fresh "quality" setting typically comes out
    bigger for these already-compressed files). Scales down only if needed."""
    save_kwargs = {"qtables": img.quantization, "icc_profile": img.info.get("icc_profile"), "optimize": True}
    sampling = JpegImagePlugin.get_sampling(img)
    if sampling >= 0:
        save_kwargs["subsampling"] = sampling
    upright = ImageOps.exif_transpose(img)
    if upright.mode not in ("RGB", "L"):
        upright = upright.convert("RGB")
    for scale in _DOWNSCALE_STEPS:
        frame = upright if scale == 1.0 else upright.resize(
            (round(upright.width * scale), round(upright.height * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        frame.save(buf, "JPEG", **save_kwargs)  # no exif= argument, so none is written
        if buf.tell() <= MAX_IMAGE_BYTES:
            return buf.getvalue()
    raise ValueError(f"still over {MAX_IMAGE_BYTES} bytes at {scale:.0%} size -- rerun prepare_photos.py on it")


def _verify(data: bytes) -> tuple[int, int]:
    """Decodes fully and confirms there's no EXIF/XMP left. Returns (w, h)."""
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        if len(img.getexif()) or "exif" in img.info or "xmp" in img.info:
            raise ValueError("metadata still present after stripping -- refusing to upload")
        return img.size


def prepare(photo_path: Path) -> PreparedImage:
    raw = photo_path.read_bytes()
    with Image.open(io.BytesIO(raw)) as img:
        if img.format != "JPEG":
            raise ValueError(f"{photo_path.name} is {img.format}, expected JPEG")
        orientation = img.getexif().get(ORIENTATION_TAG, 1)
        if orientation in (1, None):
            data, method = strip_jpeg_metadata(raw), "lossless"
        else:
            data, method = _reencode_upright(img), "re-encoded"

    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"{photo_path.name} is {len(data)} bytes, over the "
                         f"{MAX_IMAGE_BYTES}-byte Bluesky image limit -- rerun prepare_photos.py on it")
    width, height = _verify(data)
    return PreparedImage(data, width, height, method)
