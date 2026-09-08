from flask import Flask, request, jsonify, send_from_directory
import time
import threading
import json
import os
import re
import urllib.error
import urllib.request
import urllib.parse
import asyncio
import random
import hashlib
from collections import OrderedDict
from urllib.parse import quote
import sys

try:
    import websockets
except ImportError:
    websockets = None

try:
    import keyboard
except ImportError:
    keyboard = None


app = Flask(__name__)


# ============================================================
# CONFIG
# ============================================================

# ---------------- JEWELS ----------------
# 1 Jewel = 0.5 second
GIFT_SECONDS_PER_JEWEL = 0.5


# ---------------- SUPER CHAT ----------------
# $1 USD = 30 seconds
SUPERCHAT_SECONDS_PER_USD = 30


# ---------------- CURRENCY API ----------------
# Frankfurter v2 API.
# No API key is required.
EXCHANGE_RATE_API = "https://api.frankfurter.dev/v2/rate"

# Keep each currency's exchange rate for 1 hour.
EXCHANGE_RATE_CACHE_SECONDS = 3600
EXCHANGE_RATE_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "exchange_rates.json"
)

# Example:
# {
#     "CAD": {
#         "rate": 0.73,
#         "timestamp": 1786500000.0
#     },
#     "PHP": {
#         "rate": 0.017,
#         "timestamp": 1786501000.0
#     }
# }
exchange_rate_cache = {}

# Protect the exchange-rate cache because the Flask server
# and SSN listener run in different threads.
exchange_rate_lock = threading.Lock()


# ---------------- NUMPAD HOTKEYS ----------------

# Windows scan codes:
# Numpad 1 = 79
# Numpad 2 = 80
# Numpad 3 = 81
# Numpad 4 = 75
# Numpad 5 = 76
# Numpad 6 = 77

NUMPAD_SCAN_CODE_SECONDS = {
    79: 30,      # Numpad 1 = +30 seconds
    80: 60,      # Numpad 2 = +1 minute
    81: 150,     # Numpad 3 = +2.5 minutes
    75: 300,     # Numpad 4 = +5 minutes
    76: 600,     # Numpad 5 = +10 minutes
    77: 1500,    # Numpad 6 = +25 minutes
}


# No regular-number hotkeys.
# No subtract hotkeys.
SUBTRACT_HOTKEYS = {}


# ---------------- TESTING: ANIMATION PREVIEW HOTKEYS ----------------
# FOR TESTING ONLY.
#
# Lets you preview each gift/Super Chat tier animation instantly,
# without waiting for a real gift or Super Chat.
#
# These do NOT add any time to the timer - they only trigger
# the overlay animation, using the same tier rules as the table:
#
#   Tier    | Gift (jewels) | Super Chat (USD) | Look
#   small   | < 50          | < $5              | 1 emoji, gentle float
#   medium  | < 500         | < $20             | 3 emoji, slight wiggle
#   large   | < 5,000       | < $100            | 5 emoji, bigger burst + gold pop
#   huge    | >= 5,000      | >= $100           | 9 emoji, biggest burst + gold pop
#
# Set this to False (or just delete/comment the hotkeys) before
# going live, so these test combos can't be triggered by accident.
ENABLE_TEST_ANIMATION_HOTKEYS = False

# combo -> (event_type, tier label (for logging only), gift name, value)
# "value" is jewels for a gift, or USD amount for a superchat.
TEST_ANIMATION_HOTKEYS = {
    "ctrl+alt+1": ("gift", "small", "Sparkles", 10),
    "ctrl+alt+2": ("gift", "medium", "Party Hat", 200),
    "ctrl+alt+3": ("gift", "large", "Guitar", 1000),
    "ctrl+alt+4": ("gift", "huge", "Sports Car", 10000),

    "ctrl+alt+5": ("superchat", "small", None, 2),
    "ctrl+alt+6": ("superchat", "medium", None, 10),
    "ctrl+alt+7": ("superchat", "large", None, 50),
    "ctrl+alt+8": ("superchat", "huge", None, 150),
}


# ---------------- TESTING: BROWSER TEST PANEL ----------------
# FOR TESTING ONLY.
#
# An easier alternative to the hotkeys above - opens a page with
# clickable buttons for every single gift (using its real jewel
# value and image) plus Super Chat tiers, and a "simulate a busy
# stream" button that fires a random burst of gifts/Super Chats
# over time.
#
# Open it at: http://127.0.0.1:5000/test-panel
# (works from your phone too, if it's on the same wifi - use your
# PC's local IP instead of 127.0.0.1, e.g. http://192.168.1.23:5000/test-panel)
#
# Like the hotkeys, these do NOT add real time to the timer -
# they only trigger the overlay animation.
#
# Set this to False before going live to disable the panel entirely.
ENABLE_TEST_PANEL = True


# ---------------- DONATION LOGS ----------------

LOG_FOLDER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "logs"
)

# Create logs folder automatically if it doesn't exist.
os.makedirs(LOG_FOLDER, exist_ok=True)

STATE_FILE = "timer_state.json"


# ---------------- GIFT IMAGES (for the overlay animation) ----------------
# Put your actual gift PNG images in a folder called "gift_images" right
# next to this script. Each gift name is looked up (case-insensitive)
# against gift_images/_manifest.json to find its image file.
#
# If a gift has no image (missing file, or not in the manifest), the
# overlay automatically falls back to the colored confetti animation
# instead - nothing breaks.
GIFT_IMAGES_FOLDER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "gift_images"
)

os.makedirs(GIFT_IMAGES_FOLDER, exist_ok=True)

GIFT_IMAGE_MANIFEST_FILE = os.path.join(
    GIFT_IMAGES_FOLDER,
    "_manifest.json"
)


def load_gift_image_manifest():
    """
    Loads gift_images/_manifest.json, which maps a normalized
    gift name (lowercase, matching GIFT_JEWEL_VALUES keys) to
    {"file": "<image filename inside gift_images/>",
     "colors": ["#XXXXXX", "#XXXXXX"]}.

    Returns {} if the file doesn't exist yet or can't be read.
    """

    try:
        with open(GIFT_IMAGE_MANIFEST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ---------------- SOCIAL STREAM NINJA ----------------
# Set this to the Session ID shown in SSN's Session Options.
# ---------------- SOCIAL STREAM NINJA ----------------

if len(sys.argv) < 2:
    print("ERROR: SSN Session ID is required.")
    print("Usage: python timer.py SESSION_ID")
    print("Example: python timer.py CdwUvNURjp")
    sys.exit(1)

SSN_SESSION_ID = sys.argv[1]

print(f"SSN Session ID: {SSN_SESSION_ID}")
SSN_WEBSOCKET_SERVER = "wss://io.socialstream.ninja"
SSN_RECONNECT_MAX_SECONDS = 30

# This window only exists to catch the SAME event being delivered twice
# (once via the Flask POST route, once via the WebSocket listener) at
# nearly the same instant. It must NOT be long enough to also swallow
# genuine repeated gifts from a YouTube "Gift Combo" (rapid taps of the
# same gift), which look identical (same gift name/jewels/user) but are
# each real, separate donations. Keep this short.
SSN_DUPLICATE_WINDOW_SECONDS = 2


# ============================================================
# STATE
# ============================================================

timer_seconds = 0.0
bank_seconds = 0.0

timer_running = False
timer_locked = False

last_tick = time.time()



# IMPORTANT:
# RLock allows the same thread to acquire the lock again.
# This prevents add_time() -> save_state() from deadlocking.
lock = threading.RLock()


# ---------------- GIFT / SUPERCHAT EVENT FEED ----------------
# Used by the browser overlay to show a per-gift / per-superchat
# animation (instead of just a generic "+time" popup).
#
# Each event looks like:
# {
#     "id": 1,
#     "type": "gift" or "superchat",
#     "name": "Fireworks" (gift name) or None (superchat),
#     "value": 3000 (jewels) or 5.00 (USD amount),
#     "seconds": 1500.0,
#     "ts": 1786500000.0,
#     "image_url": "https://..." (SSN's real gift/Super Chat image,
#                                  or None if SSN didn't provide one -
#                                  the overlay then falls back to our
#                                  own gift_images/ folder by name)
# }
recent_events = []
next_event_id = 1


def push_event(event_type, name, value, seconds, image_url=None, combo_count=1):
    """Record a gift/superchat event so the overlay can animate it.

    combo_count is which tap this is within an in-progress Gift Combo
    (1 for a normal, non-combo gift/superchat; 2, 3, 4... for the
    2nd, 3rd, 4th... rapid tap of the SAME gift by the SAME user).
    The overlay uses this to stamp an "xN" badge on the gift image
    instead of just replaying the same plain image every tap.
    """

    global next_event_id

    with lock:
        event = {
            "id": next_event_id,
            "type": event_type,
            "name": name,
            "value": value,
            "seconds": seconds,
            "ts": time.time(),
            "image_url": image_url or None,
            "combo_count": int(combo_count) if combo_count else 1,
        }

        next_event_id += 1

        recent_events.append(event)

        # Keep only the most recent events so this never grows forever.
        if len(recent_events) > 200:
            del recent_events[: len(recent_events) - 200]

    return event


# Used to prevent the same SSN event from being processed twice.
seen_ids = set()


def is_duplicate_event(data):
    """
    Prevent the same SSN paid event from being processed
    through both Flask and WebSocket.
    """

    event_id = (
        data.get("meta", {}).get("messageId")
        or data.get("id")
    )

    # If SSN gives us a real event ID, use it.
    if event_id:
        with lock:
            if event_id in seen_ids:
                return True

            seen_ids.add(event_id)

            # Prevent unlimited memory growth.
            if len(seen_ids) > 10000:
                seen_ids.clear()

        return False

    # If there is no event ID, don't automatically reject it.
    # The WebSocket duplicate protection can still handle it.
    return False

# ============================================================
# DONATION LOGGING
# ============================================================
LOG_FOLDER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "logs"
)

# Create logs folder automatically if it doesn't exist.
os.makedirs(LOG_FOLDER, exist_ok=True)

def log_donation(message):
    """
    Write a donation event to the daily donation log.

    Every Jewel Gift and Super Chat is logged,
    whether processing succeeds or fails.
    """

    date = time.strftime("%Y%m%d")

    log_file = os.path.join(
        LOG_FOLDER,
        f"{date}.txt"
    )

    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    separator = "*" * 70

    log_entry = (
        f"\n"
        f"{separator}\n"
        f"[{timestamp}]\n"
        f"{message}\n"
        f"{separator}\n"
    )

    try:
        with open(
            log_file,
            "a",
            encoding="utf-8"
        ) as file:

            file.write(log_entry)

    except Exception as error:
        print(
            "Failed to write donation log:",
            error
        )

# ============================================================
# SAVE / LOAD
# ============================================================

def save_state():
    with lock:
        data = {
            "timer_seconds": timer_seconds,
            "bank_seconds": bank_seconds,
            "running": timer_running,
            "locked": timer_locked
        }

    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print("Failed to save timer state:", e)


def load_state():
    global timer_seconds
    global bank_seconds
    global timer_running
    global timer_locked

    if not os.path.exists(STATE_FILE):
        return

    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)

        timer_seconds = float(data.get("timer_seconds", 0))
        bank_seconds = float(data.get("bank_seconds", 0))

        # Always start paused after restarting the program.
        timer_running = False

        timer_locked = bool(data.get("locked", False))

    except Exception as e:
        print("Failed to load timer state:", e)


# ============================================================
# TIMER CORE
# ============================================================

def add_time(sec):
    global timer_seconds
    global bank_seconds

    try:
        sec = float(sec)
    except Exception:
        return

    if sec <= 0:
        return

    with lock:
        if timer_locked:
            bank_seconds += sec
        else:
            timer_seconds += sec

    save_state()


def subtract_time(sec):
    global timer_seconds

    try:
        sec = float(sec)
    except Exception:
        return

    with lock:
        timer_seconds = max(0, timer_seconds - sec)

    save_state()


def tick():
    global timer_seconds
    global last_tick

    while True:
        time.sleep(0.2)

        with lock:
            now = time.time()

            delta = now - last_tick
            last_tick = now

            if timer_running and timer_seconds > 0:
                timer_seconds = max(0, timer_seconds - delta)

        save_state()



# ============================================================
# CURRENCY CONVERSION
# ============================================================

def load_exchange_rate_cache():
    """Restore validated saved rates; a corrupt file is ignored safely."""
    if not os.path.exists(EXCHANGE_RATE_CACHE_FILE):
        return

    try:
        with open(EXCHANGE_RATE_CACHE_FILE, "r", encoding="utf-8") as file:
            saved_rates = json.load(file)
        if not isinstance(saved_rates, dict):
            raise ValueError("cache root is not an object")

        validated_rates = {}
        for currency, entry in saved_rates.items():
            if not isinstance(entry, dict):
                continue
            rate = float(entry.get("rate"))
            timestamp = float(entry.get("timestamp"))
            if rate > 0 and timestamp > 0:
                validated_rates[str(currency).upper()] = {
                    "rate": rate,
                    "timestamp": timestamp
                }

        with exchange_rate_lock:
            exchange_rate_cache.clear()
            exchange_rate_cache.update(validated_rates)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        print("Could not load exchange-rate cache; starting with an empty cache:", error)


def save_exchange_rate_cache():
    """Safely save the exchange-rate cache."""

    with exchange_rate_lock:

        cache_copy = {
            currency: {
                "rate": entry["rate"],
                "timestamp": entry["timestamp"]
            }
            for currency, entry in exchange_rate_cache.items()
            if entry.get("rate", 0) > 0
            and entry.get("timestamp", 0) > 0
        }

        temporary_file = EXCHANGE_RATE_CACHE_FILE + ".tmp"

        try:

            with open(
                temporary_file,
                "w",
                encoding="utf-8"
            ) as file:

                json.dump(
                    cache_copy,
                    file,
                    indent=2,
                    sort_keys=True
                )

                file.flush()
                os.fsync(file.fileno())

            os.replace(
                temporary_file,
                EXCHANGE_RATE_CACHE_FILE
            )

        except OSError as error:

            print(
                "Could not save exchange-rate cache:",
                error
            )

            try:

                if os.path.exists(
                    temporary_file
                ):
                    os.remove(
                        temporary_file
                    )

            except OSError:
                pass


