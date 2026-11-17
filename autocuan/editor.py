"""Renders the 9:16 1080x1920 short from the canvas design.

Canvas (405x720) is scaled x8/3 to 1080x1920:
  Hook  (0–2 s): full-bleed gameplay, white hook card, Mingo (shocked) on the card,
                 facecam bottom-left above the card, credit pill.
  Main         : full-bleed gameplay, facecam top-right, credit pill, Mingo + speech bubble,
                 lowercase word-by-word subtitles with the active word in a yellow pill.
  Outro (1.5 s): cream card, Mingo (happy), "follow @clippingaround".
  Progress bar : 8 px accent bar at the top.

All graphics are drawn with Pillow into full-frame transparent PNG "states" and played as one
overlay track (concat demuxer), so ffmpeg only does crop/scale/overlay/encode.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import ASSETS, Config, Streamer

log = logging.getLogger("editor")

W, H = 1080, 1920
K = W / 405  # canvas → output scale


def px(v: float) -> int:
    return int(round(v * K))


# ---------------------------------------------------------------- drawing helpers
@lru_cache(maxsize=64)
def font(weight: int, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(ASSETS / "fonts" / f"PlusJakartaSans-{weight}.ttf"), size)


@lru_cache(maxsize=16)
def mingo(mood: str, w: int, h: int) -> Image.Image:
    return Image.open(ASSETS / "mingo" / f"{mood}.png").convert("RGBA").resize((w, h), Image.LANCZOS)


def hex_rgb(h: str, a: int = 255) -> tuple[int, int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a


def text_w(f: ImageFont.FreeTypeFont, s: str, tracking: float = 0) -> float:
    return f.getlength(s) + tracking * max(0, len(s) - 1)


def draw_tracked(d: ImageDraw.ImageDraw, xy, s: str, f, fill, tracking: float = 0):
    x, y = xy
    if not tracking:
        d.text((x, y), s, font=f, fill=fill)
        return
    for ch in s:
        d.text((x, y), ch, font=f, fill=fill)
        x += f.getlength(ch) + tracking


def wrap(words: list[str], f, max_w: float, pad_word: int | None = None, pad: float = 0) -> list[list[int]]:
    """Greedy wrap; returns lines as lists of word indexes."""
    space = f.getlength(" ")
    lines, cur, cur_w = [], [], 0.0
    for i, w in enumerate(words):
        ww = f.getlength(w) + (2 * pad if i == pad_word else 0)
        add = ww if not cur else cur_w + space + ww
        if cur and add > max_w:
            lines.append(cur)
            cur, cur_w = [i], ww
        else:
            cur.append(i)
            cur_w = add
    if cur:
        lines.append(cur)
    return lines


def credit_pill(img: Image.Image, text: str, top: int, right: int):
    d = ImageDraw.Draw(img)
    f = font(600, px(10))
    tw = text_w(f, text)
    pw, ph = tw + 2 * px(10), px(10) * 1.25 + 2 * px(4)
    x1 = W - right
    x0 = x1 - pw
    d.rounded_rectangle((x0, top, x1, top + ph), radius=ph / 2 - 1, fill=(15, 16, 18, 153))
    d.text((x0 + px(10), top + ph / 2), text, font=f, fill=(255, 255, 255, 217), anchor="lm")


def progress_track(img: Image.Image, dark: bool = False):
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, px(3) - 1), fill=(20, 20, 20, 26) if dark else (255, 255, 255, 46))


# ---------------------------------------------------------------- frames
def hook_frame(s: Streamer, pack: dict, accent: str, lang: str) -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    progress_track(img)
    credit_pill(img, f"clip · {s.credit or s.name}", px(349), px(24))

    card_x0, card_x1, card_y0 = px(24), W - px(24), px(432)
    pad_t, pad_x, pad_b = px(18), px(20), px(20)
    f_lab, f_title = font(700, px(10)), font(800, px(25))
    label = ("GTA 6 · hari pertama" if lang == "id" else "GTA 6 · day one").upper()
    words = pack["hook"].lower().split()
    hl = (pack.get("hook_highlight") or "").lower().strip(".,!?")
    hl_i = next((i for i, w in enumerate(words) if w.strip(".,!?") == hl), None)
    pill_pad = px(6)
    lines = wrap(words, f_title, (card_x1 - card_x0) - 2 * pad_x, hl_i, pill_pad)
    lh = int(px(25) * 1.2)
    card_h = pad_t + px(10) * 1.3 + px(8) + lh * len(lines) + pad_b
    card_y1 = card_y0 + card_h

    # soft shadow
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle((card_x0, card_y0 + px(10), card_x1, card_y1 + px(10)), radius=px(18), fill=(0, 0, 0, 90))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(px(12))))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((card_x0, card_y0, card_x1, card_y1), radius=px(18), fill=(255, 255, 255, 255))

    y = card_y0 + pad_t
    draw_tracked(d, (card_x0 + pad_x, y), label, f_lab, (107, 107, 107, 255), tracking=px(10) * 0.14)
    y += px(10) * 1.3 + px(8)
    space = f_title.getlength(" ")
    for line in lines:
        x = card_x0 + pad_x
        for i in line:
            w = words[i]
            if i == hl_i:
                ww = f_title.getlength(w) + 2 * pill_pad
                d.rounded_rectangle((x, y + px(1), x + ww, y + lh - px(1)), radius=px(6), fill=hex_rgb(accent))
                d.text((x + pill_pad, y + lh / 2), w, font=f_title, fill=(20, 20, 20, 255), anchor="lm")
            else:
                ww = f_title.getlength(w)
                d.text((x, y + lh / 2), w, font=f_title, fill=(20, 20, 20, 255), anchor="lm")
            x += ww + space
        y += lh

    m = mingo("shocked", px(66), px(72))
    img.alpha_composite(m, (card_x0 + px(291), card_y0 - px(41)))
    return img


def main_base(s: Streamer, pack: dict) -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    progress_track(img)
    credit_pill(img, f"clip · {s.credit or s.name}", px(350), px(12))
    m = mingo(pack["mood"], px(58), px(63))
    img.alpha_composite(m, (px(28), px(448)))
    bubble = (pack.get("bubble") or "").lower()
    if bubble:
        d = ImageDraw.Draw(img)
        f = font(700, px(11))
        bw, bh = text_w(f, bubble) + 2 * px(11), px(11) * 1.25 + 2 * px(5)
        x0, y0 = px(86), px(448)
        d.rounded_rectangle((x0, y0, x0 + bw, y0 + bh), radius=bh / 2 - 1, fill=(255, 255, 255, 255),
                            corners=(True, True, True, False))
        d.text((x0 + px(11), y0 + bh / 2), bubble, font=f, fill=(20, 20, 20, 255), anchor="lm")
    return img


def subtitle_layer(words: list[str], active: int, accent: str) -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    f = font(800, px(25))
    left, right, top = px(28), W - px(72), px(511)
    pad = px(7)
    lines = wrap(words, f, right - left, active, pad)
    lh = int(px(25) * 1.22)
    space = f.getlength(" ")

    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ds = ImageDraw.Draw(shadow)
    d = ImageDraw.Draw(img)
    placed = []
    y = top
    for line in lines:
        widths = [f.getlength(words[i]) + (2 * pad if i == active else 0) for i in line]
        lw = sum(widths) + space * (len(line) - 1)
        x = left + ((right - left) - lw) / 2
        for i, ww in zip(line, widths):
            placed.append((i, x, y, ww))
            if i != active:
                ds.text((x, y + lh / 2 + px(2)), words[i], font=f, fill=(0, 0, 0, 150), anchor="lm")
            x += ww + space
        y += lh
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(px(2))))
    for i, x, y, ww in placed:
        if i == active:
            d.rounded_rectangle((x, y + px(1), x + ww, y + lh - px(1)), radius=px(7), fill=hex_rgb(accent))
            d.text((x + pad, y + lh / 2), words[i], font=f, fill=(20, 20, 20, 255), anchor="lm")
        else:
            d.text((x, y + lh / 2), words[i], font=f, fill=(255, 255, 255, 255), anchor="lm")
    return img


def outro_frame(handle: str, accent: str, lang: str) -> Image.Image:
    img = Image.new("RGBA", (W, H), (246, 243, 238, 255))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, px(3) - 1), fill=(20, 20, 20, 255))
    m = mingo("happy", px(150), px(163))
    f1, f2, f3 = font(800, px(30)), font(600, px(15)), font(700, px(10))
    t1 = "momen GTA 6 lainnya" if lang == "id" else "more GTA 6 moments"
    pre = "follow "
    post = " — klip baru tiap jam" if lang == "id" else " — new clips every hour"
    t3 = "SHORTS · TIKTOK · REELS · X"
    total_h = m.height + px(18) + px(30) * 1.1 + px(8) + px(15) * 1.3 + px(22) + px(18) + px(10) * 1.3
    y = (H - total_h) / 2
    img.alpha_composite(m, ((W - m.width) // 2, int(y)))
    y += m.height + px(18)
    d.text((W / 2, y), t1, font=f1, fill=(20, 20, 20, 255), anchor="mt")
    y += px(30) * 1.1 + px(8)
    pad = px(7)
    w_pre, w_h, w_post = f2.getlength(pre), f2.getlength(handle) + 2 * pad, f2.getlength(post)
    x = (W - (w_pre + w_h + w_post)) / 2
    lh = px(15) * 1.3
    d.text((x, y + lh / 2), pre, font=f2, fill=(92, 92, 92, 255), anchor="lm")
    x += w_pre
    d.rounded_rectangle((x, y - px(1), x + w_h, y + lh + px(1)), radius=px(6), fill=hex_rgb(accent))
    d.text((x + pad, y + lh / 2), handle, font=f2, fill=(20, 20, 20, 255), anchor="lm")
    x += w_h
    d.text((x, y + lh / 2), post, font=f2, fill=(92, 92, 92, 255), anchor="lm")
    y += lh + px(22) + px(18)
    tr = px(10) * 0.16
    draw_tracked(d, ((W - text_w(f3, t3, tr)) / 2, y), t3, f3, (107, 107, 107, 255), tracking=tr)
    return img


# ---------------------------------------------------------------- subtitles timeline
def chunk_words(words: list[dict], max_words: int = 5, gap: float = 0.7) -> list[list[dict]]:
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w["start"] - cur[-1]["end"] > gap
                    or cur[-1]["word"].endswith((".", "?", "!"))):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def build_overlay_states(s: Streamer, pack: dict, words: list[dict], dur: float, hook_s: float,
                         accent: str, lang: str, workdir: Path) -> Path:
    """Writes PNG states + a concat list; returns the list path."""
    states: list[tuple[Image.Image, float, float]] = []
    states.append((hook_frame(s, pack, accent, lang), 0.0, hook_s))
    base = main_base(s, pack)

    # words relative to clip start, only after the hook
    ws = [w for w in words if w["end"] > hook_s and w["start"] < dur]
    for w in ws:
        w["word"] = w["word"].lower().strip()
    t = hook_s
    for ch in chunk_words(ws):
        labels = [w["word"].strip(",") for w in ch]
        for j, w in enumerate(ch):
            st = max(t, w["start"])
            en = ch[j + 1]["start"] if j + 1 < len(ch) else min(dur, w["end"] + 0.35)
            if st > t + 0.05:
                states.append((base, t, st))
            if en - st < 0.04:
                continue
            frame = base.copy()
            frame.alpha_composite(subtitle_layer(labels, j, accent))
            states.append((frame, st, en))
            t = en
    if t < dur:
        states.append((base, t, dur))

    lines = []
    cache: dict[int, str] = {}
    for n, (img, st, en) in enumerate(states):
        if en - st <= 0:
            continue
        key = id(img)
        if key not in cache:
            p = workdir / f"ov_{n:04d}.png"
            img.save(p, compress_level=1)
            cache[key] = p.name
        lines.append(f"file '{cache[key]}'\nduration {en - st:.3f}\n")
    lines.append(f"file '{cache[id(states[-1][0])]}'\n")
    lst = workdir / "overlay.txt"
    lst.write_text("".join(lines))
    return lst


# ---------------------------------------------------------------- ffmpeg
def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def encoder_args(o: dict, bitrate: str | None = None) -> tuple[list[str], str, list[str]]:
    """(pre-input args, final filter suffix, output codec args)."""
    enc = o.get("encoder", "libx264")
    if enc == "h264_vaapi":
        rate = ["-rc_mode", "VBR", "-b:v", bitrate or "8M", "-maxrate", o.get("maxrate", "10M")]
        return ["-vaapi_device", o.get("vaapi_device", "/dev/dri/renderD128")], ",format=nv12,hwupload", \
            ["-c:v", "h264_vaapi", *rate]
    if enc == "h264_videotoolbox":
        return [], ",format=yuv420p", ["-c:v", "h264_videotoolbox", "-b:v", bitrate or "9M", "-profile:v", "high"]
    rate = ["-b:v", bitrate] if bitrate else ["-crf", str(o.get("crf", 20))]
    return [], ",format=yuv420p", ["-c:v", "libx264", "-preset", o.get("preset", "veryfast"), *rate,
                                   "-maxrate", o.get("maxrate", "10M"), "-bufsize", "20M",
                                   "-profile:v", "high", "-g", str(int(o.get("fps", 60)) * 2)]


def render(cfg: Config, s: Streamer, src: Path, start: float, end: float, pack: dict,
           words: list[dict], out: Path, bitrate: str | None = None) -> Path:
    """Render the final short. `words` are relative to `start`."""
    o, c = cfg["output"], cfg["clip"]
    fps = int(o.get("fps", 60))
    accent = o.get("accent", "#FFD84D")
    hook_s, outro_s = float(c["hook_s"]), float(c["outro_s"])
    dur = round(end - start, 3)
    lang = s.language

    workdir = out.parent / (out.stem + "_work")
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    overlay_list = build_overlay_states(s, pack, words, dur, hook_s, accent, lang, workdir)
    outro_png = workdir / "outro.png"
    outro_frame(cfg["channel_handle"], accent, lang).convert("RGB").save(outro_png)

    info = probe(src)
    v = next(st for st in info["streams"] if st["codec_type"] == "video")
    has_audio = any(st["codec_type"] == "audio" for st in info["streams"])
    sw, sh = int(v["width"]), int(v["height"])

    # gameplay crop (9:16 out of the 16:9 stream)
    cw = min(sw, int(round(sh * 9 / 16 / 2) * 2))
    cx = int(min(max(0, s.crop_center_x * sw - cw / 2), sw - cw))
    fc = s.facecam
    ac = accent.lstrip("#")

    f = []
    if fc:
        f.append("[0:v]split=3[g][f1][f2]")
    else:
        f.append("[0:v]null[g]")
    f.append(f"[g]crop={cw}:{sh}:{cx}:0,scale={W}:{H}:flags=lanczos,setsar=1,fps={fps}[bg]")
    last = "bg"
    if fc:
        fx, fy = int(fc["x"] * sw), int(fc["y"] * sh)
        fw, fh = int(fc["w"] * sw) // 2 * 2, int(fc["h"] * sh) // 2 * 2
        mw, mh = px(136) // 2 * 2, px(133) // 2 * 2
        hw, hh = px(117) // 2 * 2, px(109) // 2 * 2
        f.append(f"[f1]crop={fw}:{fh}:{fx}:{fy},scale={mw}:{mh}:force_original_aspect_ratio=increase,crop={mw}:{mh}[fcm]")
        f.append(f"[f2]crop={fw}:{fh}:{fx}:{fy},scale={hw}:{hh}:force_original_aspect_ratio=increase,crop={hw}:{hh}[fch]")
        f.append(f"[bg][fch]overlay={px(24)}:{px(305)}:enable='lt(t,{hook_s})'[a1]")
        f.append(f"[a1][fcm]overlay={W - mw}:0:enable='gte(t,{hook_s})'[a2]")
        last = "a2"
    f.append(f"[{last}][1:v]overlay=0:0:eof_action=repeat[ov]")
    f.append(f"color=c=0x{ac}:s={W}x{px(3)}:r={fps}:d={dur}[bar]")
    f.append(f"[ov][bar]overlay=x='-w+w*t/{dur}':y=0:eof_action=pass,trim=duration={dur},setpts=PTS-STARTPTS[mainv]")
    f.append(f"[2:v]scale={W}:{H},setsar=1,fps={fps},trim=duration={outro_s},setpts=PTS-STARTPTS[outv]")
    if has_audio:
        f.append(f"[0:a]aresample=48000,aformat=channel_layouts=stereo,atrim=duration={dur},asetpts=PTS-STARTPTS,"
                 f"afade=t=in:d=0.12,afade=t=out:st={max(0, dur - 0.4)}:d=0.4[maina]")
    else:
        f.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={dur}[maina]")
    f.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={outro_s}[outa]")
    pre, vsuffix, venc = encoder_args(o, bitrate)
    f.append(f"[mainv][maina][outv][outa]concat=n=2:v=1:a=1[cv][ca0]")
    f.append(f"[cv]null{vsuffix}[v]")
    f.append("[ca0]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]")

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *pre,
        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
        "-f", "concat", "-safe", "0", "-i", str(overlay_list),
        "-loop", "1", "-framerate", str(fps), "-t", f"{outro_s}", "-i", str(outro_png),
        "-filter_complex", ";".join(f), "-map", "[v]", "-map", "[a]",
        *venc, "-r", str(fps), "-c:a", "aac", "-b:a", o.get("audio_bitrate", "160k"),
        "-movflags", "+faststart", str(out),
    ]
    log.info("rendering %s (%.1fs)", out.name, dur + outro_s)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-1500:]}")
    shutil.rmtree(workdir, ignore_errors=True)
    return out


def thumbnail(video: Path, t: float = 1.0) -> Path:
    out = video.with_suffix(".jpg")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(t), "-i", str(video),
                    "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "4", str(out)], check=True)
    return out
