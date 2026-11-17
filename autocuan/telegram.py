"""Send finished shorts to Telegram in full 1080x1920.

Note: the public Bot API (api.telegram.org) refuses bot uploads over 50 MB. Clips are
encoded to stay under `max_upload_mb`; if one is still too big it is re-encoded at a
calculated bitrate (still 1080x1920). To lift the limit to 2 GB, run your own
Bot API server (github.com/tdlib/telegram-bot-api) and point `telegram.api_base` at it.
"""
from __future__ import annotations

import html
import json
import logging
import time
from pathlib import Path

import httpx

from .config import Config, env

log = logging.getLogger("telegram")


def _api(cfg: Config, method: str) -> str:
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing in .env")
    return f"{cfg['telegram']['api_base'].rstrip('/')}/bot{token}/{method}"


def _post(cfg: Config, method: str, **kw) -> dict:
    for attempt in range(5):
        try:
            r = httpx.post(_api(cfg, method), timeout=300, **kw)
            data = r.json()
            if data.get("ok"):
                return data["result"]
            retry = (data.get("parameters") or {}).get("retry_after")
            if retry:
                log.warning("telegram rate limit, waiting %ss", retry)
                time.sleep(int(retry) + 1)
                continue
            raise RuntimeError(f"telegram {method}: {data.get('description')}")
        except (httpx.TransportError, httpx.TimeoutException) as e:
            log.warning("telegram network error (%s), retry %d", e, attempt + 1)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"telegram {method} failed after retries")


def send_text(cfg: Config, text: str, chat_id=None, reply_to: int | None = None,
              buttons: list | None = None) -> dict:
    data = {"chat_id": chat_id or env("TELEGRAM_CHAT_ID"), "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if reply_to:
        data["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    return _post(cfg, "sendMessage", data=data)


def get_updates(cfg: Config, offset: int | None, timeout: int = 25) -> list[dict]:
    """Long-poll for new messages / button taps sent to the bot."""
    params = {"timeout": timeout,
              "allowed_updates": json.dumps(["message", "channel_post", "callback_query"])}
    if offset is not None:
        params["offset"] = offset
    r = httpx.get(_api(cfg, "getUpdates"), params=params, timeout=timeout + 10)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram getUpdates: {data.get('description')}")
    return data["result"]


def answer_callback(cfg: Config, callback_id: str) -> None:
    httpx.post(_api(cfg, "answerCallbackQuery"), data={"callback_query_id": callback_id}, timeout=10)


def set_commands(cfg: Config, cmds: list[tuple[str, str]]) -> None:
    _post(cfg, "setMyCommands", data={"commands": json.dumps(
        [{"command": c, "description": d} for c, d in cmds])})


def caption_for(s, pack: dict, meta: dict) -> str:
    tags = " ".join(dict.fromkeys([*s.hashtags, *pack.get("hashtags", [])]))
    score = pack.get("score")
    lines = [
        f"<b>{html.escape(pack.get('title', ''))}</b>",
        "",
        html.escape(tags),
        "",
        f"🎮 {html.escape(s.name)} · {html.escape(meta.get('reason', ''))}"
        + (f" · score {score}" if score is not None else ""),
        f"🎬 {meta.get('duration', 0):.0f}s · 1080×1920 · {meta.get('size_mb', 0):.1f} MB",
        f"🆔 <code>{html.escape(meta.get('clip_id', ''))}</code>",
    ]
    return "\n".join(lines)[:1024]


def send_video(cfg: Config, video: Path, caption: str, duration: float, thumb: Path | None = None) -> dict:
    chat_id = env("TELEGRAM_CHAT_ID")
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID missing in .env")
    as_doc = cfg["telegram"].get("send_as", "video") == "document"
    files = {("document" if as_doc else "video"): (video.name, video.open("rb"), "video/mp4")}
    if thumb and thumb.exists():
        files["thumbnail"] = (thumb.name, thumb.open("rb"), "image/jpeg")
    data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    if not as_doc:
        data.update({"width": "1080", "height": "1920", "duration": str(int(round(duration))),
                     "supports_streaming": "true"})
    try:
        return _post(cfg, "sendDocument" if as_doc else "sendVideo", data=data, files=files)
    finally:
        for f in files.values():
            f[1].close()