def get_exchange_rate(currency):
    """Get one unit of currency in USD, with fresh-cache and saved-rate fallback."""
    currency = str(currency or "").upper().strip()
    if not currency:
        return None
    if currency == "USD":
        return 1.0

    now = time.time()
    with exchange_rate_lock:
        saved_entry = exchange_rate_cache.get(currency)
        if saved_entry:
            saved_entry = dict(saved_entry)

    if saved_entry and now - saved_entry["timestamp"] < EXCHANGE_RATE_CACHE_SECONDS:
        print(f"USING CACHED RATE for {currency}: {saved_entry['rate']:.8f} USD")
        return saved_entry["rate"]

    url = f"{EXCHANGE_RATE_API}/{urllib.parse.quote(currency)}/USD"
    live_rate = None
    failure_reason = None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "YouTube-Timer/1.0"})
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
        live_rate = float(result.get("rate"))
        if live_rate <= 0:
            raise ValueError("rate is missing, invalid, or non-positive")
    except urllib.error.HTTPError as error:
        failure_reason = f"HTTP {error.code}"
    except urllib.error.URLError as error:
        failure_reason = f"network error: {error.reason}"
    except TimeoutError:
        failure_reason = "timeout"
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        failure_reason = f"invalid API response: {error}"
    except Exception as error:
        failure_reason = str(error)

    if live_rate is not None:
        with exchange_rate_lock:
            exchange_rate_cache[currency] = {"rate": live_rate, "timestamp": now}
        save_exchange_rate_cache()
        print(f"EXCHANGE RATE: 1 {currency} = {live_rate:.8f} USD")
        return live_rate

    print(f"CURRENCY API FAILED for {currency}: {failure_reason}")
    if saved_entry and saved_entry["rate"] > 0:
        print(f"USING SAVED FALLBACK RATE for {currency}: {saved_entry['rate']:.8f} USD")
        return saved_entry["rate"]

    print(f"CURRENCY CONVERSION FAILED for {currency}: no live rate and no saved fallback rate")
    return None


# ============================================================
# CURRENCY SYMBOLS
# ============================================================

# These are ONLY aliases used to identify currencies from
# the text SSN gives us.
#
# The exchange-rate system itself is NOT limited to this list.
#
# ISO currency codes are handled dynamically when SSN provides
# the three-letter code directly.

CURRENCY_SYMBOLS = {

    # ---------------- USD ----------------
    "$": "USD",
    "US$": "USD",
    "USD": "USD",

    # ---------------- PHP ----------------
    "₱": "PHP",
    "PHP": "PHP",

    # ---------------- EUR ----------------
    "€": "EUR",
    "EUR": "EUR",

    # ---------------- GBP ----------------
    "£": "GBP",
    "GBP": "GBP",

    # ---------------- JPY ----------------
    "¥": "JPY",
    "JPY": "JPY",

    # ---------------- CNY ----------------
    "CNY": "CNY",
    "CN¥": "CNY",

    # ---------------- CAD ----------------
    "CAD": "CAD",
    "CA$": "CAD",

    # ---------------- AUD ----------------
    "AUD": "AUD",
    "A$": "AUD",

    # ---------------- SGD ----------------
    "SGD": "SGD",
    "S$": "SGD",

    # ---------------- HKD ----------------
    "HKD": "HKD",
    "HK$": "HKD",

    # ---------------- KRW ----------------
    "KRW": "KRW",
    "₩": "KRW",

    # ---------------- INR ----------------
    "INR": "INR",
    "₹": "INR",

    # ---------------- BRL ----------------
    "BRL": "BRL",
    "R$": "BRL",

    # ---------------- MXN ----------------
    "MXN": "MXN",
    "MX$": "MXN",

    # ---------------- CHF ----------------
    "CHF": "CHF",

    # ---------------- EUROPE ----------------
    "SEK": "SEK",
    "NOK": "NOK",
    "DKK": "DKK",
    "PLN": "PLN",

    # ---------------- OCEANIA ----------------
    "NZD": "NZD",

    # ---------------- AFRICA ----------------
    "ZAR": "ZAR",

    # ---------------- MIDDLE EAST ----------------
    "AED": "AED",
    "SAR": "SAR",
    "ILS": "ILS",
    "₪": "ILS",
    "TRY": "TRY",

    # ---------------- ASIA ----------------
    "THB": "THB",
    "MYR": "MYR",
    "IDR": "IDR",
    "VND": "VND",
    "₫": "VND",
    "TWD": "TWD",

    # ---------------- SOUTH AMERICA ----------------
    "ARS": "ARS",
    "CLP": "CLP",
    "COP": "COP",

    # ---------------- CENTRAL AMERICA ----------------
    "CRC": "CRC",
    "GTQ": "GTQ",
    "PAB": "PAB",

    # ---------------- OTHER COMMON ----------------
    "ISK": "ISK",
    "CZK": "CZK",
    "HUF": "HUF",
    "RON": "RON",
    "BGN": "BGN",
    "RSD": "RSD",
    "UAH": "UAH",
    "RUB": "RUB",
}


def normalize_currency_code(currency):
    """
    Normalize a currency identifier.

    If it is already a valid-looking ISO 4217 code,
    return it directly.

    This means we don't have to manually list every
    possible ISO currency code.
    """

    if not currency:
        return None

    currency = str(currency).strip().upper()

    # Known symbol/code alias.
    if currency in CURRENCY_SYMBOLS:
        return CURRENCY_SYMBOLS[currency]

    # Direct ISO-style currency code.
    if re.fullmatch(r"[A-Z]{3}", currency):
        return currency

    return None


# ============================================================
# DONATION PARSER
# ============================================================

MONEY_AMOUNT_PATTERN = r"([0-9]+(?:[.,][0-9]{3})*(?:[.,][0-9]+)?)"


def parse_monetary_amount(amount_text):
    """Parse amounts such as 5.00, 1,234.56, and 1.234,56."""
    value = str(amount_text).strip().replace(" ", "")

    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        whole, fraction = value.rsplit(",", 1)
        value = whole.replace(",", "") if len(fraction) == 3 else value.replace(",", ".")
    elif "." in value:
        whole, fraction = value.rsplit(".", 1)
        value = whole.replace(".", "") if len(fraction) == 3 and value.count(".") > 1 else value

    amount = float(value)
    return amount if amount > 0 else None


def parse_donation(text):
    """
    Parse a YouTube / SSN Super Chat amount.

    Examples:

        "$5.00"
        "US$5.00"
        "CA$2.79"
        "₱100"
        "PHP 100"
        "EUR 5"
        "€5"
        "£10"
        "JPY 500"

    Returns:

        {
            "amount": 5.0,
            "currency": "USD"
        }

    or None if it cannot be parsed.
    """

    if text is None:
        return None

    text = str(text).strip()

    if not text:
        return None

    # Normalize whitespace.
    text = re.sub(r"\s+", " ", text)

    # ========================================================
    # CURRENCY CODE BEFORE NUMBER
    #
    # PHP 100
    # CAD 2.79
    # USD 5
    # ========================================================

    code_before = re.search(
        r"\b([A-Z]{3})\b\s*" +
        MONEY_AMOUNT_PATTERN,
        text.upper()
    )

    if code_before:

        currency_text = code_before.group(1)
        amount_text = code_before.group(2)

        currency = normalize_currency_code(
            currency_text
        )

        if currency:

            try:

                amount = parse_monetary_amount(amount_text)

                if amount is not None:
                    return {
                        "amount": amount,
                        "currency": currency
                    }

            except ValueError:
                pass


    # ========================================================
    # CURRENCY SYMBOL BEFORE NUMBER
    #
    # CA$2.79
    # US$5.00
    # A$10
    # S$20
    # HK$50
    # R$10
    # MX$100
    # $5
    # ₱100
    # €5
    # £10
    # ¥500
    # ========================================================

    symbol_pattern = (
        r"(US\$|CA\$|A\$|S\$|HK\$|CN¥|MX\$|R\$|"
        r"\$|₱|€|£|¥|₩|₹|₪|₫|"
        r"฿|₺|₽|₴|₦|₲|₵|₡|₫)"
    )

    symbol_before = re.search(
        symbol_pattern +
        r"\s*" + MONEY_AMOUNT_PATTERN,
        text
    )

    if symbol_before:

        symbol = symbol_before.group(1)
        amount_text = symbol_before.group(2)

        currency = normalize_currency_code(
            CURRENCY_SYMBOLS.get(symbol)
        )

        if currency:

            try:

                amount = parse_monetary_amount(amount_text)

                if amount is not None:
                    return {
                        "amount": amount,
                        "currency": currency
                    }

            except ValueError:
                pass


    # ========================================================
    # CURRENCY CODE AFTER NUMBER
    #
    # 100 PHP
    # 5 USD
    # 10 CAD
    # ========================================================

    code_after = re.search(
        MONEY_AMOUNT_PATTERN + r"\s*"
        r"\b([A-Z]{3})\b",
        text.upper()
    )

    if code_after:

        amount_text = code_after.group(1)
        currency_text = code_after.group(2)

        currency = normalize_currency_code(
            currency_text
        )

        if currency:

            try:

                amount = parse_monetary_amount(amount_text)

                if amount is not None:
                    return {
                        "amount": amount,
                        "currency": currency
                    }

            except ValueError:
                pass


    # ========================================================
    # NOTHING RECOGNIZED
    # ========================================================

    return None


# ============================================================
# SUPER CHAT PROCESSING
# ============================================================

def process_super_chat(donation_text, image_url=None):
    """
    Convert a Super Chat amount to USD and then to seconds.

    image_url, when provided by SSN (data.contentimg), is passed
    straight through to the overlay. SSN rarely attaches an image
    to a plain Super Chat, so this is usually None, and the overlay
    then falls back to the local "superchat" entry in
    gift_images/_manifest.json.
    """

    parsed = parse_donation(donation_text)

    if not parsed:

        print(
            "SUPER CHAT RECEIVED, "
            "but could not parse amount:",
            donation_text
        )

        log_donation(
            f"EVENT: SUPER CHAT\n"
            f"ORIGINAL: {donation_text}\n"
            f"RESULT: FAILED\n"
            f"REASON: Could not parse donation amount"
        )

        return False

    amount = parsed["amount"]
    currency = parsed["currency"]

    if amount <= 0:

        log_donation(
            f"EVENT: SUPER CHAT\n"
            f"ORIGINAL: {donation_text}\n"
            f"CURRENCY: {currency}\n"
            f"RESULT: FAILED\n"
            f"REASON: Amount is zero or negative"
        )

        return False

    usd_rate = get_exchange_rate(currency)

    if usd_rate is None:

        print(
            "SUPER CHAT NOT ADDED:",
            donation_text
        )

        log_donation(
            f"EVENT: SUPER CHAT\n"
            f"ORIGINAL: {donation_text}\n"
            f"CURRENCY: {currency}\n"
            f"AMOUNT: {amount}\n"
            f"RESULT: FAILED\n"
            f"REASON: Exchange rate unavailable"
        )

        return False

    usd_amount = amount * usd_rate

    seconds = (
        usd_amount *
        SUPERCHAT_SECONDS_PER_USD
    )

    add_time(seconds)

    push_event("superchat", None, usd_amount, seconds, image_url)

    print(
        "SUPER CHAT:",
        donation_text,
        "->",
        f"{usd_amount:.2f} USD",
        "->",
        f"+{seconds:.2f} seconds"
    )

    log_donation(
        f"EVENT: SUPER CHAT\n"
        f"ORIGINAL: {donation_text}\n"
        f"CURRENCY: {currency}\n"
        f"AMOUNT: {amount:.2f}\n"
        f"USD RATE: {usd_rate:.8f}\n"
        f"USD AMOUNT: ${usd_amount:.2f}\n"
        f"RESULT: SUCCESS\n"
        f"TIME ADDED: +{seconds:.2f} seconds"
    )

    return True



# ============================================================
# DONATION PARSER
# ============================================================
# RETIRED LEGACY DONATION PARSER (not used)
# ============================================================

LEGACY_CURRENCY_SYMBOLS = {
    "$": "USD",
    "US$": "USD",
    "USD": "USD",

    "₱": "PHP",
    "PHP": "PHP",

    "€": "EUR",
    "EUR": "EUR",

    "£": "GBP",
    "GBP": "GBP",

    "¥": "JPY",
    "JPY": "JPY",

    "CNY": "CNY",
    "CN¥": "CNY",

    "CAD": "CAD",
    "CA$": "CAD",

    "AUD": "AUD",
    "A$": "AUD",

    "SGD": "SGD",
    "S$": "SGD",

    "HKD": "HKD",
    "HK$": "HKD",

    "KRW": "KRW",
    "₩": "KRW",

    "INR": "INR",
    "₹": "INR",

    "BRL": "BRL",
    "R$": "BRL",

    "MXN": "MXN",
    "MX$": "MXN",

    "CHF": "CHF",
    "SEK": "SEK",
    "NOK": "NOK",
    "DKK": "DKK",
    "PLN": "PLN",
    "NZD": "NZD",
    "ZAR": "ZAR",
}


def legacy_parse_donation(text):
    """
    Parse a YouTube/SSN donation string.

    Examples:

        "$5.00"
        "US$5.00"
        "₱100.00"
        "PHP 100.00"
        "EUR 5.00"
        "€5.00"
        "£10.00"

    Returns:

        {
            "amount": 5.0,
            "currency": "USD"
        }

    or None if it cannot be parsed.
    """

    if text is None:
        return None

    text = str(text).strip()

    if not text:
        return None

    # Normalize spaces.
    text = re.sub(r"\s+", " ", text)

    # --------------------------------------------------------
    # Currency code BEFORE number
    #
    # Example:
    # PHP 100.00
    # USD 5.00
    # EUR 10
    # --------------------------------------------------------

    code_before = re.search(
        r"\b([A-Z]{3})\b\s*"
        r"([0-9]+(?:[.,][0-9]+)?)",
        text.upper()
    )

    if code_before:
        currency = code_before.group(1)
        amount_text = code_before.group(2)

        try:
            amount = float(
                amount_text.replace(",", "")
            )

            return {
                "amount": amount,
                "currency": currency
            }

        except ValueError:
            pass

    # --------------------------------------------------------
    # Currency symbol BEFORE number
    #
    # Example:
    # $5.00
    # ₱100.00
    # €5.00
    # £10.00
    # --------------------------------------------------------

    symbol_pattern = (
        r"(US\$|CA\$|A\$|S\$|HK\$|CN¥|MX\$|R\$|"
        r"\$|₱|€|£|¥|₩|₹)"
    )

    symbol_before = re.search(
        symbol_pattern +
        r"\s*([0-9]+(?:[.,][0-9]+)?)",
        text
    )

    if symbol_before:
        symbol = symbol_before.group(1)
        amount_text = symbol_before.group(2)

        currency = LEGACY_CURRENCY_SYMBOLS.get(symbol)

        if currency:
            try:
                amount = float(
                    amount_text.replace(",", "")
                )

                return {
                    "amount": amount,
                    "currency": currency
                }

            except ValueError:
                pass

    # --------------------------------------------------------
    # Currency code AFTER number
    #
    # Example:
    # 100 PHP
    # 5 USD
    # --------------------------------------------------------

    code_after = re.search(
        r"([0-9]+(?:[.,][0-9]+)?)\s*"
        r"\b([A-Z]{3})\b",
        text.upper()
    )

    if code_after:
        amount_text = code_after.group(1)
        currency = code_after.group(2)

        try:
            amount = float(
                amount_text.replace(",", "")
            )

            return {
                "amount": amount,
                "currency": currency
            }

        except ValueError:
            pass

    return None


