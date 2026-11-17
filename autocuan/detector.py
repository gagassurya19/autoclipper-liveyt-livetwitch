"""Finds hype moments from chat speed (vs. normal) and audio spikes, then cuts a raw window."""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .audio import LoudnessTracker
from .chat import ChatTracker
from .clipper import cut_window
from .config import Config, Streamer
from .recorder import list_segments, seg_dir

log = logging.getLogger("detector")


@dataclass
class Candidate:
    clip_id: str
    streamer: Streamer
    raw_path: Path            # raw window video (copy of stream)
    window_start: float       # epoch of raw_path t=0 (after offset)
    offset: float             # seconds into raw_path where the window starts
    peak_in_window: float     # seconds from window start to the chat/audio peak
    reason: str
    chat_ratio: float = 0.0
    audio_z: float = 0.0
    chat_sample: list[str] = field(default_factory=list)
    manual: bool = False      # triggered by /clip on Telegram


class Detector:
    def __init__(self, cfg: Config, s: Streamer, chat: ChatTracker | None, out_q: asyncio.Queue):
        self.cfg, self.s, self.chat, self.q = cfg, s, chat, out_q
        self.d = cfg["detector"]
        self.loud = LoudnessTracker()
        self.started = time.time()
        self.last_trigger = 0.0
        lag = self.d.get("chat_lag_s", {})
        self.chat_lag = float(lag.get(s.platform, 5) if isinstance(lag, dict) else lag)

    def manual(self, moment: float):
        """/clip from Telegram: the peak is exactly `moment` (epoch). Skips the cooldown,
        and pauses auto-triggers for a cooldown so the same moment isn't clipped twice."""
        self.last_trigger = time.time()
        log.info("[%s] TRIGGER manual /clip", self.s.id)
        asyncio.create_task(self._finish(moment, "manual /clip", 0.0, 0.0, fixed_peak=True))

    # ---- signals -------------------------------------------------------
    def chat_ratio(self, now: float) -> tuple[float, int]:
        """Chat speed vs normal, evaluated `chat_lag` seconds ago (when counts are complete)."""
        if not self.chat or not self.chat.alive:
            return 0.0, 0
        now = now - self.chat_lag
        w = self.d["chat_window_s"]
        cur = self.chat.count_between(now - w, now)
        base_s = self.d["baseline_minutes"] * 60
        first = self.chat.first_ts
        if first is None:
            return 0.0, cur
        # only real history counts as "normal" (not the silence before chat connected)
        base_start = max(now - base_s, first - self.chat_lag + 5)
        if (now - w) - base_start < 60:
            return 0.0, cur
        series = self.chat.series(base_start, now - w)
        windows = [sum(series[i:i + w]) for i in range(0, len(series) - w, w)]
        base = max(statistics.median(windows), 2.0)
        return cur / base, cur

    async def run(self, stop: asyncio.Event):
        d = seg_dir(self.cfg, self.s)
        while not stop.is_set():
            await asyncio.sleep(2)
            for st, p in list_segments(d)[-3:]:
                await asyncio.to_thread(self.loud.ingest, st, p)
            now = time.time()
            if now - self.started < self.d["warmup_s"] or now - self.last_trigger < self.d["cooldown_s"]:
                continue
            ratio, cur = self.chat_ratio(now)
            z = self.loud.zscore(now)
            reason = None
            if ratio >= self.d["chat_ratio_trigger"] and cur >= self.d["min_msgs_in_window"]:
                reason = f"chat x{ratio:.1f}"
            elif z >= self.d["audio_z_trigger"] and ratio >= 1.3:
                reason = f"audio z{z:.1f} + chat x{ratio:.1f}"
            elif z >= self.d["audio_only_z"]:
                reason = f"audio z{z:.1f}"
            if reason:
                self.last_trigger = now
                # a chat trigger refers to the moment chat_lag ago; an audio-only one to now
                trig = now - self.chat_lag if reason.startswith("chat") else now
                log.info("[%s] TRIGGER %s", self.s.id, reason)
                asyncio.create_task(self._finish(trig, reason, ratio, z))

    async def _finish(self, trig: float, reason: str, ratio: float, z: float, fixed_peak: bool = False):
        """Wait for the post-roll to be recorded, locate the peak, cut the raw window."""
        post = self.d["post_roll_s"]
        pre = self.d["pre_roll_s"]
        seg_s = self.cfg["segment_seconds"]
        # wait until the post-roll is recorded AND its chat has arrived
        wait_until = trig + post + max(seg_s * 2, self.chat_lag) + 3
        await asyncio.sleep(max(0.0, wait_until - time.time()))

        peak = trig
        chat_series = self.chat.series(trig - 20, trig + post) if (self.chat and not fixed_peak) else []
        if fixed_peak:
            pass
        elif chat_series and sum(chat_series) > 0:
            sm = [sum(chat_series[max(0, i - 1): i + 2]) for i in range(len(chat_series))]
            # chat peak happens after the moment itself
            peak = trig - 20 + sm.index(max(sm)) - self.d["chat_offset_s"]
        else:
            a = self.loud.series(trig - 20, trig + 2)
            vals = [(v if v is not None else -99) for v in a]
            if vals:
                peak = trig - 20 + vals.index(max(vals))

        t0, t1 = peak - pre, peak + post
        clip_id = f"{self.s.id}-{time.strftime('%m%d-%H%M%S', time.localtime(peak))}-{uuid.uuid4().hex[:4]}"
        out = self.cfg.data_dir / "raw" / f"{clip_id}.mp4"
        try:
            segs = list_segments(seg_dir(self.cfg, self.s))
            raw, offset = await asyncio.to_thread(cut_window, segs, seg_s, t0, t1, out)
        except Exception as e:  # noqa: BLE001
            log.error("[%s] cut failed: %s", self.s.id, e)
            return
        sample = self.chat.sample(peak - 10, peak + post) if self.chat else []
        cand = Candidate(clip_id, self.s, raw, t0, offset, peak - t0, reason, ratio, z, sample,
                         manual=fixed_peak)
        log.info("[%s] candidate %s queued", self.s.id, clip_id)
        await self.q.put(cand)
