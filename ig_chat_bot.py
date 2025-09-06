import os
import base64
import json
import re
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
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
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")

# Godziny pracy studia: 0=pon, 6=niedz. None oznacza zamkniete.
OPENING_HOURS = {
    0: None,
    1: (10, 20),
    2: (10, 20),
    3: (10, 20),
    4: (10, 20),
    5: (10, 18),
    6: (10, 18),
}

def get_user_display_name(user_id: str) -> str | None:
    """Fetch only the Instagram/Messenger username from profile."""
    try:
        url = f"https://graph.facebook.com/v23.0/{user_id}"
        params = {
            "access_token": INSTAGRAM_TOKEN,
            "fields": "username",
        }
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

def parse_duration_hours(text: str) -> int | None:
    """Extract duration in hours from free text (e.g., '2h', '2 godziny')."""
    t = text.lower()
    m = re.search(r"\b(\d{1,2})\s*(?:h|godz|godzina|godziny|godzin|hour|hours)?\b", t)
    if m:
        try:
            val = int(m.group(1))
            if 1 <= val <= 8:
                return val
        except ValueError:
            return None
    return None

# --- Text sanitization ---
def sanitize_text(s: str | None) -> str:
    if not s:
        return ""
    t = str(s)
    repl = {
        "Wroc�'aw": "Wrocław",
        "Wroc�'awiu": "Wrocławiu",
        "Jeste�": "Jesteś",
        "si�T": "się",
        "si�": "się",
        "Nie uda�'o": "Nie udało",
        "spr�?buj": "spróbuj",
        "dost�Tpno�": "dostępno",
        "dost�Tpno": "dostępno",
        "dostepnosci": "dostępności",
        "zaj�Tty": "zajęty",
        "zaj�T": "zaję",
        "w�'a�": "właś",
        "w�'a": "wła",
        "wiadomo�": "wiadomoś",
        "rezerwacj�T": "rezerwację",
        "Godziny": "Godziny",
        "Prosz�T": "Proszę",
        "godzin�T": "godzinę",
        "dzie�": "dzień",
        "wrze�": "wrześ",
        "os�?b": "osób",
        "os�": "osó",
        "Potwierd��": "Potwierdź",
        "Odrzu��": "Odrzuć",
        "Szczeg�": "Szczegó",
        "Wysy�'a": "Wysyła",
        "pi�:tek": "piątek",
        "Je�": "Jeś",
        "�??": "",
        "�'": "",
        "�": "",
    }
    for k, v in repl.items():
        t = t.replace(k, v)
    # Normalize multiple spaces leftover
    t = re.sub(r"\s+", " ", t).strip()
    return t

# Initialize Telegram bot
telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)
# Globals for Telegram runtime
TELEGRAM_APP = None
TELEGRAM_LOOP = None
TELEGRAM_THREAD = None

# OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

# DynamoDB
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table("reservations")

# Admin token for maintenance routes
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

SYSTEM_PROMPT = """
[ROLA]
Jesteś asystentem Studio Ceramiki we Wrocławiu. Obsługujesz klientów na Instagramie, odpowiadasz na ich pytania i pomagasz w rezerwacjach warsztatów.

[CEL]
- Odpowiadaj na proste pytania FAQ (adres, godziny otwarcia, ceny).
- Obsługuj rezerwacje: rozpoznawaj intencję rezerwacji i wyciągaj potrzebne dane (liczba osób, data i godzina, czas trwania zajęcia).
- Sprawdzaj dostępność terminów w kalendarzu Google.
- Informuj klienta o statusie rezerwacji (oczekuje, potwierdzona, odrzucona).

[FAQ]
- Adres: Komuny Paryskiej 55, Wrocław
- Godziny otwarcia: Pon-Pt 10:00-18:00, Sob 10:00-14:00, Nd nieczynne
- Ceny szkliwienie ceramiki: 120zł za 1 godzinę, 150zł za 2 godziny
- Ceny Koło:
Dla dzjeci (6-17 lat)
• Karnet: 3 spotkania przy kole garncarskim (ok. 2 godz. każde) + 1 zajecia ze szkliwienia. Cena: 390zł.
• Zajecia: cena 100 zł
Dla par
• 3 spotkania przy kole garncarskim (ok. 2 godz. kazde) + 1 zajecia ze szkliwienia. Cena: 480 zł.
• Zajecia: cena 120 zł.
Zajecia indywidualne
• Karnet: 3 spotkania przy kole garncarskim (ok. 2-2,5 godz. kazde) + 1 zajecia ze szkliwienia wszystkich wykonanych prac. Cena: 560 zł.
• Zajecia: cena 150 zł.

[ZASADY]
- Obsługujesz tylko 3 języki: polski, angielski i ukraiński.
- Zawsze odpowiadaj w tym języku, z którego korzysta się użytkownik, jasno i profesjonalnie.
- Przy prostych pytaniach FAQ odpowiadaj od razu bez angażowania właściciela.
- Jeśli klient chce zarezerwować warsztat:
  - Wydobądź: liczbę osób, datę i godzinę.
  - Sprawdź dostępność w Google Calendar.
  - Jeśli użytkownik podaje zakres ogólny (np. „w przyszłym tygodniu”, „w weekend”, „w tym tygodniu”), nie próbuj rezerwować. Dopytaj o KONKRETNY dzień (np. „Czy pasuje wtorek 09.09 lub środa 10.09?”). Zawsze zaproponuj 2–3 najbliższe pasujące dni z datą dzienną (dd.mm).
  - Jeśli termin jest wolny: odpowiedz „Rezerwacja oczekuje na potwierdzenie” i powiadom właściciela przez Telegram.
  - Jeśli termin jest zajęty: zaproponuj inny termin.
  - Gdy trwa rezerwacja, nie zadawaj pytań ogólnych – odpowiadaj krótko i potwierdzaj brakujące dane.
- Po potwierdzeniu przez właściciela:
  - Zapisz rezerwację w Google Calendar.
  - Wyślij klientowi wiadomość: „✅ Twoja rezerwacja została potwierdzona. Zapraszamy w dniu [data i godzina].”

[PRZEPŁYW KONWERSACJI]
1. Klient: „Jaki adres macie?” → Bot: odpowiada z FAQ.
2. Klient: „Ile kosztują warsztaty?” → Bot: odpowiada z FAQ.
3. Klient: „Chcę zarezerwować warsztat dla 3 osób w piątek o 16:00” → Bot:
   - Wydobywa: 3 osoby, piątek 16:00.
   - Sprawdza kalendarz.
   - Jeśli wolne: „Rezerwacja oczekuje na potwierdzenie.”
   - Wysyła powiadomienie do właściciela na Telegram.
4. Właściciel potwierdza w Telegram → Bot:
   - Zapisuje rezerwację w Google Calendar.
   - Wysyła do klienta: „✅ Twoja rezerwacja została potwierdzona. Zapraszamy w dniu [data i godzina].”
"""

# Google Calendar
def get_google_credentials():
    """Ładuje credentials z ENV GOOGLE_CREDENTIALS_BASE64 (base64 z JSON)."""
    creds_b64 = os.getenv("GOOGLE_CREDENTIALS_BASE64")
    if not creds_b64:
        raise RuntimeError("Brak GOOGLE_CREDENTIALS_BASE64 w ENV")
    try:
        decoded = base64.b64decode(creds_b64)
        creds_dict = json.loads(decoded)
    except Exception as e:
        raise RuntimeError(f"Nieprawidłowe GOOGLE_CREDENTIALS_BASE64: {e}")
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return creds

def _hours_to_float(val, default=2.0) -> float:
    """Coerce hours value (possibly Decimal/str/int) to float for timedelta."""
    try:
        if val is None:
            return float(default)
        # Avoid bools being treated as ints
        if isinstance(val, bool):
            return float(default)
        return float(val)
    except Exception:
        return float(default)


def add_to_google_calendar(details, date, user_id, people=None, duration_hours=2):
    try:
        creds = get_google_credentials()
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        
        dh = _hours_to_float(duration_hours, default=2.0)
        event = {
            "summary": details,
            "description": f"Rezerwacja od użytkownika {user_id}",
            "start": {"dateTime": date.isoformat(), "timeZone": "Europe/Warsaw"},
            "end": {"dateTime": (date + timedelta(hours=dh)).isoformat(), "timeZone": "Europe/Warsaw"},
        }

        # Enrich description with people count and IG id
        try:
            event["description"] = f"Liczba osob: {people if people is not None else '?'}\nIG: {user_id}"
        except Exception:
            pass

        event_result = service.events().insert(
            calendarId=GOOGLE_CALENDAR_ID or "primary", body=event
        ).execute()

        logging.info(f"✅ Rezerwacja dodana do Google Calendar: {event_result.get('htmlLink')}")
    except Exception as e:
        logging.error(f"❌ Błąd dodawania do Google Calendar: {e}")

