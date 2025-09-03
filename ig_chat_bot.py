import os
import json
import re
import logging
import threading
from datetime import datetime, timedelta

import requests
import boto3
from botocore.exceptions import ClientError
from flask import Flask, request
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler
from dateparser import parse as parse_date
from dotenv import load_dotenv

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
import asyncio

# ---- Config ----
app = Flask(__name__)
load_dotenv()

logging.basicConfig(level=logging.INFO)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
INSTAGRAM_TOKEN = os.getenv("INSTAGRAM_TOKEN")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_CHAT_ID = os.getenv("OWNER_TELEGRAM_CHAT_ID")

# Initialize Telegram bot
telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)

# OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

# DynamoDB
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table("reservations")

SYSTEM_PROMPT = """
Jesteś botem dla studia ceramiki. Obsługuj:
- FAQ: ceny (100zł/godz), godziny (pn-sb 12-20), dojazd (Komuny Paryskiej 55, 50-452 Wrocław).
- Rezerwacje: użytkownik może zapytać o warsztaty. Ty przyjmij szczegóły (liczba osób, data) i zapisz jako „pending”.
- Edycje i anulowanie: jeśli użytkownik prosi o zmianę lub anulowanie, ustaw status jako „pending_edit” lub „pending_cancel”.
- Nigdy nie potwierdzaj rezerwacji – to może zrobić tylko właściciel.
"""

def get_google_credentials():
    """Ładuje credentials z ENV GOOGLE_CREDENTIALS (JSON jako string)."""
    creds_json = os.getenv("GOOGLE_CREDENTIALS")
    if not creds_json:
        raise RuntimeError("Brak GOOGLE_CREDENTIALS w ENV")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return creds

# Google Calendar
def add_to_google_calendar(details, date, user_id):
    try:
        creds = get_google_credentials()
        service = build("calendar", "v3", credentials=creds)
        
        event = {
            "summary": details,
            "description": f"Rezerwacja od użytkownika {user_id}",
            "start": {"dateTime": date.isoformat(), "timeZone": "Europe/Warsaw"},
            "end": {"dateTime": (date + timedelta(hours=2)).isoformat(), "timeZone": "Europe/Warsaw"},
        }

        event_result = service.events().insert(
            calendarId="primary", body=event
        ).execute()

        logging.info(f"✅ Rezerwacja dodana do Google Calendar: {event_result.get('htmlLink')}")
    except Exception as e:
        logging.error(f"❌ Błąd dodawania do Google Calendar: {e}")

def generate_response(user_message):
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    )
    return response.choices[0].message.content

def send_message(recipient_id, text):
    url = f"https://graph.facebook.com/v20.0/me/messages?access_token={INSTAGRAM_TOKEN}"
    data = {
        "recipient": {"id": recipient_id},
        "message": {"text": text},
    }
    try:
        response = requests.post(url, json=data)
        if response.status_code != 200:
            logging.error(f"Instagram API error: {response.status_code} - {response.text}")
    except Exception as e:
        logging.error(f"Error sending message: {e}")

def parse_reservation_request(text):
    match_people = re.search(r"(\d+)\s*(osób|osoby|osoba)?", text.lower())
    people = int(match_people.group(1)) if match_people else 1
    date = parse_date(text, languages=["pl"])
    if not date:
        date = datetime.now() + timedelta(days=7)
    return {
        "people": people,
        "date": date,
        "details": f"Warsztat dla {people} osób",
    }

async def send_telegram_reservation_notification(reservation_id, user_id, reservation_details, date):
    """Send reservation notification to owner via Telegram with inline buttons"""
    message = f"""
  🏺 **Nowa Rezerwacja - Studio Ceramiki**

  👤 **Klient:** {user_id}
  📋 **Szczegóły:** {reservation_details}
  📅 **Data:** {date.strftime('%d.%m.%Y %H:%M')}

  Wybierz akcję:
      """
    keyboard = [
          [
              InlineKeyboardButton("✅ Potwierdź", callback_data=f"confirm_{reservation_id}_{user_id}"),
              InlineKeyboardButton("❌ Odrzuć", callback_data=f"reject_{reservation_id}_{user_id}")
          ],
          [
              InlineKeyboardButton("📝 Szczegóły", callback_data=f"details_{reservation_id}_{user_id}"),
              InlineKeyboardButton("🗑 Anuluj ", callback_data=f"cancel_{reservation_id}_{user_id}")
          ]
      ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
          await telegram_bot.send_message(
              chat_id=OWNER_CHAT_ID,
              text=message,
              reply_markup=reply_markup,
              parse_mode='Markdown'
          )
          logging.info(f"✅ Telegram notification sent for reservation {reservation_id}")
    except Exception as e:
          logging.error(f"❌ Error sending Telegram notification: {e}")

async def handle_telegram_callback(update, context):
      """Handle button clicks from Telegram"""
      query = update.callback_query
      await query.answer()

      # Parse callback data: action_reservationId_userId
      action, reservation_id, user_id = query.data.split('_', 2)

      if action == "confirm":
          update_reservation_status(reservation_id, user_id, "confirmed")

          # Add to Google Calendar
          item = table.get_item(Key={"user_id": user_id, "reservation_id": reservation_id}).get("Item")
          if item:
              add_to_google_calendar(item["details"], datetime.fromisoformat(item["date"]), user_id)

          send_message(user_id, "✅ Twoja rezerwacja została potwierdzona!")
          await query.edit_message_text("✅ Rezerwacja potwierdzona!")

      elif action == "reject":
          update_reservation_status(reservation_id, user_id, "rejected")
          send_message(user_id, "❌ Twoja rezerwacja została odrzucona.")
          await query.edit_message_text("❌ Rezerwacja odrzucona!")

      elif action == "cancel":
          update_reservation_status(reservation_id, user_id, "cancelled")
          send_message(user_id, "🗑 Twoja rezerwacja została anulowana.")
          await query.edit_message_text("🗑 Rezerwacja anulowana!")

      elif action == "details":
          # Show detailed reservation info
          item = table.get_item(Key={"user_id": user_id, "reservation_id": reservation_id}).get("Item")
          if item:
              details_text = f"""
  📋 **Szczegóły Rezerwacji**
  🆔 **ID:** {reservation_id}
  👤 **Klient:** {user_id}
  📝 **Opis:** {item['details']}
  📅 **Data:** {item['date']}
  📊 **Status:** {item['status']}
              """
              await query.edit_message_text(details_text, parse_mode='Markdown')

def send_telegram_notification_sync(reservation_id, user_id, details, date):
    """Synchronous wrapper for Telegram notification"""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            send_telegram_reservation_notification(reservation_id, user_id, details, date)
        )
        loop.close()
    except Exception as e:
        logging.error(f"Telegram notification error: {e}")

