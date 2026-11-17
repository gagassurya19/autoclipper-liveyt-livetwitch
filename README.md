# AUTO CUAN GTA VI — auto-clipper (stage 1: Telegram output)

Give it **stream URLs**. It records them, finds hype moments, edits a 1080×1920 short using the canvas design, and sends it to Telegram. Uploading to YT/TikTok/IG/X comes next.

```
stream URL ─► streamlink / yt-dlp ─► 6 s .ts ring buffer ─┐
           ─► live chat (yt-dlp / Twitch IRC) ─► chat speed ┼─► detector ─► raw 60 s window
           ─► audio RMS spikes ─────────────────────────────┘      ─► Groq Whisper (words + language)
                                                                    ─► Gemini (score, cut, hook, title)
                                                                    ─► ffmpeg render 1080×1920 ─► Telegram
```

## 1. Install (Mac now, Steam Deck later)

```bash
# Mac
brew install ffmpeg streamlink yt-dlp
# Steam Deck (inside a distrobox/Ubuntu container)
sudo apt install ffmpeg python3-venv && pip install streamlink yt-dlp

cd code
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then fill it in
```

Keep yt-dlp up to date on launch day (`pip install -U yt-dlp` or `brew upgrade yt-dlp`), because YouTube changes often.

## Manual clips from Telegram: `/clip`

Saw something funny? Send `/clip` to the bot (in the review chat, or in a private chat with it) and it clips that moment right away. No need to wait for the detector.

- `/clip` — the moment was ~10 s before your message (`manual.default_ago_s`)
- `/clip 30` — the moment was 30 s ago (up to ~10 min back, the size of the buffer)
- `/clip windah` — choose the stream when several are running (otherwise you get buttons)
- `/status` — which streams are live · `/id` — shows your user id

Manual clips skip the cooldown and `min_score`, but the spoiler check still applies. Commands are accepted from `TELEGRAM_CHAT_ID` and from user ids in `TELEGRAM_ADMIN_IDS`.

## 2. Telegram bot

1. Talk to **@BotFather** → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Make a private channel such as "clippingaround review" and add the bot as **admin**.
3. Post anything in the channel, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the `chat.id` (it starts with `-100`) into `TELEGRAM_CHAT_ID`.
4. `python -m autocuan test-telegram` should send you "✅ AUTO CUAN bot connected".

**About the size limit:** the normal Bot API rejects bot uploads over **50 MB**. Clips use CRF 20 with a 10 Mbps cap, so a 30 s clip at 1080×1920 60 fps comes out around 20 MB. If one ever goes over, it is automatically re-encoded at a lower bitrate, still in Full HD. For uploads up to 2 GB, run your own [telegram-bot-api](https://github.com/tdlib/telegram-bot-api) server and set `telegram.api_base` to it.

## 3. Optional AI keys (strongly recommended)

| Key | What you get | Without it |
|---|---|---|
| `GROQ_API_KEY` | word-by-word subtitles, plus automatic **language detection** (ID/EN) per clip | no subtitles; language falls back to `default_language` (`auto` means English) |
| `GEMINI_API_KEY` | viral score, smart 15–35 s cut, hook, Mingo mood, title, hashtags, spoiler skip | template hooks, fixed cut of 22 s before the peak to 6 s after |

## 4. Go live: just give it URLs

```bash
# one-off, straight from the command line
python -m autocuan run --url "https://www.youtube.com/watch?v=MaUcNWLyStg"
python -m autocuan run --url "<youtube live url>" --url "https://www.twitch.tv/kaicenat"

# or list them under `streams:` in config.yaml and just run
python -m autocuan run
```

Accepted URLs:
- **A YouTube live video** (`watch?v=…`, `youtu.be/…`, `youtube.com/live/…`): clips until that stream ends, then stops and tells you on Telegram.
- **A YouTube channel's live page** (`youtube.com/@channel/live`) or **a Twitch channel**: waits for the channel to go live, retrying every 30 s. You can start it hours early.

Channel name, @handle (used in the "clip · @…" pill and the hashtags), platform and language are detected from the URL. Chat speed is measured 22 s in the past on YouTube, because YouTube delivers chat about 16–21 s late; that's measured and normal.

**If YouTube says "Sign in to confirm you're not a bot":** log in to YouTube in Chrome (or Safari or Firefox) and set `recorder.cookies_from_browser: chrome` in `config.yaml`. YouTube recording and chat then go through yt-dlp with your login. The home internet on your Mac or Deck usually doesn't trigger this.

## 5. Per-stream options (facecam, language)

The design puts the facecam top-right, so it needs to know where the facecam sits in each stream:

```bash
python -m autocuan snap --url "https://www.youtube.com/watch?v=MaUcNWLyStg"
```

Open `data/snap_<id>.png` (the grid is in 10% steps) and use the dict form in `config.yaml`:

```yaml
streams:
  - url: https://www.youtube.com/watch?v=MaUcNWLyStg
    facecam: {x: 0.74, y: 0.02, w: 0.24, h: 0.27}   # fractions of the frame
    language: id          # optional: skip auto-detect
    crop_center_x: 0.5    # optional: shift the 9:16 gameplay crop
```

Options are matched by URL, so they also apply when you pass the same URL with `--url`. Without a `facecam` there's no facecam inset.

## 6. Test offline

```bash
yt-dlp -f "bv*[height<=1080]+ba" -o vod.mp4 "<url>" --download-sections "*00:10:00-00:13:00"
python -m autocuan clip --file vod.mp4 --peak 95 --url "<original url>"      # renders + sends
python -m autocuan clip --file vod.mp4 --peak 95 --name Windah --language id --no-send
```

## Tuning (config.yaml)

- **Too many clips:** raise `detector.chat_ratio_trigger` (2.5 → 3) or `cooldown_s`, or set `clip.min_score: 70`.
- **Too few clips:** lower `chat_ratio_trigger` to 2.0, or `audio_only_z` to 4.
- **Steam Deck:** `output.encoder: h264_vaapi` (GPU) and/or `fps: 30`. On a Mac: `h264_videotoolbox`.

## Files

```
autocuan/resolve.py    URL → channel name, @handle, platform, language, live status
autocuan/recorder.py   streamlink (or yt-dlp + cookies) → copy-only segments, ring buffer, stops when a video ends
autocuan/chat.py       YouTube chat via yt-dlp live_chat, Twitch via anonymous IRC
autocuan/audio.py      per-second loudness + robust z-score
autocuan/detector.py   triggers (chat lag aware), peak finding, raw window cut
autocuan/ai.py         Groq transcription + language detect, Gemini packaging (+ fallbacks)
autocuan/editor.py     canvas template → Pillow overlay states → ffmpeg render
autocuan/telegram.py   sendVideo (1080×1920, thumbnail, caption, retries)
autocuan/pipeline.py   candidate → … → Telegram
assets/                Mingo (4 moods), Plus Jakarta Sans (OFL)
```
