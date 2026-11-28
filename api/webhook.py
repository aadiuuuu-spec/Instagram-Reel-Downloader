import glob
import html
from html.parser import HTMLParser
import http.client
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

import requests
import yt_dlp

# Instagram may return more than Python's default 100 response headers.
# Keep a bounded higher limit so yt-dlp can parse the response safely.
http.client._MAXHEADERS = 200

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "7508536182"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
DEVELOPER = os.environ.get("DEVELOPER_USERNAME", "@II_YUTA_II")
SUPPORT_URL = os.environ.get("SUPPORT_GROUP_URL", "https://t.me/+xbOwGbcZA-AwZDkx")

# Note: Vercel instances are stateless. This in-memory switch is suitable for
# initial testing only and may reset when the function instance is recycled.
BOT_ENABLED = True

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
IG_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv|share/(?:p|reel))/[^\s]+", re.I)

DEBUG_ERRORS = os.environ.get("DEBUG_ERRORS", "1") == "1"
COOKIE_FILE = "/tmp/ig_cookies.txt"
_COOKIE_CACHE = {"ts": 0.0, "cookies": None}
_BAD_COOKIE_KEYS = {"path", "domain", "expires", "secure", "httponly", "samesite", "max-age"}


def _parse_cookie_text(text):
    """Parse only Instagram cookies from Netscape, JSON, or an explicit cookie header."""
    text = (text or "").strip().strip('"').strip("'")
    cookies = {}
    if not text:
        return cookies

    # Netscape exports carry a domain per row: reject every non-Instagram host.
    if "\t" in text:
        for line in text.replace("\\n", "\n").splitlines():
            line = line.strip()
            http_only = line.startswith("#HttpOnly_")
            if http_only:
                line = line[len("#HttpOnly_"):]
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain = parts[0].lstrip(".").lower()
            if domain != "instagram.com" and not domain.endswith(".instagram.com"):
                continue
            key, value = parts[5].strip(), parts[6]
            if key and key.lower() not in _BAD_COOKIE_KEYS:
                cookies[key] = value
        return cookies

    # Browser JSON exports include a domain field; require it to be Instagram.
    if text[0] in "[{":
        try:
            data = json.loads(text)
            if isinstance(data, dict) and isinstance(data.get("cookies"), list):
                data = data["cookies"]
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    domain = str(item.get("domain") or "").lstrip(".").lower()
                    if domain != "instagram.com" and not domain.endswith(".instagram.com"):
                        continue
                    key, value = str(item.get("name") or "").strip(), item.get("value")
                    if key and value is not None and key.lower() not in _BAD_COOKIE_KEYS:
                        cookies[key] = str(value)
                return cookies
            if isinstance(data, dict):
                data = {k: v for k, v in data.items()
                        if str(k).lower() not in _BAD_COOKIE_KEYS and v is not None}
                return {str(k): str(v) for k, v in data.items() if str(k).strip()}
        except Exception:
            pass

    # A plain Cookie header is accepted only via the dedicated INSTAGRAM_COOKIES
    # environment variable, so its caller explicitly assigns it to Instagram.
    for chunk in re.split(r";\s*|\n", text):
        if "=" in chunk:
            key, value = chunk.split("=", 1)
            key, value = key.strip(), value.strip()
            if key and key.lower() not in _BAD_COOKIE_KEYS:
                cookies[key] = value
    return cookies

