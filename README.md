# Instagram Reel Downloader Bot

A Telegram bot that accepts **public Instagram Reel URLs** in private chats and groups and attempts to send back the available single-file video. Use it only for videos you own or have permission to download.

## Features

- `/start` welcome screen with Developer and Support links
- Reel links accepted in private chats and groups
- Owner-only `/admin` ON/OFF controls
- No credits, referrals, gift codes, or forced channel subscription
- Telegram webhook deployment for Vercel (no long-running polling loop)

## Deploy on Vercel

1. Import this GitHub repository into Vercel.
2. Select the repository root as the project root.
3. Add these Environment Variables in **Vercel → Project → Settings → Environment Variables**:

| Variable | Value |
| --- | --- |
| `BOT_TOKEN` | Token from your Telegram bot setup |
| `OWNER_ID` | `7508536182` |
| `WEBHOOK_SECRET` | A long random secret used to verify Telegram webhook requests |
| `SETUP_SECRET` | A different long random secret used only for the setup URL |
| `DEVELOPER_USERNAME` | `@II_YUTA_II` |
| `SUPPORT_GROUP_URL` | `https://t.me/+xbOwGbcZA-AwZDkx` |

4. Deploy the project.
5. Open this URL once, replacing the placeholders:

   `https://YOUR-PROJECT.vercel.app/api/setup?key=YOUR_SETUP_SECRET`

   A successful response from Telegram confirms that the webhook was registered.
6. Open your bot and send `/start`.

Do not put bot tokens or secrets in GitHub source files or screenshots.

## Group setup

Add the bot to your group. If it should detect ordinary Reel links sent by members, adjust the bot's group privacy setting in Telegram's bot management settings. Otherwise Telegram may only deliver commands and replies to the bot.

## Important limitations

- The downloader does not use Instagram login cookies and cannot access private or login-restricted Reels.
- Download only content you own or have permission to use. Respect the creator's rights and Instagram's terms.
- Telegram's Bot API has upload-size limits; large files may not be sent.
- Instagram can change its public page format at any time, which may temporarily break extraction.
- Vercel Hobby functions have a maximum execution duration. A slow or large download may time out before it completes.
- The `/admin` ON/OFF flag currently lives in function memory. Because Vercel functions are stateless and can be recycled, this toggle may reset after a cold start or may not be shared consistently between instances. A persistent database or key-value store is needed for reliable global ON/OFF state.

## Project files

- `api/webhook.py` — Telegram webhook and bot handlers
- `api/setup.py` — one-time webhook registration endpoint
- `requirements.txt` — Python dependencies
- `vercel.json` — function duration settings
- `.env.example` — environment variable names (no real secrets)
