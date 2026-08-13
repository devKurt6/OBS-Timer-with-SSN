from flask import Flask, request, jsonify
import time
import threading
import json
import os
import re
import urllib.error
import urllib.request
import urllib.parse
import asyncio
import hashlib
from collections import OrderedDict
from urllib.parse import quote

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

# ---------------- DONATION LOGS ----------------

LOG_FOLDER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "logs"
)

# Create logs folder automatically if it doesn't exist.
os.makedirs(LOG_FOLDER, exist_ok=True)

STATE_FILE = "timer_state.json"


# ---------------- SOCIAL STREAM NINJA ----------------
# Set this to the Session ID shown in SSN's Session Options.
SSN_SESSION_ID = "CdwUvNURjp"
SSN_WEBSOCKET_SERVER = "wss://io.socialstream.ninja"
SSN_RECONNECT_MAX_SECONDS = 30
SSN_DUPLICATE_WINDOW_SECONDS = 15


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

def process_super_chat(donation_text):
    """
    Convert a Super Chat amount to USD and then to seconds.
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

# Fallback values used only when SSN omits meta.youtubeGift.jewelsAmount.
GIFT_JEWEL_VALUES = {
    "sparkles": 2, "star": 2, "100": 2, "chili": 6, "makeup brush": 6,
    "glhf": 10, "gg": 10, "thumbs up": 10, "press f": 10,
    "floating heart": 10, "happy poop": 10, "w": 10, "hiding": 10,
    "gold coin": 10, "ggez": 20, "party hat": 20, "clutch": 30,
    "flower": 30, "six seven": 67, "clock it": 80, "high five": 100,
    "marshmallow hi": 100, "sundae": 100, "jammin": 200, "flow state": 200,
    "sunshine": 200, "clapping seal": 250, "laughing disco": 250, "mvp": 250,
    "girl power": 300, "finger heart": 350, "fortune cookie": 380,
    "unc alert": 440, "blowfish": 450, "butterfly": 450,
    "party blowers": 500, "controller": 500, "power potion": 500,
    "sun balloon": 500, "tulips": 500, "watermelon": 500,
    "shopping cart": 550, "stay hydrated": 550, "picnic basket": 600,
    "mic drop": 600, "envelope": 650, "mystery gift": 650, "nap": 680,
    "island": 700, "let em cook": 750, "gg keys": 800, "husky": 800,
    "turn it up": 800, "duckling": 900, "ramen bowl": 900,
    "air travel": 1000, "guitar": 1000, "w pinata": 1000, "cheers": 1000,
    "goat trophy": 1000, "loot box": 1000, "corgi": 1000, "trophy cake": 1000,
    "sand castle": 1200, "bumblebee": 1250, "bouquet": 1500,
    "headliner": 1500, "tube dancer": 1500, "bubble heart": 1500,
    "spill the tea": 1600, "jelly friends": 2500, "bravo": 2500,
    "firework": 3000, "headphones": 3600, "hound": 3800, "biscuit tin": 5000,
    "sushi": 5500, "hot pot": 6800, "hooray": 7000, "rock star": 7800,
    "sports car": 9500, "limo": 10000, "trifle smash": 10000,
    "super gg": 10000, "seal splash": 12000, "ball party": 12000,
    "matsuri": 12000, "tokyo night": 15000, "gamer corgi": 15000,
    "night market": 15500, "idol life": 17000, "victory spin": 17000,
    "castle": 18000, "racing game": 20000, "happy birthday": 20000,
    "seaside": 22500,
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
    normalized_name = " ".join(str(gift_name or "").strip().casefold().split())
    jewels = GIFT_JEWEL_VALUES.get(normalized_name)
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

        if jewels <= 0:
            print(
                "JEWEL GIFT NOT ADDED: "
                "SSN gave no Jewel amount and "
                "the gift name was not matched."
            )

            log_donation(
                f"EVENT: JEWEL GIFT\n"
                f"GIFT: {gift_name}\n"
                f"RESULT: FAILED\n"
                f"REASON: Jewel amount unavailable"
            )

            return False

        seconds = jewels * GIFT_SECONDS_PER_JEWEL

        add_time(seconds)

        print(
            f"JEWEL DONATION: "
            f"{gift_name} -> "
            f"{jewels:g} Jewels ({source}) -> "
            f"+{seconds:.2f} seconds"
        )

        log_donation(
            f"EVENT: JEWEL GIFT\n"
            f"GIFT: {gift_name}\n"
            f"JEWELS: {jewels:g}\n"
            f"SOURCE: {source}\n"
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

        success = process_super_chat(
            donation_text
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
                    if str(data.get("event") or "").lower() not in {
                        "jeweldonation", "superchat"
                    }:
                        continue
                    if is_duplicate_event(data):
                        print("Ignored duplicate SSN event.")
                        continue

                    if is_duplicate_ssn_event(data):
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

    with lock:

        return jsonify({
            "seconds": timer_seconds,
            "bank_seconds": bank_seconds,
            "running": timer_running,
            "locked": timer_locked,
            "bank": bank_seconds
        })


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

    width: fit-content;
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

    background: rgba(58, 58, 58, 0.9);

    display: flex;
    flex-direction: column;
    align-items: center;

    text-align: center;

    color: white;

    overflow: hidden;
}

#msg {
    width: 100%;
    height: 20px;

    display: flex;
    justify-content: center;
    align-items: center;

    font-size: 15px;
    font-weight: 600;
    line-height: 1.2;

    text-align: center;

    white-space: pre-line;

    margin-top: 10px;
    margin-bottom: 0;

    opacity: 1;

    transition: opacity .8s ease;

    flex-shrink: 0;
}

#time {
    flex: 1;

    display: flex;
    justify-content: center;
    align-items: center;

    width: 100%;

    font-size: 110px;
    font-weight: 530;

    margin-top: 0;
}

</style>

</head>

<body>

<div id="wrap">

    <!--<div id="left">
        Tell or ask me anything.
    </div>-->

    <div id="right">

        <!--
        <div id="msg">
            Stream ends in...
        </div>
        -->

        <div id="time">
            00:00:00
        </div>

    </div>

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


async function update(){

    let r = await fetch('/state');

    let d = await r.json();

    document.getElementById(
        'time'
    ).innerText = fmt(d.seconds);

}


function getFadeTexts(isLocked){

    if (isLocked){

        return [
            "Definitely ending/raiding \\n streamer in...",
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

    register_hotkeys()

    threading.Thread(
        target=start_ssn_listener,
        daemon=True
    ).start()

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False
    )
