import json
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
SETUP_SECRET = os.environ.get("SETUP_SECRET", "")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        supplied = query.get("key", [""])[0]
        if not SETUP_SECRET or supplied != SETUP_SECRET:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden")
            return

        if not BOT_TOKEN:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"BOT_TOKEN is not configured")
            return

        webhook_url = os.environ.get("VERCEL_URL", "")
        if webhook_url and not webhook_url.startswith("http"):
            webhook_url = "https://" + webhook_url
        if not webhook_url:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"VERCEL_URL is unavailable")
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
        self.send_response(result.status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(result.content)
