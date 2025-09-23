import os
import base64
import json
import re
import logging
import threading
import time
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple, List
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

import requests
import boto3
from botocore.exceptions import ClientError
from flask import Flask, request, jsonify
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler
from dateparser import parse as parse_date
from dotenv import load_dotenv

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler
import asyncio
import socket

# -----------------------------
# App / Config
# -----------------------------
app = Flask(__name__)
load_dotenv()
logging.basicConfig(level=logging.INFO)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
INSTAGRAM_TOKEN = os.getenv("INSTAGRAM_TOKEN")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_CHAT_ID = (os.getenv("OWNER_TELEGRAM_CHAT_ID") or "").strip() or None
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

OPENING_HOURS = {
    0: (10, 18),
    1: (10, 18),
    2: (10, 18),
    3: (10, 18),
    4: (10, 18),
    5: (10, 18),
    6: None,
}

# -----------------------------
# Clients / Globals
# -----------------------------
client = OpenAI(api_key=OPENAI_API_KEY)

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table("reservations")

telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)
TELEGRAM_APP: Optional[Application] = None
TELEGRAM_LOOP: Optional[asyncio.AbstractEventLoop] = None
TELEGRAM_THREAD: Optional[threading.Thread] = None

# -----------------------------
# Static prompts
# -----------------------------
SYSTEM_PROMPT_FAQ = """
[ROLA]
Jesteś asystentem Studio Ceramiki we Wrocławiu. Obsługujesz klientów na Instagramie, odpowiadasz na ich pytania i pomagasz w rezerwacjach warsztatów.

[CEL]
- Odpowiadaj na proste pytania FAQ (adres, godziny otwarcia, ceny).
- Gdy rozmowa dotyczy rezerwacji, bądź zwięzły i konkretny.

[FAQ]
- Adres: Komuny Paryskiej 55, Wrocław
- Godziny otwarcia: Pon-Pt 10:00-18:00, Sob 10:00-14:00, Nd nieczynne
- Ceny szkliwienie ceramiki: 120zł za 1 godzinę, 150zł za 2 godziny
- Ceny Koło:
  Dla dzieci (6-17 lat)
  • Karnet: 3 spotkania przy kole (ok. 2 godz. każde) + 1 zajęcia ze szkliwienia. Cena: 390zł.
  • Zajęcia: 100 zł
  Dla par
  • Karnet: 3 spotkania przy kole (ok. 2 godz. każde) + 1 zajęcia ze szkliwienia. Cena: 480 zł.
  • Zajęcia: 120 zł
  Zajęcia indywidualne
  • Karnet: 3 spotkania przy kole (ok. 2-2,5 godz. każde) + 1 zajęcia ze szkliwienia wszystkich prac. Cena: 560 zł.
  • Zajęcia: 150 zł

[ZASADY]
- Obsługujesz polski, angielski i ukraiński. Zawsze odpowiadaj w języku użytkownika.
- Bądź krótki i uprzejmy.
""".strip()

SYSTEM_PROMPT_EXTRACT = """
Jesteś parserem danych rezerwacji. Z wejściowego tekstu użytkownika wyodrębniasz PÓL-A: 
intent: one of [reservation, faq, other]
people: integer (1..50) or null
raw_date: dowolny zapis daty z wiadomości lub null
raw_time: dowolny zapis godziny z wiadomości lub null
vague: bool (czy data jest ogólnikowa: np. 'w przyszłym tygodniu', 'w weekend')
duration_hours: integer (1..8) albo null
language: pl/en/uk

Zwracaj JSON w CamelCase kluczach. Nie dodawaj komentarzy ani tekstu poza JSON.
Jeśli użytkownik podał sprzeczne lub wielokrotne wartości, wybierz najbardziej prawdopodobne i ustaw missingFields odpowiednio.
""".strip()

# -----------------------------
# Utilities
# -----------------------------

def _now_pl() -> datetime:
    try:
        if ZoneInfo is not None:
            return datetime.now(ZoneInfo("Europe/Warsaw"))
    except Exception:
        pass
    return datetime.now()


def _now_pl_naive() -> datetime:
    n = _now_pl()
    return n.replace(tzinfo=None)


def _is_past_date_iso(date_iso: str) -> bool:
    try:
        d = datetime.fromisoformat(date_iso).date()
        return d < _now_pl().date()
    except Exception:
        return False


def _is_past_datetime(date_iso: str, time_str: str) -> bool:
    try:
        dt = datetime.fromisoformat(f"{date_iso}T{time_str}")
        return dt < _now_pl_naive()
    except Exception:
        return False


def _hours_to_float(val, default=2.0) -> float:
    try:
        if val is None:
            return float(default)
        if isinstance(val, bool):
            return float(default)
        return float(val)
    except Exception:
        return float(default)


def normalize_text(s: str) -> str:
    t = (s or "").lower().strip()
    t = t.replace("osóby", "osoby").replace("osobuy", "osoby")
    t = t.replace(",", " ").replace("  ", " ")
    return t


# -----------------------------
# Instagram senders
# -----------------------------

def send_message(recipient_id: str, text: str):
    url = "https://graph.facebook.com/v23.0/me/messages"
    params = {"access_token": INSTAGRAM_TOKEN}
    data = {"recipient": {"id": recipient_id}, "message": {"text": text}}
    try:
        response = requests.post(url, params=params, json=data, timeout=10)
        if response.status_code != 200:
            logging.error(f"Instagram API error: {response.status_code} - {response.text}")
        else:
            logging.info(f"📤 Sent to {recipient_id}: {text}")
    except Exception as e:
        logging.error(f"Error sending message: {e}", exc_info=True)


