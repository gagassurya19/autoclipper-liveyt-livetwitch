"""Cut a raw window [t0, t1] (epoch seconds) out of the segment ring buffer (copy-only)."""
from __future__ import annotations

import subprocess
from pathlib import Path


def cut_window(segments: list[tuple[float, Path]], seg_s: float, t0: float, t1: float, out: Path) -> tuple[Path, float]:
    """Concatenate the segments covering [t0, t1]. Returns (file, offset_of_t0_in_file)."""
    picked = [(st, p) for st, p in segments if st + seg_s + 1 > t0 and st < t1]
    if not picked:
        raise RuntimeError("no segments cover this window (buffer too short or stream offline)")
    out.parent.mkdir(parents=True, exist_ok=True)
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.resolve()}'\n" for _, p in picked))
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(out)],
        check=True, timeout=120,
    )
    lst.unlink(missing_ok=True)
    return out, max(0.0, t0 - picked[0][0])
