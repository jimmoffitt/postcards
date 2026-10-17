"""Where a feed's posts go. poster.py picks a post, writes its text and
records it; the feed's publisher prepares its images and does the posting
itself. Each platform's publisher has the same interface:

    publisher = for_feed(account, feed_cfg)
    publisher.platform               # "Bluesky", "Instagram"
    publisher.missing_credentials()  # why it can't post, or None
    publisher.check_login()          # "login as <who>", or raises
    publisher.prepare(photo_path)    # -> upload_prep.PreparedImage, metadata stripped
    publisher.publish(images, alt_texts, text)  # -> the new post's URI or link

A feed's platform is its "platform" setting in feeds.json; the limits that
go with it (photos per post, length, hashtags) are in config.PLATFORMS.
"""

from __future__ import annotations

from .bluesky import BlueskyPublisher
from .instagram import InstagramPublisher

_BY_PLATFORM = {"bluesky": BlueskyPublisher, "instagram": InstagramPublisher}


def for_feed(account: str, feed_cfg: dict):
    """The publisher for this feed's platform."""
    return _BY_PLATFORM[feed_cfg["platform"]](account)