def generate_response(user_message):
    try:
        response = client.chat.completions.create(
            model='gpt-4o-mini',
            messages=[
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': user_message},
            ],
            timeout=10
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f'OpenAI API error: {e}')
        return 'Dziękuję za wiadomość! Właściciel studia skontaktuje się z Tobą wkrótce.'
    
    
def check_availability_in_calendar(date: datetime, duration_hours=2):
    """
    Sprawdza dostępność w Google Calendar dla KONKRETNEGO przedziału [start, end).
    Bazuje na wolnych oknach dnia – nie tylko na eventach zaczynających się w przedziale.
    Zwraca: (True = wolne, False = zajęte, None = błąd), err
    """
    try:
        duration = _hours_to_float(duration_hours, default=2.0)
        req_start = date
        req_end = date + timedelta(hours=duration)

        # Wylicz wolne przedziały dla dnia i sprawdź, czy [req_start, req_end) w całości mieści się w którymś z nich
        free_ranges = compute_free_ranges_for_day(date, duration)
        for a, b in free_ranges:
            if a <= req_start and b >= req_end:
                return (True, None)
        return (False, None)
    except Exception as e:
        logging.error(f"❌ Błąd sprawdzania dostępności w kalendarzu: {e}")
        return (None, str(e))


def _rfc3339_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.isoformat().replace("+00:00", "Z")

# ---- Local time helpers (Europe/Warsaw) ----
def _now_pl() -> datetime:
    try:
        if ZoneInfo is not None:
            return datetime.now(ZoneInfo("Europe/Warsaw"))
    except Exception:
        pass
    return datetime.now()

def _now_pl_naive() -> datetime:
    n = _now_pl()
    # Use naive time for comparison with naive datetimes stored in state
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

def list_events_for_day(day: datetime):
    try:
        creds = get_google_credentials()
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        # Build day window in Europe/Warsaw and convert to UTC RFC3339
        if ZoneInfo is not None:
            tz = ZoneInfo("Europe/Warsaw")
            day_start_local = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
            day_end_local = day.replace(hour=23, minute=59, second=59, microsecond=0, tzinfo=tz)
            time_min = day_start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            time_max = day_end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        else:
            # Fallback: use naive -> UTC as-is (may shift by TZ)
            day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day.replace(hour=23, minute=59, second=59, microsecond=0)
            time_min = _rfc3339_utc(day_start)
            time_max = _rfc3339_utc(day_end)
        res = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID or "primary",
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        return res.get("items", [])
    except Exception as e:
        logging.error(f"List events error: {e}")
        return []

def compute_free_ranges_for_day(day: datetime, duration_hours: int):
    wh = OPENING_HOURS.get(day.weekday())
    if not wh:
        return []
    open_h, close_h = wh
    work_start = day.replace(hour=open_h, minute=0, second=0, microsecond=0)
    work_end = day.replace(hour=close_h, minute=0, second=0, microsecond=0)
    events = list_events_for_day(day)
    busy = []
    for ev in events:
        s = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
        e = ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date")
        if not s or not e:
            continue
        try:
            if "T" in s:
                # Parse RFC3339 and convert to local Europe/Warsaw time
                s_iso = s.replace("Z", "+00:00")
                dt_s = datetime.fromisoformat(s_iso)
                if dt_s.tzinfo is None and ZoneInfo is not None:
                    dt_s = dt_s.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
                if ZoneInfo is not None:
                    dt_s_local = dt_s.astimezone(ZoneInfo("Europe/Warsaw"))
                else:
                    dt_s_local = dt_s
                st = day.replace(hour=dt_s_local.hour, minute=dt_s_local.minute, second=0, microsecond=0)
            else:
                st = work_start
            if "T" in e:
                e_iso = e.replace("Z", "+00:00")
                dt_e = datetime.fromisoformat(e_iso)
                if dt_e.tzinfo is None and ZoneInfo is not None:
                    dt_e = dt_e.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
                if ZoneInfo is not None:
                    dt_e_local = dt_e.astimezone(ZoneInfo("Europe/Warsaw"))
                else:
                    dt_e_local = dt_e
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
    merged = []
    for iv in busy:
        if not merged or iv[0] > merged[-1][1]:
            merged.append(list(iv))
        else:
            merged[-1][1] = max(merged[-1][1], iv[1])
    busy = [(a, b) for a, b in merged]
    free = []
    cursor = work_start
    for a, b in busy:
        if a > cursor:
            free.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < work_end:
        free.append((cursor, work_end))
    need = timedelta(hours=_hours_to_float(duration_hours, default=2.0))
    free = [iv for iv in free if (iv[1] - iv[0]) >= need]
    return free

def format_free_ranges(ranges, max_items=3):
    out = []
    for i, (a, b) in enumerate(ranges[:max_items], start=1):
        out.append(f"{i}) {a.strftime('%H:%M')}–{b.strftime('%H:%M')}")
    return ", ".join(out) if out else "brak"


def send_message(recipient_id, text):
    url = f"https://graph.facebook.com/v23.0/me/messages"
    params = {"access_token": INSTAGRAM_TOKEN}
    data = {
        "recipient": {"id": recipient_id},
        "message": {"text": text},
    }
    try:
        response = requests.post(url, params=params, json=data)
        if response.status_code != 200:
            logging.error(f"❌ Instagram API error: {response.status_code} - {response.text}")
        else:
            logging.info(f"📤 Sent to {recipient_id}: {text}")
    except Exception as e:
        logging.error(f"❌ Error sending message: {e}", exc_info=True)

def normalize_text(s: str) -> str:
    t = s.lower().strip()
    # proste normalizacje/ literówki
    t = t.replace("osóby", "osoby").replace("osobuy", "osoby")
    t = t.replace(",", " ").replace("  ", " ")
    return t

def send_quick_replies(recipient_id: str, text: str, replies: list[dict]):
    """Wyślij szybkie odpowiedzi (quick replies) na Instagramie/Messengerze."""
    url = f"https://graph.facebook.com/v23.0/me/messages"
    params = {"access_token": INSTAGRAM_TOKEN}
    # sanitize titles in quick replies
    clean_replies = list(replies or [])
    data = {
        "recipient": {"id": recipient_id},
        "message": {
            "text": text,
            "quick_replies": clean_replies,
        },
    }
    try:
        response = requests.post(url, params=params, json=data)
        if response.status_code != 200:
            logging.error(f"Instagram API error (quick replies): {response.status_code} - {response.text}")
        else:
            logging.info(f"Sent quick replies to {recipient_id}: {text}")
    except Exception as e:
        logging.error(f"Error sending quick replies: {e}", exc_info=True)

# ---- [NOWE] Pomocnicze: routowanie FAQ vs rezerwacja ----
def is_faq_query(text: str) -> bool:
    """Heurystyka FAQ. Unika fałszywych trafień dla '2 godziny' (czas trwania).
    Zwraca True tylko dla zapytań o FAQ (adres, godziny otwarcia, ceny itd.).
    """
    t = text.lower().strip()
    # Jeśli wygląda na podanie czasu trwania (np. "2 godziny", "2h") – nie traktuj jak FAQ
    if re.search(r"\b\d+\s*(?:h|godz|godzin|godziny)\b", t):
        return False
    # Frazy FAQ (bardziej precyzyjne, bez gołego 'godziny')
    keywords = [
        "adres", "gdzie", "lokalizacja", "location",
        "cena", "ceny", "koszt", "ile koszt",
        "godziny otwarcia", "kiedy otwarte", "czy jest otwarte",
        "parking", "kontakt", "telefon", "mail", "email",
        "price", "prices", "open hours"
    ]
    return any(k in t for k in keywords)

def needs_reservation_input(active: dict | None) -> bool:
    """Czy trzeba kontynuować flow rezerwacji (brak danych lub czekamy na krok)?"""
    if not active:
        return False
    return (
        active.get("awaiting_confirmation")
        or active.get("awaiting_day_choice")
        or active.get("people") is None
        or active.get("date") is None
        or active.get("time") is None
        or active.get("duration") is None
    )

WEEKDAYS_PL = {
    "poniedziałek": 0, "pon": 0,
    "wtorek": 1, "wto": 1, "wt": 1,
    "środa": 2, "sroda": 2, "śr": 2, "sr": 2,
    "czwartek": 3, "czw": 3,
    "piątek": 4, "piatek": 4, "pt": 4,
    "sobota": 5, "sob": 5,
    "niedziela": 6, "nie": 6, "nd": 6
}

def extract_people(text: str):
    m = re.search(r"(\d+)\s*(osób|osoby|osoba|os|osoby\.)?", text)
    return int(m.group(1)) if m else None