def send_quick_replies(recipient_id: str, text: str, replies: List[Dict[str, Any]]):
    url = "https://graph.facebook.com/v23.0/me/messages"
    params = {"access_token": INSTAGRAM_TOKEN}
    data = {
        "recipient": {"id": recipient_id},
        "message": {"text": text, "quick_replies": replies or []},
    }
    try:
        response = requests.post(url, params=params, json=data, timeout=10)
        if response.status_code != 200:
            logging.error(f"Instagram API (quick replies) error: {response.status_code} - {response.text}")
        else:
            logging.info(f"📤 Sent quick replies to {recipient_id}")
    except Exception as e:
        logging.error(f"Error sending quick replies: {e}", exc_info=True)


# -----------------------------
# Profile helper
# -----------------------------

def get_user_display_name(user_id: str) -> Optional[str]:
    try:
        url = f"https://graph.facebook.com/v23.0/{user_id}"
        params = {"access_token": INSTAGRAM_TOKEN, "fields": "username"}
        resp = requests.get(url, params=params, timeout=5)
        if resp.status_code != 200:
            logging.warning(f"Profile fetch failed {resp.status_code}: {resp.text}")
            return None
        data = resp.json() if resp.text else {}
        un = (data.get("username") or "").strip()
        return un or None
    except Exception as e:
        logging.error(f"Error fetching display name: {e}")
        return None


# -----------------------------
# Calendar
# -----------------------------

def get_google_credentials():
    creds_b64 = os.getenv("GOOGLE_CREDENTIALS_BASE64")
    if not creds_b64:
        raise RuntimeError("Brak GOOGLE_CREDENTIALS_BASE64 w ENV")
    try:
        decoded = base64.b64decode(creds_b64)
        creds_dict = json.loads(decoded)
    except Exception as e:
        raise RuntimeError(f"Nieprawidłowe GOOGLE_CREDENTIALS_BASE64: {e}")
    creds = Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return creds


def list_events_for_day(day: datetime):
    try:
        creds = get_google_credentials()
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        if ZoneInfo is not None:
            tz = ZoneInfo("Europe/Warsaw")
            day_start_local = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
            day_end_local = day.replace(hour=23, minute=59, second=59, microsecond=0, tzinfo=tz)
            time_min = day_start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            time_max = day_end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        else:
            day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day.replace(hour=23, minute=59, second=59, microsecond=0)
            time_min = day_start.isoformat() + "Z"
            time_max = day_end.isoformat() + "Z"
        res = (
            service.events()
            .list(
                calendarId=GOOGLE_CALENDAR_ID or "primary",
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        return res.get("items", [])
    except Exception as e:
        logging.error(f"List events error: {e}")
        return []


def compute_free_ranges_for_day(day: datetime, duration_hours: int) -> List[Tuple[datetime, datetime]]:
    wh = OPENING_HOURS.get(day.weekday())
    if not wh:
        return []
    open_h, close_h = wh
    work_start = day.replace(hour=open_h, minute=0, second=0, microsecond=0)
    work_end = day.replace(hour=close_h, minute=0, second=0, microsecond=0)

    events = list_events_for_day(day)
    busy: List[Tuple[datetime, datetime]] = []
    for ev in events:
        try:
            if (ev.get("transparency") or "").lower() == "transparent":
                continue
            if (ev.get("status") or "").lower() == "cancelled":
                continue
        except Exception:
            pass
        s = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
        e = ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date")
        if not s or not e:
            continue
        try:
            if "T" in s:
                s_iso = s.replace("Z", "+00:00")
                dt_s = datetime.fromisoformat(s_iso)
                if dt_s.tzinfo is None and ZoneInfo is not None:
                    dt_s = dt_s.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
                dt_s_local = dt_s.astimezone(ZoneInfo("Europe/Warsaw")) if ZoneInfo is not None else dt_s
                st = day.replace(hour=dt_s_local.hour, minute=dt_s_local.minute, second=0, microsecond=0)
            else:
                st = work_start
            if "T" in e:
                e_iso = e.replace("Z", "+00:00")
                dt_e = datetime.fromisoformat(e_iso)
                if dt_e.tzinfo is None and ZoneInfo is not None:
                    dt_e = dt_e.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
                dt_e_local = dt_e.astimezone(ZoneInfo("Europe/Warsaw")) if ZoneInfo is not None else dt_e
                en = day.replace(hour=dt_e_local.hour, minute=dt_e_local.minute, second=0, microsecond=0)
            else:
                en = work_end
        except Exception:
            continue
        st = max(st, work_start)
        en = min(en, work_end)
        if en > st:
            busy.append((st, en))

    busy.sort(key=lambda x: x[0])
    merged: List[List[datetime]] = []
    for a, b in busy:
        if not merged or a > merged[-1][1]:
            merged.append([a, b])
        else:
            merged[-1][1] = max(merged[-1][1], b)

    free: List[Tuple[datetime, datetime]] = []
    cursor = work_start
    for a, b in [(x[0], x[1]) for x in merged]:
        if a > cursor:
            free.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < work_end:
        free.append((cursor, work_end))

    need = timedelta(hours=_hours_to_float(duration_hours, default=2.0))
    return [iv for iv in free if (iv[1] - iv[0]) >= need]


def format_free_ranges(ranges: List[Tuple[datetime, datetime]], max_items: int = 3) -> str:
    out = []
    for i, (a, b) in enumerate(ranges[:max_items], start=1):
        out.append(f"{i}) {a.strftime('%H:%M')}–{b.strftime('%H:%M')}")
    return ", ".join(out) if out else "brak"


def check_availability_in_calendar(start_dt: datetime, duration_hours: int = 2) -> Tuple[Optional[bool], Optional[str]]:
    try:
        req_end = start_dt + timedelta(hours=_hours_to_float(duration_hours, default=2.0))
        free_ranges = compute_free_ranges_for_day(start_dt, duration_hours)
        for a, b in free_ranges:
            if a <= start_dt and b >= req_end:
                return True, None
        return False, None
    except Exception as e:
        logging.error(f"Calendar availability error: {e}")
        return None, str(e)


def add_to_google_calendar(details: str, date: datetime, user_id: str, people: Optional[int] = None, duration_hours: int = 2):
    try:
        creds = get_google_credentials()
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        dh = _hours_to_float(duration_hours, default=2.0)
        event = {
            "summary": details,
            "description": f"Liczba osób: {people if people is not None else '?'}\nIG: {user_id}",
            "start": {"dateTime": date.isoformat(), "timeZone": "Europe/Warsaw"},
            "end": {"dateTime": (date + timedelta(hours=dh)).isoformat(), "timeZone": "Europe/Warsaw"},
        }
        event_result = service.events().insert(calendarId=GOOGLE_CALENDAR_ID or "primary", body=event).execute()
        logging.info(f"✅ Dodano do Google Calendar: {event_result.get('htmlLink')}")
    except Exception as e:
        logging.error(f"Błąd dodawania do Google Calendar: {e}")


# -----------------------------
# Small deterministic helpers (fallbacks for AI)
# -----------------------------
VAGUE_PHRASES = [
    "w przyszłym tygodniu",
    "w przyszlym tygodniu",
    "w tym tygodniu",
    "w nadchodzącym tygodniu",
    "w nadchodzacym tygodniu",
    "w weekend",
    "w ten weekend",
    "w następnym tygodniu",
    "w nastepnym tygodniu",
    "w przyszłym miesiącu",
    "w przyszlym miesiacu",
    "w tym miesiącu",
    "w tym miesiacu",
]

WEEKDAYS_PL = {
    "poniedziałek": 0,
    "pon": 0,
    "wtorek": 1,
    "wt": 1,
    "wto": 1,
    "środa": 2,
    "sroda": 2,
    "śr": 2,
    "sr": 2,
    "czwartek": 3,
    "czw": 3,
    "piątek": 4,
    "piatek": 4,
    "pt": 4,
    "sobota": 5,
    "sob": 5,
    "niedziela": 6,
    "nd": 6,
    "nie": 6,
}


def is_vague_date_phrase(t: str) -> bool:
    t = (t or "").lower()
    return any(p in t for p in VAGUE_PHRASES)


def extract_time_fallback(text: str) -> Optional[str]:
    m = re.search(r"\b([01]?\d|2[0-3])[:\. ]?([0-5]\d)?\b", text or "")
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2)) if m.group(2) else 0
    return f"{hh:02d}:{mm:02d}:00"