def legacy_process_super_chat(donation_text):
    """
    Convert Super Chat amount to USD
    and then convert USD to seconds.
    """

    parsed = legacy_parse_donation(donation_text)

    if not parsed:
        print(
            "SUPER CHAT RECEIVED, "
            "but could not parse amount:",
            donation_text
        )
        return

    amount = parsed["amount"]
    currency = parsed["currency"]

    if amount <= 0:
        return

    # --------------------------------------------------------
    # Convert currency to USD
    # --------------------------------------------------------

    usd_rate = get_exchange_rate(currency)

    if usd_rate is None:
        print("SUPER CHAT NOT ADDED:", donation_text)
        return

    usd_amount = amount * usd_rate

    # --------------------------------------------------------
    # Convert USD to timer seconds
    # --------------------------------------------------------

    seconds = usd_amount * SUPERCHAT_SECONDS_PER_USD

    add_time(seconds)

    print(
        "SUPER CHAT:",
        donation_text,
        "->",
        f"{usd_amount:.2f} USD",
        "->",
        f"+{seconds:.2f} seconds"
    )


# ============================================================
# SOCIAL STREAM NINJA WEBSOCKET
# ============================================================

ssn_recent_events = OrderedDict()

# ---------------- GIFT COMBO DETECTION / LOGGING ----------------
# YouTube "Gift Combos" (rapidly tapping the same gift several times)
# arrive as several separate jeweldonation events with the same gift
# name from the same user, one right after another. Each one is added
# to the timer individually (as it should be), but since you can't
# watch chat every minute, we also group them here purely for
# reporting: once a burst of the same gift from the same user goes
# quiet for COMBO_GROUP_WINDOW_SECONDS, we log one clear "COMBO
# DETECTED" summary line (count, total jewels, total seconds) so you
# can check the log file afterward instead of watching live.
COMBO_GROUP_WINDOW_SECONDS = 10

# key: (user_key, gift_name) -> {"count", "jewels", "seconds", "last_time"}
combo_tracker = {}


def track_gift_combo(user_key, gift_name, jewels, seconds):
    """Record one gift tap toward a possible combo. Actual combo
    detection/logging happens later in combo_watcher(), once the
    burst of taps has gone quiet.

    Returns the tap's position within this combo so far (1 for the
    first tap, 2 for the second, etc.), so the caller can pass it
    along to push_event() for the overlay's "xN" combo badge.
    """

    now = time.time()

    with lock:
        entry = combo_tracker.get((user_key, gift_name))

        if entry is None:
            combo_tracker[(user_key, gift_name)] = {
                "count": 1,
                "jewels": jewels,
                "seconds": seconds,
                "last_time": now,
            }
            return 1
        else:
            entry["count"] += 1
            entry["jewels"] += jewels
            entry["seconds"] += seconds
            entry["last_time"] = now
            return entry["count"]


def combo_watcher():
    """Background loop: flushes any gift-combo entry that has gone
    quiet for COMBO_GROUP_WINDOW_SECONDS. Entries with only a single
    tap are dropped silently (that single gift was already logged
    normally by process_ssn_paid_event) - only real combos (count > 1)
    get a summary log/print."""

    while True:
        time.sleep(1)

        now = time.time()
        finished = []

        with lock:
            for key, entry in list(combo_tracker.items()):
                if now - entry["last_time"] > COMBO_GROUP_WINDOW_SECONDS:
                    finished.append((key, entry))
                    del combo_tracker[key]

        for (user_key, gift_name), entry in finished:
            if entry["count"] <= 1:
                continue

            print(
                f"COMBO GIFT DETECTED: "
                f"{user_key} sent {gift_name} x{entry['count']} -> "
                f"{entry['jewels']:g} Jewels total -> "
                f"+{entry['seconds']:.2f} seconds total"
            )

            log_donation(
                f"EVENT: GIFT COMBO\n"
                f"USER: {user_key}\n"
                f"GIFT: {gift_name}\n"
                f"COMBO COUNT: {entry['count']}\n"
                f"TOTAL JEWELS: {entry['jewels']:g}\n"
                f"TOTAL TIME ADDED: +{entry['seconds']:.2f} seconds\n"
                f"RESULT: SUCCESS"
            )

# Fallback values used only when SSN omits meta.youtubeGift.jewelsAmount.
GIFT_JEWEL_VALUES = {
    "yay": 2, "sorena":2,"sparkles": 2, "star": 2, "100": 2,"treat":5, "go team" : 5, "chili": 6,"shocked" : 6, "makeup brush": 6, "x_x" : 5,":D" : 5, "microphone": 6,
    "thankful" : 7 , "glhf": 10, "gg": 10, "thumbs up": 10, "press f": 10,"hiding" : 10,
    "floating heart": 10, "happy poop": 10, "w": 10, "hiding": 10,
    "gold coin": 10, "ggez": 20, "party hat": 20, "clutch": 30,
    "flower": 30, "six seven": 67, "clock it": 80, "tea money": 90, "high five": 100,
    "marshmallow hi": 100, "sundae": 100,"Kami" : 150, "jammin": 200, "flow state": 200,
    "sunshine": 200, "clapping seal": 250, "laughing disco": 250, "mvp": 250,
    "girl power": 300, "finger heart": 350, "fortune cookie": 380, "NF!" :380,  "Good work": 400,
    "unc alert": 440, "blowfish": 450, "butterfly": 450, "foam finger" : 450,
    "party blowers": 500, "controller": 500, "power potion": 500,
    "sun balloon": 500, "tulips": 500, "watermelon": 500, "Wotagei" : 500, "Wotagei (pink)" : 500, "Wotagei (light)" : 500,
    "Wotagei (blue)" : 500, "Wotagei (red)" : 500, "Wotagei (purple)" : 500, "Wotagei (cyan)" : 500, "Wotagei (yellow)" : 500, "Wotagei (green)" : 500,
    "Wotagei (orange)" : 500, "Wotagei (white)" : 500,
    "shopping cart": 550, "stay hydrated": 550, "picnic basket": 600,
    "mic drop": 600, "envelope": 650, "mystery gift": 650, "nap": 680,
    "island": 700, "let em cook": 750, "foot ball " : 750 , "gg keys": 800, "husky": 800, "888" :888,
    "turn it up": 800, "duckling": 900, "ramen bowl": 900,
    "air travel": 1000, "guitar": 1000, "w pinata": 1000, "cheers": 1000,
    "goat trophy": 1000, "loot box": 1000, "corgi": 1000, "trophy cake": 1000,
    "sand castle": 1200, "nacho" : 1200, "bumblebee": 1250, "bouquet": 1500,
    "headliner": 1500, "tube dancer": 1500, "bubble heart": 1500,
    "spill the tea": 1600, "jelly friends": 2500, "bravo": 2500,"Beach dog": 2500, "Fireworks": 1000,
    "firework": 3000, "headphones": 3600, "hound": 3800, "biscuit tin": 5000,
    "sushi": 5500, "hot pot": 6800, "hooray": 7000, "rock star": 7800,
    "sports car": 9500, "limo": 10000, "trifle smash": 10000,
    "super gg": 10000, "seal splash": 12000, "ball party": 12000,
    "matsuri": 12000,"matsuri fan": 12000,"Woof you!":2000, "tokyo night": 15000,"game trophy" : 1500, "gamer corgi": 15000,
    "night market": 15500, "idol life": 17000, "victory spin": 17000,
    "castle": 18000, "racing game": 20000, "happy birthday": 20000,
    "seaside": 22500,
}


def normalize_gift_name_key(name):
    """Lowercase, collapse whitespace, AND strip punctuation (!, ', ., etc).

    SSN doesn't always preserve punctuation from YouTube's official gift
    name (e.g. it may report "Go Team" instead of "Go Team!"), so the
    lookup table needs to be forgiving about it - matching should not
    depend on whether a "!" (or similar) happens to be present.
    """
    name = str(name or "").casefold()
    name = re.sub(r"[^\w\s]", "", name)
    return " ".join(name.split())


# Pre-normalized lookup table (built once) so gift-name matching ignores
# punctuation differences like "Go Team" vs "Go Team!".
GIFT_JEWEL_VALUES_NORMALIZED = {
    normalize_gift_name_key(name): value
    for name, value in GIFT_JEWEL_VALUES.items()
}


def get_gift_jewel_amount(data):
    """Use SSN's numeric amount first, then the supplied gift-name lookup list."""
    gift = (data.get("meta") or {}).get("youtubeGift") or {}
    try:
        jewels = float(gift.get("jewelsAmount") or 0)
        if jewels > 0:
            return jewels, "SSN"
    except (TypeError, ValueError):
        pass

    gift_name = gift.get("giftName") or data.get("subtitle") or gift.get("altText")
    normalized_name = normalize_gift_name_key(gift_name)
    jewels = GIFT_JEWEL_VALUES_NORMALIZED.get(normalized_name)
    return (float(jewels), "gift-name fallback") if jewels is not None else (0, "unavailable")


def is_duplicate_ssn_event(data):
    """Ignore a short-window duplicate without blocking a later real donation."""

    event_data = {
        "event": data.get("event"),
        "type": data.get("type"),
        "userid": data.get("userid"),
        "chatname": data.get("chatname"),
        "chatmessage": data.get("chatmessage"),
        "hasDonation": data.get("hasDonation"),
        "youtubeGift": (data.get("meta") or {}).get("youtubeGift"),
    }
    key = hashlib.sha256(
        json.dumps(event_data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    now = time.monotonic()

    with lock:
        while (
            ssn_recent_events
            and now - next(iter(ssn_recent_events.values())) > SSN_DUPLICATE_WINDOW_SECONDS
        ):
            ssn_recent_events.popitem(last=False)

        if key in ssn_recent_events:
            return True

        ssn_recent_events[key] = now
        while len(ssn_recent_events) > 1000:
            ssn_recent_events.popitem(last=False)

    return False


def process_ssn_paid_event(data):
    """Apply only real, supported SSN YouTube paid-event values."""

    event_type = str(data.get("event") or "").lower()

    if event_type == "jeweldonation":

        gift = (data.get("meta") or {}).get("youtubeGift") or {}

        gift_name = (
                gift.get("giftName")
                or data.get("subtitle")
                or gift.get("altText")
                or "Unknown Gift"
        )

        jewels, source = get_gift_jewel_amount(data)

        user_key = data.get("chatname") or data.get("userid") or "Unknown"

        if jewels <= 0:
            # SSN sometimes hides the jewel count on later taps inside
            # a YouTube Gift Combo - it reports a generic "1 YouTube
            # Gift" instead of "N Jewels" for those repeats, even
            # though the gift name is still known. If we've *just*
            # seen this same user send this same gift (i.e. we're
            # mid-combo), reuse that combo's per-tap jewel amount
            # instead of dropping this tap entirely.
            with lock:
                combo_entry = combo_tracker.get((user_key, gift_name))

            if (
                combo_entry
                and combo_entry["count"] > 0
                and (time.time() - combo_entry["last_time"]) <= COMBO_GROUP_WINDOW_SECONDS
            ):
                jewels = combo_entry["jewels"] / combo_entry["count"]
                source = "combo fallback (jewel count hidden by SSN)"

        if jewels <= 0:
            print(
                "JEWEL GIFT NOT ADDED: "
                "SSN gave no Jewel amount and "
                "the gift name was not matched."
            )

            log_donation(
                f"EVENT: JEWEL GIFT\n"
                f"GIFT: {gift_name}\n"
                f"RAW hasDonation: {data.get('hasDonation')!r}\n"
                f"RESULT: FAILED\n"
                f"REASON: Jewel amount unavailable"
            )

            return False

        seconds = jewels * GIFT_SECONDS_PER_JEWEL

        add_time(seconds)

        # SSN's own gift image, when available. Per SSN's Event
        # Reference: "Gift images use contentimg" for YouTube
        # jeweldonation events. When present, this is the ACTUAL
        # gift artwork YouTube uses - no local image needed.
        #
        # If SSN doesn't supply one for this gift, image_url is
        # None here, and the overlay automatically falls back to
        # our own gift_images/_manifest.json lookup by gift name.
        image_url = data.get("contentimg") or None
        image_source = "SSN" if image_url else "no image from SSN"

        # Track this tap FIRST so we know its position within the
        # combo (1st, 2nd, 3rd...) and can pass that along to the
        # overlay for the "xN" combo badge on the gift image.
        combo_count = track_gift_combo(user_key, gift_name, jewels, seconds)

        push_event("gift", gift_name, jewels, seconds, image_url, combo_count)

        print(
            f"JEWEL DONATION: "
            f"{gift_name} -> "
            f"{jewels:g} Jewels ({source}) -> "
            f"+{seconds:.2f} seconds "
            f"[image: {image_source}]"
        )

        log_donation(
            f"EVENT: JEWEL GIFT\n"
            f"GIFT: {gift_name}\n"
            f"JEWELS: {jewels:g}\n"
            f"SOURCE: {source}\n"
            f"IMAGE: {image_source}\n"
            f"RESULT: SUCCESS\n"
            f"TIME ADDED: +{seconds:.2f} seconds"
        )

        return True

    if event_type == "superchat":

        donation_text = data.get("hasDonation")

        if not donation_text:
            print(
                "SUPER CHAT RECEIVED, "
                "but SSN did not provide hasDonation."
            )

            log_donation(
                "EVENT: SUPER CHAT\n"
                "RESULT: FAILED\n"
                "REASON: SSN did not provide hasDonation"
            )

            return False

        print(
            "SUPER CHAT RECEIVED:",
            donation_text
        )

        # SSN rarely attaches an image to a plain Super Chat (it's
        # just a colored text card, not an item), but if it ever
        # does supply data.contentimg, use it the same way gifts do.
        image_url = data.get("contentimg") or None

        success = process_super_chat(
            donation_text,
            image_url
        )

        if not success:
            log_donation(
                f"EVENT: SUPER CHAT\n"
                f"ORIGINAL: {donation_text}\n"
                f"RESULT: FAILED\n"
                f"REASON: Could not process donation"
            )

            return False

        return True

    return False


async def listen_to_ssn():
    """Receive SSN channel-4 events directly in this timer process."""

    if not SSN_SESSION_ID:
        print("SSN listener disabled: set SSN_SESSION_ID near the top of this file.")
        return
    if websockets is None:
        print("SSN listener disabled: install websockets with: py -m pip install websockets")
        return

    url = "{}/join/{}/4".format(
        SSN_WEBSOCKET_SERVER,
        quote(SSN_SESSION_ID, safe="")
    )
    reconnect_delay = 1

    while True:
        try:
            async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=60
            ) as websocket:
                print("Connected to SSN: listening for YouTube Gifts/Jewels and Super Chats.")
                reconnect_delay = 1

                async for message in websocket:
                    if isinstance(message, bytes):
                        message = message.decode("utf-8", errors="replace")
                    try:
                        data = json.loads(message)
                    except (TypeError, json.JSONDecodeError):
                        
                        continue

                    if not isinstance(data, dict):
                        continue
                    event_type_lc = str(data.get("event") or "").lower()

                    if event_type_lc not in {
                        "jeweldonation", "superchat"
                    }:
                        continue
                    if is_duplicate_event(data):
                        print("Ignored duplicate SSN event.")
                        continue

                    # IMPORTANT: only run the content-hash duplicate check
                    # on superchats, NOT on gifts. A rapid Gift Combo
                    # (same user tapping the same gift several times in
                    # under a couple seconds) legitimately produces
                    # several events with IDENTICAL fields - SSN often
                    # doesn't vary anything between taps. The hash check
                    # can't tell that apart from a true duplicate, so it
                    # was silently swallowing every tap after the 1st one
                    # in a combo. True duplicate deliveries are already
                    # caught above by is_duplicate_event() using SSN's
                    # real messageId/id, which IS unique per tap.
                    if event_type_lc == "superchat" and is_duplicate_ssn_event(data):
                        print("Ignored duplicate SSN paid event.")
                        continue

                    process_ssn_paid_event(data)

        except Exception as error:
            print(
                f"SSN connection lost: {error}. "
                f"Reconnecting in {reconnect_delay} second(s)."
            )
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, SSN_RECONNECT_MAX_SECONDS)