def extract_time(text: str):
    # 17, 17.00, 17:00, 17 00
    m = re.search(r"\b([01]?\d|2[0-3])[:\. ]?([0-5]\d)?\b", text)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2)) if m.group(2) else 0
    return f"{hh:02d}:{mm:02d}:00"

def parse_people_count(text: str) -> int | None:
    """Parse number of people from user text. Accepts bare digits or with labels (osoby/osób/os)."""
    t = text.lower()
    m = re.search(r"\b(?:dla|na)?\s*(\d{1,2})\s*(?:osob(?:a|y)?|osób|os)?\b", t, re.IGNORECASE)
    if not m:
        m = re.search(r"\b(\d{1,2})\b", t)
    return int(m.group(1)) if m else None

def extract_weekday(text: str):
    for w in WEEKDAYS_PL:
        if re.search(rf"\b{w}\b", text):
            return WEEKDAYS_PL[w]
    return None

def extract_concrete_date(text: str, reference: datetime | None = None):
    """
    Wyciąga konkretną datę z tekstu.
    Obsługuje formaty numeryczne: dd.mm.yyyy, d.m.yy, dd.mm, d.m.
    Dla dd.mm bez roku zwraca najbliższą przyszłą datę.
    Zwraca ISO 'YYYY-MM-DD' lub None.
    """
    ref = reference or datetime.now()
    t = text.strip().lower()

    # dd.mm.yyyy lub d.m.yy
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

    # dd.mm lub d.m -> najbliższa przyszłość
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

    # Fallback: natural language (np. "9 wrzesnia")
    try:
        dt = parse_date(
            t,
            languages=["pl"],
            settings={
                "PREFER_DATES_FROM": "future",
                "DATE_ORDER": "DMY",
                "REQUIRE_PARTS": ["day", "month"],
            },
        )
        return dt.date().isoformat() if dt else None
    except Exception:
        return None

def extract_date(text: str, reference: datetime | None = None):
    """
    Wyciąga TYLKO konkretną datę (dzień + miesiąc); ignoruje ogólniki typu
    'w przyszłym tygodniu'. Zwraca ISO 'YYYY-MM-DD' lub None.
    """
    try:
        dt = parse_date(
            text,
            languages=["pl"],
            settings={
                "PREFER_DATES_FROM": "future",
                "REQUIRE_PARTS": ["day", "month"],  # kluczowe: wymagaj konkretu
            }
        )
        return dt.date().isoformat() if dt else None
    except Exception:
        return None


def resolve_weekday_to_date(weekday_idx: int, reference: datetime | None = None):
    """Zamień nazwę dnia (np. 'wtorek') na najbliższą przyszłą datę (ISO)."""
    ref = reference or datetime.now()
    delta = (weekday_idx - ref.weekday()) % 7
    if delta == 0:
        delta = 7
    return (ref + timedelta(days=delta)).date().isoformat()

def is_vague_date_phrase(t: str) -> bool:
    t = t.lower()
    VAGUE = [
        "w przyszłym tygodniu", "w przyszlym tygodniu",
        "w tym tygodniu", "w nadchodzącym tygodniu", "w nadchodzacym tygodniu",
        "w weekend", "w ten weekend",
        "w następnym tygodniu", "w nastepnym tygodniu",
        "w przyszłym miesiacu", "w przyszlym miesiacu",
        "w tym miesiącu", "w tym miesiacu"
    ]
    return any(p in t for p in VAGUE)

def suggest_day_options(base: datetime, how_many=3, prefer_next_week=False) -> list[datetime]:
    """
    Zwraca listę 2–3 najbliższych DNI (dat) do zaproponowania.
    - prefer_next_week=True => start od najbliższego poniedziałku przyszłego tygodnia
    - inaczej start od jutra
    Pomija dni przeszłe.
    """
    now = datetime.now()
    start = now + timedelta(days=1)

    if prefer_next_week:
        # najbliższy poniedziałek PRZYSZŁEGO tygodnia
        next_mon = now + timedelta(days=(7 - now.weekday()) % 7)  # najbliższy poniedziałek (dziś -> dziś)
        if next_mon.date() <= now.date():  # gdy dziś poniedziałek – weź kolejny
            next_mon = next_mon + timedelta(days=7)
        start = next_mon

    # zbierz kolejne 7 dni i wybierz dni robocze (wt–pt); możesz zmienić wg potrzeb
    picks = []
    d = start
    while len(picks) < how_many and (d - start).days < 14:
        # pomin wczoraj / przeszłość
        if d.date() > now.date():
            picks.append(d)
        d += timedelta(days=1)

    return picks[:how_many]

def format_options_message(options: list[datetime], time_hint: str | None = None, people_hint: int | None = None) -> str:
    parts = []
    for dt in options:
        parts.append(dt.strftime("%A %d.%m").capitalize())
    joined = " lub ".join(parts)
    # z time_hint (np. "17:00") i people_hint (np. 3)
    suffix = ""
    if time_hint:
        suffix += f" o {time_hint[:5]}"
    if people_hint:
        suffix += f" dla {people_hint} osób"
    return f"Czy pasuje {joined}{suffix}?"

def extract_tomorrow_or_after(text: str) -> str | None:
    t = text.lower()
    if "pojutrze" in t:
        return (datetime.now() + timedelta(days=2)).date().isoformat()
    if "jutro" in t:
        return (datetime.now() + timedelta(days=1)).date().isoformat()
    return None

