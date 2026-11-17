"""Records a live stream into short copy-only .ts segments named by their start epoch.

streamlink --stdout <url> best | ffmpeg -c copy -f segment -strftime 1 <dir>/%s.ts

A ring buffer keeps only the last `buffer_minutes` on disk. If the stream is offline
or drops, we wait and retry forever (stream may go live later).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from pathlib import Path

from .config import Config, Streamer
from .resolve import live_status

log = logging.getLogger("recorder")

QUALITY = "1080p60,1080p,best"


def is_single_video(url: str) -> bool:
    """A specific live video (ends for good), as opposed to a channel URL (can go live again)."""
    return bool(re.search(r"(watch\?v=|youtu\.be/|youtube\.com/live/)[\w-]+", url))


def _record_cmd(cfg: Config, s: Streamer, d: Path, seg_s: int) -> str:
    rec = cfg.raw.get("recorder", {}) or {}
    cookies = rec.get("cookies_from_browser")
    backend = rec.get("backend", "streamlink")
    seg = (f"ffmpeg -hide_banner -loglevel error -i pipe:0 -map 0:v:0 -map 0:a:0? -c copy "
           f"-f segment -segment_time {seg_s} -reset_timestamps 1 -strftime 1 '{d}/%s.ts'")
    if backend == "yt-dlp" or (cookies and s.platform == "youtube"):
        ck = f"--cookies-from-browser {cookies} " if cookies else ""
        # 301/300 = 1080p60/720p60 muxed HLS on YouTube live; 96 = 1080p30
        src = (f"yt-dlp --no-warnings --quiet {ck}--hls-use-mpegts --no-part "
               f"-f '301/96/300/95/best[height<=1080]/best' -o - '{s.url}'")
    else:
        src = (f"streamlink --stdout --loglevel warning --retry-open 3 "
               f"--twitch-disable-ads '{s.url}' '{QUALITY}'")
    return f"{src} | {seg}"


def seg_dir(cfg: Config, s: Streamer) -> Path:
    d = cfg.data_dir / "segments" / s.id
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_segments(d: Path) -> list[tuple[float, Path]]:
    """Return (start_epoch, path) sorted, skipping the one still being written."""
    segs = []
    for p in d.glob("*.ts"):
        try:
            segs.append((float(p.stem), p))
        except ValueError:
            continue
    segs.sort()
    return segs[:-1]  # newest one is still open


async def _cleanup_loop(d: Path, keep_s: float, stop: asyncio.Event):
    while not stop.is_set():
        cutoff = time.time() - keep_s
        for p in d.glob("*.ts"):
            try:
                if float(p.stem) < cutoff:
                    p.unlink(missing_ok=True)
            except ValueError:
                pass
        await asyncio.sleep(30)


def _summarize_err(lines: list[str]) -> str:
    """Turn streamlink/ffmpeg stderr into one short readable line."""
    gaps = sum(int(m.group(1)) for l in lines if (m := re.search(r"Sequence gap of (\d+) segments", l)))
    keep = []
    for l in lines:
        l = l.strip()
        if not l or "Sequence gap of" in l or l in keep:
            continue
        l = re.sub(r"^\[[\w.]+\]\[\w+\]\s*|^\[[^\]]*@ 0x[0-9a-f]+\]\s*", "", l)
        keep.append(l)
    # ffmpeg's "Error opening input" lines just echo that the source died; prefer the source's reason
    src = [l for l in keep if "Error opening input" not in l and "pipe:0" not in l]
    msg = " | ".join((src or keep)[-2:])[:200]
    if gaps:
        msg = (f"{gaps} segments dropped (network hiccup); " + msg).strip("; ")
    return msg or "no error output"


async def _drain(stream, buf: deque, gaps: list):
    """Read stderr continuously, so a chatty streamlink can't fill the pipe and freeze."""
    while True:
        line = await stream.readline()
        if not line:
            return
        t = line.decode(errors="ignore").rstrip()
        buf.append(t)
        m = re.search(r"Sequence gap of (\d+) segments", t)
        if m:
            gaps[0] += int(m.group(1))


async def _wait_until_ended(s: Streamer, checks: int = 6, every: float = 10) -> str | None:
    """YouTube keeps saying is_live for ~1 min after a stream ends. Poll a few times."""
    status = None
    for i in range(checks):
        status = await asyncio.to_thread(live_status, s.url)
        if status in ("was_live", "post_live", "not_live"):
            return status
        if status == "is_live" and i >= 1:
            return status   # confirmed still live twice -> it was just a drop, reconnect
        await asyncio.sleep(every)
    return status


async def record_forever(cfg: Config, s: Streamer, stop: asyncio.Event, online: asyncio.Event):
    d = seg_dir(cfg, s)
    seg_s = int(cfg["segment_seconds"])
    single = is_single_video(s.url)
    asyncio.create_task(_cleanup_loop(d, cfg["buffer_minutes"] * 60, stop))
    while not stop.is_set():
        log.info("[%s] opening stream %s", s.id, s.url)
        cmd = _record_cmd(cfg, s, d, seg_s)
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        errbuf: deque = deque(maxlen=40)
        gaps = [0]
        drain = asyncio.create_task(_drain(proc.stderr, errbuf, gaps))
        # consider the stream online once the first segment lands
        started = time.time()
        reported_gaps = 0
        while proc.returncode is None and not stop.is_set():
            if not online.is_set() and any(float(p.stem) >= started - 1 for p in d.glob("*.ts") if p.stem.isdigit()):
                online.set()
                log.info("[%s] LIVE — recording", s.id)
            if gaps[0] - reported_gaps >= 5:
                log.info("[%s] network hiccup: %d segments dropped so far (clips near it may stutter)",
                         s.id, gaps[0])
                reported_gaps = gaps[0]
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        if stop.is_set() and proc.returncode is None:
            proc.terminate()
            await proc.wait()
            drain.cancel()
            break
        try:
            await asyncio.wait_for(drain, timeout=3)
        except asyncio.TimeoutError:
            drain.cancel()
        was_online = online.is_set()
        online.clear()
        err = _summarize_err(list(errbuf))
        if single:
            if was_online:
                log.info("[%s] stream stopped sending video — checking if it ended…", s.id)
            status = await _wait_until_ended(s)
            if status in ("was_live", "post_live", "not_live"):
                log.info("[%s] stream has ended (%s) — stopping this stream", s.id, status)
                return "ended"
            if status == "is_live":
                log.info("[%s] still live — reconnecting (%s)", s.id, err)
                continue
            log.warning("[%s] can't reach the stream (%s) — retry in 15 s", s.id, err)
            await asyncio.sleep(15)
            continue
        log.info("[%s] offline (%s) — checking again in 30 s", s.id, err)
        await asyncio.sleep(30)
