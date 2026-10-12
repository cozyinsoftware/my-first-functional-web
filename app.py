import base64
import io
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse

import qrcode
from flask import Flask, abort, g, jsonify, redirect, render_template, request, send_file, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.utils import secure_filename

app = Flask(__name__, instance_relative_config=True)

# Change this in production. Use a long random value.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key-before-production")
app.config["DATABASE"] = os.path.join(app.instance_path, "app.db")

os.makedirs(app.instance_path, exist_ok=True)
token_serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="short-link-wait")


def get_db():
    """Get one SQLite connection for the current request."""
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they do not exist yet."""
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            long_url TEXT NOT NULL,
            clicks INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            reason TEXT,
            reported_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rate_limits (
            rate_key TEXT PRIMARY KEY,
            count INTEGER NOT NULL,
            reset_at REAL NOT NULL
        );
        """
    )
    db.commit()


def is_safe_http_url(value):
    """Allow only valid public http:// or https:// URLs."""
    try:
        parsed = urlparse(value)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except ValueError:
        return False


def normalize_url(value):
    """Add https:// when the user did not enter a scheme."""
    value = (value or "").strip()

    if not value.startswith(("http://", "https://")):
        value = f"https://{value}"

    if not is_safe_http_url(value):
        return None

    return value


def points_to_this_site(url):
    """Block short links that target this same website."""
    parsed = urlparse(url)
    target_host = (parsed.hostname or "").lower()

    request_host = request.host.split(":")[0].lower()
    configured_host = os.environ.get("OWN_HOST", "").split(":")[0].lower()

    return target_host in {request_host, configured_host} - {""}


def client_ip():
    """Use proxy header when present, otherwise remote address."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def rate_limit(limit=20, period=60):
    """Basic SQLite rate limit by IP and endpoint."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            db = get_db()
            now = time.time()
            key = f"{client_ip()}:{request.endpoint}"

            row = db.execute(
                "SELECT count, reset_at FROM rate_limits WHERE rate_key = ?",
                (key,),
            ).fetchone()

            if row is None or now >= row["reset_at"]:
                db.execute(
                    """
                    INSERT INTO rate_limits (rate_key, count, reset_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(rate_key)
                    DO UPDATE SET count = excluded.count, reset_at = excluded.reset_at
                    """,
                    (key, 1, now + period),
                )
                db.commit()
            elif row["count"] >= limit:
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Too many requests. Please wait a minute."}), 429
                return render_template("rate_limited.html"), 429
            else:
                db.execute(
                    "UPDATE rate_limits SET count = count + 1 WHERE rate_key = ?",
                    (key,),
                )
                db.commit()

            return view(*args, **kwargs)
        return wrapped
    return decorator


def create_code():
    """Create a short code that is not already in the database."""
    db = get_db()

    while True:
        code = secrets.token_urlsafe(5).replace("-", "").replace("_", "")[:7]
        exists = db.execute("SELECT 1 FROM links WHERE code = ?", (code,)).fetchone()
        if not exists:
            return code


def read_wait_token(token, require_wait=False):
    """Read and check the signed waiting token."""
    try:
        data = token_serializer.loads(token, max_age=600)
    except SignatureExpired:
        return None, "This waiting link has expired. Please open the short link again."
    except BadSignature:
        return None, "Invalid waiting token."

    code = data.get("code")
    issued_at = data.get("issued_at", 0)

    if not code or not isinstance(issued_at, (int, float)):
        return None, "Invalid waiting token."

    if require_wait and time.time() - issued_at < 5:
        return None, "Please wait for the countdown to finish."

    link = get_db().execute(
        "SELECT code, long_url FROM links WHERE code = ?",
        (code,),
    ).fetchone()

    if link is None:
        return None, "This short link does not exist."

    return link, None


@app.route("/")
def scanner():
    return render_template("scanner.html")


@app.route("/video-size-changer")
def video_size_changer():
    bin_path = _get_ffmpeg_bin()
    version = _get_ffmpeg_version() if bin_path else None
    is_outdated = bool(version and re.search(r"200\d|201[0-7]", version))
    return render_template(
        "video_size_changer.html",
        ffmpeg_ready=(bin_path is not None),
        ffmpeg_outdated=is_outdated,
        ffmpeg_version=version,
    )



# ---------------------------------------------------------------------------
# Video Size Changer (FFmpeg backend)
# All user settings are validated and mapped to a strict allow-list before
# any FFmpeg command is built. Never pass raw user input to the command line.
# ---------------------------------------------------------------------------

VIDEO_MAX_BYTES = int(os.environ.get("VIDEO_MAX_BYTES", 7 * 1024 * 1024 * 1024))
VIDEO_FFMPEG_TIMEOUT = int(os.environ.get("VIDEO_FFMPEG_TIMEOUT", 7200))
VIDEO_CLEANUP_SECONDS = int(os.environ.get("VIDEO_CLEANUP_SECONDS", 30 * 60))
# Allow 7 GB uploads at the Flask level too (default Werkzeug limit would block them
# only if set elsewhere; this makes the cap explicit and consistent).
app.config["MAX_CONTENT_LENGTH"] = VIDEO_MAX_BYTES + (10 * 1024 * 1024)
VIDEO_UPLOAD_DIR = os.path.join(app.instance_path, "video_uploads")
VIDEO_OUTPUT_DIR = os.path.join(app.instance_path, "video_outputs")
os.makedirs(VIDEO_UPLOAD_DIR, exist_ok=True)
os.makedirs(VIDEO_OUTPUT_DIR, exist_ok=True)