def parse_people_count_fallback(text: str) -> Optional[int]:
    t = (text or "").lower()
    m = re.search(r"\b(?:dla|na)?\s*(\d{1,2})\s*(?:osob(?:a|y)?|osób|os)?\b", t, re.IGNORECASE)
    if not m:
        m = re.search(r"\b(\d{1,2})\b", t)
    try:
        return int(m.group(1)) if m else None
    except Exception:
        return None


def extract_concrete_date_fallback(text: str, reference: Optional[datetime] = None) -> Optional[str]:
    ref = reference or _now_pl_naive()
    t = (text or "").strip().lower()

    m_full = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b", t)
    if m_full:
        d, m, y = int(m_full.group(1)), int(m_full.group(2)), int(m_full.group(3))
        if y < 100:
            y += 2000
        try:
            dt = datetime(y, m, d)
            return dt.date().isoformat()
        except ValueError:
            pass

    m_short = re.search(r"\b(\d{1,2})\.(\d{1,2})\b", t)
    if m_short:
        d, m = int(m_short.group(1)), int(m_short.group(2))
        try:
            dt = datetime(ref.year, m, d)
            if dt.date() < ref.date():
                dt = datetime(ref.year + 1, m, d)
            return dt.date().isoformat()
        except ValueError:
            pass

    try:
        dt = parse_date(t, languages=["pl"], settings={"PREFER_DATES_FROM": "future", "DATE_ORDER": "DMY", "REQUIRE_PARTS": ["day", "month"]})
        return dt.date().isoformat() if dt else None
    except Exception:
        return None


def resolve_weekday_to_date(weekday_idx: int, reference: Optional[datetime] = None) -> str:
    ref = reference or _now_pl_naive()
    delta = (weekday_idx - ref.weekday()) % 7
    if delta == 0:
        delta = 7
    return (ref + timedelta(days=delta)).date().isoformat()


def suggest_day_options(base: Optional[datetime] = None, how_many: int = 3, prefer_next_week: bool = False) -> List[datetime]:
    now = _now_pl_naive()
    start = now + timedelta(days=1)
    if prefer_next_week:
        next_mon = now + timedelta(days=(7 - now.weekday()) % 7)
        if next_mon.date() <= now.date():
            next_mon = next_mon + timedelta(days=7)
        start = next_mon
    picks: List[datetime] = []
    d = start
    while len(picks) < how_many and (d - start).days < 14:
        if d.date() > now.date():
            picks.append(d)
        d += timedelta(days=1)
    return picks[:how_many]


# -----------------------------
# AI — structured extraction
# -----------------------------

