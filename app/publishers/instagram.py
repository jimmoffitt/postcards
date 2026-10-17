"""Posting to Instagram (a Creator or Business account), through the
Instagram API with Instagram Login: one photo, or a carousel of up to 10,
each with its alt text, and the caption.

Instagram downloads each image from a URL rather than taking its bytes, so
every image goes to Cloudflare R2 first (see r2.py) under a short-lived
link, and is deleted again once the post is published. Posting is two
steps: create a "media container" for the post (Instagram fetches the
image then), wait for it to be ready, then publish it.

Settings, in .local.env:
    <FEED>_IG_ACCESS_TOKEN  the long-lived token from the Meta app dashboard
                            (API setup with Instagram login -> Generate token)
    R2_*                    see r2.py; shared by every Instagram feed

The token lasts 60 days. It's renewed automatically about weekly, and the
current one is kept in state/<Feed>.instagram_token.json; putting a new token
in .local.env (after it lapsed, say) takes over from the saved one.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import logging
import math
import os
import time
from pathlib import Path

from PIL import Image

import config
import r2
import upload_prep

logger = logging.getLogger(__name__)
# httpx logs each request's URL at INFO, and a GET's URL carries the token.
logging.getLogger("httpx").setLevel(logging.WARNING)

GRAPH = "https://graph.instagram.com"
API_VERSION = os.environ.get("IG_API_VERSION", "v23.0")
TIMEOUT = 60  # seconds per request; media_publish can be slow

# Instagram feed photos must be between 4:5 (portrait) and 1.91:1 (landscape);
# anything outside is padded out to the nearest of the two, never cropped.
MIN_RATIO, MAX_RATIO = 4 / 5, 1.91
PAD_COLOR = (255, 255, 255)
MAX_IMAGE_BYTES = 8 * 1024 * 1024

READY_POLL_SECONDS, READY_TIMEOUT_SECONDS = 2, 120
TOKEN_REFRESH_EVERY = datetime.timedelta(days=7)
TOKEN_RETRY_EVERY = datetime.timedelta(days=1)  # a token under 24 h old can't be refreshed yet
INFLIGHT_WINDOW = datetime.timedelta(hours=24)
# Instagram's post times are to the second (and its clock isn't ours), so a
# post that went out right as the attempt started can look slightly older.
CLOCK_SLACK = datetime.timedelta(minutes=2)

STATE_DIR = config.app_path("STATE_DIR", "state")


class ApiError(RuntimeError):
    """Instagram answered with an error: the request definitely failed."""


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# Images: inside Instagram's allowed shapes
# ---------------------------------------------------------------------------

def fit_to_shape(image: upload_prep.PreparedImage) -> upload_prep.PreparedImage:
    """Pads a photo that's taller than 4:5 or wider than 1.91:1 out to that
    shape (centered, on PAD_COLOR), so Instagram neither rejects nor crops
    it. A photo already in range is returned as it is."""
    w, h = image.width, image.height
    if w / h < MIN_RATIO:
        size = (math.ceil(h * MIN_RATIO), h)
    elif w / h > MAX_RATIO:
        size = (w, math.ceil(w / MAX_RATIO))
    else:
        return image
    with Image.open(io.BytesIO(image.data)) as img:
        canvas = Image.new("RGB", size, PAD_COLOR)
        canvas.paste(img.convert("RGB"), ((size[0] - w) // 2, (size[1] - h) // 2))
        buf = io.BytesIO()
        # No exif= argument, so none is written; keep the color profile.
        canvas.save(buf, "JPEG", quality=92, optimize=True, icc_profile=img.info.get("icc_profile"))
    data = buf.getvalue()
    width, height = upload_prep.verify_clean(data)
    return upload_prep.PreparedImage(data, width, height, "padded")


# ---------------------------------------------------------------------------
# The Graph API
# ---------------------------------------------------------------------------

class _Api:
    def __init__(self, token: str):
        import httpx

        self.token = token
        self.http = httpx.Client(timeout=TIMEOUT)

    def _scrub(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text

    def _call(self, method: str, path: str, params: dict) -> dict:
        import httpx

        url = path if path.startswith("https://") else f"{GRAPH}/{API_VERSION}/{path}"
        params = {k: v for k, v in params.items() if v not in (None, "")}
        params["access_token"] = self.token
        try:
            if method == "GET":
                resp = self.http.get(url, params=params)
            else:
                resp = self.http.post(url, data=params)  # in the body, not the URL
        except httpx.HTTPError as exc:
            # Never let the token reach a log via the request URL. Not an
            # ApiError: the request may or may not have arrived.
            raise ConnectionError(self._scrub(f"{type(exc).__name__}: {exc}")) from None
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400 or "error" in body:
            err = body.get("error", {})
            raise ApiError(self._scrub(f"Instagram {resp.status_code}: {err.get('message') or resp.text[:200]}"
                                       + (f" (code {err['code']})" if "code" in err else "")))
        return body

    def get(self, path: str, **params) -> dict:
        return self._call("GET", path, params)

    def post(self, path: str, **params) -> dict:
        return self._call("POST", path, params)


# ---------------------------------------------------------------------------
# The publisher
# ---------------------------------------------------------------------------

class InstagramPublisher:
    platform = "Instagram"

    def __init__(self, account: str):
        self.account = account
        self.prefix = account.upper()
        self.env_token = os.environ.get(f"{self.prefix}_IG_ACCESS_TOKEN", "").strip()
        self.token_path = STATE_DIR / f"{account}.instagram_token.json"
        self.inflight_path = STATE_DIR / f"{account}.instagram_inflight.json"

    # -- credentials and the token ------------------------------------------

    def missing_credentials(self) -> str | None:
        missing = ([] if self.env_token else [f"{self.prefix}_IG_ACCESS_TOKEN"]) + r2.missing_settings()
        return f"{', '.join(missing)} not set in .local.env" if missing else None

    def _load_json(self, path: Path) -> dict:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)  # it holds the account's token
        tmp.replace(path)

    def _token_state(self) -> dict:
        """The saved token, unless .local.env has a different one now (a new
        token pasted in replaces whatever was saved)."""
        seed = hashlib.sha256(self.env_token.encode()).hexdigest()
        saved = self._load_json(self.token_path)
        if saved.get("seed") != seed or not saved.get("token"):
            saved = {"seed": seed, "token": self.env_token}
        return saved

    def access_token(self) -> str:
        """The current token, renewed first if it's been a week. A failed
        renewal is logged and the current token used; it's retried daily."""
        state = self._token_state()
        now = _now()
        stamp = lambda key: datetime.datetime.fromisoformat(state[key]) if state.get(key) else None
        due = stamp("refreshed_at") is None or now - stamp("refreshed_at") > TOKEN_REFRESH_EVERY
        retry_ok = stamp("refresh_tried_at") is None or now - stamp("refresh_tried_at") > TOKEN_RETRY_EVERY
        if due and retry_ok:
            state["refresh_tried_at"] = now.isoformat()
            try:
                body = _Api(state["token"]).get(f"{GRAPH}/refresh_access_token", grant_type="ig_refresh_token")
                state.update(token=body["access_token"], refreshed_at=now.isoformat(),
                             expires_at=(now + datetime.timedelta(seconds=int(body.get("expires_in", 0)))).isoformat())
                logger.info("[%s] Instagram token renewed, good until %s", self.account, state["expires_at"][:10])
            except Exception as exc:
                logger.warning("[%s] couldn't renew the Instagram token (will retry tomorrow): %s", self.account, exc)
            self._save_json(self.token_path, state)
        return state["token"]

    def check_login(self) -> str:
        api = _Api(self.access_token())
        me = api.get("me", fields="user_id,username")
        expires = self._token_state().get("expires_at")
        try:
            r2.R2().check()
        except Exception as exc:
            raise RuntimeError(f"Instagram login as @{me.get('username')} OK, but R2 failed: {exc}") from exc
        return (f"login as @{me.get('username')} on Instagram"
                + (f" (token good until {expires[:10]})" if expires else "") + "; R2 bucket writable")

    # -- posting ------------------------------------------------------------

    def prepare(self, photo_path: Path) -> upload_prep.PreparedImage:
        """Metadata stripped and upright (as for Bluesky), then padded to
        Instagram's shapes if needed."""
        image = fit_to_shape(upload_prep.prepare(photo_path))
        if len(image.data) > MAX_IMAGE_BYTES:
            raise ValueError(f"{photo_path.name} is {len(image.data)} bytes, over Instagram's 8 MB limit")
        return image

    def _wait_ready(self, api: _Api, container_id: str) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while True:
            status = api.get(container_id, fields="status_code").get("status_code")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise ApiError(f"Instagram couldn't process container {container_id}: {status}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"container {container_id} still {status} after {READY_TIMEOUT_SECONDS}s")
            time.sleep(READY_POLL_SECONDS)

    def _already_published(self, api: _Api, user_id: str, text: str) -> str | None:
        """A post interrupted mid-publish (a timeout, say) may have gone out
        anyway. If the last attempt was this same post, look for it on the
        account before posting again; returns its link if it's there."""
        inflight = self._load_json(self.inflight_path)
        if not inflight:
            return None
        started = datetime.datetime.fromisoformat(inflight["started_at"])
        if _now() - started > INFLIGHT_WINDOW:
            self.inflight_path.unlink(missing_ok=True)
            return None
        recent = api.get(f"{user_id}/media", fields="id,caption,permalink,timestamp", limit=10).get("data", [])
        live = next((m for m in recent if (m.get("caption") or "").strip() == inflight["caption"].strip()
                     and datetime.datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z") >= started - CLOCK_SLACK),
                    None)
        self.inflight_path.unlink(missing_ok=True)
        if live and inflight["caption"].strip() == text.strip():
            logger.warning("[%s] the last attempt at this post did go out (%s) -- not posting it again",
                           self.account, live.get("permalink"))
            return live.get("permalink") or f"instagram:{live['id']}"
        if live:
            logger.error("[%s] an earlier, interrupted post went out after all (%s), but its photos are still "
                         "queued -- move them to posted/ or they'll be posted again", self.account, live.get("permalink"))
        return None

    def publish(self, images: list, alt_texts: list[str], text: str) -> str:
        """Posts the images (one, or a carousel) with their alt text and the
        caption `text`. Returns the post's instagram.com link."""
        api = _Api(self.access_token())
        user_id = api.get("me", fields="user_id")["user_id"]
        done = self._already_published(api, user_id, text)
        if done:
            return done

        bucket = r2.R2()
        keys = []
        try:
            urls = []
            for image in images:
                key, url = bucket.put_temporary(image.data)
                keys.append(key)
                urls.append(url)

            if len(images) == 1:
                container = api.post(f"{user_id}/media", image_url=urls[0], caption=text, alt_text=alt_texts[0])["id"]
            else:
                children = [api.post(f"{user_id}/media", image_url=url, is_carousel_item="true", alt_text=alt)["id"]
                            for url, alt in zip(urls, alt_texts)]
                for child in children:
                    self._wait_ready(api, child)
                container = api.post(f"{user_id}/media", media_type="CAROUSEL",
                                     children=",".join(children), caption=text)["id"]
            self._wait_ready(api, container)

            # From here a timeout leaves it unknown whether the post went out;
            # the marker lets the next attempt check before posting again.
            self._save_json(self.inflight_path, {"caption": text, "started_at": _now().isoformat()})
            try:
                media_id = api.post(f"{user_id}/media_publish", creation_id=container)["id"]
            except ApiError:
                self.inflight_path.unlink(missing_ok=True)  # a definite "no": nothing was posted
                raise
            self.inflight_path.unlink(missing_ok=True)
            try:
                return api.get(media_id, fields="permalink").get("permalink") or f"instagram:{media_id}"
            except Exception:
                return f"instagram:{media_id}"  # posted; the link is just a nicety
        finally:
            for key in keys:
                try:
                    bucket.delete(key)
                except Exception as exc:
                    logger.warning("[%s] couldn't delete %s from R2 (the bucket's lifecycle rule will): %s",
                                   self.account, key, exc)