def start_ssn_listener():
    try:
        asyncio.run(listen_to_ssn())
    except Exception as error:
        print("SSN listener stopped:", error)


# ============================================================
# SSN INPUT
# ============================================================

@app.route("/", methods=["POST"])
def event():

    data = request.get_json(
        force=True,
        silent=True
    ) or {}

    # ========================================================
    # DUPLICATE EVENT PROTECTION
    # ========================================================

    # ========================================================
    # DUPLICATE EVENT PROTECTION
    # ========================================================

    if is_duplicate_event(data):
        print("Ignored duplicate SSN event.")
        return "OK"

    # Only actual SSN Gift/Jewel and Super Chat events can add time.
    process_ssn_paid_event(data)


    # ========================================================
    # NORMAL CHAT
    # ========================================================

    return "OK"


# ============================================================
# STATE
# ============================================================

@app.route("/state")
def state():

    since = request.args.get("since", default=0, type=int) or 0

    with lock:

        new_events = [
            event for event in recent_events
            if event["id"] > since
        ]

        last_event_id = (
            recent_events[-1]["id"]
            if recent_events
            else since
        )

        return jsonify({
            "seconds": timer_seconds,
            "bank_seconds": bank_seconds,
            "running": timer_running,
            "locked": timer_locked,
            "bank": bank_seconds,
            "events": new_events,
            "last_event_id": last_event_id
        })


# ============================================================
# GIFT IMAGES
# ============================================================

@app.route("/gift-images-manifest")
def gift_images_manifest():
    """
    Returns the gift-name -> filename mapping so the overlay
    knows which gifts have a real image available.
    """
    return jsonify(load_gift_image_manifest())


@app.route("/gift-image/<path:filename>")
def gift_image(filename):
    """
    Serves an individual gift image file from the gift_images
    folder next to this script.
    """
    return send_from_directory(GIFT_IMAGES_FOLDER, filename)


# ============================================================
# TESTING: BROWSER TEST PANEL
# ============================================================
# FOR TESTING ONLY. See ENABLE_TEST_PANEL near the top of this
# file. These routes never touch the real timer - they only add
# an entry to the gift/superchat event feed the overlay reads.

@app.route("/test/gift")
def test_gift():

    if not ENABLE_TEST_PANEL:
        return jsonify({"ok": False, "error": "Test panel disabled"}), 403

    name = request.args.get("name", "Test Gift")
    jewels = request.args.get("jewels", default=100, type=float)

    seconds = jewels * GIFT_SECONDS_PER_JEWEL

    add_time(seconds)

    # Route this through the SAME combo-tracking path a real gift tap
    # uses. This means rapidly clicking this same button several times
    # in a row (within COMBO_GROUP_WINDOW_SECONDS) is ALSO a valid way
    # to test combo detection - not just the dedicated "Combo x5"
    # button below. Uses the shared TEST_COMBO_USER so it's easy to
    # spot in the logs afterward.
    combo_count = track_gift_combo(TEST_COMBO_USER, name, jewels, seconds)
    push_event("gift", name, jewels, seconds, combo_count=combo_count)

    return jsonify({
        "ok": True,
        "name": name,
        "jewels": jewels,
        "seconds": seconds,
        "combo_count": combo_count
    })


@app.route("/test/superchat")
def test_superchat():

    if not ENABLE_TEST_PANEL:
        return jsonify({"ok": False, "error": "Test panel disabled"}), 403

    usd = request.args.get("usd", default=5, type=float)

    seconds = usd * SUPERCHAT_SECONDS_PER_USD

    add_time(seconds)
    push_event("superchat", None, usd, seconds)

    return jsonify({"ok": True, "usd": usd, "seconds": seconds})


# A fixed, easy-to-spot "user" name for combo test fires, so they're
# obvious if you go looking through your real logs afterward.
TEST_COMBO_USER = "Test Panel"


def run_test_combo(name, jewels, count, gap):
    """
    Fires the SAME gift several times in a row, spaced 'gap' seconds
    apart, just like a real YouTube Gift Combo. Goes through the exact
    same add_time / push_event / track_gift_combo path a real gift
    does, so this is a faithful test of:
      - the timer actually adding time for every tap (not just the
        first one)
      - the overlay animation firing once per tap
      - combo_watcher() printing/logging a "COMBO GIFT DETECTED"
        summary a few seconds after the last tap
    """

    seconds = jewels * GIFT_SECONDS_PER_JEWEL

    for _ in range(count):
        add_time(seconds)
        combo_count = track_gift_combo(TEST_COMBO_USER, name, jewels, seconds)
        push_event("gift", name, jewels, seconds, combo_count=combo_count)
        time.sleep(gap)


@app.route("/test/combo")
def test_combo():

    if not ENABLE_TEST_PANEL:
        return jsonify({"ok": False, "error": "Test panel disabled"}), 403

    name = request.args.get("name", "Test Gift")
    jewels = request.args.get("jewels", default=100, type=float)
    count = request.args.get("count", default=5, type=int)
    gap = request.args.get("gap", default=0.6, type=float)

    # Keep test values sane.
    count = max(2, min(count, 50))
    gap = max(0.05, min(gap, 5.0))

    threading.Thread(
        target=run_test_combo,
        args=(name, jewels, count, gap),
        daemon=True
    ).start()

    return jsonify({
        "ok": True,
        "name": name,
        "jewels": jewels,
        "count": count,
        "gap": gap
    })


def run_simulated_stream(count, min_gap, max_gap):
    """
    Fires a random burst of gift/Super Chat test events, spaced
    out over time, to simulate what a busy stream chat looks like.
    Runs in a background thread so it doesn't block the request.
    These DO add real time, same as a real gift/Super Chat would.
    """

    gift_names = list(GIFT_JEWEL_VALUES.keys())

    for _ in range(count):

        if random.random() < 0.75:
            name = random.choice(gift_names)
            jewels = GIFT_JEWEL_VALUES[name]
            seconds = jewels * GIFT_SECONDS_PER_JEWEL

            add_time(seconds)
            push_event("gift", name, jewels, seconds)
        else:
            usd = round(random.choice(
                [1, 2, 5, 10, 20, 50, 100, 200]
            ) * random.uniform(0.8, 1.2), 2)
            seconds = usd * SUPERCHAT_SECONDS_PER_USD

            add_time(seconds)
            push_event("superchat", None, usd, seconds)

        time.sleep(random.uniform(min_gap, max_gap))


@app.route("/test/simulate")
def test_simulate():

    if not ENABLE_TEST_PANEL:
        return jsonify({"ok": False, "error": "Test panel disabled"}), 403

    count = request.args.get("count", default=20, type=int)
    min_gap = request.args.get("min_gap", default=0.4, type=float)
    max_gap = request.args.get("max_gap", default=2.0, type=float)

    count = max(1, min(count, 200))

    threading.Thread(
        target=run_simulated_stream,
        args=(count, min_gap, max_gap),
        daemon=True
    ).start()

    return jsonify({"ok": True, "count": count})


@app.route("/test-panel")
def test_panel():

    if not ENABLE_TEST_PANEL:
        return (
            "Test panel is disabled. "
            "Set ENABLE_TEST_PANEL = True in timer.py to use it.",
            403
        )

    gift_rows = "".join(
        f'<div class="gift-item">'
        f'<button class="gift-btn" '
        f'onclick="fireGift(\'{name.replace(chr(39), chr(92)+chr(39))}\', {jewels})">'
        f'{name}<span class="jewels">{jewels:g} 💎</span>'
        f'</button>'
        f'<button class="combo-btn" '
        f'onclick="fireCombo(\'{name.replace(chr(39), chr(92)+chr(39))}\', {jewels})">'
        f'🔥 Combo x5'
        f'</button>'
        f'</div>'
        for name, jewels in sorted(
            GIFT_JEWEL_VALUES.items(), key=lambda kv: kv[1]
        )
    )

    html = """
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Test Panel</title>
    <style>
        body {
            font-family: -apple-system, Arial, sans-serif;
            background: #1c1c1e;
            color: white;
            margin: 0;
            padding: 20px;
        }
        h1 { font-size: 20px; margin: 0 0 4px; }
        p.hint { color: #aaa; font-size: 13px; margin: 0 0 20px; }
        h2 { font-size: 15px; color: #aaa; margin: 24px 0 10px; }
        .row { display: flex; flex-wrap: wrap; gap: 8px; }
        button {
            border: none;
            border-radius: 10px;
            padding: 10px 14px;
            font-size: 14px;
            cursor: pointer;
            color: white;
        }
        .gift-btn {
            background: #333;
            display: flex;
            flex-direction: column;
            align-items: flex-start;
            gap: 4px;
            min-width: 110px;
        }
        .gift-btn .jewels {
            font-size: 12px;
            color: #7CFF5E;
        }
        .gift-btn:active { background: #4CAF50; }
        .gift-item {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }
        .combo-btn {
            background: #b23b3b;
            font-size: 12px;
            padding: 6px 10px;
        }
        .combo-btn:active { background: #ff5c5c; }
        .sc-btn { background: #2b6b3a; min-width: 90px; }
        .sc-btn:active { background: #4CAF50; }
        .sim-btn {
            background: #a34ed1;
            font-size: 16px;
            padding: 14px 20px;
        }
        .sim-btn:active { background: #c46bff; }
        #status {
            margin-top: 14px;
            font-size: 13px;
            color: #7CFF5E;
            min-height: 18px;
        }
    </style>
    </head>
    <body>

        <h1>🎬 Overlay Test Panel</h1>
        <p class="hint">
            Tapping these DOES add real time to the running timer
            (just like a real gift/Super Chat would) - not just the
            animation. Keep the OBS overlay (or another tab of this
            page's main URL) open to see it fire. Use the red
            "🔥 Combo x5" button on a gift to simulate a YouTube Gift
            Combo (5 rapid taps of that same gift) - watch the console
            or your logs/ folder a few seconds after it finishes for a
            "COMBO GIFT DETECTED" summary.
        </p>

        <h2>Simulate a busy stream</h2>
        <div class="row">
            <button class="sim-btn" onclick="fireSimulate(15)">
                🔥 Simulate 15 events
            </button>
            <button class="sim-btn" onclick="fireSimulate(40)">
                🔥🔥 Simulate 40 events
            </button>
        </div>

        <h2>Super Chat tiers</h2>
        <div class="row">
            <button class="sc-btn" onclick="fireSuperchat(2)">💵 $2 (small)</button>
            <button class="sc-btn" onclick="fireSuperchat(10)">💰 $10 (medium)</button>
            <button class="sc-btn" onclick="fireSuperchat(50)">🤑 $50 (large)</button>
            <button class="sc-btn" onclick="fireSuperchat(150)">💸 $150 (huge)</button>
        </div>

        <h2>Every gift (smallest → biggest)</h2>
        <div class="row">
            __GIFT_ROWS__
        </div>

        <div id="status"></div>

    <script>
        function setStatus(msg){
            document.getElementById('status').textContent = msg;
        }

        async function fireGift(name, jewels){
            setStatus('Fired: ' + name + ' (' + jewels + ' jewels)');
            await fetch('/test/gift?name=' + encodeURIComponent(name) +
                        '&jewels=' + encodeURIComponent(jewels));
        }

        async function fireCombo(name, jewels){
            setStatus('Firing COMBO: ' + name + ' x5 (rapid taps)... watch for "COMBO GIFT DETECTED" in a few seconds.');
            await fetch('/test/combo?name=' + encodeURIComponent(name) +
                        '&jewels=' + encodeURIComponent(jewels) +
                        '&count=5&gap=0.6');
        }

        async function fireSuperchat(usd){
            setStatus('Fired Super Chat: $' + usd);
            await fetch('/test/superchat?usd=' + encodeURIComponent(usd));
        }

        async function fireSimulate(count){
            setStatus('Simulating ' + count + ' random events...');
            await fetch('/test/simulate?count=' + count);
        }
    </script>

    </body>
    </html>
    """

    html = html.replace("__GIFT_ROWS__", gift_rows)

    return html


# ============================================================
# CONTROLS
# ============================================================

@app.route("/start")
def start():

    global timer_running
    global last_tick

    with lock:

        timer_running = True
        last_tick = time.time()

    save_state()

    return "start"


@app.route("/pause")
def pause():

    global timer_running

    with lock:
        timer_running = False

    save_state()

    return "pause"


@app.route("/reset")
def reset():

    global timer_seconds

    with lock:
        timer_seconds = 0

    save_state()

    return "reset"


@app.route("/lock")
def lock_timer():

    global timer_locked

    with lock:
        timer_locked = True

    save_state()

    return "locked"


@app.route("/unlock")
def unlock():

    global timer_locked

    with lock:
        timer_locked = False

    save_state()

    return "unlocked"


@app.route("/add/<sec>")
def add(sec):

    try:
        add_time(float(sec))
    except Exception:
        return "invalid", 400

    return "added"


@app.route("/subtract/<sec>")
def subtract(sec):

    try:
        subtract_time(float(sec))
    except Exception:
        return "invalid", 400

    return "subtracted"


@app.route("/apply_bank")
def apply_bank():

    global timer_seconds
    global bank_seconds

    with lock:
        timer_seconds += bank_seconds
        bank_seconds = 0

    save_state()

    return "applied"