ALLOWED_VIDEO_EXTS = {"mp4", "mov", "avi", "mkv", "webm", "mpeg", "mpg", "m4v"}
ALLOWED_FORMATS = {"mp4", "webm", "mov", "mkv", "avi"}
ALLOWED_VIDEO_CODECS = {"libx264", "libx265", "libvpx-vp9", "libvpx", "mpeg4"}
ALLOWED_AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis", "ac3"}
ALLOWED_PRESETS = {
    "ultrafast", "superfast", "veryfast", "faster",
    "fast", "medium", "slow", "slower", "veryslow",
}
ALLOWED_FPS = {24, 25, 30, 50, 60}
ALLOWED_ROTATIONS = {0, 90, 180, 270}
ALLOWED_ASPECTS = {"source", "16:9", "4:3", "1:1", "9:16"}
STANDARD_TASKS = {"compress", "mp4", "webm", "custom"}
STANDARD_RESOLUTIONS = {"source", "360p", "480p", "720p", "1080p", "2160p"}
STANDARD_FPS = {"source", "24", "30", "60"}
STANDARD_QUALITY = {"low", "medium", "high"}
RESOLUTION_HEIGHTS = {"360p": 360, "480p": 480, "720p": 720, "1080p": 1080, "2160p": 2160}
QUALITY_CRF = {"low": 30, "medium": 23, "high": 18}
BITRATE_RE = re.compile(r"^\d+\s*[kKmM]?$")
TIME_RE = re.compile(r"^\d+(\.\d+)?$|^\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?$")
JOB_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _video_error(message, status=400):
    return jsonify({"error": message}), status


def _fmt_size(bytes_val):
    """Human-friendly size for error messages (KB, MB, or GB)."""
    try:
        kb = bytes_val / 1024
    except (TypeError, ZeroDivisionError):
        return "unknown size"
    if kb < 1024:
        return f"{max(int(kb), 1)} KB"
    mb = kb / 1024
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{int(mb)} MB"


_CACHED_FFMPEG_BIN = None
_CACHED_FFMPEG_SEARCHED = False