def handle_reservation_step(sender_id, user_message):
    """
    Krokowa obsługa rezerwacji z prostą pamięcią:
    - Składanie informacji z wielu wiadomości
    - Dopytywanie o konkretny dzień, gdy padnie ogólnik ('w przyszłym tygodniu')
    - Zapamiętywanie zaproponowanych opcji dni (suggested_options) i czekanie na wybór
    - Krótkie potwierdzenie ('tak/ok') przed zapisaniem pending
    """
    txt = normalize_text(user_message)

    # Szybki reset stanu, gdy user to komunikuje
    if any(k in txt for k in ["reset", "od nowa", "zacznijmy od nowa", "zmień termin", "zmien termin"]):
        fresh = {
            "people": None, "date": None, "time": None,
            "awaiting_confirmation": False,
            "awaiting_day_choice": False,
            "suggested_options": [],
            "details": None, "status": "in_progress",
            "updated_at": datetime.now().isoformat(),
        }
        table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **fresh})
        return "Zaczynamy od nowa 😊 Podaj proszę liczbę osób."

    # Wczytaj stan
    current = table.get_item(Key={"user_id": sender_id, "reservation_id": "current"}).get("Item")
    if not current:
        current = {
            "people": None, "date": None, "time": None,
            "awaiting_confirmation": False,
            "awaiting_day_choice": False,
            "suggested_options": [],
            "details": None, "status": "in_progress"
        }
        # Start wizard mode: ask sequentially for date -> time -> people
        current["wizard"] = True
        current["wizard_step"] = "date"
        current["updated_at"] = datetime.now().isoformat()
        table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
        return "Za chwilę dostaniesz serię pytań do rezerwacji. Najpierw podaj proszę datę (np. 09.09 lub 9 września)."

    # [NOWE] Jeśli nie czekamy na potwierdzenie/wybór i użytkownik pyta o FAQ — nie pchaj rezerwacji
    if True:
        if is_faq_query(user_message):
            return "FAQ_BYPASS"

    # Uzytkownik potwierdza tekstowo, finalnie potwierdza wlasciciel w Telegramie.

    # Jesli kreator jest na kroku 'duration', najpierw sprawdz dostępność w Google Calendar, zanim poprosisz o potwierdzenie.
    try:
        if current.get("wizard") and current.get("wizard_step") == "duration" and not current.get("awaiting_confirmation"):
            dur_try = parse_duration_hours(txt)
            if dur_try:
                current["duration"] = dur_try
                full_dt = datetime.fromisoformat(current["date"] + "T" + current["time"])
                is_free, err = check_availability_in_calendar(full_dt, duration_hours=dur_try)
                if is_free is None:
                    return "⚠️ Nie udało się sprawdzić dostępności. Podaj proszę inny termin albo spróbuj ponownie za chwilę."
                if not is_free:
                    # proponuj wolne okna i wróć do wyboru godziny
                    current["awaiting_confirmation"] = False
                    current["wizard_step"] = "time"
                    current["updated_at"] = datetime.now().isoformat()
                    table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                    free_ranges = compute_free_ranges_for_day(full_dt, dur_try)
                    opts = format_free_ranges(free_ranges, max_items=3)
                    return f"Ten termin jest zajety. Dostepne przedzialy tego dnia (min {dur_try}h): {opts}. Podaj prosze inna godzine z powyzszych lub inny dzien."
                # wolne -> pros o potwierdzenie
                current["wizard_step"] = "confirm"
                current["awaiting_confirmation"] = True
                current["updated_at"] = datetime.now().isoformat()
                table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                human_dt = full_dt.strftime("%d.%m.%Y %H:%M")
                return f"Prosze o potwierdzenie: zarezerwowac warsztat dla {current['people']} osob na {dur_try} godz. w dniu {human_dt}? Odpowiedz 'Tak', aby przejsc dalej."
    except Exception:
        pass

    # Jeśli mamy juz date, godzine i osoby, ale brak czasu trwania -> zapytaj o duration
    try:
        if (
            current.get("date") and current.get("time") and current.get("people")
            and not current.get("duration") and not current.get("awaiting_day_choice")
            and not current.get("awaiting_confirmation")
        ):
            return "Na ile godzin chcesz zarezerwowac? Rekomendujemy 2 godziny. Napisz np. '2 godziny'."
    except Exception:
        pass

    # Wizard mode: sequential questions date -> time -> people -> confirm
    if current.get("wizard"):
        step = current.get("wizard_step", "date")

        # cancel/back controls
        if any(k in txt for k in ["anuluj", "przerwij", "cancel"]):
            table.delete_item(Key={"user_id": sender_id, "reservation_id": "current"})
            return "Anulowano rezerwacje. Napisz, gdy bedziesz gotowy."
        if any(k in txt for k in ["wstecz", "cofnij", "back"]):
            order = ["date", "time", "people", "duration", "confirm"]
            if step in order:
                i = order.index(step)
                step = order[max(0, i-1)]
                current["wizard_step"] = step

        if step == "date":
            nd = extract_concrete_date(txt)
            if not nd:
                wd = extract_weekday(txt)
                if wd is not None:
                    nd = resolve_weekday_to_date(wd)
            if not nd:
                quick = extract_tomorrow_or_after(txt)
                if quick:
                    nd = quick
            if nd:
                # Reject past dates
                if _is_past_date_iso(nd):
                    current["awaiting_day_choice"] = True
                    current["wizard_step"] = "date"
                    # suggest 2–3 upcoming days
                    opts_days = suggest_day_options(datetime.now(), how_many=3, prefer_next_week=False)
                    current["suggested_options"] = [o.date().isoformat() for o in opts_days]
                    current["updated_at"] = datetime.now().isoformat()
                    table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                    msg = format_options_message(opts_days, time_hint=None, people_hint=current.get("people"))
                    return f"Nie można rezerwować terminu z przeszłości. {msg} Odpowiedz 1, 2 lub 3 albo wpisz przyszłą datę."
                current["date"] = nd
                current["wizard_step"] = "time"
                current["updated_at"] = datetime.now().isoformat()
                table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                return "Dzięki! Podaj proszę godzinę (np. 17:00)."
            else:
                return "Podaj proszę konkretną datę (np. 09.09 lub 9 września)."

        if step == "time":
            tm = extract_time(txt)
            if tm:
                # If date already chosen, ensure combined datetime is not in the past
                if current.get("date") and _is_past_datetime(current["date"], tm):
                    # Keep date, just ask for a future time
                    current["time"] = None
                    current["wizard_step"] = "time"
                    current["updated_at"] = datetime.now().isoformat()
                    table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                    return "Ta godzina już minęła. Podaj proszę przyszłą godzinę (np. 17:00)."
                current["time"] = tm
                current["wizard_step"] = "people"
                current["updated_at"] = datetime.now().isoformat()
                table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                return "Ile osób ma wziąć udział? (np. 'dla 2 osób' lub '2 osoby')."
            else:
                return "Podaj proszę godzinę rezerwacji (np. 17:00)."

        if step == "people":
            new_p = parse_people_count(txt)
            if new_p:
                current["people"] = new_p
                current["details"] = f"Warsztat dla {new_p} osob"
                # Ask for duration next
                current["wizard_step"] = "duration"
                current["updated_at"] = datetime.now().isoformat()
                table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                return "Na ile godzin chcesz zarezerwowac zajecia? Dla jednego zdjęcia rekomendujemy 2 godziny. Wpisz proszę ilość godzin (np. '2 godziny' lub samo '2')."
            else:
                return "Podaj proszę liczbę osób (np. 'dla 2 osób' lub '2 osoby')."

        if step == "duration":
            dur = parse_duration_hours(txt)
            if dur:
                # Ustal czas trwania i sprawdź dostępność w Google Calendar zanim poprosisz o potwierdzenie
                current["duration"] = dur
                full_dt = datetime.fromisoformat(current["date"] + "T" + current["time"])
                is_free, err = check_availability_in_calendar(full_dt, duration_hours=dur)
                if is_free is None:
                    return "⚠️ Nie udało się sprawdzić dostępności. Podaj proszę inny termin albo spróbuj ponownie za chwilę."
                if not is_free:
                    # Nie ustawiaj trybu potwierdzenia – poproś o inną godzinę/dzień i pokaż wolne przedziały
                    current["awaiting_confirmation"] = False
                    current["wizard_step"] = "time"
                    current["updated_at"] = datetime.now().isoformat()
                    table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                    free_ranges = compute_free_ranges_for_day(full_dt, dur)
                    opts = format_free_ranges(free_ranges, max_items=3)
                    return f"Ten termin jest zajęty. Dostępne przedziały tego dnia (min {dur}h): {opts}. Podaj proszę inną godzinę z powyższych lub inny dzień."

                # Wolne – teraz dopiero prosimy o potwierdzenie
                current["wizard_step"] = "confirm"
                current["awaiting_confirmation"] = True
                current["updated_at"] = datetime.now().isoformat()
                table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                human_dt = full_dt.strftime("%d.%m.%Y %H:%M")
                return f"Proszę o potwierdzenie: zarezerwować warsztat dla {current['people']} osób na {dur} godz. w dniu {human_dt}? Odpowiedz 'tak', aby przejść dalej."
            else:
                return "Podaj proszę czas trwania w godzinach (np. 2). Rekomendujemy 2 godziny."

        if step == "confirm":
            # allow on-the-fly corrections of date/time/people
            corrected = False
            tm = extract_time(txt)
            if tm and tm != current.get("time"):
                current["time"] = tm; corrected = True
            nd = extract_concrete_date(txt)
            if not nd:
                wd = extract_weekday(txt)
                if wd is not None:
                    nd = resolve_weekday_to_date(wd)
            if not nd:
                quick = extract_tomorrow_or_after(txt)
                if quick:
                    nd = quick
            if nd and nd != current.get("date"):
                current["date"] = nd; corrected = True
            nppl = parse_people_count(txt)
            if nppl and nppl != current.get("people"):
                current["people"] = nppl; current["details"] = f"Warsztat dla {nppl} osob"; corrected = True
            if corrected:
                current["updated_at"] = datetime.now().isoformat()
                table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                full_dt = datetime.fromisoformat(current["date"] + "T" + current["time"])
                # Ponownie sprawdź dostępność po korekcie
                dur2 = current.get("duration") or 2
                is_free, err = check_availability_in_calendar(full_dt, duration_hours=dur2)
                if is_free is None:
                    return "⚠️ Nie udało się sprawdzić dostępności. Podaj proszę inny termin albo spróbuj ponownie za chwilę."
                if not is_free:
                    free_ranges = compute_free_ranges_for_day(full_dt, dur2)
                    current["awaiting_confirmation"] = False
                    if free_ranges:
                        # Ten sam dzień ma okna -> wyczyść godzinę i poproś o inną
                        current["wizard_step"] = "time"
                        current["time"] = None
                        current["awaiting_day_choice"] = False
                        current["updated_at"] = datetime.now().isoformat()
                        table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                        opts = format_free_ranges(free_ranges, max_items=3)
                        return f"Ten termin jest zajęty. Dostępne przedziały tego dnia (min {dur2}h): {opts}. Podaj proszę inną godzinę z powyższych lub inny dzień."
                    else:
                        # Brak okien tego dnia -> poproś o inny dzień
                        current["wizard_step"] = "date"
                        current["time"] = None
                        current["date"] = None
                        current["awaiting_day_choice"] = True
                        opts_days = suggest_day_options(datetime.now(), how_many=3, prefer_next_week=False)
                        current["suggested_options"] = [o.date().isoformat() for o in opts_days]
                        current["updated_at"] = datetime.now().isoformat()
                        table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                        msg = format_options_message(opts_days, time_hint=None, people_hint=current.get("people"))
                        return f"Ten termin jest zajęty i brak wolnych okien tego dnia. {msg} Odpowiedz 1, 2 lub 3 albo wpisz konkretną datę."
                human_dt = full_dt.strftime("%d.%m.%Y %H:%M")
                return f"Zaktualizowalem szczegoly. Potwierdzic rezerwacje dla {current['people']} osob w dniu {human_dt}? Odpowiedz 'tak'."
            # else: fall-through to the usual 'tak' handling below

    # --- 1) Krótkie potwierdzenie „tak/ok” ---
    if current.get("awaiting_confirmation") and txt in {"tak", "ok", "okej", "potwierdzam", "potwierdz", "potwierdź", "zgoda", "tak.", "ok.", "ok", "yes", "y", "👍", "✅"}:
        full_date = datetime.fromisoformat(current["date"] + "T" + current["time"])
        _dur = current.get("duration") or 2
        # Past guard on confirmation
        if full_date < _now_pl_naive():
            free_ranges = compute_free_ranges_for_day(full_date, _dur)
            current["awaiting_confirmation"] = False
            if full_date.date() == _now_pl_naive().date() and free_ranges:
                current["wizard_step"] = "time"
                current["time"] = None
                current["awaiting_day_choice"] = False
                current["updated_at"] = datetime.now().isoformat()
                table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                opts = format_free_ranges(free_ranges, max_items=3)
                return f"Nie można rezerwować terminu z przeszłości. Dostępne dzisiaj (min {_dur}h): {opts}. Podaj proszę inną godzinę lub inny dzień."
            else:
                current["wizard_step"] = "date"
                current["time"] = None
                current["date"] = None
                current["awaiting_day_choice"] = True
                opts_days = suggest_day_options(datetime.now(), how_many=3, prefer_next_week=False)
                current["suggested_options"] = [o.date().isoformat() for o in opts_days]
                current["updated_at"] = datetime.now().isoformat()
                table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                msg = format_options_message(opts_days, time_hint=None, people_hint=current.get("people"))
                return f"Nie można rezerwować terminu z przeszłości. {msg} Odpowiedz 1, 2 lub 3 albo wpisz przyszłą datę."
        is_free, err = check_availability_in_calendar(full_date, duration_hours=_dur)
        if is_free is None:
            return "⚠️ Nie udało się sprawdzić dostępności. Podaj proszę inny termin albo spróbuj ponownie za chwilę."
        if not is_free:
            free_ranges = compute_free_ranges_for_day(full_date, _dur)
            current["awaiting_confirmation"] = False
            if free_ranges:
                # Ten sam dzień: wyczyść godzinę i poproś o inną
                current["wizard_step"] = "time"
                current["time"] = None
                current["awaiting_day_choice"] = False
                current["updated_at"] = datetime.now().isoformat()
                table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                opts = format_free_ranges(free_ranges, max_items=3)
                return f"Ten termin jest zajety. Dostepne przedzialy tego dnia (min {_dur}h): {opts}. Podaj prosze inna godzine z powyzszych lub inny dzien."
            else:
                # Brak okien tego dnia: poproś o inny dzień i zaproponuj 2–3
                current["wizard_step"] = "date"
                current["time"] = None
                current["date"] = None
                current["awaiting_day_choice"] = True
                opts_days = suggest_day_options(datetime.now(), how_many=3, prefer_next_week=False)
                current["suggested_options"] = [o.date().isoformat() for o in opts_days]
                current["updated_at"] = datetime.now().isoformat()
                table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                msg = format_options_message(opts_days, time_hint=None, people_hint=current.get("people"))
                return f"Ten termin jest zajety i brak wolnych okien tego dnia. {msg} Odpowiedz 1, 2 lub 3 albo wpisz konkretną datę."

        # zapis pending + powiadomienie
        pending = save_reservation(sender_id, {
            "people": current["people"],
            "date": current["date"],
            "time": current["time"],
            "details": current.get("details") or f"Warsztat dla {current['people']} osób"
        }, status="pending")

        if pending:
            threading.Thread(
                target=send_telegram_notification_sync,
                args=(
                    pending["reservation_id"],
                    sender_id,
                    f"{pending.get('details')} • {pending.get('people')} os • {pending.get('duration')} h",
                    pending["date"],
                )
            ).start()
            current["awaiting_confirmation"] = False
            current["updated_at"] = datetime.now().isoformat()
            table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
            return "✅ Rezerwacja oczekuje na potwierdzenie."
        else:
            return "⚠️ Nie udało się zapisać rezerwacji. Spróbuj proszę ponownie."

    # --- 2) Oczekiwanie na wybór dnia z zaproponowanych opcji ---
    if current.get("awaiting_day_choice") and current.get("suggested_options"):
        # wybór 1/2/3 lub 'pierwsza/druga/trzecia'
        idx_map = {
            "1": 0, "pierwsza": 0, "pierwszy": 0,
            "2": 1, "druga": 1, "drugi": 1,
            "3": 2, "trzecia": 2, "trzeci": 2
        }
        if txt in idx_map and idx_map[txt] < len(current["suggested_options"]):
            chosen_iso = current["suggested_options"][idx_map[txt]]
            # Past date guard
            if _is_past_date_iso(chosen_iso):
                # keep awaiting_day_choice and re-present options
                opts_dt = [datetime.fromisoformat(o + "T00:00:00") for o in current["suggested_options"]]
                msg = format_options_message(opts_dt, time_hint=(current["time"][:5] if current.get("time") else None),
                                             people_hint=current.get("people"))
                return f"Nie można rezerwować terminu z przeszłości. {msg} Odpowiedz 1, 2 lub 3 albo wpisz przyszłą datę."
            current["date"] = chosen_iso
            current["awaiting_day_choice"] = False
            current["suggested_options"] = []
        else:
            # pozwól też na wpisanie pełnej daty lub nazwy dnia (nadpisze wybór)
            d = extract_concrete_date(txt)
            if d:
                if _is_past_date_iso(d):
                    opts_dt = [datetime.fromisoformat(o + "T00:00:00") for o in current["suggested_options"]]
                    msg = format_options_message(opts_dt, time_hint=(current["time"][:5] if current.get("time") else None),
                                                 people_hint=current.get("people"))
                    return f"Nie można rezerwować terminu z przeszłości. {msg} Odpowiedz 1, 2 lub 3 albo wpisz przyszłą datę."
                current["date"] = d
                current["awaiting_day_choice"] = False
                current["suggested_options"] = []
            else:
                wd = extract_weekday(txt)
                if wd is not None:
                    nd2 = resolve_weekday_to_date(wd)
                    if _is_past_date_iso(nd2):
                        opts_dt = [datetime.fromisoformat(o + "T00:00:00") for o in current["suggested_options"]]
                        msg = format_options_message(opts_dt, time_hint=(current["time"][:5] if current.get("time") else None),
                                                     people_hint=current.get("people"))
                        return f"Nie można rezerwować terminu z przeszłości. {msg} Odpowiedz 1, 2 lub 3 albo wpisz przyszłą datę."
                    current["date"] = nd2
                    current["awaiting_day_choice"] = False
                    current["suggested_options"] = []
                else:
                    # nie rozpoznano wyboru – przypomnij opcje
                    # sformatuj ponownie propozycje
                    opts_dt = [datetime.fromisoformat(o + "T00:00:00") for o in current["suggested_options"]]
                    msg = format_options_message(
                        opts_dt,
                        time_hint=(current["time"][:5] if current.get("time") else None),
                        people_hint=current.get("people")
                    )
                    current["updated_at"] = datetime.now().isoformat()
                    table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                    return f"Nie rozpoznałem wyboru. Odpowiedz 1/2/3 albo wpisz konkretną datę. {msg}"

    # --- 3) Uzupełnianie braków z bieżącej wiadomości ---
    if current["people"] is None:
        p = extract_people(txt)
        if p:
            current["people"] = p

    if current["time"] is None:
        tm = extract_time(txt)
        if tm:
            current["time"] = tm

    if current["date"] is None:
        # 'jutro/pojutrze'
        quick = extract_tomorrow_or_after(txt)
        if quick:
            current["date"] = quick
        else:
            # konkretna data
            d = extract_concrete_date(txt)
            if d:
                if _is_past_date_iso(d):
                    # propose options and keep asking for future date
                    opts = suggest_day_options(datetime.now(), how_many=3, prefer_next_week=False)
                    current["awaiting_day_choice"] = True
                    current["suggested_options"] = [o.date().isoformat() for o in opts]
                    current["updated_at"] = datetime.now().isoformat()
                    table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                    msg = format_options_message(opts, time_hint=(current["time"][:5] if current.get("time") else None),
                                                 people_hint=current.get("people"))
                    return f"Nie można rezerwować terminu z przeszłości. {msg} Odpowiedz 1, 2 lub 3 albo wpisz przyszłą datę."
                current["date"] = d
            else:
                # nazwa dnia tygodnia
                wd = extract_weekday(txt)
                if wd is not None:
                    nd3 = resolve_weekday_to_date(wd)
                    if _is_past_date_iso(nd3):
                        opts = suggest_day_options(datetime.now(), how_many=3, prefer_next_week=False)
                        current["awaiting_day_choice"] = True
                        current["suggested_options"] = [o.date().isoformat() for o in opts]
                        current["updated_at"] = datetime.now().isoformat()
                        table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                        msg = format_options_message(opts, time_hint=(current["time"][:5] if current.get("time") else None),
                                                     people_hint=current.get("people"))
                        return f"Nie można rezerwować terminu z przeszłości. {msg} Odpowiedz 1, 2 lub 3 albo wpisz przyszłą datę."
                    current["date"] = nd3
                else:
                    # ogólnik → zaproponuj 2–3 opcje i zapamiętaj je
                    if is_vague_date_phrase(txt):
                        prefer_next = any(k in txt for k in ["przysz", "nastepn"])
                        opts = suggest_day_options(datetime.now(), how_many=3, prefer_next_week=prefer_next)
                        current["awaiting_day_choice"] = True
                        current["suggested_options"] = [o.date().isoformat() for o in opts]
                        # zbuduj wiadomość z opcjami
                        msg = format_options_message(
                            opts,
                            time_hint=(current["time"][:5] if current.get("time") else None),
                            people_hint=current.get("people")
                        )
                        # zapisz i wróć z pytaniem
                        if current.get("people"):
                            current["details"] = f"Warsztat dla {current['people']} osób"
                        current["updated_at"] = datetime.now().isoformat()
                        table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
                        return f"Potrzebuję konkretnego dnia. {msg} Odpowiedz 1, 2 lub 3."

    # --- 4) Zapisz stan i pytania naprowadzające ---
    if current.get("people"):
        current["details"] = f"Warsztat dla {current['people']} osób"
    current["updated_at"] = datetime.now().isoformat()
    table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})

    if not current["people"]:
        return "Proszę podać, ile osób ma wziąć udział w warsztacie."
    if not current["date"] and not current["time"]:
        return "Podaj proszę dzień i godzinę (np. „wtorek 17:00”)."
    if not current["date"]:
        if current.get("awaiting_day_choice"):
            # już zaproponowaliśmy opcje
            opts_dt = [datetime.fromisoformat(o + "T00:00:00") for o in current.get("suggested_options", [])]
            msg = format_options_message(opts_dt, time_hint=(current["time"][:5] if current.get("time") else None),
                                         people_hint=current.get("people"))
            return f"Potrzebuję konkretnego dnia. {msg} Odpowiedz 1, 2 lub 3."
        return "Proszę podać konkretną datę (np. „9 września”)."
    if not current["time"]:
        return "Proszę podać godzinę rezerwacji (np. „18:00”)."

    # --- 5) Mamy komplet → najpierw walidacja przyszłości, potem sprawdź dostępność i poproś o potwierdzenie ---
    full_dt = datetime.fromisoformat(current["date"] + "T" + current["time"])
    _dur2 = current.get("duration") or 2
    # Guard: do not allow booking in the past
    if full_dt < _now_pl_naive():
        free_ranges = compute_free_ranges_for_day(full_dt, _dur2)
        if full_dt.date() == _now_pl_naive().date() and free_ranges:
            current["awaiting_confirmation"] = False
            current["wizard_step"] = "time"
            current["time"] = None
            current["awaiting_day_choice"] = False
            current["updated_at"] = datetime.now().isoformat()
            table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
            opts = format_free_ranges(free_ranges, max_items=3)
            return f"Nie można rezerwować terminu z przeszłości. Dostępne dzisiaj (min {_dur2}h): {opts}. Podaj proszę inną godzinę lub inny dzień."
        else:
            current["awaiting_confirmation"] = False
            current["wizard_step"] = "date"
            current["time"] = None
            current["date"] = None
            current["awaiting_day_choice"] = True
            opts_days = suggest_day_options(datetime.now(), how_many=3, prefer_next_week=False)
            current["suggested_options"] = [o.date().isoformat() for o in opts_days]
            current["updated_at"] = datetime.now().isoformat()
            table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
            msg = format_options_message(opts_days, time_hint=None, people_hint=current.get("people"))
            return f"Nie można rezerwować terminu z przeszłości. {msg} Odpowiedz 1, 2 lub 3 albo wpisz przyszłą datę."
    is_free, err = check_availability_in_calendar(full_dt, duration_hours=_dur2)
    if is_free is None:
        return "⚠️ Nie udało się sprawdzić dostępności. Podaj proszę inny termin albo spróbuj ponownie za chwilę."
    if is_free:
        human_dt = full_dt.strftime("%d.%m.%Y %H:%M")
        current["awaiting_confirmation"] = True
        current["awaiting_day_choice"] = False
        current["suggested_options"] = []
        current["updated_at"] = datetime.now().isoformat()
        table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
        return f"Proszę o potwierdzenie: zarezerwować warsztat dla {current['people']} osób w dniu {human_dt}? Odpowiedz „tak”, aby przejść dalej."
    else:
        # Termin zajęty: zaproponuj inne godziny tego dnia lub inne dni i utrzymaj flow
        free_ranges = compute_free_ranges_for_day(full_dt, _dur2)
        if free_ranges:
            current["awaiting_confirmation"] = False
            current["wizard_step"] = "time"
            current["time"] = None
            current["awaiting_day_choice"] = False
            current["updated_at"] = datetime.now().isoformat()
            table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
            opts = format_free_ranges(free_ranges, max_items=3)
            return f"Ten termin jest zajety. Dostepne przedzialy tego dnia (min {_dur2}h): {opts}. Podaj prosze inna godzine z powyzszych lub inny dzien."
        else:
            current["awaiting_confirmation"] = False
            current["wizard_step"] = "date"
            current["time"] = None
            current["date"] = None
            current["awaiting_day_choice"] = True
            opts_days = suggest_day_options(datetime.now(), how_many=3, prefer_next_week=False)
            current["suggested_options"] = [o.date().isoformat() for o in opts_days]
            current["updated_at"] = datetime.now().isoformat()
            table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **current})
            msg = format_options_message(opts_days, time_hint=None, people_hint=current.get("people"))
            return f"Ten termin jest zajety i brak wolnych okien tego dnia. {msg} Odpowiedz 1, 2 lub 3 albo wpisz konkretną datę."