def load_instagram_cookies():
    """Load only the explicitly configured INSTAGRAM_COOKIES secret.

    Accepted formats: Cookie request header (name=value; ...), Netscape
    cookies.txt text, or a JSON cookie export. Never scan unrelated env vars
    and never fetch a URL from an environment variable.
    """
    now = time.time()
    if _COOKIE_CACHE["cookies"] is not None and now - _COOKIE_CACHE["ts"] < 600:
        return _COOKIE_CACHE["cookies"]

    raw_value = (os.environ.get("INSTAGRAM_COOKIES") or "").strip()
    cookies = _parse_cookie_text(raw_value) if raw_value else {}

    # Remove any stale file left by a previous warm invocation/configuration.
    try:
        if not cookies:
            if os.path.exists(COOKIE_FILE):
                os.remove(COOKIE_FILE)
        else:
            lines = ["# Netscape HTTP Cookie File"]
            for key, value in cookies.items():
                # Keep cookie data out of logs; only write the values to the
                # private temporary cookie file required by yt-dlp.
                lines.append("\t".join([
                    ".instagram.com", "TRUE", "/", "TRUE",
                    "2147483647", key, value,
                ]))
            with open(COOKIE_FILE, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            try:
                os.chmod(COOKIE_FILE, 0o600)
            except OSError:
                logging.warning("Could not restrict permissions on Instagram cookie file")
    except OSError:
        logging.exception("Could not update Instagram cookie file")
        cookies = {}

    _COOKIE_CACHE.update(ts=now, cookies=cookies)
    logging.info("Instagram cookie configuration loaded: %s cookie entries", len(cookies))
    return cookies

def cookie_header(cookies):
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def get_ffmpeg():
    """Returns (ffmpeg_exe, ffmpeg_dir) using the binary bundled by imageio-ffmpeg, or (None, None)."""
    try:
        import imageio_ffmpeg
        source = imageio_ffmpeg.get_ffmpeg_exe()
        os.makedirs("/tmp/ffbin", exist_ok=True)
        target = "/tmp/ffbin/ffmpeg"
        if not os.path.exists(target):
            shutil.copy(source, target)
        os.chmod(target, 0o755)
        return target, "/tmp/ffbin"
    except Exception:
        logging.exception("ffmpeg unavailable")
        return None, None


def shortcode_to_media_id(shortcode):
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if len(shortcode) > 28:
        shortcode = shortcode[:-28]
    number = 0
    for char in shortcode:
        number = number * 64 + alphabet.index(char)
    return number


def tg(method, data=None, files=None, timeout=25):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")
    response = requests.post(f"{API}/{method}", data=data, files=files, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", "Telegram API error"))
    return payload.get("result")


def send_text(chat_id, text, reply_markup=None, parse_mode=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return tg("sendMessage", payload)


def edit_text(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    return tg("editMessageText", payload)


def is_supported_instagram_url(url):
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and parsed.netloc.lower() in {
        "instagram.com", "www.instagram.com"
    } and bool(re.match(r"^/(p|reel|reels|tv|share/(p|reel))/", parsed.path, re.I))


def start_markup():
    return {
        "inline_keyboard": [
            [{
                "text": "📥 𝖣ᴏᴡɴʟᴏᴀᴅ 𝖱ᴇᴇʟ",
                "callback_data": "download_help",
                "style": "success",
            }],
            [
                {
                    "text": "👨‍💻 𝖣ᴇᴠᴇʟᴏᴘᴇʀ",
                    "url": f"https://t.me/{DEVELOPER.lstrip('@')}",
                    "style": "danger",
                },
                {
                    "text": "💬 𝖲ᴜᴘᴘᴏʀᴛ",
                    "url": SUPPORT_URL,
                    "style": "danger",
                },
            ],
        ]
    }


def admin_markup():
    return {
        "inline_keyboard": [[
            {"text": "🟢 TURN ON", "callback_data": "admin_on"},
            {"text": "🔴 TURN OFF", "callback_data": "admin_off"},
        ]]
    }


def download_public_page_media(url, temp_dir, image_only=False):
    """Fetch media explicitly exposed by a public Instagram page's Open Graph metadata."""
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    session = requests.Session()
    session.headers.update(headers)
    # Use only the user-configured Instagram cookies; never fetch or log secrets.
    for key, value in load_instagram_cookies().items():
        session.cookies.set(key, value, domain=".instagram.com", path="/")
    response = session.get(url, timeout=25, allow_redirects=True)
    response.raise_for_status()
    page = response.text[:5_000_000]

    class _MetaParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.values = {}
        def handle_starttag(self, tag, attrs):
            if tag.lower() != "meta":
                return
            attrs = {str(k).lower(): v for k, v in attrs if k}
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            value = attrs.get("content")
            if key in {"og:image", "og:video", "og:video:secure_url", "twitter:image"} and value:
                self.values.setdefault(key, html.unescape(value).replace("&amp;", "&"))

    parser = _MetaParser()
    parser.feed(page)
    found = parser.values
    media_url = (found.get("og:image") or found.get("twitter:image") if image_only else
                 found.get("og:video:secure_url") or found.get("og:video") or
                 found.get("og:image") or found.get("twitter:image"))
    if not media_url or not media_url.startswith("https://"):
        logging.info("Public Instagram page exposed no usable Open Graph media metadata")
        return []
    media = session.get(media_url, timeout=35, stream=True)
    media.raise_for_status()
    content_type = media.headers.get("content-type", "").lower()
    ext = ".mp4" if "video" in content_type or ".mp4" in urlparse(media_url).path.lower() else ".jpg"
    path = os.path.join(temp_dir, "instagram_public_fallback" + ext)
    total = 0
    with open(path, "wb") as output:
        for chunk in media.iter_content(1024 * 128):
            if chunk:
                total += len(chunk)
                if total > 48 * 1024 * 1024:
                    raise RuntimeError("Instagram media exceeds the 48 MB download limit.")
                output.write(chunk)
    return [path] if total else []



def _save_stream(session, media_url, path_base, headers=None):
    response = session.get(media_url, timeout=35, stream=True, headers=headers)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    ext = ".mp4" if "video" in content_type else ".jpg"
    if "audio" in content_type:
        ext = ".m4a"
    path = path_base + ext
    total = 0
    with open(path, "wb") as output:
        for chunk in response.iter_content(128 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > 48 * 1024 * 1024:
                raise RuntimeError("Instagram media exceeds the 48 MB per-file limit.")
            output.write(chunk)
    return path if total else None


def download_instagram_api_media(url, temp_dir, meta=None):
    """Instagram private media-info API (media id from shortcode). Uses login cookies when present.
    Returns every photo/video of a post, including carousels."""
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) < 2 or parts[0].lower() not in ("p", "reel", "reels", "tv"):
        return []
    media_id = shortcode_to_media_id(parts[1])
    cookies = load_instagram_cookies()
    headers = {
        "User-Agent": "Instagram 275.0.0.27.98 Android (33/13; 420dpi; 1080x2400; Google; Pixel 7; panther; tensor; en_US; 458229237)",
        "X-IG-App-ID": "567067343352427",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if cookies:
        headers["Cookie"] = cookie_header(cookies)
        if cookies.get("csrftoken"):
            headers["X-CSRFToken"] = cookies["csrftoken"]
    payload = None
    last_error = None
    for endpoint in (
        f"https://i.instagram.com/api/v1/media/{media_id}/info/",
        f"https://www.instagram.com/api/v1/media/{media_id}/info/",
    ):
        try:
            response = requests.get(endpoint, headers=headers, timeout=25)
            response.raise_for_status()
            payload = response.json()
            if payload.get("items"):
                break
        except Exception as exc:
            last_error = exc
    if not payload or not payload.get("items"):
        raise RuntimeError(f"media-info API failed: {last_error}")
    root = payload["items"][0]
    if meta is not None:
        caption = (root.get("caption") or {}).get("text") if isinstance(root.get("caption"), dict) else None
        if caption:
            meta["title"] = caption[:900]
    media_items = root.get("carousel_media") or [root]
    saved = []
    session = requests.Session()
    cdn_headers = {"User-Agent": headers["User-Agent"]}
    for index, item in enumerate(media_items, start=1):
        if item.get("media_type") == 2 or item.get("video_versions"):
            versions = item.get("video_versions") or []
            media_url = versions[0].get("url") if versions else None
        else:
            candidates = (item.get("image_versions2") or {}).get("candidates") or []
            media_url = candidates[0].get("url") if candidates else None
        if not media_url:
            continue
        path = _save_stream(session, media_url, os.path.join(temp_dir, f"instagram_api_{index}"), cdn_headers)
        if path:
            saved.append(path)
    return saved



def download_instagram_photo_post(url, temp_dir):
    """Dedicated photo-only path: download the largest available image variant for each photo."""
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) < 2 or parts[0].lower() != "p":
        return []
    media_id = shortcode_to_media_id(parts[1])
    cookies = load_instagram_cookies()
    headers = {
        "User-Agent": "Instagram 275.0.0.27.98 Android (33/13; 420dpi; 1080x2400; Google; Pixel 7; panther; tensor; en_US; 458229237)",
        "X-IG-App-ID": "567067343352427",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if cookies:
        headers["Cookie"] = cookie_header(cookies)
        if cookies.get("csrftoken"):
            headers["X-CSRFToken"] = cookies["csrftoken"]

    payload = None
    last_error = None
    for endpoint in (
        f"https://i.instagram.com/api/v1/media/{media_id}/info/",
        f"https://www.instagram.com/api/v1/media/{media_id}/info/",
    ):
        try:
            response = requests.get(endpoint, headers=headers, timeout=25)
            response.raise_for_status()
            candidate = response.json()
            if candidate.get("items"):
                payload = candidate
                break
        except Exception as exc:
            last_error = exc
    if not payload:
        raise RuntimeError(f"photo media-info unavailable: {last_error}")

    root = payload["items"][0]
    media_items = root.get("carousel_media") or [root]
    session = requests.Session()
    saved = []
    for index, item in enumerate(media_items, start=1):
        if item.get("media_type") == 2 or item.get("video_versions"):
            continue
        candidates = (item.get("image_versions2") or {}).get("candidates") or []
        if not candidates:
            continue
        best = max(
            candidates,
            key=lambda candidate: (
                int(candidate.get("width") or 0) * int(candidate.get("height") or 0),
                int(candidate.get("width") or 0)
            )
        )
        media_url = best.get("url")
        if not media_url or urlparse(media_url).scheme != "https":
            continue
        path = _save_stream(
            session, media_url,
            os.path.join(temp_dir, f"instagram_original_photo_{index}"),
            {"User-Agent": headers["User-Agent"]}
        )
        if path and os.path.splitext(path)[1].lower() in (".jpg", ".jpeg", ".png", ".webp"):
            saved.append(path)
    return saved

def resolve_public_instagram_url(url):
    """Follow public share-link redirects without using an authenticated session."""
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; PublicMediaBot/1.0)"},
        allow_redirects=True,
        timeout=20,
        stream=True,
    )
    try:
        final_url = response.url
    finally:
        response.close()
    parsed = urlparse(final_url)
    if parsed.netloc.lower() not in {"instagram.com", "www.instagram.com"}:
        raise RuntimeError("Instagram share link redirected outside Instagram.")
    return final_url


MEDIA_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v", ".webm",
              ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav")


def _collect_files(temp_dir):
    return sorted(
        path for path in glob.glob(os.path.join(temp_dir, "**", "*"), recursive=True)
        if os.path.isfile(path) and os.path.getsize(path) > 0
        and os.path.splitext(path)[1].lower() in MEDIA_EXTS
    )


def fetch_media(url, temp_dir, meta, errors):
    """Tries several strategies. Photo/carousel posts try the cookie API first (yt-dlp only returns videos)."""
    parsed_path = urlparse(url).path
    # Optional session cookies from Vercel environment variables may help yt-dlp
    # retrieve media that is already publicly accessible to the configured account.
    # They are never used to access private posts or bypass Instagram restrictions.
    cookies = load_instagram_cookies()
    ffmpeg_exe, ffmpeg_dir = get_ffmpeg()

    def run_api():
        return download_instagram_api_media(url, temp_dir, meta)

    def run_ytdlp():
        # Safe diagnostics: report the installed extractor version, never cookies or headers.
        logging.info("yt-dlp version in runtime: %s", getattr(yt_dlp.version, "__version__", "unknown"))
        options = {
            "outtmpl": os.path.join(temp_dir, "ytdlp_%(playlist_index)s_%(id)s.%(ext)s"),
            "format": "bv*+ba/b" if ffmpeg_dir else "b",
            "merge_output_format": "mp4",
            "noplaylist": False,
            # Do not suppress extractor failures: bubble them to fetch_media(),
            # where the full sanitized exception is written to Vercel runtime logs.
            "quiet": True,
            "no_warnings": False,
            "socket_timeout": 25,
            "retries": 2,
            "max_filesize": 48 * 1024 * 1024,
        }
        if ffmpeg_dir:
            options["ffmpeg_location"] = ffmpeg_dir
        if cookies and os.path.isfile(COOKIE_FILE):
            options["cookiefile"] = COOKIE_FILE
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                raise RuntimeError("yt-dlp returned no media information; check the preceding extractor warning/error in Vercel logs.")
            if info and info.get("title"):
                meta["title"] = info["title"][:900]
        return _collect_files(temp_dir)

    def run_instaloader():
        if not re.match(r"^/(p|reel|reels|tv)/", parsed_path, re.I):
            return []
        import instaloader
        loader = instaloader.Instaloader(
            dirname_pattern=os.path.join(temp_dir, "il"),
            filename_pattern="{shortcode}",
            download_pictures=True,
            download_videos=True,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern="",
        )
        if cookies.get("sessionid") and cookies.get("csrftoken"):
            loader.context.load_session(cookies.get("ds_user_id", "user"), cookies)
        shortcode = parsed_path.strip("/").split("/")[1]
        post = instaloader.Post.from_shortcode(loader.context, shortcode)
        meta["title"] = (post.caption or "Instagram media")[:900]
        loader.download_post(post, target="media")
        return _collect_files(os.path.join(temp_dir, "il"))

    def run_instaloader_photos():
        """Public photo-only provider for image posts and image-only carousels.

        Mixed photo/video carousels are deliberately left to the full-media
        provider so video slides are not silently omitted.
        """
        if not re.match(r"^/p/", parsed_path, re.I):
            return []
        import instaloader
        loader = instaloader.Instaloader(
            dirname_pattern=os.path.join(temp_dir, "il_photos"),
            filename_pattern="{shortcode}",
            download_pictures=True,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern="",
            quiet=True,
        )
        shortcode = parsed_path.strip("/").split("/")[1]
        post = instaloader.Post.from_shortcode(loader.context, shortcode)
        if post.is_video:
            return []
        if post.typename == "GraphSidecar":
            if any(node.is_video for node in post.get_sidecar_nodes()):
                return []
        meta["title"] = (post.caption or "Instagram photos")[:900]
        loader.download_post(post, target="photos")
        return _collect_files(os.path.join(temp_dir, "il_photos"))

    def run_og(image_only=False):
        return download_public_page_media(url, temp_dir, image_only=image_only)

    # Dedicated photo-only extraction runs first for /p/ posts.
    # It selects the largest image candidate per carousel item and skips video thumbnails.
    if re.match(r"^/p/", parsed_path, re.I):
        try:
            photo_files = download_instagram_photo_post(url, temp_dir)
            if photo_files:
                logging.info("Dedicated original-photo pipeline returned %s photos", len(photo_files))
                return photo_files
            logging.info("Dedicated original-photo pipeline found no photo media; continuing normal extraction")
        except Exception as exc:
            logging.warning("Dedicated original-photo pipeline failed: %s: %s", type(exc).__name__, str(exc)[:200])

    # Prefer media explicitly exposed by the public page. This avoids making
    # several API/GraphQL requests that may trigger Instagram feedback_required.
    # yt-dlp remains next for full video + original audio; API and Instaloader
    # remain last-resort extractors for posts/carousels.
    is_post_url = bool(re.match(r"^/p/", parsed_path, re.I))
    if is_post_url:
        steps = [
            ("public-page-metadata", run_og),
            ("instaloader-photo-carousel", run_instaloader_photos),
            ("yt-dlp", run_ytdlp),
            ("instagram-media-info", run_api),
            ("instaloader-public-post", run_instaloader),
        ]
    else:
        steps = [("yt-dlp", run_ytdlp), ("public-page-metadata", run_og)]

    for name, func in steps:
        try:
            files = func()
            if files:
                logging.info("Strategy %s returned %s files", name, len(files))
                return files
            errors.append(f"{name}: no files")
        except Exception as exc:
            logging.exception("Strategy %s failed", name)
            errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:200]}")
    return []


def download_reel(chat_id, url, reply_to=None):
    status = send_text(chat_id, "⏳ Processing public Instagram media...")
    status_id = status["message_id"]

    try:
        with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
            url = resolve_public_instagram_url(url)
            meta = {"title": "Instagram media"}
            errors = []
            candidates = fetch_media(url, temp_dir, meta, errors)
            title = meta["title"]
            if not candidates:
                raise RuntimeError("No downloadable media was returned by Instagram. " + " | ".join(errors))

            album_items = []
            separate_items = []
            audio_items = []
            for path in sorted(candidates):
                ext = os.path.splitext(path)[1].lower()
                size = os.path.getsize(path)
                if size > 50 * 1024 * 1024:
                    logging.warning("Skipping oversized Instagram item: %s", os.path.basename(path))
                    continue
                if ext in (".jpg", ".jpeg", ".png", ".webp"):
                    album_items.append((path, "photo"))
                elif ext in (".mp4", ".mov", ".m4v", ".webm"):
                    album_items.append((path, "video"))
                    # Extract a separate audio file from video posts/Reels.
                    try:
                        ffmpeg, _ = get_ffmpeg()
                        if not ffmpeg:
                            raise RuntimeError("ffmpeg not available")
                        audio_path = os.path.splitext(path)[0] + "_audio.mp3"
                        subprocess.run(
                            [ffmpeg, "-y", "-i", path, "-vn", "-acodec", "libmp3lame",
                             "-q:a", "4", audio_path],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=90,
                            check=True,
                        )
                        if os.path.isfile(audio_path) and os.path.getsize(audio_path) > 0:
                            audio_items.append(audio_path)
                    except Exception:
                        logging.exception("Audio extraction unavailable for %s", os.path.basename(path))
                elif ext in (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"):
                    audio_items.append(path)
                else:
                    separate_items.append((path, "document"))

            if not album_items and not separate_items and not audio_items:
                raise RuntimeError("No supported photo, video, or audio files were downloaded.")

            if len(album_items) == 1:
                path, kind = album_items[0]
                method = "sendPhoto" if kind == "photo" else "sendVideo"
                field = "photo" if kind == "photo" else "video"
                with open(path, "rb") as media:
                    tg(method, {"chat_id": chat_id, "caption": title[:1024]},
                       files={field: (os.path.basename(path), media)}, timeout=240)
            else:
                for offset in range(0, len(album_items), 10):
                    batch = album_items[offset:offset + 10]
                    if len(batch) == 1:
                        path, kind = batch[0]
                        method = "sendPhoto" if kind == "photo" else "sendVideo"
                        field = "photo" if kind == "photo" else "video"
                        with open(path, "rb") as media:
                            tg(method, {"chat_id": chat_id, "caption": title[:1024]},
                               files={field: (os.path.basename(path), media)}, timeout=240)
                        continue
                    media_payload = []
                    open_files = []
                    upload_files = {}
                    try:
                        for index, (path, kind) in enumerate(batch):
                            attach_name = f"file{index}"
                            media_payload.append({
                                "type": kind,
                                "media": f"attach://{attach_name}",
                                **({"caption": title[:1024]} if index == 0 else {}),
                            })
                            handle = open(path, "rb")
                            open_files.append(handle)
                            upload_files[attach_name] = (os.path.basename(path), handle)
                        tg("sendMediaGroup", {
                            "chat_id": chat_id,
                            "media": json.dumps(media_payload),
                        }, files=upload_files, timeout=240)
                    finally:
                        for handle in open_files:
                            handle.close()

            for path in audio_items:
                with open(path, "rb") as media:
                    tg("sendAudio", {"chat_id": chat_id, "caption": title[:1024]},
                       files={"audio": (os.path.basename(path), media)}, timeout=240)

            for path, kind in separate_items:
                with open(path, "rb") as media:
                    tg("sendDocument", {"chat_id": chat_id, "caption": title[:1024]},
                       files={"document": (os.path.basename(path), media)}, timeout=240)

            tg("deleteMessage", {"chat_id": chat_id, "message_id": status_id})
    except Exception as exc:
        logging.exception("Instagram media download failed for path %s", urlparse(url).path)
        message = str(exc)
        if DEBUG_ERRORS and OWNER_ID:
            try:
                send_text(OWNER_ID, f"DEBUG {urlparse(url).path}\n{message[:3000]}")
            except Exception:
                pass
        lowered = message.lower()
        if any(term in lowered for term in ("checkpoint_required", "feedback_required", "rate limit", "too many requests", "http 429", "403 forbidden")):
            message = "Instagram is temporarily restricting automated access. Please try again later, or open the post in Instagram. The bot only supports publicly accessible media."
        elif "private" in lowered or "login" in lowered:
            message = "This post is not publicly accessible. The bot can only process public Instagram media."
        else:
            message = "I couldn't retrieve public media from this Instagram link. The post may be unavailable, unsupported, or blocked by Instagram."
        try:
            edit_text(chat_id, status_id, f"❌ {message}")
        except Exception:
            send_text(chat_id, f"❌ {message}")


def process_update(update):
    global BOT_ENABLED

    callback = update.get("callback_query")
    if callback:
        data = callback.get("data", "")
        user_id = callback.get("from", {}).get("id")
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        message_id = callback.get("message", {}).get("message_id")

        if data in ("admin_on", "admin_off"):
            if user_id != OWNER_ID:
                tg("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Owner only."})
                return
            BOT_ENABLED = data == "admin_on"
            state = "ON" if BOT_ENABLED else "OFF"
            edit_text(chat_id, message_id, f"⚙️ Bot Control\n\nStatus: {state}", admin_markup())
            tg("answerCallbackQuery", {"callback_query_id": callback["id"], "text": f"Bot turned {state}."})
            return

        if data == "download_help":
            tg("answerCallbackQuery", {"callback_query_id": callback["id"]})
            send_text(chat_id, "Send a public Instagram post, Reel, or TV link here. Only download media you own or have permission to use.")
            return

    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")
    text = message.get("text", "").strip()
    if not chat_id or not text:
        return

    if text.split()[0].split("@")[0] == "/admin":
        if user_id != OWNER_ID:
            send_text(chat_id, "⛔ This command is restricted to the bot owner.")
            return
        send_text(chat_id, f"⚙️ Bot Control\n\nStatus: {'ON' if BOT_ENABLED else 'OFF'}", admin_markup())
        return

    if text.split()[0].split("@")[0] == "/start":
        if not BOT_ENABLED and user_id != OWNER_ID:
            return
        first_name = message.get("from", {}).get("first_name", "there")
        first_name = (first_name.replace("&", "&amp;").replace("<", "&lt;")
                      .replace(">", "&gt;"))
        small_caps = str.maketrans({
            "a":"ᴀ","b":"ʙ","c":"ᴄ","d":"ᴅ","e":"ᴇ","f":"ꜰ","g":"ɢ",
            "h":"ʜ","i":"ɪ","j":"ᴊ","k":"ᴋ","l":"ʟ","m":"ᴍ","n":"ɴ",
            "o":"ᴏ","p":"ᴘ","q":"ǫ","r":"ʀ","s":"ꜱ","t":"ᴛ","u":"ᴜ",
            "v":"ᴠ","w":"ᴡ","x":"x","y":"ʏ","z":"ᴢ",
        })
        first_name = first_name.translate(small_caps)
        send_text(
            chat_id,
            "⎯‌꯭‌ <b>ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ ʀᴇᴇʟ ᴅᴏᴡɴʟᴏᴀᴅᴇʀ</b> ⎯‌꯭‌\n\n"
            f"👋 <b>ʜᴇʟʟᴏ, {first_name}!</b>\n\n"
            "<blockquote>📥 <b>ᴛʜɪꜱ ʙᴏᴛ ʜᴇʟᴘꜱ ʏᴏᴜ ᴅᴏᴡɴʟᴏᴀᴅ ɪɴꜱᴛᴀɢʀᴀᴍ ʀᴇᴇʟꜱ.</b></blockquote>\n"
            "<blockquote><b>ᴛᴏ ᴅᴏᴡɴʟᴏᴀᴅ ᴀ ʀᴇᴇʟ:</b>\n"
            "1. ᴄᴏᴘʏ ᴛʜᴇ ɪɴꜱᴛᴀɢʀᴀᴍ ʀᴇᴇʟ ʟɪɴᴋ.\n"
            "2. ꜱᴇɴᴅ ᴛʜᴇ ʟɪɴᴋ ᴛᴏ ᴛʜɪꜱ ʙᴏᴛ.\n"
            "3. ɢᴇᴛ ᴛʜᴇ ᴀᴠᴀɪʟᴀʙʟᴇ ᴠɪᴅᴇᴏ ᴅɪʀᴇᴄᴛʟʏ ɪɴ ᴛᴇʟᴇɢʀᴀᴍ.</blockquote>\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "<blockquote>💬 <b>ɴᴇᴇᴅ ʜᴇʟᴘ?</b> ᴜꜱᴇ ᴛʜᴇ ꜱᴜᴘᴘᴏʀᴛ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ.</blockquote>",
            start_markup(),
            parse_mode="HTML",
        )
        return

    if not BOT_ENABLED:
        return

    match = IG_RE.search(text)
    if match and is_supported_instagram_url(match.group(0).rstrip(".,)")):
        download_reel(chat_id, match.group(0).rstrip(".,)"))


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if WEBHOOK_SECRET and self.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            self.send_response(403)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            update = json.loads(self.rfile.read(length) or b"{}")
            process_update(update)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        except Exception:
            logging.exception("Webhook update processing failed")
            # Acknowledge the update to avoid Telegram retry storms; the exception
            # is now visible in Vercel runtime logs for diagnosis.
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Instagram Reel Downloader webhook is running.")