@app.route("/clear_bank")
def clear_bank():

    global bank_seconds

    with lock:
        bank_seconds = 0

    save_state()

    return "cleared"


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/dashboard")
def dashboard():

    return """
<!doctype html>
<html>

<head>

<title>Timer Dashboard</title>

<style>

body {
    font-family: Arial;
    background:#111;
    color:white;
    padding:20px;
}

button {
    margin:5px;
    padding:10px;
}

</style>

</head>

<body>

<h2>Timer Dashboard</h2>

<h3>Controls</h3>

<button onclick="fetch('/start')">
Start
</button>

<button onclick="fetch('/pause')">
Pause
</button>

<button onclick="fetch('/reset')">
Reset
</button>


<h3>Lock</h3>

<button onclick="fetch('/lock')">
Lock
</button>

<button onclick="fetch('/unlock')">
Unlock
</button>


<h3>Bank</h3>

<button onclick="fetch('/apply_bank')">
Apply Bank
</button>

<button onclick="fetch('/clear_bank')">
Clear Bank
</button>


<h3>Manual Super Chat Add Time</h3>

<p>
Numeric keypad only:
Numpad 1 = +30s,
Numpad 2 = +1 min,
Numpad 3 = +2.5 min,
Numpad 4 = +5 min,
Numpad 5 = +10 min,
Numpad 6 = +25 min.
</p>

<p>
The regular number row does nothing.
</p>


<h3>Subtract Time</h3>

<button onclick="fetch('/subtract/30')">
-30 sec
</button>

<button onclick="fetch('/subtract/60')">
-1 min
</button>

<button onclick="fetch('/subtract/150')">
-2.5 min
</button>

<button onclick="fetch('/subtract/300')">
-5 min
</button>

<button onclick="fetch('/subtract/600')">
-10 min
</button>

<button onclick="fetch('/subtract/1500')">
-25 min
</button>

</body>

</html>
"""


# ============================================================
# TIMER UI
# ============================================================