async def send_telegram_reservation_notification(reservation_id, user_id, reservation_details, date):
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
          await (TELEGRAM_APP.bot if 'TELEGRAM_APP' in globals() and TELEGRAM_APP is not None else telegram_bot).send_message(
              chat_id=OWNER_CHAT_ID,
              text=message,
              reply_markup=reply_markup,
              parse_mode='Markdown'
          )
          logging.info(f"✅ Telegram notification sent for reservation {reservation_id}")
    except Exception as e:
          logging.error(f"❌ Error sending Telegram notification: {e}")

async def handle_telegram_callback(update, context):
    query = update.callback_query
    await query.answer()

    action, reservation_id, user_id = query.data.split('_', 2)

    if action == "confirm":
        # pobierz rezerwację "current"
        current = table.get_item(
            Key={"user_id": user_id, "reservation_id": "current"}
        ).get("Item")

        if current:
            # wygeneruj nowe ID dla finalnej rezerwacji
            new_id = str(datetime.now().timestamp())
            full_date = datetime.fromisoformat(current["date"] + "T" + current["time"])

            # przenieś do DynamoDB jako confirmed
            table.put_item(
                Item={
                    "user_id": user_id,
                    "reservation_id": new_id,
                    "details": current.get("user_name") or current.get("details") or f"Klient {user_id}",
                    "date": full_date.isoformat(),
                    "people": current.get("people"),
                    "duration": current.get("duration") or 2,
                    "status": "confirmed",
                    "reminded": False,
                }
            )

            # usuń tymczasową rezerwację
            table.delete_item(
                Key={"user_id": user_id, "reservation_id": "current"}
            )

            # dodaj do Google Calendar
            add_to_google_calendar(
                details=current.get("user_name") or current.get("details") or f"Klient {user_id}",
                date=full_date,
                user_id=user_id,
                people=current.get("people"),
                duration_hours=current.get("duration") or 2,
            )

            # wyślij potwierdzenie do klienta
            send_message(user_id, f"✅ Twoja rezerwacja została potwierdzona. Zapraszamy w dniu {full_date.strftime('%d.%m.%Y %H:%M')}")

            # odpowiedź w Telegramie
            await query.edit_message_text("✅ Rezerwacja potwierdzona!")

    elif action == "reject":
        # usuń rezerwację "current"
        table.delete_item(
            Key={"user_id": user_id, "reservation_id": "current"}
        )
        send_message(user_id, "❌ Twoja rezerwacja została odrzucona.")
        await query.edit_message_text("❌ Rezerwacja odrzucona!")


