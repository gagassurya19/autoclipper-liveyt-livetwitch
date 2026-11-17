"""Cloud AI helpers: Groq Whisper (word timestamps) and Gemini (score + hook + title + cut)."""
from __future__ import annotations

import json
import time
import logging
import random
import subprocess
import tempfile
from pathlib import Path

import httpx

from .config import Config, Streamer, env

log = logging.getLogger("ai")


# ---------------------------------------------------------------- transcription
LANG_CODES = {"indonesian": "id", "english": "en", "malay": "id", "id": "id", "en": "en"}


def transcribe(cfg: Config, video: Path, start: float, dur: float, language: str) -> tuple[list[dict], str | None]:
    """Return ([{'word','start','end'}] relative to `start`, detected language code).
    language="auto" lets Whisper detect it. Empty result if no GROQ_API_KEY."""
    key = env("GROQ_API_KEY")
    if not key:
        return [], None
    with tempfile.TemporaryDirectory() as td:
        mp3 = Path(td) / "a.mp3"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", str(video),
             "-t", f"{dur:.3f}", "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(mp3)],
            check=True, timeout=120,
        )
        for attempt in range(3):
            try:
                r = httpx.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {key}"},
                    data={
                        "model": cfg["ai"]["transcribe_model"],
                        **({} if language == "auto" else {"language": language}),
                        "response_format": "verbose_json",
                        "timestamp_granularities[]": "word",
                        "temperature": "0",
                    },
                    files={"file": ("a.mp3", mp3.read_bytes(), "audio/mpeg")},
                    timeout=120,
                )
                r.raise_for_status()
                j = r.json()
                det = LANG_CODES.get(str(j.get("language", "")).lower())
                words = j.get("words") or []
                return [{"word": w["word"].strip(), "start": float(w["start"]), "end": float(w["end"])}
                        for w in words if w.get("word", "").strip()], det
            except Exception as e:  # noqa: BLE001
                log.warning("groq attempt %d failed: %s", attempt + 1, e)
    return [], None


# ---------------------------------------------------------------- copywriting
FALLBACK_HOOKS = {
    "en": [("he did not expect this", "not"), ("chat could not believe it", "not"),
           ("this went wrong so fast", "wrong"), ("nobody saw this coming", "nobody")],
    "id": [("dia gak nyangka bakal gini", "gak"), ("chat langsung heboh", "heboh"),
           ("ini kacau banget", "kacau"), ("gak ada yang nyangka", "nyangka")],
}

PROMPT = """You pick and package a short-form clip (YouTube Shorts / TikTok / Reels / X) from a GTA 6 \
day-one livestream by {name}. Language for all text: {lang_name}.

Raw window is {win:.0f}s long. Chat/audio peak is at t={peak:.1f}s.
Transcript with word times (seconds from window start):
{transcript}

Live chat messages around the peak:
{chat}

Return JSON only:
{{
 "score": 0-100 (how likely this goes viral as a standalone short; boring/no context = low),
 "spoiler": true if it shows story cutscenes or major story spoilers,
 "start": seconds, "end": seconds  (one self-contained moment, {min_s}-{max_s}s long, must include the peak; \
start on a sentence boundary with a little setup, end right after the payoff),
 "hook": "max 8 words, lowercase, curiosity hook shown in the first 2s, {lang_name}",
 "hook_highlight": "one word copied exactly from hook to highlight",
 "mood": "happy" | "shocked" | "laugh" | "cool"  (mascot reaction),
 "bubble": "1-3 word mascot reaction, lowercase, {lang_name}",
 "title": "max 70 chars, platform title, {lang_name}, no hashtags",
 "hashtags": ["3-5 extra relevant hashtags"]
}}"""


def _fmt_transcript(words: list[dict]) -> str:
    if not words:
        return "(no transcript available)"
    lines, cur, t0 = [], [], words[0]["start"]
    for w in words:
        cur.append(w["word"])
        if len(cur) >= 10 or w["word"].endswith((".", "?", "!")):
            lines.append(f"[{t0:.1f}] {' '.join(cur)}")
            cur = []
            t0 = w["end"]
    if cur:
        lines.append(f"[{t0:.1f}] {' '.join(cur)}")
    return "\n".join(lines)


def package(cfg: Config, s: Streamer, words: list[dict], chat: list[str], win: float, peak: float) -> dict:
    c = cfg["clip"]
    fallback_hook, fallback_hl = random.choice(FALLBACK_HOOKS.get(s.language, FALLBACK_HOOKS["en"]))
    start = max(0.0, peak - c["default_before_peak_s"])
    out = {
        "score": None, "spoiler": False,
        "start": start, "end": min(win, peak + c["default_after_peak_s"]),
        "hook": fallback_hook, "hook_highlight": fallback_hl, "mood": "shocked",
        "bubble": "no way…" if s.language == "en" else "hah?!",
        "title": f"{s.name} GTA 6 moment", "hashtags": [], "ai": False,
    }
    key = env("GEMINI_API_KEY")
    if key:
        prompt = PROMPT.format(
            name=s.name, lang_name="Indonesian (casual, gaul)" if s.language == "id" else "English",
            win=win, peak=peak, transcript=_fmt_transcript(words),
            chat="\n".join(f"- {m}" for m in chat[:60]) or "(none)", min_s=c["min_s"], max_s=c["max_s"],
        )
        # Gemini often answers 503 "overloaded" at peak times: retry, then try backup models
        models = [cfg["ai"]["copy_model"], *(cfg["ai"].get("copy_fallback_models") or [])]
        last_err = None
        for model in models:
            for attempt in range(3):
                try:
                    r = httpx.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                        headers={"x-goog-api-key": key},
                        json={"contents": [{"parts": [{"text": prompt}]}],
                              "generationConfig": {"responseMimeType": "application/json", "temperature": 0.7}},
                        timeout=60,
                    )
                    if r.status_code in (429, 500, 502, 503, 504):
                        raise httpx.HTTPStatusError(f"{r.status_code} from {model}", request=r.request, response=r)
                    r.raise_for_status()
                    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    data = json.loads(txt)
                    out.update({k: v for k, v in data.items() if v not in (None, "")})
                    out["ai"] = True
                    break
                except httpx.HTTPStatusError as e:
                    last_err = e
                    if e.response.status_code not in (429, 500, 502, 503, 504):
                        break
                    log.info("gemini %s busy (%s), retry %d", model, e.response.status_code, attempt + 1)
                    time.sleep(2 * (attempt + 1))
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    break
            if out["ai"]:
                if model != models[0]:
                    log.info("gemini: used backup model %s", model)
                break
        if not out["ai"]:
            log.warning("gemini failed, using template copy: %s", last_err)

    # sanitise the cut
    st, en = float(out["start"]), float(out["end"])
    st, en = max(0.0, min(st, win)), max(0.0, min(en, win))
    if en - st < c["min_s"]:
        en = min(win, st + c["min_s"])
        st = max(0.0, en - c["min_s"])
    if en - st > c["max_s"]:
        en = st + c["max_s"]
    # snap to word gaps so we don't cut mid-word
    if words:
        inside = [w for w in words if w["start"] < st < w["end"]]
        if inside:
            st = max(0.0, inside[0]["start"] - 0.15)
        inside = [w for w in words if w["start"] < en < w["end"]]
        if inside:
            en = min(win, inside[0]["end"] + 0.25)
    out["start"], out["end"] = round(st, 2), round(en, 2)
    if out["mood"] not in ("happy", "shocked", "laugh", "cool"):
        out["mood"] = "shocked"
    return out
