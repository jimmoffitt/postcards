"""Posting to Bluesky (AT Protocol): one post with up to 4 images, each
with its alt text and aspect ratio, and the post's hashtags made into
clickable tags.

Credentials come from .local.env: <FEED>_BSKY_HANDLE and
<FEED>_BSKY_APP_PASSWORD, with the feed name uppercased.
"""

from __future__ import annotations

import os
import re

PLACEHOLDER_PASSWORD = "xxxx-xxxx-xxxx-xxxx"  # as in .local.env.example


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


class BlueskyPublisher:
    platform = "Bluesky"

    def __init__(self, account: str):
        self.account = account
        self.prefix = account.upper()
        self.handle = os.environ.get(f"{self.prefix}_BSKY_HANDLE", "").strip()
        self.app_password = os.environ.get(f"{self.prefix}_BSKY_APP_PASSWORD", "").strip()

    def missing_credentials(self) -> str | None:
        """Why this feed can't post, or None if its credentials are set. A
        placeholder password counts as missing: Bluesky allows only 10
        failed logins a day, so it's not worth trying."""
        if not self.handle or not self.app_password or self.app_password == PLACEHOLDER_PASSWORD:
            return f"{self.prefix}_BSKY_HANDLE / {self.prefix}_BSKY_APP_PASSWORD not set in .local.env"
        return None

    def check_login(self) -> str:
        """Logs in, to prove the credentials work. Returns what to show on
        success; raises with Bluesky's own message on failure."""
        from atproto import Client

        try:
            Client().login(self.handle, self.app_password)
        except Exception as exc:
            content = getattr(getattr(exc, "response", None), "content", None)
            raise RuntimeError(f"login as {self.handle} failed: {getattr(content, 'message', None) or exc}") from exc
        return f"login as {self.handle}"

    def publish(self, images: list, alt_texts: list[str], text: str) -> str:
        """Posts `text` with `images` (upload_prep.PreparedImage), each with
        its alt text. Returns the new post's at:// URI."""
        from atproto import Client, models

        client = Client()
        client.login(self.handle, self.app_password)
        resp = client.send_images(
            text=text,
            images=[image.data for image in images],
            image_alts=alt_texts,
            facets=build_hashtag_facets(text),
            # Without an aspect ratio Bluesky falls back to a square crop.
            image_aspect_ratios=[models.AppBskyEmbedDefs.AspectRatio(width=image.width, height=image.height)
                                 for image in images],
        )
        return getattr(resp, "uri", "")