def send_telegram_notification_sync(reservation_id, user_id, details, date):
    """Synchronous wrapper for Telegram notification using the global Telegram loop."""
    try:
        # Use existing global loop if available; else fallback to thread + loop
        global TELEGRAM_LOOP
        if TELEGRAM_LOOP is None:
            # initialize a dedicated loop in a background thread lazily
            def _runner():
                loop = asyncio.new_event_loop()
                globals()['TELEGRAM_LOOP'] = loop
                asyncio.set_event_loop(loop)
                loop.run_forever()
            thr = threading.Thread(target=_runner, daemon=True)
            thr.start()
            # small wait to ensure loop is ready
            for _ in range(20):
                if TELEGRAM_LOOP is not None:
                    break
                time.sleep(0.05)
        # Try send with small retry on pool saturation
        last_err = None
        for _ in range(3):
            fut = asyncio.run_coroutine_threadsafe(
                send_telegram_reservation_notification(reservation_id, user_id, details, date),
                TELEGRAM_LOOP,
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

# ---- Reservations ----
def save_reservation(user_id, reservation_data, status="pending"):
    """
    Zapisz rezerwację do DynamoDB (gdy wszystkie dane są kompletne).
    reservation_data powinno zawierać: people, date, time, details.
    """
    try:
        reservation_id = str(datetime.now().timestamp())
        full_date = datetime.fromisoformat(reservation_data["date"] + "T" + reservation_data["time"])

        # Enrich missing fields from current state
        try:
            cur = table.get_item(Key={"user_id": user_id, "reservation_id": "current"}).get("Item")
        except Exception:
            cur = None

        people = reservation_data.get("people") or (cur.get("people") if cur else None)
        duration = reservation_data.get("duration") or (cur.get("duration") if cur else 2)
        user_name = reservation_data.get("user_name") or (cur.get("user_name") if cur else None)
        details = reservation_data.get("details")
        if not details:
            # prefer display name
            details = user_name or get_user_display_name(user_id) or f"Klient {user_id}"

        # Zapisz do DynamoDB
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

        return {
            "reservation_id": reservation_id,
            "details": details,
            "date": full_date,
            "people": people,
            "duration": duration,
            "status": status,
        }

    except ClientError as e:
        logging.error(e)
        return None

def confirm_current_reservation(user_id: str) -> str:
    """Finalize current reservation: check availability, add to Google Calendar, mark confirmed."""
    try:
        current = table.get_item(Key={"user_id": user_id, "reservation_id": "current"}).get("Item")
    except ClientError as e:
        logging.error(f"DynamoDB get_item error: {e}")
        current = None

    if not current:
        return "Nie znaleziono aktywnej rezerwacji do potwierdzenia."

    if not current.get("date") or not current.get("time") or not current.get("people"):
        return "Brakuje danych rezerwacji. Podaj prosze date, godzine i liczbe osob."

    try:
        full_date = datetime.fromisoformat(current["date"] + "T" + current["time"])
    except Exception:
        return "Nieprawidlowy format daty/godziny. Podaj prosze ponownie."

    is_free, err = check_availability_in_calendar(full_date, duration_hours=(current.get("duration") or 2))
    if is_free is None:
        return "Nie udalo sie sprawdzic dostepnosci. Sprobuj ponownie pozniej."
    if not is_free:
        # reset awaiting flag so user can choose another time
        current["awaiting_confirmation"] = False
        current["updated_at"] = datetime.now().isoformat()
        table.put_item(Item={"user_id": user_id, "reservation_id": "current", **current})
        return "Ten termin jest juz zajety. Podaj prosze inny dzien lub godzine."

    # Save confirmed reservation, remove current, add to Calendar
    try:
        reservation_id = str(datetime.now().timestamp())
        details = current.get("details") or f"Warsztat dla {current.get('people')} osob"
        table.put_item(
            Item={
                "user_id": user_id,
                "reservation_id": reservation_id,
                "details": details,
                "date": full_date.isoformat(),
                "status": "confirmed",
                "reminded": False,
            }
        )
        table.delete_item(Key={"user_id": user_id, "reservation_id": "current"})
        add_to_google_calendar(details, full_date, user_id)
        return f"✅ Twoja rezerwacja zostala potwierdzona. Zapraszamy w dniu {full_date.strftime('%d.%m.%Y %H:%M')}"
    except Exception as e:
        logging.error(f"Finalize/Calendar error: {e}")
        return "Nie udalo sie potwierdzic rezerwacji. Sprobuj prosze ponownie."

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
    """Setup Telegram bot handlers and return Application (without starting)."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CallbackQueryHandler(handle_telegram_callback))
    return application

def _telegram_loop_runner():
    """Run Telegram bot polling in a dedicated event loop/thread."""
    global TELEGRAM_APP, TELEGRAM_LOOP
    loop = asyncio.new_event_loop()
    TELEGRAM_LOOP = loop
    asyncio.set_event_loop(loop)
    try:
        # Build application and start polling
        app = loop.run_until_complete(setup_telegram_bot())
        TELEGRAM_APP = app
        # Ensure webhook is disabled before polling
        try:
            loop.run_until_complete(app.bot.delete_webhook(drop_pending_updates=True))
        except Exception:
            pass
        # Run polling (blocks until stop is called)
        loop.run_until_complete(app.run_polling())
    finally:
        try:
            if TELEGRAM_APP is not None:
                loop.run_until_complete(TELEGRAM_APP.stop())
                loop.run_until_complete(TELEGRAM_APP.shutdown())
        except Exception:
            pass
        loop.close()

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
@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    # --- Weryfikacja webhooka (Instagram/Messenger) ---
    if request.method == 'GET':
        if request.args.get('hub.verify_token') == VERIFY_TOKEN:
            return request.args.get('hub.challenge')
        return 'Invalid token', 403

    # --- Obsługa zdarzeń ---
    if request.method == 'POST':
        try:
            data = request.get_json()
            logging.info(f'📥 Webhook received: {json.dumps(data, indent=2)}')

            if not data or data.get('object') not in ('instagram', 'page'):
                return 'IGNORED', 200

            for entry in data.get('entry', []):
                for messaging_event in entry.get('messaging', []):
                    # Obsługa postback (np. z przycisków IG)
                    sender_id = messaging_event['sender']['id']
                    if 'postback' in messaging_event:
                        try:
                            payload = messaging_event['postback'].get('payload')
                        except Exception:
                            payload = None
                        if payload in ("CONFIRM", "IG_CONFIRM"):
                            # Treat as user's confirmation -> create pending and notify owner via Telegram
                            reply = handle_reservation_step(sender_id, "tak")
                            send_message(sender_id, reply)
                            continue
                        if payload in ("REJECT", "IG_REJECT"):
                            try:
                                table.delete_item(Key={"user_id": sender_id, "reservation_id": "current"})
                            except Exception:
                                pass
                            send_message(sender_id, "Odrzucono rezerwacje. Jesli chcesz, podaj inny termin.")
                            continue

                    # ignoruj zdarzenia bez message (np. read/delivery)
                    if 'message' not in messaging_event:
                        continue

                    msg = messaging_event['message']

                    # ignoruj echa własnych wiadomości
                    if msg.get('is_echo', False):
                        continue

                    # Quick replies (IG/Messenger)
                    if isinstance(msg, dict) and msg.get('quick_reply'):
                        payload = msg['quick_reply'].get('payload')
                        if payload in ("CONFIRM", "IG_CONFIRM"):
                            reply = handle_reservation_step(sender_id, "tak")
                            send_message(sender_id, reply)
                            continue
                        if payload in ("REJECT", "IG_REJECT"):
                            try:
                                table.delete_item(Key={"user_id": sender_id, "reservation_id": "current"})
                            except Exception:
                                pass
                            send_message(sender_id, "Odrzucono rezerwacje. Jesli chcesz, podaj inny termin.")
                            continue

                    # tylko tekst
                    user_message = msg.get('text')
                    if not user_message:
                        continue

                    logging.info(f'💬 Message from {sender_id}: {user_message}')

                    # --- Czy to rezerwacja (intencja) albo mamy już otwarty proces? ---
                    try:
                        active = table.get_item(
                            Key={"user_id": sender_id, "reservation_id": "current"}
                        ).get("Item")
                        # [NOWE] Auto-wygaśnięcie stanu po 24h braku aktywności
                        if active and active.get("updated_at"):
                            try:
                                if datetime.fromisoformat(active["updated_at"]) < datetime.now() - timedelta(hours=2):
                                    table.delete_item(Key={"user_id": sender_id, "reservation_id": "current"})
                                    active = None
                            except Exception:
                                pass
                    except ClientError as e:
                        logging.error(f"DynamoDB get_item error: {e}")
                        active = None

                    lower = user_message.lower()

                    # Corrections while awaiting confirmation: update time/date/people if provided
                    if active and active.get("awaiting_confirmation"):
                        corrected = False
                        tm = extract_time(user_message)
                        if tm and tm != active.get("time"):
                            active["time"] = tm
                            corrected = True
                        nd = extract_concrete_date(user_message)
                        if not nd:
                            wd = extract_weekday(user_message)
                            if wd is not None:
                                nd = resolve_weekday_to_date(wd)
                        if not nd:
                            quick = extract_tomorrow_or_after(user_message)
                            if quick:
                                nd = quick
                        if nd and nd != active.get("date"):
                            active["date"] = nd
                            corrected = True
                        # strict people extraction (avoid bare numbers like day of month)
                        new_p = None
                        m1 = re.search(r"\b(?:dla|na)\s*(\d{1,2})\s*(?:osób|osoby|osoba|os)?\b", user_message, re.IGNORECASE)
                        if m1:
                            new_p = int(m1.group(1))
                        else:
                            m2 = re.search(r"\b(\d{1,2})\s*(?:osób|osoby|osoba|os)\b", user_message, re.IGNORECASE)
                            if m2:
                                new_p = int(m2.group(1))
                        if new_p and new_p != active.get("people"):
                            active["people"] = new_p
                            corrected = True

                        if corrected and active.get("date") and active.get("time") and active.get("people"):
                            active["updated_at"] = datetime.now().isoformat()
                            table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **active})
                            full_dt = datetime.fromisoformat(active["date"] + "T" + active["time"])
                            # Sprawdź dostępność w Google Calendar przed ponownym proszeniem o potwierdzenie
                            dur3 = active.get("duration") or 2
                            is_free, err = check_availability_in_calendar(full_dt, duration_hours=dur3)
                            if is_free is None:
                                send_message(sender_id, "⚠️ Nie udało się sprawdzić dostępności. Podaj proszę inny termin albo spróbuj ponownie za chwilę.")
                                continue
                            if not is_free:
                                # Zaproponuj okna czasowe i wróć do wyboru godziny
                                active["awaiting_confirmation"] = False
                                active["wizard_step"] = "time"
                                active["updated_at"] = datetime.now().isoformat()
                                table.put_item(Item={"user_id": sender_id, "reservation_id": "current", **active})
                                free_ranges = compute_free_ranges_for_day(full_dt, dur3)
                                opts = format_free_ranges(free_ranges, max_items=3)
                                send_message(sender_id, f"Ten termin jest zajęty. Dostępne przedziały tego dnia (min {dur3}h): {opts}. Podaj proszę inną godzinę z powyższych lub inny dzień.")
                                continue
                            human_dt = full_dt.strftime("%d.%m.%Y %H:%M")
                            try:
                                quicks = [
                                    {"content_type": "text", "title": "Potwierdź", "payload": "CONFIRM"},
                                    {"content_type": "text", "title": "Odrzuć", "payload": "REJECT"},
                                ]
                                send_quick_replies(sender_id, f"Zaktualizowalem szczegoly. Potwierdzic rezerwacje dla {active['people']} osob w dniu {human_dt}?", quicks)
                            except Exception as _e:
                                logging.error(f"Failed to send quick replies: {_e}")
                            send_message(sender_id, f"Zaktualizowałem szczegóły. Potwierdzić rezerwację dla {active['people']} osób w dniu {human_dt}? Odpowiedz \u201etak\u201d, aby przejść dalej.")
                            continue
                    intents_rez = any(k in lower for k in [
                        "rezerw", "zarezerw", "termin", "terminy",
                        "wolne terminy", "dostępność", "dostepnosc",
                        "book", "booking", "zapisać", "zapisać się", "zapisy"
                    ])

                    # Najpierw: jeśli to rezerwacja lub naprawdę trzeba kontynuować flow
                    if intents_rez or needs_reservation_input(active):
                        reply = handle_reservation_step(sender_id, user_message)
                        if reply == "FAQ_BYPASS":
                            # użytkownik zapytał o FAQ w trakcie, odpowiadamy FAQ i nie ruszamy rezerwacji
                            try:
                                response_text = generate_response(user_message)
                            except Exception as e:
                                logging.error(f'OpenAI error: {e}', exc_info=True)
                                response_text = 'Dziękuję za wiadomość! Właściciel studia skontaktuje się z Tobą wkrótce. 🏺'
                            send_message(sender_id, response_text)
                            continue
                        else:
                            # If awaiting confirmation, present IG quick replies
                            try:
                                active2 = table.get_item(Key={"user_id": sender_id, "reservation_id": "current"}).get("Item")
                            except Exception:
                                active2 = None
                            if active2 and active2.get("awaiting_confirmation"):
                                quicks = [
                                    {"content_type": "text", "title": "Potwierdź", "payload": "CONFIRM"},
                                    {"content_type": "text", "title": "Odrzuć", "payload": "REJECT"},
                                ]
                                send_quick_replies(sender_id, reply, quicks)
                            else:
                                send_message(sender_id, reply)
                            continue

                    # Jeśli to czyste FAQ – odpowiadamy FAQ (nawet jeśli "current" istnieje)
                    if is_faq_query(user_message):
                        try:
                            response_text = generate_response(user_message)
                        except Exception as e:
                            logging.error(f'OpenAI error: {e}', exc_info=True)
                            response_text = 'Dziękuję za wiadomość! Właściciel studia skontaktuje się z Tobą wkrótce. 🏺'
                        send_message(sender_id, response_text)
                        continue

                    # --- Inne wiadomości: odpowiedź AI (FAQ, pytania ogólne) ---
                    try:
                        response_text = generate_response(user_message)
                    except Exception as e:
                        logging.error(f'OpenAI error: {e}', exc_info=True)
                        response_text = 'Dziękuję za wiadomość! Właściciel studia skontaktuje się z Tobą wkrótce. 🏺'

                    send_message(sender_id, response_text)

        except Exception as e:
            logging.error(f'❌ Webhook error: {e}', exc_info=True)
            return 'ERROR', 500

        return 'OK', 200


# ---- Admin Maintenance ----
@app.route('/admin/clear_cache', methods=['POST', 'GET'])
def admin_clear_cache():
    token = request.args.get('token') or request.headers.get('X-Admin-Token')
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        return 'Forbidden', 403
    user_id = request.args.get('user_id')
    if not user_id:
        return 'Missing user_id', 400
    try:
        table.delete_item(Key={"user_id": user_id, "reservation_id": "current"})
        return 'OK', 200
    except Exception as e:
        logging.error(f'Admin clear_cache error: {e}', exc_info=True)
        return 'ERROR', 500


# ---- Admin: Test Google Calendar insert ----
@app.route('/admin/test_calendar_insert', methods=['GET', 'POST'])
def admin_test_calendar_insert():
    token = request.args.get('token') or request.headers.get('X-Admin-Token')
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        return 'Forbidden', 403

    # Params
    q_date = request.args.get('date')  # e.g., 2025-09-10 or 10.09.2025
    q_time = request.args.get('time', '10:00')  # e.g., 16:00
    details = request.args.get('details', 'Test reservation')
    duration = int(request.args.get('duration', '2'))
    user_id = request.args.get('user_id', 'admin-test')

    # Build datetime (naively, Europe/Warsaw provided in event)
    if q_date:
        dt = parse_date(f"{q_date} {q_time}")
    else:
        now = datetime.now()
        dt = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

    if not dt:
        return 'Invalid date/time', 400

    try:
        # Locally reuse add_to_google_calendar with provided duration
        creds = get_google_credentials()
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        event = {
            "summary": details,
            "description": f"Rezerwacja testowa od {user_id}",
            "start": {"dateTime": dt.isoformat(), "timeZone": "Europe/Warsaw"},
            "end": {"dateTime": (dt + timedelta(hours=duration)).isoformat(), "timeZone": "Europe/Warsaw"},
        }
        calendar_id = os.getenv('GOOGLE_CALENDAR_ID', 'primary')
        result = service.events().insert(calendarId=calendar_id, body=event).execute()
        link = result.get('htmlLink')
        logging.info(f"Test insert OK (calendarId={calendar_id}): {link}")
        return f"OK: inserted event at {dt.strftime('%Y-%m-%d %H:%M')} (calendarId={calendar_id}) -> {link}", 200
    except Exception as e:
        logging.error(f"Test insert ERROR: {e}", exc_info=True)
        return f"ERROR: {e}", 500



if __name__ == "__main__":
    # Start Telegram bot in background thread with its own event loop
    TELEGRAM_THREAD = threading.Thread(target=_telegram_loop_runner, daemon=True)
    TELEGRAM_THREAD.start()

    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