def ai_extract_reservation_fields(message_text: str) -> Dict[str, Any]:
    """Use OpenAI function-calling style to extract reservation intent and fields.
    Falls back to deterministic regexes if AI is unavailable.
    Returns fields: intent, people, raw_date, raw_time, duration_hours, language, vague, missingFields
    """
    try:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "set_fields",
                    "description": "Ustal dane rezerwacji z tekstu.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "intent": {"type": "string", "enum": ["reservation", "faq", "other"]},
                            "people": {"type": ["integer", "null"]},
                            "raw_date": {"type": ["string", "null"]},
                            "raw_time": {"type": ["string", "null"]},
                            "duration_hours": {"type": ["integer", "null"]},
                            "vague": {"type": "boolean"},
                            "language": {"type": "string"},
                            "missingFields": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["intent", "language", "vague", "missingFields"],
                    },
                },
            }
        ]
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_EXTRACT},
                {"role": "user", "content": message_text},
            ],
            tools=tools,
            tool_choice="auto",
            timeout=12,
        )
        choice = resp.choices[0]
        if choice.message.tool_calls:
            args = choice.message.tool_calls[0].function.arguments
            data = json.loads(args or "{}")
            # Ensure defaults
            for k in ["people", "raw_date", "raw_time", "duration_hours"]:
                data.setdefault(k, None)
            data.setdefault("missingFields", [])
            return data
    except Exception as e:
        logging.warning(f"AI extract failed, fallback used: {e}")

    # Fallbacks
    intent = "reservation" if any(x in (message_text or "").lower() for x in ["rezerw", "book", "termin"]) else "other"
    people = parse_people_count_fallback(message_text)
    raw_time = extract_time_fallback(message_text)
    raw_date = extract_concrete_date_fallback(message_text)
    vague = is_vague_date_phrase(message_text)
    duration = None
    # simple duration
    m = re.search(r"\b(\d{1,2})\s*(?:h|godz|godzina|godziny|godzin|hour|hours)\b", (message_text or "").lower())
    if m:
        try:
            v = int(m.group(1))
            if 1 <= v <= 8:
                duration = v
        except Exception:
            pass
    if duration is None:
        m2 = re.match(r"^\s*(\d{1,2})\s*$", (message_text or "").strip())
        if m2:
            try:
                v = int(m2.group(1))
                if 1 <= v <= 8:
                    duration = v
            except Exception:
                pass
    missing = []
    if intent == "reservation":
        if not people:
            missing.append("people")
        if not raw_date and not vague:
            missing.append("date")
        if not raw_time:
            missing.append("time")
    return {
        "intent": intent,
        "people": people,
        "raw_date": raw_date,
        "raw_time": raw_time,
        "duration_hours": duration,
        "vague": vague,
        "language": "pl",
        "missingFields": missing,
    }


# -----------------------------
# Reservation state & persistence
# -----------------------------

def _load_current(user_id: str) -> Dict[str, Any]:
    try:
        item = table.get_item(Key={"user_id": user_id, "reservation_id": "current"}).get("Item")
    except Exception:
        item = None
    if item and item.get("updated_at"):
        try:
            # expire after 2h inactivity
            if datetime.fromisoformat(item["updated_at"]) < _now_pl_naive() - timedelta(hours=2):
                table.delete_item(Key={"user_id": user_id, "reservation_id": "current"})
                item = None
        except Exception:
            pass
    if not item:
        item = {
            "people": None,
            "date": None,
            "time": None,
            "duration": None,
            "awaiting_confirmation": False,
            "awaiting_day_choice": False,
            "suggested_options": [],
            "details": None,
            "status": "in_progress",
        }
        table.put_item(Item={"user_id": user_id, "reservation_id": "current", **item, "updated_at": _now_pl_naive().isoformat()})
    return item


def _save_current(user_id: str, state: Dict[str, Any]):
    state = dict(state)
    state["updated_at"] = _now_pl_naive().isoformat()
    table.put_item(Item={"user_id": user_id, "reservation_id": "current", **state})


def save_reservation(user_id: str, reservation_data: Dict[str, Any], status: str = "pending") -> Optional[Dict[str, Any]]:
    try:
        reservation_id = uuid4().hex
        full_date = datetime.fromisoformat(reservation_data["date"] + "T" + reservation_data["time"])
        cur = _load_current(user_id)
        people = reservation_data.get("people") or cur.get("people")
        duration = reservation_data.get("duration") or cur.get("duration") or 2
        user_name = reservation_data.get("user_name") or cur.get("user_name")
        details = reservation_data.get("details") or user_name or get_user_display_name(user_id) or f"Klient {user_id}"

        table.put_item(
            Item={
                "user_id": user_id,
                "reservation_id": reservation_id,
                "details": details,
                "date": full_date.isoformat(),
                "people": people,
                "duration": duration,
                "user_name": user_name or details,
                "status": status,
                "reminded": False,
            }
        )
        return {"reservation_id": reservation_id, "details": details, "date": full_date, "people": people, "duration": duration, "status": status}
    except ClientError as e:
        logging.error(e)
        return None


# -----------------------------
# Business logic — single orchestrator using AI extraction
# -----------------------------

def is_faq_query(text: str) -> bool:
    t = (text or "").lower().strip()
    if re.search(r"\b\d+\s*(?:h|godz|godzin|godziny)\b", t):
        return False
    for k in [
        "adres",
        "gdzie",
        "lokalizacja",
        "location",
        "cena",
        "ceny",
        "koszt",
        "ile koszt",
        "godziny otwarcia",
        "kiedy otwarte",
        "czy jest otwarte",
        "parking",
        "kontakt",
        "telefon",
        "mail",
        "email",
        "price",
        "prices",
        "open hours",
    ]:
        if k in t:
            return True
    return False


def _format_options_message(options: List[datetime], time_hint: Optional[str] = None, people_hint: Optional[int] = None) -> str:
    labels = [dt.strftime("%A %d.%m").capitalize() for dt in options]
    joined = " lub ".join(labels)
    suffix = ""
    if time_hint:
        suffix += f" o {time_hint[:5]}"
    if people_hint:
        suffix += f" dla {people_hint} osób"
    return f"Czy pasuje {joined}{suffix}? Odpowiedz 1, 2 lub 3 albo wpisz konkretną datę."