@app.route("/timer")
def timer_page():

    return """
<!doctype html>

<html>

<head>

<meta charset="utf-8">

<title>Timer</title>

<style>

html, body {
    margin: 0;
    background: transparent;
    overflow: hidden;
    font-family: Arial, sans-serif;
    font-weight: 400;
}

#wrap {
    position: absolute;
    top: 18px;
    left: 0;

    display: flex;
    align-items: center;
    justify-content: flex-start;

    gap: 57px;
    z-index: 9999;
    width: fit-content;
    
}
.box-bg {
    position: absolute;
    top: 0;
    left: 0;
 
    width: 100%;
    height: 100%;
 
    background: rgba(58, 58, 58, 0.9);
 
    /* Above #fx-layer (z-index: 1) - gifts/emoji are hidden the
       moment they're behind this box, not just behind the digits.
       Still below #time-row (z-index: 3) so the digits stay visible
       on top of the grey background. */
    z-index: 2;
}
#left {
    width: 337px;
    height: 115px;
    background: rgba(58, 58, 58, 0.9);
    color: white;

    display: flex;
    align-items: center;
    justify-content: center;

    font-size: 53px;
    font-weight: 549;
    line-height: 0.95;

    text-align: center;

    letter-spacing: .5px;

    padding: 0;
}

#right {
    width: 421.27px;
    height: 115px;

    # background: rgba(58, 58, 58, 0.9);

    display: flex;
    flex-direction: column;
    align-items: center;

    text-align: center;

    color: white;

    overflow: visible;

    position: relative;
    z-index: 2;
}

#time-row {
    flex: 1;

    display: flex;
    justify-content: center;
    align-items: center;
    position: relative; z-index: 3;
    width: 100%;
}

/* ---------------- GIFT / SUPERCHAT EMOJI ANIMATION ---------------- */

#fx-layer {
    position: absolute;
    left: 0;
    top: 0;

    width: 100%;
    height: 100%;

    overflow: visible;
    z-index: 1;
    pointer-events: none;
}

/* ---------------- FLY-TO-DIGIT ANIMATION (shared by gifts + superchats) ---------------- */
/* Every gift/Super Chat animation spawns OUTSIDE the timer box to
   the right, slides left, and shrinks away right as it reaches
   the last digit - which is the exact moment the timer number
   jumps and pops (see scheduleLanding() in JS). */

.confetti-piece,
.gift-image-fly,
.gift-image-wrap,
.superchat-wrap,
.superchat-emoji {
    position: absolute;
    left: 50%;
    top: 50%;

    opacity: 0;

    will-change: transform, opacity;

    /* animation is now assigned per-element from JS (see
       playHoldThenFly()), since it plays in two phases:
       1) appearAtStart  2) flyOnly - with a pause in between. */
}

/* Wraps a flying gift image so a combo "xN" badge can be stamped on
   top of it and travel/fade together as ONE unit (same pattern the
   Super Chat ticket + $ label already use). The wrap itself gets the
   position/animation treatment above; the image and badge inside are
   just normal, statically-positioned children. */
.gift-image-wrap {
    pointer-events: none;
}

.gift-image-wrap .gift-image-fly {
    position: static;
    display: block;
    opacity: 1;
}

.confetti-piece.shape-circle {
    border-radius: 50%;
}

.confetti-piece.shape-square {
    border-radius: 2px;
}

.gift-image-fly {
    object-fit: contain;
    pointer-events: none;
    filter: drop-shadow(0 0 14px var(--glow, #FFD700));
}

/* ---------------- COMBO COUNT BADGE ("xN") ---------------- */
/* Stamped on the bottom-right corner of a gift image whenever it's
   the 2nd (or later) rapid tap of a YouTube Gift Combo, so repeated
   taps of the same gift read as "x2", "x3", "x4"... instead of just
   looking like the same plain gift image over and over. Pops in with
   a little bounce right as the image itself appears. */
.gift-combo-badge {
    position: absolute;
    right: -6px;
    bottom: -2px;

    padding: 3px 9px;
    border-radius: 999px;

    background: linear-gradient(135deg, #FF5C5C, #B23B3B);
    color: #FFFFFF;

    font-weight: 800;
    white-space: nowrap;

    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.5);
    box-shadow: 0 0 10px rgba(0, 0, 0, 0.35), 0 0 8px rgba(255, 92, 92, 0.7);

    pointer-events: none;
    animation: comboBadgePop 0.35s ease;
}

@keyframes comboBadgePop {
    0% {
        opacity: 0;
        transform: scale(0.3);
    }
    60% {
        opacity: 1;
        transform: scale(1.25);
    }
    100% {
        opacity: 1;
        transform: scale(1);
    }
}

.superchat-emoji {
    white-space: nowrap;
    pointer-events: none;
}

/* Super Chat now flies in as ONE unit - the ticket image (or a
   fallback emoji, see buildSuperchatEmojiIcon() in the JS) with the
   dollar amount printed right below the "SUPERCHAT" wording, INSIDE
   the same box. Both move/fade together automatically since they're
   just normal children of .superchat-wrap, which is what actually
   gets the appearAtStart/flyOnly animation - no separate animation
   needed for the text. */
.superchat-wrap {
    pointer-events: none;
}

.superchat-image {
    display: block;
    width: 100%;
    height: auto;
    pointer-events: none;
    filter: drop-shadow(0 0 14px var(--glow, #FFD700));
}

.superchat-emoji-icon {
    display: block;
    text-align: center;
    line-height: 1;
    pointer-events: none;
    filter: drop-shadow(0 0 14px var(--glow, #FFD700));
}

.superchat-value-text {
    position: absolute;
    left: 50%;
    top: 68%;
    transform: translate(-50%, -50%);

    white-space: nowrap;
    pointer-events: none;

    font-weight: 800;
    color: #FFFFFF;
    text-shadow:
        0 1px 2px rgba(0, 0, 0, 0.6),
        0 0 6px rgba(0, 0, 0, 0.45);
}

@keyframes flyToDigit {
    0% {
        opacity: 0;
        transform:
            translate(
                calc(-50% + var(--start-x, 260px)),
                calc(-50% + var(--start-y, 0px))
            )
            scale(var(--start-scale, 0.5))
            rotate(var(--start-rot, 0deg));
    }
    12% {
        opacity: 1;
    }
    70% {
        opacity: 1;
        transform:
            translate(
                calc(-50% + var(--end-x, 0px)),
                calc(-50% + var(--end-y, 0px))
            )
            scale(var(--peak-scale, 1.1))
            rotate(var(--end-rot, 0deg));
    }
    100% {
        /* Absorbed into the last digit right as it lands. */
        opacity: 0;
        transform:
            translate(
                calc(-50% + var(--end-x, 0px)),
                calc(-50% + var(--end-y, 0px))
            )
            scale(var(--end-scale, 0.15))
            rotate(var(--end-rot, 0deg));
    }
}

/* ---------------- PHASE 1: APPEAR + HOLD ---------------- */
/* Fades/pops the gift image or emoji in at its start position (out
   to the right) and leaves it sitting there, fully visible, doing
   nothing. See HOLD_BEFORE_FLY_SECONDS in the JS below - that's how
   long it stays like this before phase 2 (flyOnly) kicks in. */
@keyframes appearAtStart {
    0% {
        opacity: 0;
        transform:
            translate(
                calc(-50% + var(--start-x, 260px)),
                calc(-50% + var(--start-y, 0px))
            )
            scale(calc(var(--start-scale, 0.5) * 0.8))
            rotate(var(--start-rot, 0deg));
    }
    100% {
        opacity: 1;
        transform:
            translate(
                calc(-50% + var(--start-x, 260px)),
                calc(-50% + var(--start-y, 0px))
            )
            scale(var(--start-scale, 0.5))
            rotate(var(--start-rot, 0deg));
    }
}

/* ---------------- PHASE 2: FLY LEFT + ABSORB ---------------- */
/* Same slide-left-and-shrink-into-the-digit motion as flyToDigit
   above, just starting from "already visible" (opacity 1) instead
   of fading in, since appearAtStart already handled that part. */
@keyframes flyOnly {
    0% {
        opacity: 1;
        transform:
            translate(
                calc(-50% + var(--start-x, 260px)),
                calc(-50% + var(--start-y, 0px))
            )
            scale(var(--start-scale, 0.5))
            rotate(var(--start-rot, 0deg));
    }
    70% {
        opacity: 1;
        transform:
            translate(
                calc(-50% + var(--end-x, 0px)),
                calc(-50% + var(--end-y, 0px))
            )
            scale(var(--peak-scale, 1.1))
            rotate(var(--end-rot, 0deg));
    }
    100% {
        opacity: 0;
        transform:
            translate(
                calc(-50% + var(--end-x, 0px)),
                calc(-50% + var(--end-y, 0px))
            )
            scale(var(--end-scale, 0.15))
            rotate(var(--end-rot, 0deg));
    }
}

#msg {
    width: 100%;
    height: 20px;

    display: flex;
    justify-content: center;
    align-items: center;

    font-size: 20px;
    font-weight: 650;
    line-height: 1.2;

    text-align: center;

    white-space: pre-line;

    margin-top: 8px;
    margin-bottom: 0;

    opacity: 1;

    transition: opacity .8s ease;

    flex-shrink: 0;
}

#time {
    font-size: 110px;
    font-weight: 530;

    margin-top: -18px;

    transition: transform 0.15s ease, color 0.15s ease;
}

#time.time-increased {
    animation: timeIncreasePop 0.5s ease;
}

@keyframes timeIncreasePop {
    0% {
        transform: scale(1);
        color: inherit;
        text-shadow: none;
    }
    30% {
        transform: scale(1.15);
        color: var(--pop-color, #4CAF50);
        text-shadow: 0 0 16px var(--pop-glow, #4CAF50);
    }
    100% {
        transform: scale(1);
        color: inherit;
        text-shadow: none;
    }
}

#time.time-increased-huge {
    animation: timeIncreasePopHuge 0.9s ease;
}

@keyframes timeIncreasePopHuge {
    0% {
        transform: scale(1);
        color: inherit;
        text-shadow: none;
    }
    30% {
        transform: scale(1.35);
        color: var(--pop-color, #FFD700);
        text-shadow: 0 0 22px var(--pop-glow, #FFD700);
    }
    60% {
        transform: scale(1.1);
        color: var(--pop-color, #FFD700);
        text-shadow: 0 0 22px var(--pop-glow, #FFD700);
    }
    100% {
        transform: scale(1);
        color: inherit;
        text-shadow: none;
    }
}

/* ---------------- LANDING BURST (ON THE OBJECT, NOT THE DIGIT) ---------------- */
/* Plays the instant a gift image or Super Chat itself "arrives" at
   the timer (see spawnBurstAtLanding(), called from
   playHoldThenFly() right when the pop-style flyOnlyPop keyframe
   below reaches its arrival point) - a soft ring plus a few small
   bubble-like particles bursting outward from the exact spot,
   colored to match that object. The timer digit's own pop animation
   is untouched - this is purely the object exploding/popping. */
.impact-ring {
    position: absolute;
    left: 50%;
    top: 50%;

    width: 14px;
    height: 14px;

    border-radius: 50%;
    border: 3px solid var(--impact-color, #FFD700);
    box-shadow: 0 0 10px var(--impact-color, #FFD700);

    opacity: 0;
    pointer-events: none;
    will-change: transform, opacity;

    animation: impactRingBurst 0.45s ease-out forwards;
}

@keyframes impactRingBurst {
    0% {
        opacity: 0.85;
        transform:
            translate(calc(-50% + var(--impact-x, 0px)), calc(-50% + var(--impact-y, 0px)))
            scale(0.2);
    }
    100% {
        opacity: 0;
        transform:
            translate(calc(-50% + var(--impact-x, 0px)), calc(-50% + var(--impact-y, 0px)))
            scale(5);
    }
}

/* Small, angular confetti-like pieces that fly outward AND tumble
   (rotate) as they go, instead of soft round bubbles - reads as a
   burst of little flecks breaking off the object, not a bubble
   popping. Paired with .impact-ring above for a shockwave-plus-
   confetti landing effect. */
.impact-confetti {
    position: absolute;
    left: 50%;
    top: 50%;

    border-radius: 1px;
    box-shadow: 0 0 4px var(--impact-color, #FFD700);

    opacity: 0;
    pointer-events: none;
    will-change: transform, opacity;

    animation: impactConfettiBurst 0.6s ease-out forwards;
}

@keyframes impactConfettiBurst {
    0% {
        opacity: 1;
        transform:
            translate(calc(-50% + var(--impact-x, 0px)), calc(-50% + var(--impact-y, 0px)))
            rotate(var(--confetti-start-rot, 0deg))
            scale(1);
    }
    100% {
        opacity: 0;
        transform:
            translate(calc(-50% + var(--spark-x, 0px)), calc(-50% + var(--spark-y, 0px)))
            rotate(var(--confetti-end-rot, 360deg))
            scale(0.5);
    }
}

/* ---------------- EXPERIMENTAL: ARRIVAL COUNTDOWN LABEL ---------------- */
/* Small "Gift arriving in 3s..." style label shown right next to
   the object while it's holding at its start position, counting
   down each second. See ENABLE_ARRIVAL_COUNTDOWN in the JS below -
   set that to false to turn this off entirely. */
.arrival-countdown {
    position: absolute;
    left: 50%;
    top: 50%;

    white-space: nowrap;
    pointer-events: none;

    font-size: 13px;
    font-weight: 700;
    color: #FFFFFF;
    text-shadow:
        0 1px 2px rgba(0, 0, 0, 0.6),
        0 0 6px rgba(0, 0, 0, 0.45);

    background: rgba(0, 0, 0, 0.55);
    padding: 3px 9px;
    border-radius: 10px;

    opacity: 0;
    transition: opacity 0.25s ease;
    will-change: opacity;
}

.arrival-countdown.visible {
    opacity: 1;
}

/* ---------------- EXPERIMENTAL: GLITTER TRAIL WHILE FLYING ---------------- */
/* Tiny sparkle dots spawned repeatedly along the object's flight
   path (see startGlitterTrail() in the JS), so it looks like it's
   leaving a trail of glitter as it travels toward the timer. See
   ENABLE_GLITTER_TRAIL below - set that to false to turn this off. */
.glitter-particle {
    position: absolute;
    left: 50%;
    top: 50%;

    border-radius: 50%;
    background: radial-gradient(
        circle at 35% 35%,
        #ffffff,
        var(--glitter-color, #FFD700) 70%
    );
    box-shadow: 0 0 6px var(--glitter-color, #FFD700);

    opacity: 0;
    pointer-events: none;
    will-change: transform, opacity;

    animation: glitterFade 0.5s ease-out forwards;
}

@keyframes glitterFade {
    0% {
        opacity: 1;
        transform:
            translate(calc(-50% + var(--gx, 0px)), calc(-50% + var(--gy, 0px)))
            scale(1);
    }
    100% {
        opacity: 0;
        transform:
            translate(calc(-50% + var(--gx, 0px)), calc(-50% + var(--gy, 0px) - 12px))
            scale(0.2);
    }
}

/* ---------------- PHASE 2b: BUBBLE-POP FINISH ---------------- */
/* Used instead of flyOnly (see flyOnlyPop below) for objects that
   should look like they arrive and then inflate/pop like a bubble,
   rather than smoothly shrinking away. Shares the exact same
   arrival point (70%) as flyOnly - so the timer digit still updates
   at the same instant either way - it just keeps going afterward
   with a quick inflate + snap-vanish instead of a plain shrink. */
@keyframes flyOnlyPop {
    0% {
        opacity: 1;
        transform:
            translate(
                calc(-50% + var(--start-x, 260px)),
                calc(-50% + var(--start-y, 0px))
            )
            scale(var(--start-scale, 0.5))
            rotate(var(--start-rot, 0deg));
    }
    70% {
        opacity: 1;
        transform:
            translate(
                calc(-50% + var(--end-x, 0px)),
                calc(-50% + var(--end-y, 0px))
            )
            scale(var(--peak-scale, 1.1))
            rotate(var(--end-rot, 0deg));
    }
    85% {
        /* Quick inflate right after arriving - the "about to pop"
           moment of a bubble. */
        opacity: 1;
        transform:
            translate(
                calc(-50% + var(--end-x, 0px)),
                calc(-50% + var(--end-y, 0px))
            )
            scale(calc(var(--peak-scale, 1.1) * 1.35))
            rotate(var(--end-rot, 0deg));
    }
    100% {
        /* Snap-vanish, like the bubble popped. */
        opacity: 0;
        transform:
            translate(
                calc(-50% + var(--end-x, 0px)),
                calc(-50% + var(--end-y, 0px))
            )
            scale(var(--end-scale, 0.05))
            rotate(var(--end-rot, 0deg));
    }
}


</style>

</head>

<body>

<div id="wrap">

    <!--<div id="left">
        Tell or ask me anything.
    </div>-->




    <!--<div id="right">
    
        
        <div id="msg">
            Stream ends in...
        </div>
        

        <div id="time-row">

            <div id="time">
                00:00:00
            </div>

        </div> 

        

    </div>-->
    
    <div id="right">
    <div class="box-bg">
         <div id="msg">Stream ends in...
        </div>
        
        <div id="time-row">

            <div id="time">
                00:00:00
            </div>

        </div>
    </div>
    <div id="fx-layer"></div>
</div>


<script>

function fmt(s){

    s = Math.max(
        0,
        Math.floor(s)
    );

    let h = Math.floor(
        s / 3600
    );

    let m = Math.floor(
        (s % 3600) / 60
    );

    let sec = s % 60;

    return String(h).padStart(2,'0') + ":" +
           String(m).padStart(2,'0') + ":" +
           String(sec).padStart(2,'0');
}


// ---------------- GIFT CONFETTI: SCALES PER-GIFT BY JEWEL VALUE ----------------
// Instead of 4 fixed buckets, every gift's animation is computed
// directly from its own jewel amount (particle count/size/speed/
// height all scale smoothly with jewels), and each gift NAME gets
// its own stable color scheme (hashed from the name). Two gifts
// with different names rarely look identical, and gifts with
// bigger jewel values always look bigger/faster/more energetic.

// Based on the GIFT_JEWEL_VALUES table: smallest gift is 2 jewels
// (Yay/Sorena/Sparkles/Star/100), biggest is 22,500 (Seaside).
const GIFT_MIN_JEWELS = 2;
const GIFT_MAX_JEWELS = 22500;

function giftIntensity(jewels){
    jewels = Math.max(GIFT_MIN_JEWELS, Number(jewels) || GIFT_MIN_JEWELS);

    // Log scale, since gift values range from 2 to 22,500 -
    // a linear scale would make almost everything look "small".
    const t =
        (Math.log(jewels) - Math.log(GIFT_MIN_JEWELS)) /
        (Math.log(GIFT_MAX_JEWELS) - Math.log(GIFT_MIN_JEWELS));

    return Math.min(1, Math.max(0, t));
}

function lerp(a, b, t){
    return a + (b - a) * t;
}

// Turns a gift name into a stable 0-360 hue, so the SAME gift
// always animates in the SAME colors (e.g. "Guitar" is always
// the same color scheme every time it's gifted).
function hashHue(name){
    let hash = 0;
    const text = String(name || "gift").toLowerCase();

    for (let i = 0; i < text.length; i++){
        hash = (hash * 31 + text.charCodeAt(i)) >>> 0;
    }

    return hash % 360;
}

// ---------------- SHARED: WHERE THINGS FLY FROM/TO ----------------
// Every gift/Super Chat animation starts OUTSIDE the timer box to
// the right, and flies left, disappearing right at the box's
// outer edge - BEFORE it ever overlaps/enters the box, so it
// never gets hidden behind the box-bg (which sits on top of the
// fx-layer). Positions are measured live from the DOM, in px
// relative to the center of #fx-layer, since that's what
// left:50%+transform uses.
function getFlyPositions(){

    const fx = document.getElementById('fx-layer');
    const timeEl = document.getElementById('time');

    const fxRect = fx.getBoundingClientRect();
    const timeRect = timeEl.getBoundingClientRect();

    const fxCenterX = fxRect.left + fxRect.width / 2;
    const fxCenterY = fxRect.top + fxRect.height / 2;

    // Land right at the OUTER edge of the box itself - NOT deep
    // inside near the last digit. This makes the gift/superchat
    // disappear right as it reaches the box's edge, before it
    // ever visually overlaps the timer digits (box-bg and
    // fx-layer share the exact same rect, so fxRect.right IS the
    // box's right edge).
    const endX = Math.round((fxRect.right - (-15)) - fxCenterX);
    const endY = Math.round(
        (timeRect.top + timeRect.height / 3) - fxCenterY
    );

    // Start well outside the box, to the right, with a little
    // random extra distance/jitter so repeated gifts don't all
    // look identical.
    const startX = Math.round(
        fxRect.width / 2 + 80 
    );
    const startY = Math.round(endY + (Math.random() - 0.5) * 18);

    return { startX, startY, endX, endY };
}

// ---------------- HOLD BEFORE FLYING LEFT ----------------
// How long (in seconds) a gift image / emoji sits fully visible at
// its start position, doing nothing, before it starts sliding left
// toward the timer. Gives viewers a real chance to see it instead
// of it just flashing by. Change this one number to adjust the pause.
const HOLD_BEFORE_FLY_SECONDS = .5;

// How long the little fade/pop-in at the start position takes,
// right before the hold begins.
const APPEAR_DURATION = 0.3;

// ---------------- WHEN THE TIMER DIGIT SHOULD "LAND" ----------------
// The flyOnly keyframe (see CSS above) reaches the timer digit's
// position at the 70% mark of its own timeline - it's still fully
// visible there (peak-scale), and only shrinks/fades away from 70%
// to 100%. Setting this to 0.7 makes the timer digit update/pop the
// moment the gift ARRIVES at the digit, instead of waiting for it to
// finish shrinking away. Set it back to 1.0 to restore the old
// "wait for full disappear" behavior. Must match the 70% used in the
// flyOnly/flyToDigit @keyframes above if you ever change those.
const LAND_AT_FRACTION = 0.7;

// ---------------- EXPERIMENTAL: ARRIVAL COUNTDOWN + GLITTER TRAIL ----------------
// Two "try it out" extras, each independently toggleable:
//
//   ENABLE_ARRIVAL_COUNTDOWN - shows a "Gift arriving in 3s..." style
//   label next to the object while it holds, counting down each
//   second, disappearing right when it starts flying.
//
//   ENABLE_GLITTER_TRAIL - once the object starts flying, spawns a
//   trail of small sparkle dots along its actual flight path, so it
//   looks like it's leaving a glitter trail as it travels.
//
// Set either to false to turn that extra off without touching
// anything else.
const ENABLE_ARRIVAL_COUNTDOWN = false;
const ENABLE_GLITTER_TRAIL = true;

// Creates the "<text> in Ns" label at the object's own start
// position (read from its --start-x/--start-y), positioned a bit
// above it, and counts down once per second.
function spawnCountdownLabel(el, text, holdSeconds){
    const fx = document.getElementById('fx-layer');

    const startX = parseFloat(el.style.getPropertyValue('--start-x')) || 0;
    const startY = parseFloat(el.style.getPropertyValue('--start-y')) || 0;

    const label = document.createElement('div');
    label.className = 'arrival-countdown';
    label.style.transform =
        `translate(calc(-50% + ${startX}px), calc(-50% + ${startY - 60}px))`;

    let remaining = Math.ceil(holdSeconds);
    label.textContent = `${text} in ${remaining}s`;

    fx.appendChild(label);
    requestAnimationFrame(() => label.classList.add('visible'));

    const interval = setInterval(() => {
        remaining -= 1;
        if (remaining > 0){
            label.textContent = `${text} in ${remaining}s`;
        } else {
            clearInterval(interval);
        }
    }, 1000);

    return { label, interval };
}

// Fades the countdown label out and cleans up its interval/timer.
function removeCountdownLabel(handle){
    if (!handle) return;
    clearInterval(handle.interval);
    handle.label.classList.remove('visible');
    setTimeout(() => handle.label.remove(), 250);
}

// Spawns one small glitter dot at (x, y) in #fx-layer's coordinate
// space, with a little random jitter so a trail of them looks
// scattered rather than a single dotted line.
function spawnGlitterParticle(x, y, color){
    const fx = document.getElementById('fx-layer');
    const particle = document.createElement('div');
    particle.className = 'glitter-particle';

    const size = 3 + Math.random() * 4;
    particle.style.width = size + 'px';
    particle.style.height = size + 'px';

    particle.style.setProperty('--gx', Math.round(x + (Math.random() - 0.5) * 14) + 'px');
    particle.style.setProperty('--gy', Math.round(y + (Math.random() - 0.5) * 14) + 'px');
    particle.style.setProperty('--glitter-color', color);

    fx.appendChild(particle);
    particle.addEventListener('animationend', () => particle.remove());
}

// Repeatedly spawns glitter dots along the object's actual flight
// path for as long as it's still moving. The object only moves
// during the first LAND_AT_FRACTION share of flyDuration (see the
// flyOnly/flyOnlyPop keyframes - position is reached at 70%, then
// it just shrinks in place) - so the trail stops exactly when the
// object arrives, instead of continuing after it's already landed.
function startGlitterTrail(el, flyDuration, color){
    const startX = parseFloat(el.style.getPropertyValue('--start-x')) || 0;
    const startY = parseFloat(el.style.getPropertyValue('--start-y')) || 0;
    const endX = parseFloat(el.style.getPropertyValue('--end-x')) || 0;
    const endY = parseFloat(el.style.getPropertyValue('--end-y')) || 0;

    const travelSeconds = flyDuration * LAND_AT_FRACTION;
    const trailColor = color || '#FFD700';
    const startTime = performance.now();

    const glitterInterval = setInterval(() => {
        const elapsed = (performance.now() - startTime) / 1000;
        const frac = Math.min(1, elapsed / travelSeconds);

        const gx = startX + (endX - startX) * frac;
        const gy = startY + (endY - startY) * frac;

        spawnGlitterParticle(gx, gy, trailColor);

        if (frac >= 1){
            clearInterval(glitterInterval);
        }
    }, 45);
}

// Runs an element through: appear at start position -> hold there,
// fully visible -> fly left and get absorbed into the timer digit.
// `flyDuration` is the same "how long the slide takes" number the
// spawn functions already computed (jewel/tier based). `extraDelay`
// staggers multiple pieces/emoji from the same event slightly, same
// as the old animationDelay did.
//
// `options` is optional:
//   - popOnArrival: true  -> uses flyOnlyPop instead of flyOnly, so
//     the object inflates like a bubble and snap-vanishes right
//     after arriving, instead of smoothly shrinking away.
//   - burstColor: "#RRGGBB" (or any CSS color) -> spawns a small
//     ring + bubble-particle burst at the object's own landing spot,
//     the instant it arrives. Omit either option for the original,
//     plain shrink-and-fade behavior (used by confetti pieces).
//   - countdownText: "Gift arriving" (or similar) -> shows the
//     "<text> in Ns" countdown label during the hold (EXPERIMENTAL,
//     see ENABLE_ARRIVAL_COUNTDOWN above).
//   - glitterTrail: true -> leaves a sparkle trail along the flight
//     path (EXPERIMENTAL, see ENABLE_GLITTER_TRAIL above).
//   - glitterColor: color used for that sparkle trail.
//
// Removes the element itself once the fly phase finishes, and
// returns the TOTAL time (appear + hold + fly) so the caller can
// tell scheduleLanding() when the timer number should actually land -
// i.e. only once the whole show, hold included, is done.
function playHoldThenFly(el, flyDuration, extraDelay, options){
    extraDelay = extraDelay || 0;
    options = options || {};

    const animationName = options.popOnArrival ? 'flyOnlyPop' : 'flyOnly';

    el.style.animation =
        `appearAtStart ${APPEAR_DURATION}s ease ${extraDelay}s forwards`;

    const flyStartDelay =
        extraDelay + APPEAR_DURATION + HOLD_BEFORE_FLY_SECONDS;

    // ---------------- EXPERIMENTAL: ARRIVAL COUNTDOWN ----------------
    let countdownHandle = null;

    if (ENABLE_ARRIVAL_COUNTDOWN && options.countdownText){
        setTimeout(() => {
            countdownHandle = spawnCountdownLabel(
                el,
                options.countdownText,
                HOLD_BEFORE_FLY_SECONDS
            );
        }, extraDelay * 1000);
    }

    setTimeout(() => {
        if (countdownHandle){
            removeCountdownLabel(countdownHandle);
            countdownHandle = null;
        }

        el.style.animation =
            `${animationName} ${flyDuration}s linear forwards`;

        el.addEventListener('animationend', () => el.remove());

        // ---------------- EXPERIMENTAL: GLITTER TRAIL ----------------
        if (ENABLE_GLITTER_TRAIL && options.glitterTrail){
            startGlitterTrail(el, flyDuration, options.glitterColor);
        }
    }, flyStartDelay * 1000);

    // NOTE: this is the value scheduleLanding() uses to time the
    // timer digit's update/pop - it's scaled by LAND_AT_FRACTION so
    // the digit lands when the gift ARRIVES (70%), not when it
    // finishes disappearing (100%). The element itself still keeps
    // playing/removing on its own via the animationend listener
    // above, independent of this returned number.
    const landDelay = extraDelay + APPEAR_DURATION + HOLD_BEFORE_FLY_SECONDS
        + (flyDuration * LAND_AT_FRACTION);

    // Fire the burst at the SAME instant the object visually
    // arrives (70% mark), reading its own --end-x/--end-y so the
    // burst appears exactly where that specific object lands.
    if (options.burstColor){
        setTimeout(() => {
            const endX = parseFloat(el.style.getPropertyValue('--end-x')) || 0;
            const endY = parseFloat(el.style.getPropertyValue('--end-y')) || 0;
            spawnBurstAtLanding(endX, endY, options.burstColor);
        }, landDelay * 1000);
    }

    return landDelay;
}

// Spawns a shockwave ring + a handful of tumbling confetti pieces
// at (endX, endY) - the same coordinate space the fly animations
// use (relative to #fx-layer's center). Called from
// playHoldThenFly() above right as an object arrives, so it looks
// like THAT object bursting apart, not a generic effect on the
// timer itself.
function spawnBurstAtLanding(endX, endY, color){
    const fx = document.getElementById('fx-layer');

    // ---------------- SHOCKWAVE RING ----------------
    const ring = document.createElement('div');
    ring.className = 'impact-ring';
    ring.style.setProperty('--impact-x', endX + 'px');
    ring.style.setProperty('--impact-y', endY + 'px');
    ring.style.setProperty('--impact-color', color);

    fx.appendChild(ring);
    ring.addEventListener('animationend', () => ring.remove());

    // ---------------- CONFETTI BURST ----------------
    const particleCount = 9;

    for (let i = 0; i < particleCount; i++){
        // Spread pieces evenly around a circle, with a little
        // randomness so repeated bursts don't look identical.
        const angle = (Math.PI * 2 * i) / particleCount + (Math.random() * 0.5 - 0.25);
        const dist = 24 + Math.random() * 30;

        const piece = document.createElement('div');
        piece.className = 'impact-confetti';

        // Small rectangles (not circles) so they read as confetti
        // flecks rather than bubbles.
        const w = 4 + Math.random() * 5;
        const h = 3 + Math.random() * 4;
        piece.style.width = w + 'px';
        piece.style.height = h + 'px';
        piece.style.backgroundColor = color;

        piece.style.setProperty('--impact-x', endX + 'px');
        piece.style.setProperty('--impact-y', endY + 'px');
        piece.style.setProperty(
            '--spark-x',
            Math.round(endX + Math.cos(angle) * dist) + 'px'
        );
        piece.style.setProperty(
            '--spark-y',
            Math.round(endY + Math.sin(angle) * dist) + 'px'
        );
        piece.style.setProperty('--impact-color', color);

        // Each piece tumbles as it flies out, instead of staying
        // upright like the old round particles did.
        piece.style.setProperty(
            '--confetti-start-rot',
            Math.round(Math.random() * 360) + 'deg'
        );
        piece.style.setProperty(
            '--confetti-end-rot',
            Math.round(360 + Math.random() * 360) + 'deg'
        );

        fx.appendChild(piece);
        piece.addEventListener('animationend', () => piece.remove());
    }
}

function spawnGiftConfettiAnimation(ev){

    const jewels = Number(ev.value) || 0;
    const intensity = giftIntensity(jewels);

    // Continuous scaling from the exact jewel value.
    const count = Math.round(lerp(5, 55, intensity));
    const baseSize = Math.round(lerp(7, 16, intensity));
    const duration = lerp(0.7, 0.7, intensity);

    // Stable per-gift-name color scheme (base hue + two
    // analogous hues for a bit of variety within the burst).
    const hue = hashHue(ev.name);
    const hues = [hue, (hue + 35) % 360, (hue + 325) % 360];

    const fx = document.getElementById('fx-layer');
    const target = getFlyPositions();

    let totalDuration = 0;

    for (let i = 0; i < count; i++){

        const piece = document.createElement('div');

        const isCircle = Math.random() < 0.35;

        piece.className =
            'confetti-piece ' +
            (isCircle ? 'shape-circle' : 'shape-square');

        const w = Math.round(baseSize * (0.7 + Math.random() * 0.7));
        const h = isCircle
            ? w
            : Math.round(baseSize * (0.4 + Math.random() * 0.5));

        piece.style.width = w + 'px';
        piece.style.height = h + 'px';

        const pieceHue = hues[Math.floor(Math.random() * hues.length)];
        const lightness = 55 + Math.round(Math.random() * 15);

        piece.style.backgroundColor =
            `hsl(${pieceHue}, 85%, ${lightness}%)`;

        // Each piece starts at a slightly different spot along the
        // right side and lands slightly around the target digit,
        // so the burst has some spread instead of a single line.
        const jitter = 10 + count * 0.4;

        const startX = target.startX + Math.round((Math.random() - 0.5) * 30);
        const startY = target.startY + Math.round((Math.random() - 0.5) * 40);
        const endX = target.endX + Math.round((Math.random() - 0.5) * jitter);
        const endY = target.endY + Math.round((Math.random() - 0.5) * jitter);

        const startRot = Math.round((Math.random() - 0.5) * 180);
        const endRot = startRot + Math.round(180 + Math.random() * 360);

        piece.style.setProperty('--start-x', startX + 'px');
        piece.style.setProperty('--start-y', startY + 'px');
        piece.style.setProperty('--end-x', endX + 'px');
        piece.style.setProperty('--end-y', endY + 'px');
        piece.style.setProperty('--start-rot', startRot + 'deg');
        piece.style.setProperty('--end-rot', endRot + 'deg');
        piece.style.setProperty('--peak-scale', '1.1');
        piece.style.setProperty('--end-scale', '0.1');

        fx.appendChild(piece);

        const pieceExtraDelay = Math.random() * 0.15;
        const pieceTotal = playHoldThenFly(piece, duration, pieceExtraDelay);

        totalDuration = Math.max(totalDuration, pieceTotal);
    }

    return { duration: totalDuration, big: intensity > 0.55 };
}


// ---------------- GIFT IMAGES: REAL GIFT ARTWORK ----------------
// If a gift has a matching image in gift_images/ (see
// GIFT_IMAGE_FILES below, loaded from /gift-images-manifest),
// the overlay shows the ACTUAL gift picture instead of confetti.
// It flies in from OUTSIDE the timer box, lands on the numbers,
// and gets "absorbed" right as the timer digits update/pop.
//
// Gifts with no matching image automatically fall back to the
// confetti animation above, so nothing ever shows blank.

// Populated on page load from /gift-images-manifest.
// Maps a normalized gift name -> image filename in gift_images/.
let GIFT_IMAGE_FILES = {};

async function loadGiftImageManifest(){
    try {
        const r = await fetch('/gift-images-manifest');
        GIFT_IMAGE_FILES = await r.json();
    } catch (e){
        GIFT_IMAGE_FILES = {};
    }
}

// ---------------- GIFT COLORS (for the timer pop) ----------------
// Each gift's own two-tone color now lives in gift_images/_manifest.json
// itself, right next to that gift's image filename - one file to edit
// instead of two. GIFT_IMAGE_FILES (loaded below from
// /gift-images-manifest) already has both: { file, colors }.
function getGiftPopColors(giftName){
    const key = String(giftName || "").trim().toLowerCase();
    const entry = GIFT_IMAGE_FILES[key];
    return (entry && entry.colors) ? entry.colors : null;
}

// ---------------- TEST SWITCH ----------------
// true  -> gifts AND Super Chats ALSO trigger the expanding/gold
//          timer pop (the classic look).
// false -> gifts and Super Chats animate on their own, WITHOUT
//          the timer digits popping/expanding at all.
// Flip this to compare the two looks - no other code needs to change.
const EXPAND_TIMER_ON_INCREASE = true;

function applyTimerPop(big, giftName){
    const timeEl = document.getElementById('time');

    // Same animation as always (timeIncreasePop / timeIncreasePopHuge) -
    // only the two colors it flashes through change, based on which
    // gift triggered it. No matching gift (e.g. Super Chat, numpad
    // hotkey) -> falls back to the original green/gold.
    const colors = getGiftPopColors(giftName);

    if (colors){
        timeEl.style.setProperty('--pop-color', colors[0]);
        timeEl.style.setProperty('--pop-glow', colors[1]);
    } else {
        timeEl.style.removeProperty('--pop-color');
        timeEl.style.removeProperty('--pop-glow');
    }

    timeEl.classList.remove('time-increased', 'time-increased-huge');

    void timeEl.offsetWidth;

    timeEl.classList.add(
        big ? 'time-increased-huge' : 'time-increased'
    );
}

function spawnGiftImageAnimation(ev, imageSrc){

    const jewels = Number(ev.value) || 0;
    const intensity = giftIntensity(jewels);

    // Gift image is always the same size as the digit timer box
    // (115px) - no more jewel-based size scaling. Duration still
    // scales with jewel value so bigger gifts still feel bigger.
    const size = 155;
    const duration = lerp(0.7, 0.7, intensity);

    const target = getFlyPositions();
    const startRot = Math.round((Math.random() - 0.5) * 40);

    const fx = document.getElementById('fx-layer');

    // The wrap is what actually flies/fades (see .gift-image-wrap in
    // the CSS) - the image and, for combo taps, the "xN" badge are
    // just plain children riding along inside it as one unit.
    const wrap = document.createElement('div');
    wrap.className = 'gift-image-wrap';
    wrap.style.width = size + 'px';
    wrap.style.height = size + 'px';

    const img = document.createElement('img');
    img.className = 'gift-image-fly';
    // imageSrc is either SSN's own real gift image URL (ev.image_url,
    // preferred - see spawnGiftAnimation below) or our local
    // /gift-image/<file> fallback from gift_images/_manifest.json.
    img.src = imageSrc;
    img.alt = ev.name || 'gift';
    img.style.width = '100%';
    img.style.height = '100%';

    // If the image fails to load (missing/renamed file), fall
    // back to confetti instead of showing a broken image icon.
    img.addEventListener('error', () => {
        wrap.remove();
        spawnGiftConfettiAnimation(ev);
    });

    wrap.appendChild(img);

    // ---------------- COMBO COUNT BADGE ----------------
    // combo_count is set by the backend (see push_event/track_gift_combo)
    // to this tap's position within an in-progress Gift Combo. The very
    // first tap (1) shows the plain gift image, same as always - only
    // the 2nd, 3rd, 4th... taps get an "xN" badge stamped on them, so a
    // rapid combo visibly counts up instead of replaying an identical
    // gift image every time.
    const comboCount = Number(ev.combo_count) || 1;

    if (comboCount > 1){
        const badge = document.createElement('div');
        badge.className = 'gift-combo-badge';
        badge.textContent = 'x' + comboCount;
        badge.style.fontSize = Math.round(size * 0.22) + 'px';
        wrap.appendChild(badge);
    }

    wrap.style.setProperty('--start-x', target.startX + 'px');
    wrap.style.setProperty('--start-y', target.startY + 'px');
    wrap.style.setProperty('--end-x', target.endX + 'px');
    wrap.style.setProperty('--end-y', target.endY + 'px');
    wrap.style.setProperty('--start-rot', startRot + 'deg');
    wrap.style.setProperty('--end-rot', '0deg');

    // Keep the gift a STEADY size the whole time it flies -
    // peak-scale matches start-scale, so it never grows mid-flight.
    // It only shrinks away at the very end, right as it lands.
    wrap.style.setProperty('--start-scale', '0.5');
    wrap.style.setProperty('--peak-scale', '0.5');
    wrap.style.setProperty('--end-scale', '0.12');
    wrap.style.setProperty('--glow', 'hsl(270, 90%, 65%)');

    fx.appendChild(wrap);

    const totalDuration = playHoldThenFly(wrap, duration, 0, {
        // popOnArrival removed - that's what caused the extra
        // inflate/grow right as it arrived. Now it just shrinks
        // away smoothly.
        burstColor: 'hsl(270, 90%, 65%)',
        countdownText: `${ev.name || 'Gift'} arriving`,
        glitterTrail: true,
        glitterColor: 'hsl(270, 90%, 65%)'
    });

    return { duration: totalDuration, big: intensity > 0.55 };
}

function spawnGiftAnimation(ev){

    const key = String(ev.name || "").trim().toLowerCase();
    const entry = GIFT_IMAGE_FILES[key];
    const localFile = entry && entry.file;

    // 1) Prefer SSN's own real gift image (ev.image_url), pulled
    //    straight from YouTube's gift catalog by SSN itself.
    // 2) Fall back to our local gift_images/_manifest.json entry
    //    for this gift name, if SSN didn't supply one.
    // 3) Fall back to confetti if neither is available.
    const imageSrc = ev.image_url || (localFile ? ('/gift-image/' + localFile) : null);

    if (imageSrc){
        return spawnGiftImageAnimation(ev, imageSrc);
    }

    return spawnGiftConfettiAnimation(ev);
}


// ---------------- SUPER CHAT: REAL ARTWORK + $ VALUE ON THE TICKET ----------------
// If gift_images/_manifest.json has a "superchat" entry (see
// GIFT_IMAGE_FILES, loaded from /gift-images-manifest, same as
// gifts use), the overlay flies in that actual Super Chat ticket
// image - ONE copy, same as a real gift - with the exact dollar
// amount printed right below the "SUPERCHAT" wording, baked into
// the same box so it travels and fades together with the ticket.
// No matching image -> falls back to a single emoji instead, so
// nothing ever shows blank.
const SUPERCHAT_EMOJI = {
    small: "💵",
    medium: "💰",
    large: "🤑",
    huge: "💸"
};

// Width in px of the flying ticket/emoji box, scaled by tier.
// Height follows automatically from the image's own aspect ratio
// (or a fixed ratio for the emoji fallback - see buildSuperchatIcon).
const SUPERCHAT_TIER_WIDTH = { small: 120, medium: 165, large: 210, huge: 260 };
const SUPERCHAT_TIER_DURATION = { small: 0.7, medium: 0.7, large: 0.7, huge: 0.7 };

// Font size for the "$12.50" printed below the ticket text, scaled
// with tier so it stays readable without overflowing the ticket.
//const SUPERCHAT_VALUE_FONT_SIZE = { small: 46, medium: 48, large: 52, huge: 55 };
const SUPERCHAT_VALUE_FONT_SIZE = { small: 46, medium: 46, large: 46, huge: 46 };
function tierFromUsd(usd){
    usd = Number(usd) || 0;
    if (usd < 5) return "small";
    if (usd < 20) return "medium";
    if (usd < 100) return "large";
    return "huge";
}

// Formats a USD amount as a currency string, e.g. 12 -> "$12.00",
// 7.5 -> "$7.50". Falls back to a plain "$0.00" if the value is
// missing/invalid, so the label never renders blank.
function formatSuperchatValue(usd){
    usd = Number(usd);
    if (!isFinite(usd)) usd = 0;

    try {
        return new Intl.NumberFormat('en-US', {
            style: 'currency',
            currency: 'USD'
        }).format(usd);
    } catch (e){
        return '$' + usd.toFixed(2);
    }
}

// Builds the fallback emoji icon used when no "superchat" image is
// in the manifest (or the real image fails to load).
function buildSuperchatEmojiIcon(tier, width){
    const span = document.createElement('span');
    span.className = 'superchat-emoji-icon';
    span.textContent = SUPERCHAT_EMOJI[tier];
    span.style.fontSize = Math.round(width * 0.7) + 'px';
    return span;
}

function spawnSuperchatAnimation(ev){

    const tier = tierFromUsd(ev.value);

    // Super Chat image is always the same size as the digit timer
    // box (115px) - no more tier-based width. Duration and the $
    // label font size still scale with tier.
    const width = 295;
    const duration = SUPERCHAT_TIER_DURATION[tier];

    // Real Super Chat artwork: prefer SSN's own image (ev.image_url),
    // then fall back to the local "superchat" entry in
    // gift_images/_manifest.json - same lookup gifts already use.
    const imageEntry = GIFT_IMAGE_FILES['superchat'];
    const localFile = imageEntry && imageEntry.file;
    const imageSrc = ev.image_url || (localFile ? ('/gift-image/' + localFile) : null);

    const fx = document.getElementById('fx-layer');
    const target = getFlyPositions();
    const startRot = Math.round((Math.random() - 0.5) * 20);

    // Single flying box - the ticket image (or emoji) plus the $
    // value both live inside it, so they move/fade as ONE unit.
    const wrap = document.createElement('div');
    wrap.className = 'superchat-wrap';
    wrap.style.width = width + 'px';
    wrap.style.setProperty('--glow', '#FFD700');

    if (imageSrc){
        const img = document.createElement('img');
        img.className = 'superchat-image';
        img.src = imageSrc;
        img.alt = 'Super Chat';

        // If the real image fails to load (missing/renamed file),
        // swap in the emoji instead of a broken image icon.
        img.addEventListener('error', () => {
            img.remove();
            wrap.insertBefore(
                buildSuperchatEmojiIcon(tier, width),
                wrap.firstChild
            );
        });

        wrap.appendChild(img);
    } else {
        // No ticket-shaped box to measure, so give the wrap a fixed
        // height itself, tall enough for the emoji + label below it.
        wrap.style.height = Math.round(width * 0.85) + 'px';
        wrap.appendChild(buildSuperchatEmojiIcon(tier, width));
    }

    const label = document.createElement('div');
    label.className = 'superchat-value-text';
    label.textContent = formatSuperchatValue(ev.value);
    label.style.fontSize = SUPERCHAT_VALUE_FONT_SIZE[tier] + 'px';
    wrap.appendChild(label);

    wrap.style.setProperty('--start-x', target.startX + 'px');
    wrap.style.setProperty('--start-y', target.startY + 'px');
    wrap.style.setProperty('--end-x', target.endX + 'px');
    wrap.style.setProperty('--end-y', target.endY + 'px');
    wrap.style.setProperty('--start-rot', startRot + 'deg');
    wrap.style.setProperty('--end-rot', '0deg');

    // Keep the Super Chat box a STEADY size the whole time it flies -
    // peak-scale matches start-scale, so it never grows mid-flight.
    // It only shrinks away at the very end, right as it lands.
    wrap.style.setProperty('--start-scale', '0.5');
    wrap.style.setProperty('--peak-scale', '0.5');
    wrap.style.setProperty('--end-scale', '0.12');

    fx.appendChild(wrap);

    const totalDuration = playHoldThenFly(wrap, duration, 0, {
        // popOnArrival removed - that's what caused the extra
        // inflate/grow right as it arrived. Now it just shrinks
        // away smoothly, same steady feel as gifts.
        burstColor: '#FFD700',
        countdownText: 'Super Chat arriving',
        glitterTrail: true,
        glitterColor: '#FFD700'
    });

    return { duration: totalDuration, big: (tier === 'large' || tier === 'huge') };
}


let previousSeconds = null;
let lastEventId = 0;
let firstStateLoad = true;

// ---------------- DELAYED "LANDING" FOR THE TIMER NUMBER ----------------
// While a gift/Super Chat animation is still flying in, the timer
// digits are frozen at their old value. Only once the animation
// finishes ("lands") does the number jump to the new total and
// (optionally) pop. If several land at once, the number waits for
// the LAST one, then jumps straight to the latest known value.
let lastKnownSeconds = 0;
let displayedSeconds = 0;
let pendingLandings = 0;

function renderDisplayedSeconds(){
    document.getElementById('time').innerText = fmt(displayedSeconds);
}

function scheduleLanding(anim, giftName){
    if (!anim) return;

    pendingLandings++;

    setTimeout(() => {
        pendingLandings = Math.max(0, pendingLandings - 1);

        if (pendingLandings === 0){
            displayedSeconds = lastKnownSeconds;
            renderDisplayedSeconds();
        }

        if (EXPAND_TIMER_ON_INCREASE){
            applyTimerPop(!!anim.big, giftName);
        }
    }, Math.max(0, anim.duration) * 1000);
}


async function update(){

    let r = await fetch('/state?since=' + lastEventId);

    let d = await r.json();

    const timeEl = document.getElementById('time');

    lastKnownSeconds = d.seconds;

    let handledByEvent = false;

    // Skip animating on the very first load so old/backlogged
    // events don't all fire at once when the overlay starts.
    if (!firstStateLoad && d.events && d.events.length){
        for (const ev of d.events){
            const anim = (ev.type === "superchat")
                ? spawnSuperchatAnimation(ev)
                : spawnGiftAnimation(ev);

            scheduleLanding(anim, ev.type === "superchat" ? null : ev.name);
        }
        handledByEvent = true;
    }

    if (firstStateLoad){
        // First load: just sync straight to the real value,
        // no freeze/reveal needed.
        displayedSeconds = d.seconds;
    }

    firstStateLoad = false;

    if (typeof d.last_event_id === "number"){
        lastEventId = d.last_event_id;
    }

    // Fallback pop (no gift/superchat animation) for time added
    // outside of gifts/superchats, e.g. numpad hotkeys - these
    // aren't part of the fly-in system, so update immediately.
    if (
        !handledByEvent &&
        previousSeconds !== null &&
        d.seconds > previousSeconds
    ){
        timeEl.style.removeProperty('--pop-color');
        timeEl.style.removeProperty('--pop-glow');

        timeEl.classList.remove('time-increased');

        void timeEl.offsetWidth;

        timeEl.classList.add('time-increased');
    }

    previousSeconds = d.seconds;

    // While a gift/superchat animation is still flying in
    // (pendingLandings > 0), keep the displayed number frozen -
    // it gets updated by scheduleLanding() instead. Otherwise,
    // keep it in sync with the real, ticking value.
    if (pendingLandings === 0){
        displayedSeconds = d.seconds;
    }

    renderDisplayedSeconds();

}



function getFadeTexts(isLocked){

    if (isLocked){

        return [
            "Definitely ending/raiding streamer in...",
            "Timer locked."
        ];

    }

    return [
        "Stream ends in...",
        "Super Chat/Gift to add time"
    ];

}


let index = 0;

function fadeTextSwap(newText){

    const el =
        document.getElementById("msg");

    // fade out
    el.style.opacity = 0;

    setTimeout(() => {

        el.innerText = newText;

        el.style.opacity = 1;

    }, 400);

}


let currentLocked = false;

let activeTexts =
    getFadeTexts(false);


setInterval(async () => {

    let r = await fetch('/state');

    let d = await r.json();

    // detect lock change
    if (
        d.locked !==
        currentLocked
    ){

        currentLocked =
            d.locked;

        activeTexts =
            getFadeTexts(
                currentLocked
            );

        index = 0;

    }

    index =
        (index + 1) %
        activeTexts.length;

    fadeTextSwap(
        activeTexts[index]
    );

}, 5000);


setInterval(
    update,
    500
);

loadGiftImageManifest();
update();

</script>

</body>

</html>
"""


