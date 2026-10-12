# Quick Tools (+ Video Size Changer)

Flask website with QR scanner, image-to-text, link-to-QR, link shortener, and a new
**Video Size Changer** (Standard presets + Professional full-control conversion via FFmpeg).

## 1. Install FFmpeg on Windows (required for video conversion)

Option A — winget (recommended):

```powershell
winget install --id Gyan.FFmpeg -e
ffmpeg -version
```

Option B — manual:

1. Download the `ffmpeg-release-essentials.zip` from https://www.gyan.dev/ffmpeg/builds/
2. Extract to `C:\ffmpeg`
3. Add `C:\ffmpeg\bin` to PATH (System Properties → Environment Variables → Path → Edit → New)
4. Open a NEW terminal and verify:

```powershell
ffmpeg -version
```

The app calls `ffmpeg` from PATH via `subprocess` (no shell). If it is missing,
`POST /api/video/convert` returns HTTP 500 with install instructions.

## 2. Run the project

```powershell
cd C:\Users\Acer\Desktop\website
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
py app.py
```

Open http://127.0.0.1:5000/video-size-changer

Optional env vars:

```powershell
$env:VIDEO_MAX_BYTES = 7516192768     # default 7 GB
$env:VIDEO_FFMPEG_TIMEOUT = 7200      # default 7200 s (2 h, needed for large files)
$env:VIDEO_CLEANUP_SECONDS = 1800     # default 30 min
```

> Note: 7 GB uploads need free disk space in `instance/` and a long timeout.
> The browser upload progress bar stays active (no client-side timeout).

## 3. How it works

- `GET /video-size-changer` — Standard/Professional UI.
- `POST /api/video/convert` (multipart `video` + settings) — validates extension
  (mp4/mov/avi/mkv/webm/mpeg/mpg/m4v), size cap, and maps every setting to an
  allow-list before building the FFmpeg arg list (never raw user input).
- `GET /api/video/download/<job_id>` — downloads the converted file.
- `GET /api/video/estimate?original_bytes=&quality=&resolution=` — rough size hint.
- Uploads are deleted right after encoding; outputs older than
  `VIDEO_CLEANUP_SECONDS` are removed by a background thread.

## 4. Security notes

- Extension + size validation (7 GB cap), `JOB_RE` check on downloads.
- Container/codec compatibility enforced (e.g. WebM → VP8/VP9 + Opus/Vorbis).
- Numeric ranges enforced (width/height 16–7680, FPS 1–120, CRF 0–51,
  video 50k–100M, audio 32k–1000k, trim mm:ss/seconds with end > start).
- `subprocess.run([...], timeout=VIDEO_FFMPEG_TIMEOUT)` with list args only — no `shell=True`.
