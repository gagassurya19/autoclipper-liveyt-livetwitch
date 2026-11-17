"""Per-second loudness (dBFS) of finished recorder segments."""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import numpy as np

SR = 8000


def segment_loudness(path: Path) -> list[float]:
    """RMS level in dBFS for each second of the segment's audio."""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn",
         "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"],
        capture_output=True, timeout=60,
    ).stdout
    a = np.frombuffer(out, dtype=np.int16).astype(np.float32) / 32768.0
    n = len(a) // SR
    if n == 0:
        return []
    a = a[: n * SR].reshape(n, SR)
    rms = np.sqrt((a ** 2).mean(axis=1)) + 1e-9
    return (20 * np.log10(rms)).round(2).tolist()


class LoudnessTracker:
    def __init__(self, keep_s: int = 900):
        self.db: dict[int, float] = {}
        self.done: set[str] = set()
        self.keep_s = keep_s
        self.lock = threading.Lock()

    def ingest(self, start_epoch: float, path: Path):
        if path.name in self.done:
            return
        self.done.add(path.name)
        vals = segment_loudness(path)
        with self.lock:
            for i, v in enumerate(vals):
                self.db[int(start_epoch) + i] = v
            if len(self.db) > self.keep_s + 120:
                cut = max(self.db) - self.keep_s
                for k in [k for k in self.db if k < cut]:
                    del self.db[k]
        if len(self.done) > 2000:
            self.done = set(list(self.done)[-500:])

    def series(self, t0: float, t1: float) -> list[float | None]:
        with self.lock:
            return [self.db.get(i) for i in range(int(t0), int(t1))]

    def zscore(self, now: float, recent_s: int = 3, base_s: int = 300) -> float:
        """Robust z-score of the last few seconds vs the last 5 minutes."""
        with self.lock:
            latest = max(self.db) if self.db else None
            if latest is None:
                return 0.0
            recent = [self.db[i] for i in range(latest - recent_s + 1, latest + 1) if i in self.db]
            base = [self.db[i] for i in range(latest - base_s, latest - recent_s) if i in self.db]
        if len(base) < 30 or not recent:
            return 0.0
        b = np.array(base)
        med = float(np.median(b))
        mad = float(np.median(np.abs(b - med))) * 1.4826 + 0.5
        return (float(np.mean(recent)) - med) / mad
