"""Telegram remote control: send /clip to the bot and it clips the moment you just saw.

  /clip            clip the moment ~`manual.default_ago_s` seconds before your message
  /clip 30         the moment was 30 s ago
  /clip windah 20  pick a stream by name when several are running (any order)
  /status          list the streams being clipped

Accepted from the review chat (TELEGRAM_CHAT_ID) and from user ids listed in
TELEGRAM_ADMIN_IDS (comma-separated), e.g. a private chat with the bot.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from . import telegram
from .config import Config, env

log = logging.getLogger("commands")


class CommandBot:
    def __init__(self, cfg: Config, detectors: dict):
        self.cfg = cfg
        self.detectors = detectors          # streamer id -> Detector (live ones only)
        m = cfg.raw.get("manual") or {}
        self.default_ago = float(m.get("default_ago_s", 10))
        self.max_ago = float(cfg["buffer_minutes"]) * 60 - cfg["detector"]["pre_roll_s"] - 30
        self.chat_id = str(env("TELEGRAM_CHAT_ID") or "")
        self.admins = {x.strip() for x in (env("TELEGRAM_ADMIN_IDS") or "").split(",") if x.strip()}
        self.offset: int | None = None

    # ---- auth ------------------------------------------------------------
    def allowed(self, chat_id, user_id) -> bool:
        return str(chat_id) == self.chat_id or (user_id is not None and str(user_id) in self.admins)

    # ---- loop ------------------------------------------------------------
    async def run(self, stop: asyncio.Event):
        try:
            await asyncio.to_thread(telegram.set_commands, self.cfg, [
                ("clip", "clip what just happened (/clip 30 = 30 s ago)"),
                ("status", "streams being clipped"),
            ])
        except Exception as e:  # noqa: BLE001
            log.debug("setMyCommands failed: %s", e)
        # skip commands sent while the clipper was off
        try:
            old = await asyncio.to_thread(telegram.get_updates, self.cfg, -1, 0)
            if old:
                self.offset = old[-1]["update_id"] + 1
        except Exception as e:  # noqa: BLE001
            log.warning("telegram getUpdates failed: %s", e)
        log.info("telegram commands on: send /clip to the bot")
        while not stop.is_set():
            try:
                ups = await asyncio.to_thread(telegram.get_updates, self.cfg, self.offset, 25)
            except Exception as e:  # noqa: BLE001
                log.warning("telegram getUpdates failed: %s", e)
                await asyncio.sleep(5)
                continue
            for u in ups:
                self.offset = u["update_id"] + 1
                try:
                    await self.handle(u)
                except Exception:  # noqa: BLE001
                    log.exception("command failed")

    # ---- handling ----------------------------------------------------------
    async def handle(self, u: dict):
        if "callback_query" in u:
            return await self.handle_button(u["callback_query"])
        msg = u.get("message") or u.get("channel_post")
        if not msg or not msg.get("text"):
            return
        text = msg["text"].strip()
        if not text.startswith("/"):
            return
        parts = text.split()
        cmd = parts[0][1:].split("@")[0].lower()
        args = parts[1:]
        chat_id = msg["chat"]["id"]
        user_id = (msg.get("from") or {}).get("id")
        if cmd not in ("clip", "c", "status", "id"):
            return
        if cmd == "id":
            return await self.reply(chat_id, msg, f"chat id <code>{chat_id}</code> · your user id <code>{user_id}</code>")
        if not self.allowed(chat_id, user_id):
            log.warning("ignored /%s from chat %s user %s (not allowed)", cmd, chat_id, user_id)
            return await self.reply(chat_id, msg, "⛔ not allowed. Add your user id "
                                    f"<code>{user_id}</code> to TELEGRAM_ADMIN_IDS in .env")
        if cmd == "status":
            names = [d.s.name for d in self.detectors.values()]
            return await self.reply(chat_id, msg, "🔴 clipping: " + (", ".join(names) or "nothing live"))

        sent_at = float(msg.get("date", time.time()))
        ago, words = self.default_ago, []
        for a in args:
            if re.fullmatch(r"\d+(\.\d+)?s?", a):
                ago = float(a.rstrip("s"))
            else:
                words.append(a.lower())
        if ago > self.max_ago:
            return await self.reply(chat_id, msg, f"⚠️ only the last {int(self.max_ago)} s are kept — try a smaller number")
        moment = sent_at - ago

        dets = list(self.detectors.values())
        if words:
            q = " ".join(words)
            dets = [d for d in dets if q in d.s.name.lower() or q in d.s.id.lower()]
        if not dets:
            return await self.reply(chat_id, msg, "⚠️ no live stream matches" if words else "⚠️ nothing is live right now")
        if len(dets) > 1:
            buttons = [[{"text": d.s.name, "callback_data": f"clip|{int(moment)}|{d.s.id}"[:64]}] for d in dets]
            return await asyncio.to_thread(telegram.send_text, self.cfg, "Which stream?", chat_id=chat_id,
                                           reply_to=msg.get("message_id"), buttons=buttons)
        await self.trigger(dets[0], moment, ago, chat_id, msg)

    async def handle_button(self, cb: dict):
        chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
        user_id = (cb.get("from") or {}).get("id")
        try:
            await asyncio.to_thread(telegram.answer_callback, self.cfg, cb["id"])
        except Exception:  # noqa: BLE001
            pass
        if not self.allowed(chat_id, user_id):
            return
        try:
            _, ts, sid = cb.get("data", "").split("|", 2)
        except ValueError:
            return
        det = self.detectors.get(sid)
        if not det:
            return await asyncio.to_thread(telegram.send_text, self.cfg, "⚠️ that stream is no longer live",
                                           chat_id=chat_id)
        await self.trigger(det, float(ts), time.time() - float(ts), chat_id, cb.get("message"))

    async def trigger(self, det, moment: float, ago: float, chat_id, msg):
        det.manual(moment)
        at = time.strftime("%H:%M:%S", time.localtime(moment))
        await self.reply(chat_id, msg, f"🎬 clipping <b>{det.s.name}</b> at {at} ({ago:.0f} s ago) — "
                         "the short arrives here in ~1–2 min")

    async def reply(self, chat_id, msg, text: str):
        await asyncio.to_thread(telegram.send_text, self.cfg, text, chat_id=chat_id,
                                reply_to=(msg or {}).get("message_id"))
