import importlib.util
import json
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
SETUP_SECRET = os.environ.get("SETUP_SECRET", "")


def handle_setup(request):
    query = parse_qs(urlparse(request.path).query)
    supplied = query.get("key", [""])[0]

    if not SETUP_SECRET or supplied != SETUP_SECRET:
        request.send_response(403)
        request.end_headers()
        request.wfile.write(b"Forbidden")
        return

    if query.get("check", [""])[0] == "1":
        try:
            info_response = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo",
                timeout=15,
            )
            info_response.raise_for_status()
            info = info_response.json()
            if not info.get("ok"):
                raise RuntimeError(info.get("description", "Telegram API error"))
            details = info.get("result", {})
            safe_result = {
                "ok": True,
                "webhook_url": details.get("url", ""),
                "pending_update_count": details.get("pending_update_count", 0),
                "last_error_date": details.get("last_error_date"),
                "last_error_message": details.get("last_error_message"),
                "max_connections": details.get("max_connections"),
                "allowed_updates": details.get("allowed_updates", []),
            }
            body = json.dumps(safe_result).encode("utf-8")
            request.send_response(200)
            request.send_header("Content-Type", "application/json")
            request.end_headers()
            request.wfile.write(body)
            return
        except Exception as exc:
            body = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
            request.send_response(502)
            request.send_header("Content-Type", "application/json")
            request.end_headers()
            request.wfile.write(body)
            return

    if not BOT_TOKEN:
        request.send_response(500)
        request.end_headers()
        request.wfile.write(b"BOT_TOKEN is not configured")
        return

    webhook_url = os.environ.get("VERCEL_URL", "")
    if webhook_url and not webhook_url.startswith("http"):
        webhook_url = "https://" + webhook_url
    if not webhook_url:
        request.send_response(500)
        request.end_headers()
        request.wfile.write(b"VERCEL_URL is unavailable")
        return

    payload = {
        "url": webhook_url.rstrip("/") + "/api/webhook",
        "allowed_updates": json.dumps(["message", "edited_message", "callback_query"]),
    }
    if WEBHOOK_SECRET:
        payload["secret_token"] = WEBHOOK_SECRET

    result = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
        data=payload,
        timeout=20,
    )
    request.send_response(result.status_code)
    request.send_header("Content-Type", "application/json")
    request.end_headers()
    request.wfile.write(result.content)


def handle_webhook(request):
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        request.send_response(403)
        request.end_headers()
        return

    try:
        length = int(request.headers.get("Content-Length", "0"))
        update = json.loads(request.rfile.read(length) or b"{}")

        module_path = os.path.join(os.path.dirname(__file__), "webhook.py")
        spec = importlib.util.spec_from_file_location("instagram_reel_webhook", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.process_update(update)

        request.send_response(200)
        request.end_headers()
        request.wfile.write(b"OK")
    except Exception as exc:
        print(f"Webhook processing error: {exc}")
        request.send_response(200)
        request.end_headers()
        request.wfile.write(b"OK")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/setup":
            handle_setup(self)
        elif path == "/api/webhook":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Instagram Reel Downloader webhook is running.")
        elif path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Instagram Reel Downloader is deployed.")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if urlparse(self.path).path != "/api/webhook":
            self.send_response(404)
            self.end_headers()
            return
        handle_webhook(self)
