"""Cloudflare R2 as a short-lived public link for an image: Instagram's API
won't take image bytes, only a URL it downloads from while the post is
being created. Each image is uploaded under a random name, handed over as
a presigned link that expires in minutes, and deleted once the post is
published. Add a lifecycle rule to the bucket (delete after 1 day) as a
backstop for anything a crash leaves behind.

Settings, in .local.env (shared by every Instagram feed):
    R2_ACCOUNT_ID         Cloudflare account ID (dashboard -> R2 -> Account details)
    R2_ACCESS_KEY_ID      an R2 API token's key, limited to Object Read & Write on the bucket
    R2_SECRET_ACCESS_KEY  ...and its secret
    R2_BUCKET             the bucket's name; keep it private (no public access)

R2 speaks the S3 API, so this uses boto3.
"""

from __future__ import annotations

import os
import secrets

SETTINGS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
PREFIX = "outbox/"
LINK_SECONDS = 15 * 60


def missing_settings() -> list[str]:
    return [name for name in SETTINGS if not os.environ.get(name, "").strip()]


class R2:
    def __init__(self):
        import boto3
        from botocore.config import Config

        self.bucket = os.environ["R2_BUCKET"].strip()
        self.client = boto3.client(
            "s3",
            endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID'].strip()}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"].strip(),
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"].strip(),
            region_name="auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        )

    def put_temporary(self, data: bytes) -> tuple[str, str]:
        """Uploads a JPEG under a random name. Returns (key, a link to it
        that works for LINK_SECONDS)."""
        key = f"{PREFIX}{secrets.token_hex(16)}.jpg"
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType="image/jpeg")
        url = self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=LINK_SECONDS)
        return key, url

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def check(self) -> None:
        """Raises unless these credentials can do what posting needs: write
        and delete an object (a token limited to objects may not be allowed
        bucket-level calls, so this tries the real thing on a tiny file)."""
        key = f"{PREFIX}check-{secrets.token_hex(8)}.txt"
        self.client.put_object(Bucket=self.bucket, Key=key, Body=b"postcards --check")
        self.delete(key)