def handle_reservation_step(sender_id: str, user_message: str) -> str:
    txt = normalize_text(user_message)

    # Reset flow
    if any(k in txt for k in ["reset", "od nowa", "zacznijmy od nowa", "zmien termin", "zmień termin"]):
        fresh = {
            "people": None,
            "date": None,
            "time": None,
            "duration": None,
            "awaiting_confirmation": False,
            "awaiting_day_choice": False,
            "suggested_options": [],
            "details": None,
            "status": "in_progress",
        }
        _save_current(sender_id, fresh)
        return "Zaczynamy od nowa 😊 Podaj proszę liczbę osób."

    # Load / bootstrap
    current = _load_current(sender_id)

    # If we are already awaiting explicit confirmation, interpret simple confirms/declines
    if current.get("awaiting_confirmation"):
        accept_terms = {
            "tak",
            "potwierdz",
            "potwierdzam",
            "ok",
            "okej",
            "zgoda",
            "zgadzam sie",
        }
        reject_terms = {
            "nie",
            "anuluj",
            "rezygnuje",
            "odrzuc",
            "nie teraz",
        }
        if txt in accept_terms:
            # Ensure we have all required fields
            if current.get("date") and current.get("time") and current.get("people"):
                # Persist reservation as pending and notify owner via Telegram
                pending = save_reservation(
                    sender_id,
                    {
                        "date": current["date"],
                        "time": current["time"],
                        "people": current.get("people"),
                        "duration": current.get("duration") or 2,
                        "details": current.get("details") or get_user_display_name(sender_id) or f"Klient {sender_id}",
                    },
                    status="pending",
                )
                current["awaiting_confirmation"] = False
                _save_current(sender_id, current)
                if pending:
                    try:
                        send_telegram_notification_sync(pending["reservation_id"], sender_id, pending["details"], pending["date"])
                    except Exception:
                        pass
                    when_txt = pending["date"].strftime("%d.%m.%Y %H:%M")
                    return f"Dziękuję za potwierdzenie! Zgłoszenie rezerwacji dla {pending['people']} osób w dniu {when_txt} zostało przekazane właścicielowi do akceptacji. Otrzymasz wiadomość, gdy tylko potwierdzimy."
                else:
                    return "Wystąpił błąd podczas zapisu rezerwacji. Spróbuj proszę ponownie lub podaj inny termin."
            else:
                # Missing something despite awaiting confirmation — fall back to regular flow
                current["awaiting_confirmation"] = False
                _save_current(sender_id, current)
        elif txt in reject_terms:
            try:
                table.delete_item(Key={"user_id": sender_id, "reservation_id": "current"})
            except Exception:
                pass
            return "Odrzucono rezerwację. Jeżeli chcesz, podaj inny termin."

    # Heuristic: FAQ bypass if there is no reservation intent at all
    ai = ai_extract_reservation_fields(user_message)
    if ai.get("intent") != "reservation" and is_faq_query(user_message):
        return "FAQ_BYPASS"

    # Merge AI extracted fields into state
    if ai.get("people"):
        current["people"] = ai.get("people")
    if ai.get("duration_hours"):
        current["duration"] = ai.get("duration_hours")

    # Date resolution
    new_date_iso: Optional[str] = None
    if ai.get("raw_date"):
        # try to build ISO date from AI raw
        d = extract_concrete_date_fallback(ai.get("raw_date"))
        if d:
            new_date_iso = d
    # vague: suggest options
    if ai.get("vague") and not new_date_iso:
        opts = suggest_day_options(how_many=3, prefer_next_week=("przysz" in txt or "nastepn" in txt))
        current["awaiting_day_choice"] = True
        current["suggested_options"] = [o.date().isoformat() for o in opts]
        _save_current(sender_id, current)
        return _format_options_message(opts, time_hint=(current.get("time")[:5] if current.get("time") else None), people_hint=current.get("people"))

    if new_date_iso:
        if _is_past_date_iso(new_date_iso):
            opts = suggest_day_options(how_many=3)
            current["awaiting_day_choice"] = True
            current["suggested_options"] = [o.date().isoformat() for o in opts]
            _save_current(sender_id, current)
            return "Nie można rezerwować terminu z przeszłości. " + _format_options_message(opts, time_hint=(current.get("time")[:5] if current.get("time") else None), people_hint=current.get("people"))
        current["date"] = new_date_iso

    # Time resolution
    new_time = None
    if ai.get("raw_time"):
        # normalize HH:MM
        m = re.search(r"\b([01]?\d|2[0-3])[:\. ]?([0-5]\d)?\b", ai["raw_time"]) or re.search(r"\b([01]?\d|2[0-3])\b", ai["raw_time"]) 
        if m:
            hh = int(m.group(1))
            mm = int(m.group(2)) if m.lastindex and m.group(2) else 0
            new_time = f"{hh:02d}:{mm:02d}:00"
    if new_time:
        if current.get("date") and _is_past_datetime(current["date"], new_time):
            return "Ta godzina już minęła. Podaj proszę przyszłą godzinę (np. 17:00)."
        current["time"] = new_time

    # Day choice phase (user answers 1/2/3)
    if current.get("awaiting_day_choice") and current.get("suggested_options"):
        idx_map = {"1": 0, "pierwsza": 0, "pierwszy": 0, "2": 1, "druga": 1, "drugi": 1, "3": 2, "trzecia": 2, "trzeci": 2}
        if txt in idx_map and idx_map[txt] < len(current["suggested_options"]):
            chosen_iso = current["suggested_options"][idx_map[txt]]
            if _is_past_date_iso(chosen_iso):
                opts_dt = [datetime.fromisoformat(o + "T00:00:00") for o in current["suggested_options"]]
                return "Nie można rezerwować terminu z przeszłości. " + _format_options_message(opts_dt, time_hint=(current.get("time")[:5] if current.get("time") else None), people_hint=current.get("people"))
            current["date"] = chosen_iso
            current["awaiting_day_choice"] = False
            current["suggested_options"] = []

    # Persist interim
    _save_current(sender_id, current)

    # Ask for missing fields
    if not current.get("people"):
        return "Proszę podać, ile osób ma wziąć udział w warsztacie (np. 'dla 2 osób')."
    if not current.get("date") and not current.get("time"):
        return "Podaj proszę dzień i godzinę (np. 'wtorek 17:00')."
    if not current.get("date"):
        if current.get("awaiting_day_choice"):
            opts_dt = [datetime.fromisoformat(o + "T00:00:00") for o in current.get("suggested_options", [])]
            return "Potrzebuję konkretnego dnia. " + _format_options_message(opts_dt, time_hint=(current.get("time")[:5] if current.get("time") else None), people_hint=current.get("people"))
        return "Proszę podać konkretną datę (np. '9 września' albo '09.09')."
    if not current.get("time"):
        return "Proszę podać godzinę rezerwacji (np. '18:00')."
    
    if not current.get("duration"):
        m_bare = re.match(r"^\s*(\d{1,2})\s*$", txt)
        if m_bare:
            v = int(m_bare.group(1))
            if 1 <= v <= 8:
                current["duration"] = v

    # Duration default / ask
    if not current.get("duration"):
        if ai.get("duration_hours"):
            current["duration"] = ai.get("duration_hours")
        else:
            _save_current(sender_id, current)
            return "Na ile godzin chcesz zarezerwować? Rekomendujemy 2 godziny. Napisz np. '2 godziny'."

    # At this point we have date+time+people+duration — validate and check availability
    full_dt = datetime.fromisoformat(current["date"] + "T" + current["time"])
    if full_dt < _now_pl_naive():
        free_ranges = compute_free_ranges_for_day(full_dt, current.get("duration") or 2)
        if full_dt.date() == _now_pl_naive().date() and free_ranges:
            current["time"] = None
            _save_current(sender_id, current)
            return "Nie można rezerwować terminu z przeszłości. Dostępne dziś: " + format_free_ranges(free_ranges) + ". Podaj proszę inną godzinę lub dzień."
        current["date"], current["time"] = None, None
        opts = suggest_day_options(how_many=3)
        current["awaiting_day_choice"] = True
        current["suggested_options"] = [o.date().isoformat() for o in opts]
        _save_current(sender_id, current)
        return "Nie można rezerwować terminu z przeszłości. " + _format_options_message(opts, people_hint=current.get("people"))

    is_free, err = check_availability_in_calendar(full_dt, duration_hours=current.get("duration") or 2)
    if is_free is None:
        return "⚠️ Nie udało się sprawdzić dostępności. Podaj proszę inny termin albo spróbuj ponownie za chwilę."

    if is_free:
        current["awaiting_confirmation"] = True
        _save_current(sender_id, current)
        human_dt = full_dt.strftime("%d.%m.%Y %H:%M")
        quicks = [
            {"content_type": "text", "title": "Potwierdź", "payload": "CONFIRM"},
            {"content_type": "text", "title": "Odrzuć", "payload": "REJECT"},
        ]
        try:
            send_quick_replies(sender_id, f"Proszę o potwierdzenie: zarezerwować warsztat dla {current['people']} osób na {current['duration']}h w dniu {human_dt}?", quicks)
        except Exception:
            pass
        # We've already sent the quick replies message; don't send a duplicate plain text
        return "__NOOP__"

    # busy -> propose alternatives
    free_ranges = compute_free_ranges_for_day(full_dt, current.get("duration") or 2)
    if free_ranges:
        current["time"] = None
        current["awaiting_day_choice"] = False
        _save_current(sender_id, current)
        return f"Ten termin jest zajęty. Dostępne przedziały tego dnia: {format_free_ranges(free_ranges)}. Podaj proszę inną godzinę z powyższych lub inny dzień."

    # no windows this day -> propose other days
    current["date"], current["time"] = None, None
    current["awaiting_day_choice"] = True
    opts = suggest_day_options(how_many=3)
    current["suggested_options"] = [o.date().isoformat() for o in opts]
    _save_current(sender_id, current)
    return "Ten termin jest zajęty i brak wolnych okien tego dnia. " + _format_options_message(opts, people_hint=current.get("people"))