# ============================================================
# GLOBAL HOTKEYS
# ============================================================

def register_hotkeys():

    if keyboard is None:

        print(
            "Global hotkeys NOT enabled."
        )

        print(
            "Install keyboard with:"
        )

        print(
            "pip install keyboard"
        )

        return


    # Prevent rapid duplicate key events.
    last_press_by_scan_code = {}

    debounce_seconds = 0.25


    allowed_digit_names_by_scan_code = {

        79: {
            "1",
            "num 1",
            "numpad 1"
        },

        80: {
            "2",
            "num 2",
            "numpad 2"
        },

        81: {
            "3",
            "num 3",
            "numpad 3"
        },

        75: {
            "4",
            "num 4",
            "numpad 4"
        },

        76: {
            "5",
            "num 5",
            "numpad 5"
        },

        77: {
            "6",
            "num 6",
            "numpad 6"
        }

    }


    navigation_names = {

        "left",
        "right",
        "up",
        "down",

        "home",
        "end",

        "page up",
        "page down",

        "insert",
        "delete"

    }


    def on_key_event(event):

        if event.event_type != "down":
            return


        seconds = NUMPAD_SCAN_CODE_SECONDS.get(
            event.scan_code
        )

        if seconds is None:
            return


        key_name = (
            event.name or ""
        ).lower()


        # Navigation keys can share
        # scan codes with numpad keys.
        if key_name in navigation_names:
            return


        # Reject top-row numbers.
        if key_name not in allowed_digit_names_by_scan_code.get(
            event.scan_code,
            set()
        ):

            print(
                f"Ignored numpad-like "
                f"scan_code={event.scan_code} "
                f"name={event.name!r}"
            )

            return


        now = time.time()

        last_press = (
            last_press_by_scan_code.get(
                event.scan_code,
                0
            )
        )


        if (
            now - last_press
            < debounce_seconds
        ):
            return


        last_press_by_scan_code[
            event.scan_code
        ] = now


        add_time(seconds)


        print(
            f"NUMPAD HOTKEY "
            f"scan_code={event.scan_code} "
            f"name={event.name!r} "
            f"-> +{seconds}s"
        )


    try:

        keyboard.hook(
            on_key_event
        )


        for (
            scan_code,
            seconds
        ) in NUMPAD_SCAN_CODE_SECONDS.items():

            print(
                f"Registered NUMPAD "
                f"scan code {scan_code} "
                f"-> +{seconds}s"
            )


        if SUBTRACT_HOTKEYS:

            print(
                "WARNING: "
                "SUBTRACT_HOTKEYS is not empty, "
                "but subtract hotkeys are disabled."
            )


        print(
            "Global numpad add hotkeys enabled."
        )

        print(
            "Regular number row should do nothing."
        )


    except Exception as e:

        print(
            "Global hotkeys failed to start:",
            e
        )

        print(
            "Try running Command Prompt "
            "as Administrator."
        )


