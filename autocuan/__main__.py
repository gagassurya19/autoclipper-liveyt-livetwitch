"""CLI.

  python -m autocuan run                            clip every URL under `streams:` in config.yaml
  python -m autocuan run --url <url> [--url <url>]  clip these URLs instead (YouTube live / Twitch)
  python -m autocuan test-telegram                  check bot token / chat id
  python -m autocuan clip --file X.mp4 --peak 95 [--url <source url>] [--language id] [--no-send]
                                                    offline test on any downloaded VOD
  python -m autocuan snap --url <url> [--file X.mp4] [--at 30]
                                                    save a gridded frame to measure the facecam box
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import subprocess
import time
from pathlib import Path

from . import config as config_mod
from . import pipeline, telegram
from .commands import CommandBot
from .chat import ChatTracker
from .db import DB
from .detector import Candidate, Detector
from .config import Streamer
from .recorder import record_forever
from .resolve import resolve

log = logging.getLogger("main")


def find_entry(cfg, url: str):
    """Use the per-stream options from config.yaml if this URL is listed there."""
    for e in cfg.stream_entries:
        if (e if isinstance(e, str) else e.get("url")) == url:
            return e
    return url


async def stream_task(cfg, s: Streamer, q: asyncio.Queue, stop: asyncio.Event, live: dict):
    """Record + chat + detect one stream until it ends or we stop."""
    mine = asyncio.Event()
    online = asyncio.Event()
    chat = ChatTracker(s, cookies_from_browser=(cfg.raw.get("recorder") or {}).get("cookies_from_browser"))
    chat.start()
    detector = Detector(cfg, s, chat, q)
    live[s.id] = detector                      # reachable by Telegram /clip
    det = asyncio.create_task(detector.run(mine))
    rec = asyncio.create_task(record_forever(cfg, s, mine, online))
    stopper = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait({rec, stopper}, return_when=asyncio.FIRST_COMPLETED)
    if rec in done and rec.result() == "ended":
        await asyncio.sleep(cfg["detector"]["post_roll_s"] + 20)  # let the last clip finish cutting
        try:
            await asyncio.to_thread(telegram.send_text, cfg, f"⏹ {s.name} stream ended — stopped clipping it")
        except Exception:  # noqa: BLE001
            pass
    live.pop(s.id, None)
    mine.set()
    chat.stop()
    for t in (det, rec, stopper):
        t.cancel()


async def run_live(cfg, urls: list[str] | None):
    db = DB(cfg.data_dir / "clips.db")
    q: asyncio.Queue[Candidate] = asyncio.Queue()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    entries = [find_entry(cfg, u) for u in urls] if urls else cfg.stream_entries
    if not entries:
        raise SystemExit("no streams: add URLs under `streams:` in config.yaml or pass --url")
    default_lang = cfg.raw.get("default_language", "auto")
    streamers = [await asyncio.to_thread(resolve, e, default_lang) for e in entries]
    live: dict = {}
    tasks = [asyncio.create_task(stream_task(cfg, s, q, stop, live)) for s in streamers]
    if (cfg.raw.get("manual") or {}).get("telegram_commands", True):
        tasks.append(asyncio.create_task(CommandBot(cfg, live).run(stop)))

    async def worker(n: int):
        while not stop.is_set():
            try:
                cand = await asyncio.wait_for(q.get(), timeout=2)
            except asyncio.TimeoutError:
                continue
            log.info("editor-%d: %s (queue %d)", n, cand.clip_id, q.qsize())
            await asyncio.to_thread(pipeline.process, cfg, db, cand)

    for n in range(int(cfg["workers"]["editors"])):
        tasks.append(asyncio.create_task(worker(n)))

    try:
        await asyncio.to_thread(telegram.send_text, cfg,
                                "🟢 auto-clipper started: " + ", ".join(s.name for s in streamers)
                                + "\nSend /clip when you see something funny (/clip 30 = 30 s ago)")
    except Exception as e:  # noqa: BLE001
        log.warning("telegram start message failed: %s", e)
    log.info("running for: %s — Ctrl+C to stop", ", ".join(s.id for s in streamers))
    await stop.wait()
    for t in tasks:
        t.cancel()


def offline_clip(cfg, args):
    if args.url:
        s = resolve(find_entry(cfg, args.url), args.language or cfg.raw.get("default_language", "auto"))
    else:
        s = Streamer(id="test", name=args.name or "streamer", platform="other", url="",
                     language=args.language or "auto", credit=args.name or "",
                     hashtags=["#GTA6", "#GTAVI", "#shorts"])
    if args.language:
        s.language = args.language
    d = cfg["detector"]
    src = Path(args.file).resolve()
    t0 = max(0.0, args.peak - d["pre_roll_s"])
    cand = Candidate(
        clip_id=f"{s.id}-test-{time.strftime('%H%M%S')}", streamer=s, raw_path=src,
        window_start=0, offset=t0, peak_in_window=args.peak - t0, reason="manual test",
    )
    out = pipeline.process(cfg, DB(cfg.data_dir / "clips.db"), cand, send=not args.no_send)
    print(out or "failed — see log")


def snap(cfg, args):
    s = resolve(find_entry(cfg, args.url))
    out = cfg.data_dir / f"snap_{s.id}.png"
    grid = ("drawgrid=w=iw/10:h=ih/10:t=2:c=yellow@0.6,"
            "drawtext=text='grid = 10%% steps  (x,y,w,h as fractions)':x=20:y=20:fontsize=36:fontcolor=yellow")
    if args.file:
        cmd = f"ffmpeg -y -loglevel error -ss {args.at} -i '{args.file}' -frames:v 1 -vf \"{grid}\" '{out}'"
    else:
        cmd = (f"streamlink --stdout '{s.url}' best | ffmpeg -y -loglevel error -i pipe:0 "
               f"-frames:v 1 -vf \"{grid}\" '{out}'")
    subprocess.run(cmd, shell=True, check=True, timeout=120)
    print(f"saved {out}\nThen in config.yaml use the dict form for this URL:\n"
          f"  - url: {s.url}\n    facecam: {{x: .., y: .., w: .., h: ..}}")


def main():
    ap = argparse.ArgumentParser(prog="autocuan")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--url", action="append", help="stream URL (repeat for several); default: config streams")
    sub.add_parser("test-telegram")
    c = sub.add_parser("clip")
    c.add_argument("--file", required=True)
    c.add_argument("--url", help="original stream URL (for channel name/@handle)")
    c.add_argument("--name", help="streamer name for the credit pill, if no --url")
    c.add_argument("--language", choices=["id", "en", "auto"])
    c.add_argument("--peak", type=float, required=True, help="seconds into the file where the hype peaks")
    c.add_argument("--no-send", action="store_true")
    sn = sub.add_parser("snap")
    sn.add_argument("--url", required=True)
    sn.add_argument("--file")
    sn.add_argument("--at", type=float, default=30)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)-9s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = config_mod.load()
    from . import resolve as resolve_mod
    resolve_mod.COOKIES_FROM_BROWSER = (cfg.raw.get("recorder") or {}).get("cookies_from_browser")

    if args.cmd == "run":
        asyncio.run(run_live(cfg, args.url))
    elif args.cmd == "test-telegram":
        print(telegram.send_text(cfg, "✅ AUTO CUAN bot connected"))
    elif args.cmd == "clip":
        offline_clip(cfg, args)
    elif args.cmd == "snap":
        snap(cfg, args)


if __name__ == "__main__":
    main()
