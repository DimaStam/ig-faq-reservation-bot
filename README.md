# Studio Ceramiki – Instagram Chat Bot

A production‑ready Flask app that handles Instagram DM conversations for Studio Ceramiki (Wrocław): answers FAQs, guides users through booking a workshop, stores reservations in DynamoDB, notifies the owner on Telegram, and (on confirmation) creates a Google Calendar event. The bot also sends users a reminder 24 hours before the visit.

## Overview
- **Channel:** Instagram DMs via Meta Graph API webhook (`/webhook`).
- **Assistant:** Answers multilingual FAQs (PL/EN/UK) and drives a booking flow.
- **Extraction:** Uses OpenAI to parse intent, date, time, people, and duration; has regex fallbacks.
- **State:** Persists conversation state and reservations in AWS DynamoDB table `reservations`.
- **Owner Alerts:** Sends a Telegram message with inline buttons to confirm or reject.
- **Calendar:** On confirm, adds an event to Google Calendar.
- **Reminders:** Sends DM reminders ~24h before the event (APScheduler job).
- **Admin:** Endpoint to clear per‑user in‑progress state.

## Key Features
- **FAQ + Booking:** Detects whether a message is FAQ or a reservation and responds appropriately.
- **Smart duration:** Accepts inputs like `2`, `2h`, `2 godziny`.
- **Opening hours aware:** Suggests only viable time windows based on configured opening hours.
- **Inline confirm:** Quick replies on IG; inline buttons on Telegram for the owner.
- **Resilience:** Fallbacks for parsing dates/times; background retry for Telegram send.

## Architecture
- **Web Server:** Flask app exposing `POST /webhook` for Meta subscriptions and `GET /webhook` for verification.
  - See `ig_chat_bot.py:1164` for the route definition.
- **Telegram:** Background thread runs `python-telegram-bot` polling (no Telegram webhook required).
  - Entrypoint starts the polling thread; see `ig_chat_bot.py:1331`.
- **Scheduler:** APScheduler background job invokes reminders hourly; see `ig_chat_bot.py:1156–1158`.
- **Storage:** DynamoDB table `reservations` with composite key: `user_id` (PK, String) + `reservation_id` (SK, String).
- **Calendar:** Google Calendar API with a Service Account (credentials passed via base64 env var).
- **AI:** OpenAI chat completions (`gpt-4o-mini`) for FAQ and field extraction.

## Environment Variables
Set these (e.g., in `.env`):
- `OPENAI_API_KEY`: OpenAI API key.
- `INSTAGRAM_TOKEN`: Page/IG access token to call `me/messages`.
- `VERIFY_TOKEN`: Arbitrary string for webhook verification (Meta handshake).
- `TELEGRAM_BOT_TOKEN`: Bot token from BotFather.
- `OWNER_TELEGRAM_CHAT_ID`: Telegram chat ID of the owner to receive alerts.
- `GOOGLE_CALENDAR_ID`: Calendar ID (email) to insert events into (default: `primary`).
- `GOOGLE_CREDENTIALS_BASE64`: Base64 of a Google service account JSON.
- `AWS_REGION`: AWS region for DynamoDB (default: `us-east-1`).
- `ADMIN_TOKEN`: Token required for the admin endpoint.
- `PORT`: Port for Flask (default: `5000`).
- Standard AWS creds (for DynamoDB): `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (and optionally `AWS_SESSION_TOKEN`).

## Local Development
- Python 3.9+ recommended.
- Install deps:
  - `python -m venv venv && source venv/bin/activate` (Linux/macOS)
  - `python -m venv venv && venv\Scripts\activate` (Windows)
  - `pip install -r requirements.txt`
- Ensure DynamoDB table exists:
  - Name: `reservations`
  - Keys: `user_id` (String, partition), `reservation_id` (String, sort)
- Run:
  - `python ig_chat_bot.py`
  - Exposes `http://localhost:5000/webhook`
- Tunnel for Meta (example): `ngrok http 5000`

## Webhook Setup (Meta / Instagram)
- App must subscribe to messages and be connected to your Instagram account.
- Verification:
  - Meta sends `GET /webhook?hub.verify_token=...&hub.challenge=...`.
  - The app responds with the challenge if `VERIFY_TOKEN` matches.
- Messages:
  - Meta sends events to `POST /webhook`; the bot processes DMs and replies via `https://graph.facebook.com/v23.0/me/messages`.

## Telegram Setup
- Create a bot via BotFather, set `TELEGRAM_BOT_TOKEN`.
- Get the owner’s chat ID (e.g., with a temporary script or @userinfobot) and set `OWNER_TELEGRAM_CHAT_ID`.
- The app runs polling automatically in a background thread; no Telegram webhook required.

## Google Calendar Setup
- Create a Service Account in Google Cloud and download the JSON.
- Share the target calendar with the service account’s email (write access).
- Base64‑encode the JSON and set `GOOGLE_CREDENTIALS_BASE64`.
  - Linux/macOS: `base64 -w0 service-account.json`
  - Windows (PowerShell): `[Convert]::ToBase64String([IO.File]::ReadAllBytes('service-account.json'))`

## Admin Endpoint
- Clear a user’s in‑progress state:
  - `POST/GET /admin/clear_cache?user_id=<IG_USER_ID>&token=<ADMIN_TOKEN>`
  - See example in `readme.txt`.

## Deployment
- Procfile is provided for Heroku‑style platforms:
  - `web: python ig_chat_bot.py`
- Ensure all env vars are set, and that the instance can reach AWS, Meta, Telegram, and Google APIs.

## Typical Flow
1. User writes on Instagram, e.g., “Chcę zarezerwować wtorek 17:00 dla 2 osób na 2h”.
2. Bot extracts fields (people/date/time/duration), asks for any missing info, and validates availability.
3. If a slot is free, bot asks the user to confirm (quick reply buttons).
4. Owner receives a Telegram message with “Potwierdź/Odrzuć”.
5. On confirm, the app creates a Calendar event and notifies the user; on reject, it notifies the user.
6. A reminder DM is sent roughly 24h before the reservation.

## Files
- `ig_chat_bot.py`: Main app (Flask server, Telegram polling, reservation logic, calendar, scheduler).
- `requirements.txt`: Python dependencies.
- `Procfile`: Process declaration for deployment.
- `.env`: Local environment variables (not committed).
- `readme.txt`: Quick local notes (ngrok/admin example).

---
Need help wiring a specific platform (Heroku, Fly.io, Docker) or setting up Meta/Telegram/Google credentials? Ask and I’ll add tailored steps.