def register_test_animation_hotkeys():
    """
    FOR TESTING ONLY.

    Registers global hotkeys that trigger the overlay's
    gift/superchat tier animations on demand, so you don't
    have to wait for a real gift or Super Chat to preview them.

    These hotkeys do NOT add any time to the timer.

    Controlled by ENABLE_TEST_ANIMATION_HOTKEYS near the top
    of this file - set it to False to turn this off entirely.
    """

    if not ENABLE_TEST_ANIMATION_HOTKEYS:
        print(
            "Test animation hotkeys are DISABLED "
            "(ENABLE_TEST_ANIMATION_HOTKEYS = False)."
        )
        return

    if keyboard is None:
        print(
            "Test animation hotkeys NOT enabled "
            "(the 'keyboard' module is not installed)."
        )
        return

    def make_handler(event_type, name, value, combo, tier_label):
        def handler():
            push_event(event_type, name, value, 0)
            print(
                f"TEST HOTKEY {combo} -> "
                f"{event_type} ({tier_label} tier) "
                f"animation triggered"
            )
        return handler

    try:

        for combo, (
            event_type,
            tier_label,
            name,
            value
        ) in TEST_ANIMATION_HOTKEYS.items():

            keyboard.add_hotkey(
                combo,
                make_handler(
                    event_type,
                    name,
                    value,
                    combo,
                    tier_label
                )
            )

        print(
            "Test animation hotkeys enabled (TESTING ONLY):"
        )

        for combo, (
            event_type,
            tier_label,
            name,
            value
        ) in TEST_ANIMATION_HOTKEYS.items():

            label = (
                name if event_type == "gift"
                else f"${value} Super Chat"
            )

            print(
                f"  {combo} -> "
                f"{event_type} / {tier_label} tier "
                f"({label})"
            )

    except Exception as e:

        print(
            "Test animation hotkeys failed to start:",
            e
        )

        print(
            "Try running Command Prompt "
            "as Administrator."
        )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    load_state()
    load_exchange_rate_cache()

    last_tick = time.time()

    threading.Thread(
        target=tick,
        daemon=True
    ).start()

    threading.Thread(
        target=combo_watcher,
        daemon=True
    ).start()

    register_hotkeys()
    register_test_animation_hotkeys()

    threading.Thread(
        target=start_ssn_listener,
        daemon=True
    ).start()

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False
    )
