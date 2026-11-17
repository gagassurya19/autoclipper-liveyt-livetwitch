"""Turn a stream URL into a Streamer: platform, channel name, @handle, language, hashtags.

Works with:
  https://www.youtube.com/watch?v=XXXX      (a live video)
  https://www.youtube.com/@channel/live     (whatever that channel is streaming)
  https://youtu.be/XXXX
  https://www.twitch.tv/channel
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from urllib.parse import urlparse

from .config import Streamer

log = logging.getLogger("resolve")

LANG_TAGS = {
    "id": ["#GTA6", "#GTA6Indonesia", "#shorts"],
    "en": ["#GTA6", "#GTAVI", "#shorts"],
}


def platform_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "twitch.tv" in host:
        return "twitch"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    return "other"


COOKIES_FROM_BROWSER: str | None = None   # set from config at startup


def ytdlp_info(url: str) -> dict:
    ck = ["--cookies-from-browser", COOKIES_FROM_BROWSER] if COOKIES_FROM_BROWSER else []
    try:
        out = subprocess.run(
            ["yt-dlp", "--no-warnings", "--skip-download", "--no-playlist", *ck, "-J", url],
            capture_output=True, text=True, timeout=90,
        )
        return (json.loads(out.stdout) if out.stdout.strip() else None) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("yt-dlp metadata failed for %s: %s", url, e)
        return {}


def live_status(url: str) -> str | None:
    """'is_live' | 'was_live' | 'post_live' | 'not_live' | 'is_upcoming' | None (unknown)."""
    return ytdlp_info(url).get("live_status")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())[:20] or "stream"


def resolve(entry: str | dict, default_language: str = "auto") -> Streamer:
    opts = {"url": entry} if isinstance(entry, str) else dict(entry)
    url = opts["url"].strip()
    plat = platform_of(url)
    info = ytdlp_info(url)

    name = opts.get("name") or info.get("channel") or info.get("uploader")
    handle = info.get("uploader_id") or ""
    if plat == "twitch" and not handle:
        handle = urlparse(url).path.strip("/").split("/")[0]
    if not name:
        name = handle.lstrip("@") or urlparse(url).path.strip("/") or "stream"
    credit = opts.get("credit") or (("@" + handle.lstrip("@")) if handle else name)

    lang = opts.get("language") or default_language
    if lang == "auto" and info.get("language"):
        lang = str(info["language"]).split("-")[0].lower()

    base = LANG_TAGS.get(lang, LANG_TAGS["en"])
    own = "#" + _slug(handle or name)
    hashtags = opts.get("hashtags") or [*base[:-1], own, base[-1]]

    sid = opts.get("id") or f"{_slug(handle or name)}-{hashlib.sha1(url.encode()).hexdigest()[:4]}"
    s = Streamer(
        id=sid, name=name, platform=plat, url=url, language=lang, credit=credit, hashtags=hashtags,
        crop_center_x=float(opts.get("crop_center_x", 0.5)), facecam=opts.get("facecam"),
    )
    log.info("stream %s → %s (%s, %s, lang=%s, %s)", url, name, credit, plat, lang,
             info.get("live_status", "status unknown"))
    return s