# -----------------------------
# Owner notifications (Telegram)
# -----------------------------

async def send_telegram_reservation_notification(reservation_id: str, user_id: str, reservation_details: str, date: datetime):
    try:
        item = table.get_item(Key={"user_id": user_id, "reservation_id": reservation_id}).get("Item") or {}
    except Exception:
        item = {}
    username = item.get("user_name") or get_user_display_name(user_id) or user_id
    dur = item.get("duration")
    
    message = f"""
🏺 **Nowa Rezerwacja - Studio Ceramiki**

👤 **Klient:** {username}
📋 **Szczegóły:** {dur}h
📅 **Data:** {date.strftime('%d.%m.%Y %H:%M')}

Wybierz akcję:
"""
    keyboard = [
        [
            InlineKeyboardButton("✅ Potwierdź", callback_data=f"confirm_{reservation_id}_{user_id}"),
            InlineKeyboardButton("❌ Odrzuć", callback_data=f"reject_{reservation_id}_{user_id}"),
        ],
        [
            InlineKeyboardButton("📝 Szczegóły", callback_data=f"details_{reservation_id}_{user_id}"),
            InlineKeyboardButton("🗑 Anuluj ", callback_data=f"cancel_{reservation_id}_{user_id}"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await (TELEGRAM_APP.bot if TELEGRAM_APP is not None else telegram_bot).send_message(
            chat_id=OWNER_CHAT_ID, text=message, reply_markup=reply_markup, parse_mode="Markdown"
        )
        logging.info(f"✅ Telegram notification sent for reservation {reservation_id}")
    except Exception as e:
        logging.error(f"Error sending Telegram notification: {e}")


async def handle_telegram_callback(update, context):
    query = update.callback_query
    await query.answer()
    try:
        action, reservation_id, user_id = query.data.split('_', 2)
    except Exception:
        await query.edit_message_text("⚠️ Błędne dane akcji.")
        return

    if action == "confirm":
        item = table.get_item(Key={"user_id": user_id, "reservation_id": reservation_id}).get("Item")
        if not item:
            await query.edit_message_text("⚠️ Rezerwacja nie istnieje.")
            return
        if item.get("status") == "confirmed":
            await query.edit_message_text("ℹ️ Rezerwacja była już potwierdzona.")
            return
        if item.get("status") != "pending":
            await query.edit_message_text("⚠️ Nieprawidłowy status rezerwacji.")
            return
        try:
            full_date = datetime.fromisoformat(item["date"])
        except Exception:
            await query.edit_message_text("⚠️ Błędny format daty.")
            return

        table.update_item(
            Key={"user_id": user_id, "reservation_id": reservation_id},
            UpdateExpression="SET #s=:s, reminded=:r",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "confirmed", ":r": False},
        )
        add_to_google_calendar(
            details=item.get("details") or item.get("user_name") or f"Klient {user_id}",
            date=full_date,
            user_id=user_id,
            people=item.get("people"),
            duration_hours=item.get("duration") or 2,
        )
        send_message(user_id, f"✅ Twoja rezerwacja została potwierdzona. Zapraszamy w dniu {full_date.strftime('%d.%m.%Y %H:%M')}")
        # Do not edit the original details message; send a separate confirmation
        await query.message.reply_text("✅ Rezerwacja potwierdzona!")

    elif action == "reject":
        table.update_item(
            Key={"user_id": user_id, "reservation_id": reservation_id},
            UpdateExpression="SET #s=:s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "rejected"},
        )
        send_message(user_id, "❌ Twoja rezerwacja została odrzucona.")
        await query.edit_message_text("❌ Rezerwacja odrzucona!")


def send_telegram_notification_sync(reservation_id: str, user_id: str, details: str, date: datetime):
    try:
        global TELEGRAM_LOOP
        if TELEGRAM_LOOP is None:
            def _runner():
                loop = asyncio.new_event_loop()
                globals()['TELEGRAM_LOOP'] = loop
                asyncio.set_event_loop(loop)
                loop.run_forever()
            thr = threading.Thread(target=_runner, daemon=True)
            thr.start()
            for _ in range(20):
                if TELEGRAM_LOOP is not None:
                    break
                time.sleep(0.05)
        last_err = None
        for _ in range(3):
            fut = asyncio.run_coroutine_threadsafe(
                send_telegram_reservation_notification(reservation_id, user_id, details, date), TELEGRAM_LOOP
            )
            try:
                fut.result(timeout=15)
                last_err = None
                break
            except Exception as e:
                last_err = e
                if 'Pool timeout' in str(e) or 'connection pool' in str(e):
                    time.sleep(0.5)
                    continue
                else:
                    break
        if last_err:
            raise last_err
    except Exception as e:
        logging.error(f"Telegram notification error: {e}")


async def setup_telegram_bot():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CallbackQueryHandler(handle_telegram_callback))
    return application


def _telegram_loop_runner():
    global TELEGRAM_APP, TELEGRAM_LOOP
    loop = asyncio.new_event_loop()
    TELEGRAM_LOOP = loop
    asyncio.set_event_loop(loop)
    try:
        app_ = loop.run_until_complete(setup_telegram_bot())
        TELEGRAM_APP = app_
        # Ensure no Telegram webhook is set (we use polling here)
        try:
            loop.run_until_complete(app_.bot.delete_webhook(drop_pending_updates=True))
        except Exception:
            pass

        # Start Application without installing signal handlers (we're in a thread)
        loop.run_until_complete(app_.initialize())
        loop.run_until_complete(app_.start())
        loop.run_until_complete(app_.updater.start_polling())

        # Keep the thread's loop running
        loop.run_forever()
    finally:
        try:
            if TELEGRAM_APP is not None:
                # Graceful shutdown sequence
                try:
                    loop.run_until_complete(TELEGRAM_APP.updater.stop())
                except Exception:
                    pass
                loop.run_until_complete(TELEGRAM_APP.stop())
                loop.run_until_complete(TELEGRAM_APP.shutdown())
        except Exception:
            pass
        loop.close()


# -----------------------------
# AI generic response (FAQ / small talk)
# -----------------------------

def generate_response(user_message: str) -> str:
    try:
        response = client.chat.completions.create(
            model='gpt-4o-mini',
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_FAQ},
                {"role": "user", "content": user_message},
            ],
            timeout=10,
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f'OpenAI API error: {e}')
        return 'Dziękuję za wiadomość! Właściciel studia skontaktuje się z Tobą wkrótce. 🏺'


