"""Where a feed's posts go. poster.py picks a post, prepares its images,
writes its text and records it; the feed's publisher does the posting
itself. Each platform's publisher has the same interface:

    publisher = for_feed(account)
    publisher.missing_credentials()  # why it can't post, or None
    publisher.check_login()          # "login as <who>", or raises
    publisher.publish(images, alt_texts, text)  # -> the new post's URI
"""

from __future__ import annotations

from .bluesky import BlueskyPublisher


def for_feed(account: str) -> BlueskyPublisher:
    """The publisher for this feed. Every feed posts to Bluesky for now."""
    return BlueskyPublisher(account)