def _get_ffmpeg_bin():
    """Find the path to the ffmpeg executable on Windows or other OS."""
    global _CACHED_FFMPEG_BIN, _CACHED_FFMPEG_SEARCHED
    if _CACHED_FFMPEG_SEARCHED and _CACHED_FFMPEG_BIN and os.path.isfile(_CACHED_FFMPEG_BIN):
        return _CACHED_FFMPEG_BIN

    def _prepare_bin(bin_path):
        global _CACHED_FFMPEG_BIN, _CACHED_FFMPEG_SEARCHED
        if bin_path and os.path.isfile(bin_path):
            bin_dir = os.path.dirname(os.path.abspath(bin_path))
            cur_path = os.environ.get("PATH", "")
            if bin_dir and bin_dir.lower() not in cur_path.lower().split(os.pathsep):
                os.environ["PATH"] = bin_dir + os.pathsep + cur_path
            _CACHED_FFMPEG_BIN = bin_path
            _CACHED_FFMPEG_SEARCHED = True
            return bin_path
        return None

    env_bin = os.environ.get("FFMPEG_BINARY") or os.environ.get("FFMPEG_PATH")
    if env_bin:
        res = _prepare_bin(env_bin)
        if res:
            return res

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    user_profile = os.environ.get("USERPROFILE", "")

    # Prioritize modern package managers (winget, scoop, chocolatey)
    priority_candidates = []
    if local_app_data:
        winget_dir = os.path.join(local_app_data, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(winget_dir):
            try:
                for root, dirs, files in os.walk(winget_dir):
                    if "ffmpeg.exe" in files:
                        priority_candidates.append(os.path.join(root, "ffmpeg.exe"))
            except OSError:
                pass
        priority_candidates.append(os.path.join(local_app_data, "Programs", "ffmpeg", "bin", "ffmpeg.exe"))

    if user_profile:
        priority_candidates.append(os.path.join(user_profile, "scoop", "apps", "ffmpeg", "current", "bin", "ffmpeg.exe"))
        priority_candidates.append(os.path.join(user_profile, "scoop", "shims", "ffmpeg.exe"))

    for c in priority_candidates:
        res = _prepare_bin(c)
        if res:
            return res

    found = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if found:
        return _prepare_bin(found)

    if os.name == "nt":
        try:
            import winreg
            for root_key, subkey in [
                (winreg.HKEY_CURRENT_USER, r"Environment"),
                (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            ]:
                try:
                    with winreg.OpenKey(root_key, subkey) as key:
                        val, _ = winreg.QueryValueEx(key, "Path")
                        for p in (val or "").split(";"):
                            p = os.path.expandvars(p.strip())
                            candidate = os.path.join(p, "ffmpeg.exe")
                            res = _prepare_bin(candidate)
                            if res:
                                return res
                except OSError:
                    pass
        except ImportError:
            pass

    fallback_candidates = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\ffmpeg\ffmpeg.exe",
        os.path.join(program_files, "ffmpeg", "bin", "ffmpeg.exe"),
        os.path.join(program_files_x86, "ffmpeg", "bin", "ffmpeg.exe"),
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        os.path.join(app.root_path, "ffmpeg.exe"),
        os.path.join(app.root_path, "bin", "ffmpeg.exe"),
        os.path.join(app.instance_path, "ffmpeg.exe"),
        os.path.join(app.instance_path, "bin", "ffmpeg.exe"),
    ]

    import sys
    if sys.prefix:
        fallback_candidates.append(os.path.join(sys.prefix, "Scripts", "ffmpeg.exe"))
        fallback_candidates.append(os.path.join(sys.prefix, "ffmpeg.exe"))
    if hasattr(sys, "base_prefix") and sys.base_prefix:
        fallback_candidates.append(os.path.join(sys.base_prefix, "Scripts", "ffmpeg.exe"))
        fallback_candidates.append(os.path.join(sys.base_prefix, "ffmpeg.exe"))

    for c in fallback_candidates:
        res = _prepare_bin(c)
        if res:
            return res

    try:
        import imageio_ffmpeg
        res = _prepare_bin(imageio_ffmpeg.get_ffmpeg_exe())
        if res:
            return res
    except Exception:
        pass

    _CACHED_FFMPEG_SEARCHED = True
    _CACHED_FFMPEG_BIN = None
    return None



def _get_ffmpeg_version():
    """Extract first line of ffmpeg -version for diagnostics."""
    ffmpeg_bin = _get_ffmpeg_bin()
    if not ffmpeg_bin:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg_bin, "-version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if "ffmpeg version" in line:
                return line
        return None
    except Exception:
        return None


def _ffmpeg_available():
    return _get_ffmpeg_bin() is not None


@app.get("/api/video/info")
def video_info():
    bin_path = _get_ffmpeg_bin()
    version = _get_ffmpeg_version()
    is_outdated = False
    if version:
        # Detect older builds prior to FFmpeg 4.0 (e.g. 2013-2017)
        if re.search(r"200\d|201[0-7]", version):
            is_outdated = True
    return jsonify({
        "available": bin_path is not None,
        "engine": "FFmpeg" if bin_path else "Not found",
        "path": bin_path,
        "version": version,
        "outdated": is_outdated,
    })


def _ffmpeg_encoders():
    """Cached set of encoder names reported by `ffmpeg -encoders`."""
    if not hasattr(_ffmpeg_encoders, "cache"):
        _ffmpeg_encoders.cache = None
    if _ffmpeg_encoders.cache is None:
        ffmpeg_bin = _get_ffmpeg_bin() or "ffmpeg"
        try:
            proc = subprocess.run(
                [ffmpeg_bin, "-hide_banner", "-encoders"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=15,
            )
            # Very old FFmpeg (e.g. 2013 builds) does not know -hide_banner and
            # exits with an error; retry without it.
            if proc.returncode != 0:
                proc = subprocess.run(
                    [ffmpeg_bin, "-encoders"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=15,
                )
            names = set()
            for line in proc.stdout.decode("utf-8", "replace").splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[0][0] in "VAS":
                    names.add(parts[1])
            _ffmpeg_encoders.cache = names
        except (OSError, subprocess.SubprocessError):
            _ffmpeg_encoders.cache = set()
    return _ffmpeg_encoders.cache


def _encoder_available(name):
    """Check whether this FFmpeg build includes the given encoder."""
    encoders = _ffmpeg_encoders()
    if not encoders:
        return True  # Could not probe; let FFmpeg itself report the error.
    # `mp3`/`aac` style codec names map to lib* encoders on old builds.
    aliases = {
        "mp3": {"mp3", "libmp3lame"},
        "aac": {"aac", "libvo_aacenc", "libfaac"},
        "opus": {"opus", "libopus"},
        "vorbis": {"vorbis", "libvorbis"},
        "ac3": {"ac3", "ac3_fixed"},
    }
    if name in aliases:
        return bool(encoders & aliases[name])
    return name in encoders


def _resolve_audio_codec(codec_name):
    """Resolve logical codec name to the specific encoder supported by FFmpeg."""
    if codec_name == "opus":
        return "libopus" if _encoder_available("libopus") else "opus"
    if codec_name == "mp3":
        return "libmp3lame" if _encoder_available("libmp3lame") else "mp3"
    if codec_name == "vorbis":
        return "libvorbis" if _encoder_available("libvorbis") else "vorbis"
    return codec_name



def _cleanup_old_files():
    now = time.time()
    for folder in (VIDEO_UPLOAD_DIR, VIDEO_OUTPUT_DIR):
        try:
            for name in os.listdir(folder):
                path = os.path.join(folder, name)
                try:
                    if os.path.isfile(path) and now - os.path.getmtime(path) > VIDEO_CLEANUP_SECONDS:
                        os.remove(path)
                except OSError:
                    continue
        except OSError:
            continue


def _cleanup_loop():
    while True:
        time.sleep(5 * 60)
        _cleanup_old_files()


_cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True)
_cleanup_thread.start()


def _allowed_extension(filename):
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    return ext if ext in ALLOWED_VIDEO_EXTS else None


def _parse_time_to_seconds(value):
    """Accept seconds ('12.5') or 'mm:ss' / 'hh:mm:ss'. Returns float or None."""
    value = (value or "").strip()
    if not value:
        return None
    if not TIME_RE.match(value):
        return None
    try:
        if ":" in value:
            parts = [float(p) for p in value.split(":")]
            if any(p < 0 for p in parts) or len(parts) > 3:
                return None
            total = 0.0
            for part in parts:
                total = total * 60 + part
            return total
        total = float(value)
        return total if total >= 0 else None
    except ValueError:
        return None


def _parse_bitrate(value):
    value = (value or "").strip().lower().replace(" ", "")
    if not value:
        return None
    if not BITRATE_RE.match(value):
        return None
    return value.replace("m", "M")


def _bitrate_to_bps(value):
    try:
        v = value.strip().lower().replace(" ", "")
        if v.endswith("k"):
            return int(float(v[:-1]) * 1000)
        if v.endswith("m"):
            return int(float(v[:-1]) * 1000000)
        return int(float(v))
    except (ValueError, AttributeError):
        return None


def _validate_pro_settings(form):
    """Validate professional-mode fields. Returns (params, warnings, error)."""
    warnings = []
    params = {}

    out_format = (form.get("format") or "mp4").strip().lower()
    if out_format not in ALLOWED_FORMATS:
        return None, warnings, "Invalid output format."
    params["format"] = out_format

    vcodec = (form.get("video_codec") or "libx264").strip()
    if vcodec not in ALLOWED_VIDEO_CODECS:
        return None, warnings, "Invalid video codec."
    # Container/codec compatibility guard.
    if out_format == "webm" and vcodec not in {"libvpx-vp9", "libvpx"}:
        return None, warnings, "WebM only supports VP8 (libvpx) or VP9 (libvpx-vp9) video."
    if out_format == "avi" and vcodec not in {"mpeg4", "libx264"}:
        return None, warnings, "AVI only supports mpeg4 or libx264 in this tool."
    params["video_codec"] = vcodec
    if not _encoder_available(vcodec):
        have = sorted(n for n in ("libx264", "libx265", "libvpx", "libvpx-vp9", "mpeg4")
                      if _encoder_available(n))
        hint = f" Available here: {', '.join(have)}." if have else ""
        return None, warnings, f"This server's FFmpeg does not include '{vcodec}'.{hint}"

    remove_audio = (form.get("remove_audio") or "").strip().lower() in {"1", "true", "on", "yes"}
    params["remove_audio"] = remove_audio
    if not remove_audio:
        acodec = (form.get("audio_codec") or "aac").strip()
        if acodec not in ALLOWED_AUDIO_CODECS:
            return None, warnings, "Invalid audio codec."
        if out_format == "webm" and acodec not in {"opus", "vorbis"}:
            return None, warnings, "WebM audio must be opus or vorbis."
        params["audio_codec"] = acodec
        if not _encoder_available(acodec):
            return None, warnings, f"This server's FFmpeg does not include '{acodec}' audio. Try aac or mp3."
    else:
        params["audio_codec"] = None

    width = (form.get("width") or "").strip()
    height = (form.get("height") or "").strip()
    try:
        params["width"] = int(width) if width else None
        params["height"] = int(height) if height else None
    except ValueError:
        return None, warnings, "Width and height must be whole numbers."
    for dim in ("width", "height"):
        if params[dim] is not None and not 16 <= params[dim] <= 7680:
            return None, warnings, "Width and height must be between 16 and 7680."
    if (params["width"] is None) != (params["height"] is None):
        # Allow single-dimension scale; FFmpeg keeps aspect with -2.
        pass

    fps = (form.get("fps") or "").strip()
    if fps:
        try:
            fps_num = float(fps)
        except ValueError:
            return None, warnings, "FPS must be a number between 1 and 120."
        if not 1 <= fps_num <= 120:
            return None, warnings, "FPS must be between 1 and 120."
        params["fps"] = fps_num
    else:
        params["fps"] = None

    vbitrate = _parse_bitrate(form.get("video_bitrate") or "")
    if (form.get("video_bitrate") or "").strip() and vbitrate is None:
        return None, warnings, "Video bitrate looks invalid. Examples: 800k, 2M, 5000k."
    if vbitrate:
        bps = _bitrate_to_bps(vbitrate)
        if bps is None or not 50_000 <= bps <= 100_000_000:
            return None, warnings, "Video bitrate must be between 50k and 100M."
    params["video_bitrate"] = vbitrate

    abitrate = _parse_bitrate(form.get("audio_bitrate") or "")
    if (form.get("audio_bitrate") or "").strip() and abitrate is None:
        return None, warnings, "Audio bitrate looks invalid. Examples: 96k, 128k, 320k."
    if abitrate:
        bps = _bitrate_to_bps(abitrate)
        if bps is None or not 32_000 <= bps <= 1_000_000:
            return None, warnings, "Audio bitrate must be between 32k and 1000k."
    params["audio_bitrate"] = abitrate

    crf = (form.get("crf") or "").strip()
    if crf:
        try:
            crf_num = int(crf)
        except ValueError:
            return None, warnings, "CRF must be a whole number between 0 and 51."
        if not 0 <= crf_num <= 51:
            return None, warnings, "CRF must be between 0 and 51."
        if vcodec in {"libvpx-vp9", "libvpx"} and crf_num > 63:
            return None, warnings, "CRF must be between 0 and 51."
        params["crf"] = crf_num
    else:
        params["crf"] = None

    aspect = (form.get("aspect") or "source").strip()
    if aspect not in ALLOWED_ASPECTS:
        return None, warnings, "Invalid aspect ratio."
    params["aspect"] = aspect

    try:
        rotation = int((form.get("rotation") or "0").strip() or 0)
    except ValueError:
        return None, warnings, "Rotation must be 0, 90, 180, or 270."
    if rotation not in ALLOWED_ROTATIONS:
        return None, warnings, "Rotation must be 0, 90, 180, or 270."
    params["rotation"] = rotation

    preset = (form.get("preset") or "veryfast").strip()
    if preset not in ALLOWED_PRESETS:
        return None, warnings, "Invalid encoding preset."
    params["preset"] = preset

    start = _parse_time_to_seconds(form.get("trim_start") or "")
    end = _parse_time_to_seconds(form.get("trim_end") or "")
    if (form.get("trim_start") or "").strip() and start is None:
        return None, warnings, "Start time looks invalid. Use seconds or mm:ss."
    if (form.get("trim_end") or "").strip() and end is None:
        return None, warnings, "End time looks invalid. Use seconds or mm:ss."
    if start is not None and end is not None and end <= start:
        return None, warnings, "Trim end must be after trim start."
    if end is not None and end > 12 * 3600:
        return None, warnings, "Trim end is over 12 hours; please shorten it."
    params["trim_start"] = start
    params["trim_end"] = end

    # Heuristic warnings for very large outputs.
    if params["width"] and params["height"] and params["width"] * params["height"] >= 3840 * 2160:
        warnings.append("4K output can be very large and slow to encode.")
    if vbitrate and _bitrate_to_bps(vbitrate) and _bitrate_to_bps(vbitrate) >= 20_000_000:
        warnings.append("Video bitrate above 20M may create a very large file.")
    if params["crf"] is not None and params["crf"] <= 17:
        warnings.append("CRF 17 or lower means near-lossless quality and very large files.")
    if params["fps"] and params["fps"] >= 60:
        warnings.append("60+ FPS increases file size and encoding time.")
    return params, warnings, None


def _standard_to_params(form):
    task = (form.get("task") or "compress").strip().lower()
    resolution = (form.get("resolution") or "source").strip().lower()
    fps = (form.get("fps") or "source").strip().lower()
    quality = (form.get("quality") or "medium").strip().lower()
    if task not in STANDARD_TASKS:
        return None, [], "Invalid standard preset."
    if resolution not in STANDARD_RESOLUTIONS:
        return None, [], "Invalid resolution preset."
    if fps not in STANDARD_FPS:
        return None, [], "Invalid FPS preset."
    if quality not in STANDARD_QUALITY:
        return None, [], "Invalid quality preset."

    warnings = []
    params = {
        "format": "mp4", "video_codec": "libx264", "audio_codec": "aac",
        "width": None, "height": None, "fps": None, "video_bitrate": None,
        "audio_bitrate": None, "crf": QUALITY_CRF[quality], "aspect": "source",
        "rotation": 0, "trim_start": None, "trim_end": None,
        "remove_audio": False, "preset": "veryfast",
    }
    if task == "mp4":
        params["format"] = "mp4"
    elif task == "webm":
        params["format"] = "webm"
        # Prefer VP9, but old FFmpeg builds (e.g. 2013) only ship VP8 (libvpx).
        if _encoder_available("libvpx-vp9"):
            params["video_codec"] = "libvpx-vp9"
        else:
            params["video_codec"] = "libvpx"
            warnings.append("This server's FFmpeg has no VP9 encoder; using VP8 instead.")
        params["audio_codec"] = "opus"
    elif task == "compress":
        params["preset"] = "veryfast"
    if resolution != "source":
        params["height"] = RESOLUTION_HEIGHTS[resolution]
    if fps != "source":
        params["fps"] = float(fps)
    if resolution == "2160p" or quality == "high":
        warnings.append("High resolution / high quality can create a very large file.")
    return params, warnings, None


def _build_ffmpeg_args(params, input_path, output_path):
    """Build a safe FFmpeg argument list from validated params only."""
    ffmpeg_bin = _get_ffmpeg_bin() or "ffmpeg"
    args = [ffmpeg_bin, "-nostdin", "-y"]
    if params["trim_start"] is not None:
        args += ["-ss", str(params["trim_start"])]
    args += ["-i", input_path]
    if params["trim_end"] is not None:
        if params["trim_start"] is not None:
            args += ["-t", str(params["trim_end"] - params["trim_start"])]
        else:
            args += ["-t", str(params["trim_end"])]

    vf_filters = []

    # 1. Rotation first so downstream scale/aspect filters match rotated frame
    if params["rotation"] == 90:
        vf_filters.append("transpose=1")
    elif params["rotation"] == 180:
        vf_filters.append("transpose=1,transpose=1")
    elif params["rotation"] == 270:
        vf_filters.append("transpose=2")

    # 2. Scaling (use -2 to guarantee even dimensions for H.264 / H.265 / YUV420p)
    if params["width"] and params["height"]:
        w = (params["width"] // 2) * 2
        h = (params["height"] // 2) * 2
        vf_filters.append(f"scale={w}:{h}")
    elif params["height"] and not params["width"]:
        h = (params["height"] // 2) * 2
        vf_filters.append(f"scale=-2:{h}")
    elif params["width"] and not params["height"]:
        w = (params["width"] // 2) * 2
        vf_filters.append(f"scale={w}:-2")

    # 3. Aspect ratio padding (if requested)
    if params["aspect"] != "source":
        w, h = {"16:9": (1280, 720), "4:3": (960, 720), "1:1": (720, 720), "9:16": (720, 1280)}[params["aspect"]]
        vf_filters.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease")
        vf_filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
        vf_filters.append(f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")

    # 4. Guarantee even dimensions for H.264/H.265 if no scale filter was applied
    if params["video_codec"] in {"libx264", "libx265"} and not any(f.startswith("scale=") for f in vf_filters):
        vf_filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")

    if vf_filters:
        args += ["-vf", ",".join(vf_filters)]

    args += ["-c:v", params["video_codec"]]
    if params["video_codec"] in {"libx264", "libx265"}:
        args += ["-pix_fmt", "yuv420p"]
        args += ["-preset", params["preset"]]
    # Note: mpeg4 does NOT accept -preset (only x264/x265/vpx accept presets)

    if params["crf"] is not None and params["video_codec"] not in {"mpeg4"}:
        args += ["-crf", str(params["crf"])]
        if params["video_codec"] in {"libvpx-vp9", "libvpx"} and not params["video_bitrate"]:
            args += ["-b:v", "0"]

    if params["video_bitrate"]:
        args += ["-b:v", params["video_bitrate"]]
    if params["fps"]:
        fps_val = params["fps"]
        args += ["-r", str(int(fps_val) if float(fps_val).is_integer() else fps_val)]

    # Streamability: faststart allows instant web playback for MP4 and MOV
    if params["format"] in {"mp4", "mov"}:
        args += ["-movflags", "+faststart"]

    # Map primary video stream (ignores embedded album art or secondary poster streams)
    args += ["-map", "0:v:0"]
    # Prevent subtitle / unknown metadata streams from breaking conversion
    args += ["-sn"]

    if params["remove_audio"]:
        args += ["-an"]
    else:
        # Map audio optionally (? allows videos without an audio track to convert cleanly)
        args += ["-map", "0:a?"]
        audio_codec = _resolve_audio_codec(params["audio_codec"])
        args += ["-c:a", audio_codec]
        args += ["-strict", "-2"]
        if params["audio_bitrate"]:
            args += ["-b:a", params["audio_bitrate"]]

    args += [output_path]
    return args



# In-memory job registry for async video conversion. The old code ran FFmpeg
# synchronously inside the request handler, so a large file (multi-GB) blocked
# the HTTP response for 15+ minutes with zero real feedback and any FFmpeg
# stdin prompt hung the request until the 2 h timeout. Jobs now run in a
# background thread and the frontend polls /api/video/status/<job_id>.
VIDEO_JOBS = {}
VIDEO_JOBS_LOCK = threading.Lock()


def _set_job(job_id, **fields):
    with VIDEO_JOBS_LOCK:
        job = VIDEO_JOBS.setdefault(job_id, {})
        job.update(fields)
        job["updated_at"] = time.time()


def _run_video_job(job_id, ffmpeg_args, input_path, output_path, warnings, out_ext):
    """Background worker: run FFmpeg, record done/error state, clean up input."""
    _set_job(job_id, state="processing", progress=5)
    try:
        # stdin=DEVNULL + -nostdin: never let FFmpeg wait on stdin.
        # stderr is redirected to a file (not a PIPE) so long encodes with
        # megabytes of log output cannot fill a pipe buffer.
        log_path = os.path.join(VIDEO_OUTPUT_DIR, f"{job_id}.log")
        try:
            with open(log_path, "wb") as log_fh:
                result = subprocess.run(
                    ffmpeg_args, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=log_fh,
                    timeout=VIDEO_FFMPEG_TIMEOUT,
                )
            returncode = result.returncode
            detail = ""
            try:
                with open(log_path, "rb") as log_fh:
                    log_fh.seek(0, os.SEEK_END)
                    tail_start = max(0, log_fh.tell() - 4096)
                    log_fh.seek(tail_start)
                    detail = log_fh.read().decode("utf-8", "replace")
            except OSError:
                detail = ""
        finally:
            try:
                if os.path.exists(log_path):
                    os.remove(log_path)
            except OSError:
                pass
    except subprocess.TimeoutExpired:
        for path in (input_path, output_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        _set_job(job_id, state="error",
                 error="Conversion timed out. Try a shorter video or lower settings.")
        return
    except OSError:
        _set_job(job_id, state="error",
                 error="Could not start FFmpeg. Please try again later.")
        return
    finally:
        # Remove the upload right away in every case; keep output for download.
        try:
            if os.path.exists(input_path):
                os.remove(input_path)
        except OSError:
            pass

    if returncode != 0 or not os.path.exists(output_path):
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except OSError:
            pass
        detail_lines = [l.strip() for l in detail.replace("\r", "\n").splitlines() if l.strip()]
        error_lines = [l for l in detail_lines if not l.startswith("frame=") and not l.startswith("size=")]
        short_hint = " ".join(error_lines[-3:])[-300:] if error_lines else (" ".join(detail_lines[-2:])[-200:] if detail_lines else "")

        if not short_hint and returncode != 0:
            short_hint = "FFmpeg process was interrupted or terminated unexpectedly (e.g. server restart or out of memory)."

        if "Unknown encoder" in detail:
            missing = short_hint.split("Unknown encoder")[-1].strip(" '\"")[:40]
            _set_job(job_id, state="error",
                     error=f"FFmpeg on this server has no encoder '{missing}'. Try MP4 + H.264 + AAC.",
                     error_detail=short_hint)
        elif "Invalid data found" in detail or "moov atom not found" in detail:
            _set_job(job_id, state="error",
                     error="FFmpeg could not read this file. It may be corrupt or DRM-protected.",
                     error_detail=short_hint)
        elif "Unrecognized option" in detail or "Option not found" in detail:
            _set_job(job_id, state="error",
                     error=f"FFmpeg rejected an option ({short_hint}). Try updating FFmpeg or using MP4 + H.264 + AAC.",
                     error_detail=short_hint)
        else:
            msg = "Conversion failed. Try different settings (e.g. MP4 + H.264 + AAC)."
            if short_hint:
                msg += f" Details: {short_hint}"
            _set_job(job_id, state="error", error=msg, error_detail=short_hint)
        return

    try:
        output_size = os.path.getsize(output_path)
    except OSError:
        output_size = 0
    job_warnings = list(warnings or [])
    if output_size > 100 * 1024 * 1024:
        job_warnings.append("Output is over 100 MB; downloads may be slow on mobile data.")
    _cleanup_old_files()
    with VIDEO_JOBS_LOCK:
        VIDEO_JOBS[job_id].update({
            "state": "done",
            "progress": 100,
            "output_bytes": output_size,
            "format": out_ext,
            "warnings": job_warnings,
            "updated_at": time.time(),
        })


@app.post("/api/video/convert")
@rate_limit(limit=60, period=60)
def video_convert():

    if not _ffmpeg_available():
        return _video_error(
            "FFmpeg is not installed on the server. See README for Windows install steps.", 500
        )

    uploaded = request.files.get("video")
    if uploaded is None or not uploaded.filename:
        return _video_error("Please choose a video file first.")

    ext = _allowed_extension(uploaded.filename)
    if ext is None:
        return _video_error(
            "Unsupported file type. Please upload MP4, MOV, AVI, MKV, WebM, or MPEG."
        )

    # Read with size cap to enforce file-size limits safely.
    # Fast pre-check from the Content-Length header before touching the stream.
    if (request.content_length or 0) > app.config["MAX_CONTENT_LENGTH"]:
        return _video_error(
            f"File is too large. Limit is {_fmt_size(VIDEO_MAX_BYTES)}."
        )
    uploaded.stream.seek(0, os.SEEK_END)
    size = uploaded.stream.tell()
    uploaded.stream.seek(0)
    if size <= 0:
        return _video_error("The uploaded file looks empty.")
    if size > VIDEO_MAX_BYTES:
        return _video_error(
            f"File is too large ({_fmt_size(size)}). Limit is {_fmt_size(VIDEO_MAX_BYTES)}."
        )

    mode = (request.form.get("mode") or "standard").strip().lower()
    if mode == "standard":
        params, warnings, error = _standard_to_params(request.form)
    elif mode == "professional":
        params, warnings, error = _validate_pro_settings(request.form)
    else:
        return _video_error("Invalid mode. Choose Standard or Professional.")
    if error:
        return _video_error(error)

    job_id = secrets.token_urlsafe(16).replace("-", "").replace("_", "")[:24]
    safe_in = f"{job_id}_in.{ext}"
    out_ext = params["format"]
    safe_out = f"{job_id}_out.{out_ext}"
    input_path = os.path.join(VIDEO_UPLOAD_DIR, safe_in)
    output_path = os.path.join(VIDEO_OUTPUT_DIR, safe_out)

    try:
        uploaded.save(input_path)
    except OSError:
        return _video_error("Could not save the upload. Please try again.", 500)

    ffmpeg_args = _build_ffmpeg_args(params, input_path, output_path)

    with VIDEO_JOBS_LOCK:
        VIDEO_JOBS[job_id] = {
            "state": "queued", "progress": 0, "warnings": list(warnings or []),
            "format": out_ext, "created_at": time.time(), "updated_at": time.time(),
        }
    # Prune jobs older than the download window so the dict cannot grow forever.
    with VIDEO_JOBS_LOCK:
        cutoff = time.time() - VIDEO_CLEANUP_SECONDS - 600
        for old_id in [jid for jid, j in VIDEO_JOBS.items()
                       if j.get("updated_at", 0) < cutoff]:
            VIDEO_JOBS.pop(old_id, None)

    worker = threading.Thread(
        target=_run_video_job,
        args=(job_id, ffmpeg_args, input_path, output_path, warnings, out_ext),
        daemon=True,
    )
    worker.start()

    # Return immediately (HTTP 202) so the request never hangs during encoding.
    # The frontend polls status_url until the job is done or errors out.
    return jsonify({
        "job_id": job_id,
        "status_url": url_for("video_status", job_id=job_id),
        "message": "Upload received. Conversion started.",
    }), 202


@app.get("/api/video/status/<job_id>")
def video_status(job_id):
    if not JOB_RE.match(job_id or ""):
        return _video_error("Unknown job.", 404)
    with VIDEO_JOBS_LOCK:
        job = VIDEO_JOBS.get(job_id)
        job_snapshot = dict(job) if job else None
    # Fall back to the filesystem: server may have restarted after the upload,
    # or this worker never saw the job dict (multi-process deployment).
    if job_snapshot is None:
        for ext in ALLOWED_FORMATS:
            path = os.path.join(VIDEO_OUTPUT_DIR, f"{job_id}_out.{ext}")
            if os.path.exists(path):
                try:
                    output_size = os.path.getsize(path)
                except OSError:
                    output_size = 0
                return jsonify({
                    "job_id": job_id, "state": "done", "progress": 100,
                    "download_url": url_for("video_download", job_id=job_id),
                    "stream_url": url_for("video_stream", job_id=job_id),
                    "output_bytes": output_size, "format": ext, "warnings": [],
                })
        return _video_error("Unknown job. It may have expired; please convert again.", 404)
    state = job_snapshot.get("state", "queued")
    # Fake-but-honest progress while encoding so the UI never looks frozen.
    # Real FFmpeg progress parsing would need -progress pipe wiring; the elapsed
    # timer at least proves the server is alive.
    if state == "processing":
        elapsed = time.time() - job_snapshot.get("created_at", time.time())
        # Creep 5 -> 95 over ~20 min, never hitting 100 before done.
        progress = min(95, 5 + int(elapsed / 1200 * 90))
        job_snapshot["progress"] = progress
    elif state == "queued":
        job_snapshot["progress"] = 0
    if state == "done":
        job_snapshot["download_url"] = url_for("video_download", job_id=job_id)
        job_snapshot["stream_url"] = url_for("video_stream", job_id=job_id)
    return jsonify({
        "job_id": job_id,
        "state": state,
        "progress": job_snapshot.get("progress", 0),
        "download_url": job_snapshot.get("download_url"),
        "stream_url": job_snapshot.get("stream_url"),
        "output_bytes": job_snapshot.get("output_bytes"),
        "format": job_snapshot.get("format"),
        "warnings": job_snapshot.get("warnings", []),
        "error": job_snapshot.get("error"),
        "error_detail": job_snapshot.get("error_detail"),
    })


@app.get("/api/video/stream/<job_id>")
def video_stream(job_id):
    if not JOB_RE.match(job_id or ""):
        abort(404)
    mimetypes = {
        "mp4": "video/mp4",
        "webm": "video/webm",
        "mov": "video/quicktime",
        "mkv": "video/x-matroska",
        "avi": "video/x-msvideo",
    }
    for ext in ALLOWED_FORMATS:
        path = os.path.join(VIDEO_OUTPUT_DIR, f"{job_id}_out.{ext}")
        if os.path.exists(path):
            mime = mimetypes.get(ext, "application/octet-stream")
            return send_file(path, as_attachment=False, mimetype=mime)
    return render_template("not_found.html"), 404


@app.get("/api/video/download/<job_id>")
def video_download(job_id):
    if not JOB_RE.match(job_id or ""):
        abort(404)
    for ext in ALLOWED_FORMATS:
        path = os.path.join(VIDEO_OUTPUT_DIR, f"{job_id}_out.{ext}")
        if os.path.exists(path):
            return send_file(path, as_attachment=True, download_name=f"converted.{ext}")
    return render_template("not_found.html"), 404


@app.get("/api/video/estimate")
def video_estimate():
    """Rough output-size estimate so Standard Mode can show a hint."""
    try:
        original = float(request.args.get("original_bytes") or 0)
    except ValueError:
        original = 0
    quality = (request.args.get("quality") or "medium").lower()
    resolution = (request.args.get("resolution") or "source").lower()
    factor = {"low": 0.35, "medium": 0.6, "high": 0.9}.get(quality, 0.6)
    res_factor = {
        "source": 1.0, "2160p": 1.6, "1080p": 1.0,
        "720p": 0.6, "480p": 0.4, "360p": 0.3,
    }.get(resolution, 1.0)
    vb = _bitrate_to_bps(_parse_bitrate(request.args.get("video_bitrate") or "") or "")
    ab = _bitrate_to_bps(_parse_bitrate(request.args.get("audio_bitrate") or "") or "")
    duration = None
    try:
        duration = float(request.args.get("duration_sec") or 0) or None
    except ValueError:
        duration = None
    if vb and duration:
        estimate = int((vb + (ab or 128_000)) * duration / 8)
    elif original > 0:
        estimate = int(original * factor * res_factor)
    else:
        estimate = 0
    return jsonify({"estimated_bytes": estimate})


@app.route("/image-to-text")
def image_to_text():
    return render_template("ocr.html")


@app.route("/link-to-qr")
def link_to_qr():
    return render_template("qr_generator.html")


@app.route("/link-shortener")
def link_shortener():
    return render_template("shortener.html")


@app.route("/about")
def about():
    return render_template(
        "info.html",
        title="About",
        text="This website provides simple browser tools for QR scanning, OCR, QR generation, and safe short links.",
    )


@app.route("/privacy-policy")
def privacy():
    return render_template(
        "info.html",
        title="Privacy Policy",
        text="Uploaded images are handled in your browser for QR scanning and OCR. Short links and bad-link reports are saved in SQLite. Do not submit private information.",
    )


@app.route("/terms")
def terms():
    return render_template(
        "info.html",
        title="Terms",
        text="Use this website only for legal and safe links. Bad, harmful, misleading, or illegal links may be removed.",
    )


@app.route("/contact")
def contact():
    return render_template(
        "info.html",
        title="Contact",
        text="Add your email address or contact form details in this page before publishing the website.",
    )


@app.post("/api/generate-qr")
@rate_limit(limit=30, period=60)
def generate_qr():
    data = request.get_json(silent=True) or {}
    url = normalize_url(data.get("url"))

    if not url:
        return jsonify({"error": "Please enter a valid http:// or https:// link."}), 400

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)

    image = qr.make_image(fill_color="#111827", back_color="white")
    image_buffer = io.BytesIO()
    image.save(image_buffer, format="PNG")

    image_data = base64.b64encode(image_buffer.getvalue()).decode("utf-8")
    return jsonify({"url": url, "image": f"data:image/png;base64,{image_data}"})


@app.post("/api/shorten")
@rate_limit(limit=15, period=60)
def shorten():
    data = request.get_json(silent=True) or {}
    long_url = normalize_url(data.get("url"))

    if not long_url:
        return jsonify({"error": "Please enter a valid http:// or https:// link."}), 400

    if points_to_this_site(long_url):
        return jsonify({"error": "You cannot shorten a link to this website."}), 400

    db = get_db()
    existing = db.execute(
        "SELECT code FROM links WHERE long_url = ?",
        (long_url,),
    ).fetchone()

    if existing:
        code = existing["code"]
    else:
        code = create_code()
        db.execute(
            "INSERT INTO links (code, long_url, clicks, created_at) VALUES (?, ?, ?, ?)",
            (
                code,
                long_url,
                0,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db.commit()

    short_url = f"{request.url_root.rstrip('/')}/s/{code}"
    return jsonify({"short_url": short_url})


@app.route("/s/<code>")
def short_ad_page(code):
    link = get_db().execute(
        "SELECT code FROM links WHERE code = ?",
        (code,),
    ).fetchone()

    if link is None:
        return render_template("not_found.html"), 404

    # The signed timestamp makes the backend enforce the five-second wait.
    token = token_serializer.dumps({"code": code, "issued_at": time.time()})
    return render_template("ad_gate.html", code=code, token=token)


@app.post("/api/unlock")
@rate_limit(limit=30, period=60)
def unlock_link():
    data = request.get_json(silent=True) or {}
    token = data.get("token", "")

    link, error = read_wait_token(token, require_wait=True)
    if error:
        return jsonify({"error": error}), 400

    open_url = url_for("open_short_link", code=link["code"], token=token)
    return jsonify({"open_url": open_url})


@app.route("/go/<code>")
def open_short_link(code):
    token = request.args.get("token", "")
    link, error = read_wait_token(token, require_wait=True)

    if error or link["code"] != code:
        return render_template("not_found.html"), 404

    db = get_db()
    db.execute("UPDATE links SET clicks = clicks + 1 WHERE code = ?", (code,))
    db.commit()

    return redirect(link["long_url"], code=302)


@app.post("/api/report")
@rate_limit(limit=10, period=60)
def report_link():
    data = request.get_json(silent=True) or {}
    token = data.get("token", "")
    reason = (data.get("reason", "") or "").strip()[:500]

    link, error = read_wait_token(token, require_wait=False)
    if error:
        return jsonify({"error": error}), 400

    db = get_db()
    db.execute(
        "INSERT INTO reports (code, reason, reported_at) VALUES (?, ?, ?)",
        (
            link["code"],
            reason,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.commit()

    return jsonify({"message": "Thanks. Your report was saved."})


@app.errorhandler(404)
def page_not_found(error):
    return render_template("not_found.html"), 404


@app.errorhandler(413)
def too_large(error):
    if request.path.startswith("/api/video/"):
        return jsonify({
            "error": f"File is too large. Limit is {_fmt_size(VIDEO_MAX_BYTES)}."
        }), 413
    return render_template("not_found.html"), 413


if __name__ == "__main__":
    with app.app_context():
        init_db()

    # debug=True is useful only on your computer.
    # use_reloader=False prevents Flask reloader from restarting server and killing active conversion threads when files are saved
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)