"""Live chat reader, run in a background thread.

YouTube: yt-dlp writes the live chat (`--sub-langs live_chat`) to a JSON-lines file as it
         arrives; we tail that file. (yt-dlp is actively maintained, unlike chat-downloader.)
Twitch:  anonymous IRC (irc.chat.twitch.tv, justinfan login) — no account or API key needed.

Keeps per-second message counts and the recent messages, which are handed to the AI
copywriter as context for a clip.
"""
from __future__ import annotations

import collections
import json
import logging
import random
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from .config import Streamer

log = logging.getLogger("chat")


def _text_renderers(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "liveChatTextMessageRenderer":
                yield v
            else:
                yield from _text_renderers(v)
    elif isinstance(o, list):
        for x in o:
            yield from _text_renderers(x)


class ChatTracker:
    def __init__(self, s: Streamer, keep_s: int = 900, cookies_from_browser: str | None = None):
        self.s = s
        self.keep_s = keep_s
        self.cookies = cookies_from_browser
        self.counts: dict[int, int] = collections.defaultdict(int)
        self.messages: collections.deque[tuple[float, str, str]] = collections.deque(maxlen=5000)
        self.lock = threading.Lock()
        self.alive = False
        self.total = 0
        self.first_ts: float | None = None   # first message we saw (baseline starts here)
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._t = threading.Thread(target=self._run, daemon=True, name=f"chat-{s.id}")

    def start(self):
        self._t.start()

    def stop(self):
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    # ---- queries -------------------------------------------------------
    def count_between(self, t0: float, t1: float) -> int:
        with self.lock:
            return sum(self.counts.get(i, 0) for i in range(int(t0), int(t1)))

    def series(self, t0: float, t1: float) -> list[int]:
        with self.lock:
            return [self.counts.get(i, 0) for i in range(int(t0), int(t1))]

    def sample(self, t0: float, t1: float, n: int = 60) -> list[str]:
        with self.lock:
            msgs = [m for (ts, _a, m) in self.messages if t0 <= ts <= t1]
        step = max(1, len(msgs) // n)
        return msgs[::step][:n]

    # ---- ingest --------------------------------------------------------
    def _add(self, ts: float, author: str, text: str):
        now = time.time()
        # trust platform timestamps only if sane (clock skew / backlog)
        if not (now - 60 < ts < now + 5):
            ts = now
        with self.lock:
            if self.first_ts is None:
                self.first_ts = now
            self.counts[int(ts)] += 1
            self.messages.append((ts, author, text[:200]))
            self.total += 1
            if len(self.counts) > self.keep_s + 120:
                cut = int(now) - self.keep_s
                for k in [k for k in self.counts if k < cut]:
                    del self.counts[k]

    def _run(self):
        while not self._stop.is_set():
            try:
                if self.s.platform == "twitch":
                    self._twitch()
                else:
                    self._youtube()
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] chat error: %s", self.s.id, e)
            self.alive = False
            if not self._stop.is_set():
                time.sleep(15)

    def _youtube(self):
        if not shutil.which("yt-dlp"):
            raise RuntimeError("yt-dlp not installed")
        td = Path(tempfile.mkdtemp(prefix=f"chat-{self.s.id}-"))
        ck = ["--cookies-from-browser", self.cookies] if self.cookies else []
        cmd = ["yt-dlp", "--no-warnings", "--quiet", "--skip-download", "--write-subs",
               "--sub-langs", "live_chat", *ck, "-o", str(td / "chat.%(ext)s"), self.s.url]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        f = None
        buf = ""
        last_data = time.time()
        try:
            while not self._stop.is_set():
                if f is None:
                    cands = [p for p in td.glob("chat.live_chat.json*") if "Frag" not in p.name]
                    if cands:
                        f = sorted(cands, key=lambda p: len(p.name))[0].open("r", encoding="utf-8", errors="ignore")
                        log.info("[%s] chat connected (YouTube via yt-dlp)", self.s.id)
                chunk = f.read() if f else ""
                if chunk:
                    last_data = time.time()
                    self.alive = True
                    buf += chunk
                    *lines, buf = buf.split("\n")
                    for ln in lines:
                        try:
                            obj = json.loads(ln)
                        except ValueError:
                            continue
                        for r in _text_renderers(obj):
                            runs = (r.get("message") or {}).get("runs") or []
                            text = "".join(x.get("text") or (x.get("emoji", {}).get("shortcuts") or [""])[0]
                                           for x in runs)
                            author = (r.get("authorName") or {}).get("simpleText", "")
                            ts = int(r.get("timestampUsec", 0)) / 1e6 or time.time()
                            self._add(ts, author, text)
                else:
                    if self._proc.poll() is not None:
                        err = (self._proc.stderr.read() or b"").decode(errors="ignore")[-300:]
                        raise RuntimeError(f"yt-dlp chat exited: {err.strip() or 'stream offline?'}")
                    if time.time() - last_data > 120:
                        self.alive = False  # quiet chat or stalled; keep waiting
                    time.sleep(0.5)
        finally:
            if f:
                f.close()
            if self._proc.poll() is None:
                self._proc.terminate()
            shutil.rmtree(td, ignore_errors=True)

    def _twitch(self):
        chan = urlparse(self.s.url).path.strip("/").split("/")[0].lower()
        sock = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=30)
        nick = f"justinfan{random.randint(10000, 99999)}"
        sock.sendall(f"PASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{chan}\r\n".encode())
        log.info("[%s] chat connected (Twitch IRC #%s)", self.s.id, chan)
        self.alive = True
        buf = ""
        sock.settimeout(5)
        try:
            while not self._stop.is_set():
                try:
                    data = sock.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    raise RuntimeError("twitch irc closed")
                buf += data.decode(errors="ignore")
                *lines, buf = buf.split("\r\n")
                for ln in lines:
                    if ln.startswith("PING"):
                        sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                    elif " PRIVMSG #" in ln:
                        author = ln[1:ln.find("!")] if ln.startswith(":") else ""
                        text = ln.split(" :", 1)[1] if " :" in ln else ""
                        self._add(time.time(), author, text)
        finally:
            sock.close()
