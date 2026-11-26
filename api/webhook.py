import glob
import html
import json
import logging
import os
import re
import tempfile
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

import requests
import yt_dlp

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "7508536182"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
DEVELOPER = os.environ.get("DEVELOPER_USERNAME", "@II_YUTA_II")
SUPPORT_URL = os.environ.get("SUPPORT_GROUP_URL", "https://t.me/+xbOwGbcZA-AwZDkx")

# Note: Vercel instances are stateless. This in-memory switch is suitable for
# initial testing only and may reset when the function instance is recycled.
BOT_ENABLED = True

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
IG_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels|share/reel)/[^\s]+", re.I)


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
    } and bool(re.match(r"^/(p|reel|reels|share/reel)/", parsed.path, re.I))


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


def download_public_page_media(url, temp_dir):
    """Last-resort fallback for media exposed in a public page's Open Graph tags."""
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    response = requests.get(url, headers=headers, timeout=25, allow_redirects=True)
    response.raise_for_status()
    page = response.text[:5_000_000]
    found = {}
    for key in ("og:image", "og:video", "og:video:secure_url"):
        pattern_a = r"<meta[^>]+(?:property|name)=['\\\"]" + re.escape(key) + r"['\\\"][^>]+content=['\\\"]([^'\\\"]+)"
        pattern_b = r"<meta[^>]+content=['\\\"]([^'\\\"]+)['\\\"][^>]+(?:property|name)=['\\\"]" + re.escape(key) + r"['\\\"]"
        match = re.search(pattern_a, page, re.I) or re.search(pattern_b, page, re.I)
        if match:
            found[key] = html.unescape(match.group(1)).replace("&amp;", "&")
    media_url = found.get("og:video:secure_url") or found.get("og:video") or found.get("og:image")
    if not media_url or not media_url.startswith("https://"):
        return []
    media = requests.get(media_url, headers=headers, timeout=35, stream=True)
    media.raise_for_status()
    content_type = media.headers.get("content-type", "").lower()
    ext = ".mp4" if "video" in content_type else ".jpg"
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


def download_reel(chat_id, url, reply_to=None):
    status = send_text(chat_id, "⏳ Processing public Instagram media...")
    status_id = status["message_id"]

    try:
        with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
            candidates = []
            title = "Instagram media"
            parsed_path = urlparse(url).path

            # yt-dlp handles Reels/videos. Instagram photo posts often return
            # "There is no video in this post", so use Instaloader for /p/ posts.
            try:
                options = {
                    "outtmpl": os.path.join(temp_dir, "%(playlist_index)s_%(id)s.%(ext)s"),
                    "format": "best/bestvideo+bestaudio",
                    "noplaylist": False,
                    "ignoreerrors": True,
                    "quiet": True,
                    "no_warnings": True,
                    "socket_timeout": 25,
                    "retries": 2,
                    "max_filesize": 48 * 1024 * 1024,
                }
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if info:
                        title = (info.get("title") or title)[:900]
                candidates = [
                    path for path in glob.glob(os.path.join(temp_dir, "*"))
                    if os.path.isfile(path) and os.path.getsize(path) > 0
                    and not path.endswith((".json", ".txt"))
                ]
            except Exception:
                logging.exception("yt-dlp extraction failed for %s", parsed_path)

            if not candidates and re.match(r"^/p/", parsed_path, re.I):
                try:
                    import instaloader
                    loader = instaloader.Instaloader(
                        dirname_pattern=temp_dir,
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
                    shortcode = parsed_path.strip("/").split("/")[1]
                    post = instaloader.Post.from_shortcode(loader.context, shortcode)
                    title = (post.caption or "Instagram photo post")[:900]
                    loader.download_post(post, target="media")
                    candidates = [
                        path for path in glob.glob(os.path.join(temp_dir, "**", "*"), recursive=True)
                        if os.path.isfile(path) and os.path.getsize(path) > 0
                        and os.path.splitext(path)[1].lower() in (
                            ".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v"
                        )
                    ]
                except Exception:
                    logging.exception("Instaloader photo fallback failed for %s", parsed_path)

            if not candidates:
                try:
                    candidates = download_public_page_media(url, temp_dir)
                    if candidates:
                        logging.info("Downloaded public Open Graph media fallback for %s", parsed_path)
                except Exception:
                    logging.exception("Public page metadata fallback failed for %s", parsed_path)

            if not candidates:
                raise RuntimeError("No public downloadable media was returned by Instagram.")

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
                        import subprocess
                        import imageio_ffmpeg
                        audio_path = os.path.splitext(path)[0] + "_audio.mp3"
                        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
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
        if "private" in message.lower() or "login" in message.lower():
            message = "This Instagram post is not publicly accessible. I can only process public posts you own or have permission to download."
        else:
            message = "I couldn't download this Instagram media. It may be unavailable, unsupported, private, or too large."
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
            send_text(chat_id, "Send a public Instagram Reel link here. Only download videos you own or have permission to use.")
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
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Instagram Reel Downloader webhook is running.")