# -----------------------------
# Scheduler (reminders)
# -----------------------------

def send_reminders():
    now = _now_pl_naive()
    tomorrow = now + timedelta(days=1)
    try:
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
    except Exception:
        items = []

    for item in items:
        try:
            if datetime.fromisoformat(item["date"]) - now < timedelta(hours=24):
                dt = datetime.fromisoformat(item["date"]) 
                human = dt.strftime('%d.%m %H:%M')
                send_message(item["user_id"], f"📅 Przypomnienie: Twoja wizyta jutro o {human}. Szczegóły: {item.get('details') or ''}".strip())
                table.update_item(
                    Key={"user_id": item["user_id"], "reservation_id": item["reservation_id"]},
                    UpdateExpression="SET reminded = :true",
                    ExpressionAttributeValues={":true": True},
                )
        except Exception:
            continue

scheduler = BackgroundScheduler()
scheduler.add_job(send_reminders, "interval", hours=1)
scheduler.start()


# -----------------------------
# Webhook
# -----------------------------
@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    # Verification
    if request.method == 'GET':
        if request.args.get('hub.verify_token') == VERIFY_TOKEN:
            return request.args.get('hub.challenge')
        return 'Invalid token', 403

    # Events
    if request.method == 'POST':
        try:
            data = request.get_json()
            logging.info(f'📥 Webhook received: {json.dumps(data, indent=2)}')

            if not data or data.get('object') not in ('instagram', 'page'):
                return 'IGNORED', 200

            for entry in data.get('entry', []):
                for messaging_event in entry.get('messaging', []):
                    sender_id = messaging_event['sender']['id']

                    # IG postbacks (buttons)
                    if 'postback' in messaging_event:
                        payload = (messaging_event['postback'] or {}).get('payload')
                        if payload in ("CONFIRM", "IG_CONFIRM"):
                            reply = handle_reservation_step(sender_id, "tak")
                            if reply and reply != "__NOOP__":
                                send_message(sender_id, reply)
                            continue
                        if payload in ("REJECT", "IG_REJECT"):
                            try:
                                table.delete_item(Key={"user_id": sender_id, "reservation_id": "current"})
                            except Exception:
                                pass
                            send_message(sender_id, "Odrzucono rezerwację. Jeśli chcesz, podaj inny termin.")
                            continue

                    # Ignore non-message events
                    if 'message' not in messaging_event:
                        continue
                    msg = messaging_event['message']

                    # Ignore our echoes
                    if msg.get('is_echo', False):
                        continue

                    # Quick replies (IG/Messenger)
                    if isinstance(msg, dict) and msg.get('quick_reply'):
                        payload = msg['quick_reply'].get('payload')
                        if payload in ("CONFIRM", "IG_CONFIRM"):
                            reply = handle_reservation_step(sender_id, "tak")
                            if reply and reply != "__NOOP__":
                                send_message(sender_id, reply)
                            continue
                        if payload in ("REJECT", "IG_REJECT"):
                            try:
                                table.delete_item(Key={"user_id": sender_id, "reservation_id": "current"})
                            except Exception:
                                pass
                            send_message(sender_id, "Odrzucono rezerwację. Jeśli chcesz, podaj inny termin.")
                            continue

                    user_message = msg.get('text')
                    if not user_message:
                        continue

                    logging.info(f'💬 Message from {sender_id}: {user_message}')

                    # When awaiting confirmation, small in-message corrections
                    active = _load_current(sender_id)

                    # Commands available in chat
                    cmd = (user_message or '').strip().lower()
                    if cmd == '/clear':
                        try:
                            table.delete_item(Key={"user_id": sender_id, "reservation_id": "current"})
                        except Exception:
                            pass
                        send_message(sender_id, "Wyczyszczono rozmowę i dane rezerwacji. Napisz, w czym mogę pomóc?")
                        continue
                    if active.get("awaiting_confirmation"):
                        corrected = False
                        # AI try to extract quick corrections
                        ai = ai_extract_reservation_fields(user_message)
                        if ai.get("raw_time"):
                            nt = extract_time_fallback(ai.get("raw_time"))
                            if nt and nt != active.get("time"):
                                active["time"] = nt
                                corrected = True
                        if ai.get("raw_date"):
                            nd = extract_concrete_date_fallback(ai.get("raw_date"))
                            if nd and nd != active.get("date"):
                                active["date"] = nd
                                corrected = True
                        if ai.get("people") and ai.get("people") != active.get("people"):
                            active["people"] = ai.get("people")
                            corrected = True
                        if corrected and active.get("date") and active.get("time") and active.get("people"):
                            _save_current(sender_id, active)
                            full_dt = datetime.fromisoformat(active["date"] + "T" + active["time"])
                            dur3 = active.get("duration") or 2
                            is_free, err = check_availability_in_calendar(full_dt, duration_hours=dur3)
                            if is_free is None:
                                send_message(sender_id, "⚠️ Nie udało się sprawdzić dostępności. Podaj proszę inny termin albo spróbuj ponownie za chwilę.")
                                continue
                            if not is_free:
                                active["awaiting_confirmation"] = False
                                _save_current(sender_id, active)
                                free_ranges = compute_free_ranges_for_day(full_dt, dur3)
                                opts = format_free_ranges(free_ranges, max_items=3)
                                send_message(sender_id, f"Ten termin jest zajęty. Dostępne przedziały tego dnia (min {dur3}h): {opts}. Podaj proszę inną godzinę z powyższych lub inny dzień.")
                                continue
                            human_dt = full_dt.strftime("%d.%m.%Y %H:%M")
                            quicks = [
                                {"content_type": "text", "title": "Potwierdź", "payload": "CONFIRM"},
                                {"content_type": "text", "title": "Odrzuć", "payload": "REJECT"},
                            ]
                            try:
                                send_quick_replies(sender_id, f"Zaktualizowałem szczegóły. Potwierdzić rezerwację dla {active['people']} osób w dniu {human_dt}?", quicks)
                            except Exception:
                                pass
                            continue

                    # Route to reservation or FAQ
                    # If conversation already in progress OR AI says intent reservation -> run orchestrator
                    if active.get("awaiting_confirmation") or any(active.get(k) for k in ("people", "date", "time")):
                        reply = handle_reservation_step(sender_id, user_message)
                        if reply == "__NOOP__":
                            continue
                        if reply == "FAQ_BYPASS":
                            send_message(sender_id, generate_response(user_message))
                        else:
                            send_message(sender_id, reply)
                        continue

                    ai_intent = ai_extract_reservation_fields(user_message).get("intent")
                    if ai_intent == "reservation":
                        reply = handle_reservation_step(sender_id, user_message)
                        if reply != "__NOOP__":
                            if reply == "FAQ_BYPASS":
                                send_message(sender_id, generate_response(user_message))
                            else:
                                send_message(sender_id, reply)
                        continue

                    # pure FAQ / other
                    send_message(sender_id, generate_response(user_message))

        except Exception as e:
            logging.error(f'❌ Webhook error: {e}', exc_info=True)
            return 'ERROR', 500

        return 'OK', 200

# -----------------------------
# Entrypoint
# -----------------------------
if __name__ == "__main__":
    # Start Telegram bot in background thread with its own event loop
    TELEGRAM_THREAD = threading.Thread(target=_telegram_loop_runner, daemon=True)
    TELEGRAM_THREAD.start()

    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
