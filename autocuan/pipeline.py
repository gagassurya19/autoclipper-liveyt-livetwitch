"""Candidate → transcribe → AI package → render 1080x1920 → Telegram."""
from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

from . import ai, editor, telegram
from .config import Config
from .db import DB
from .detector import Candidate

log = logging.getLogger("pipeline")


def process(cfg: Config, db: DB, cand: Candidate, send: bool = True) -> Path | None:
    s = cand.streamer
    d = cfg["detector"]
    c = cfg["clip"]
    db.upsert(cand.clip_id, streamer=s.id, reason=cand.reason, status="candidate")
    try:
        info = editor.probe(cand.raw_path)
        file_dur = float(info["format"]["duration"])
        win = min(d["pre_roll_s"] + d["post_roll_s"], file_dur - cand.offset)

        words, detected = ai.transcribe(cfg, cand.raw_path, cand.offset, win, s.language)
        if s.language == "auto":
            lang = detected or ("en" if cfg.raw.get("default_language", "auto") == "auto"
                                else cfg["default_language"])
            s = dataclasses.replace(s, language=lang)
            if lang == "id" and "#GTA6Indonesia" not in s.hashtags:
                s.hashtags = ["#GTA6Indonesia", *s.hashtags]
        pack = ai.package(cfg, s, words, cand.chat_sample, win, cand.peak_in_window)
        log.info("[%s] %s score=%s cut=%.1f-%.1f hook=%r", s.id, cand.clip_id, pack.get("score"),
                 pack["start"], pack["end"], pack["hook"])

        if pack.get("spoiler"):
            db.upsert(cand.clip_id, status="skipped", error="spoiler", title=pack.get("title"))
            log.info("[%s] skipped (spoiler)", cand.clip_id)
            return None
        score = pack.get("score")
        if score is not None and not cand.manual and int(score) < int(c.get("min_score", 0)):
            db.upsert(cand.clip_id, status="skipped", score=int(score), error="low score")
            log.info("[%s] skipped (score %s)", cand.clip_id, score)
            return None

        rel = [{**w, "start": w["start"] - pack["start"], "end": w["end"] - pack["start"]}
               for w in words if w["end"] > pack["start"] and w["start"] < pack["end"]]
        out_dir = cfg.data_dir / "renders"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{cand.clip_id}.mp4"
        start, end = cand.offset + pack["start"], cand.offset + pack["end"]
        editor.render(cfg, s, cand.raw_path, start, end, pack, rel, out)

        total = (end - start) + float(c["outro_s"])
        max_mb = float(cfg["telegram"]["max_upload_mb"])
        size_mb = out.stat().st_size / 2**20
        if size_mb > max_mb:
            kbps = int((max_mb * 8 * 1024 * 0.92) / total - 170)
            log.info("[%s] %.1f MB > %.0f MB, re-encoding at %d kbps", cand.clip_id, size_mb, max_mb, kbps)
            editor.render(cfg, s, cand.raw_path, start, end, pack, rel, out, bitrate=f"{kbps}k")
            size_mb = out.stat().st_size / 2**20
        db.upsert(cand.clip_id, status="rendered", score=score, title=pack.get("title"), path=str(out))

        if send:
            thumb = editor.thumbnail(out, t=1.0)
            cap = telegram.caption_for(s, pack, {"reason": cand.reason, "duration": total,
                                                 "size_mb": size_mb, "clip_id": cand.clip_id})
            msg = telegram.send_video(cfg, out, cap, total, thumb)
            db.upsert(cand.clip_id, status="sent", tg_message_id=msg.get("message_id"))
            log.info("[%s] sent to Telegram (%.1f MB)", cand.clip_id, size_mb)
        return out
    except Exception as e:  # noqa: BLE001
        log.exception("[%s] failed", cand.clip_id)
        db.upsert(cand.clip_id, status="failed", error=str(e)[:500])
        if send:
            try:
                telegram.send_text(cfg, f"⚠️ clip {cand.clip_id} failed: {str(e)[:300]}")
            except Exception:  # noqa: BLE001
                pass
        return None