# ---- Reservations ----
def save_reservation(user_id, user_message, status="pending"):
    try:
        reservation = parse_reservation_request(user_message)
        reservation_id = str(datetime.now().timestamp())

        # Save to DynamoDB
        table.put_item(
            Item={
                "user_id": user_id,
                "reservation_id": reservation_id,
                "details": reservation["details"],
                "date": reservation["date"].isoformat(),
                "status": status,
                "reminded": False,
            }
        )
    except ClientError as e:
        logging.error(e)
        # Send Telegram notification instead of email
        threading.Thread(
            target=send_telegram_notification_sync,
            args=(reservation_id, user_id, reservation["details"], reservation["date"])
        ).start()
    except ClientError as e:
        logging.error(e)

        return reservation
    except ClientError as e:
        logging.error(e)
        return None

def update_reservation_status(reservation_id, user_id, new_status):
    try:
        table.update_item(
            Key={"user_id": user_id, "reservation_id": reservation_id},
            UpdateExpression="SET #s = :status",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":status": new_status},
        )
    except ClientError as e:
        logging.error(e)

async def setup_telegram_bot():
    """Setup Telegram bot handlers"""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Add callback handler for button clicks
    application.add_handler(CallbackQueryHandler(handle_telegram_callback))

    # Start the bot
    await application.initialize()
    await application.start()

    return application

# ---- Scheduler (przypomnienia) ----
def send_reminders():
    now = datetime.now()
    tomorrow = now + timedelta(days=1)
    items = table.scan(
        FilterExpression="date BETWEEN :start AND :end AND reminded = :false AND #s = :confirmed",
        ExpressionAttributeValues={
            ":start": now.isoformat(),
            ":end": tomorrow.isoformat(),
            ":false": False,
            ":confirmed": "confirmed",
        },
        ExpressionAttributeNames={"#s": "status"},
    )["Items"]

    for item in items:
        if datetime.fromisoformat(item["date"]) - now < timedelta(hours=24):
            send_message(
                item["user_id"],
                f"📅 Przypomnienie: Twoja wizyta jutro o {item['date']}. Szczegóły: {item['details']}",
            )
            table.update_item(
                Key={"user_id": item["user_id"], "reservation_id": item["reservation_id"]},
                UpdateExpression="SET reminded = :true",
                ExpressionAttributeValues={":true": True},
            )

scheduler = BackgroundScheduler()
scheduler.add_job(send_reminders, "interval", hours=1)
scheduler.start()

# ---- Webhook ----
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge")
        return "Invalid token", 403

    if request.method == "POST":
        try:
            data = request.get_json()
            if data and data.get("object") == "instagram":
                for entry in data.get("entry", []):
                    for messaging_event in entry.get("messaging", []):
                        sender_id = messaging_event["sender"]["id"]
                        if "message" in messaging_event:
                            user_message = messaging_event["message"]["text"]

                            # AI odpowiedź
                            response_text = generate_response(user_message)
                            send_message(sender_id, response_text)

                            # logika rezerwacji
                            if "rezerwacja" in user_message.lower():
                                reservation = save_reservation(sender_id, user_message, status="pending")
                                if reservation:
                                    send_message(
                                        sender_id,
                                        f"📝 Twoja rezerwacja jest wstępnie zapisana ({reservation['details']} w dniu {reservation['date'].strftime('%d.%m.%Y %H:%M')}). Właściciel studia musi ją jeszcze potwierdzić ✅.",
                                    )
        except Exception as e:
            logging.error(f"Webhook error: {e}")
            return "ERROR", 500
        return "OK", 200

if __name__ == "__main__":
    # Setup Telegram bot
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    telegram_app = loop.run_until_complete(setup_telegram_bot())

    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
