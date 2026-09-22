from flask import Flask, request, jsonify, send_from_directory
import time
import threading
import queue
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
import subprocess

try:
    import websockets
except ImportError:
    websockets = None

try:
    import keyboard
except ImportError:
    keyboard = None


app = Flask(__name__)


@app.after_request
def _allow_extension_requests(response):
    """Permit the browser-extension overlay (running on youtube.com,
    relaying through its background service worker) to POST combo
    updates to this local server. Harmless for a personal/local-only
    tool - this server only listens on 127.0.0.1 to begin with.

    Access-Control-Allow-Private-Network answers Chrome's newer
    Private Network Access preflight, which can otherwise block a
    request originating from a public https:// page/extension context
    toward a private/loopback address like 127.0.0.1.
    """

    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


# ============================================================
# CONFIG
# ============================================================

# ---------------- JEWELS ----------------
# 1 Jewel = 1 second
GIFT_SECONDS_PER_JEWEL = .5


# ---------------- SUPER CHAT ----------------
# $1 USD = 60 seconds
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


# ---------------- GIFT EVENT COLOR (overlay animation tint) ----------------
# NOTE: Corsair/iCUE keyboard lighting has moved out of this file -
# see keyboard_color_server.py, which runs as its own separate
# process/server. This constant is unrelated to that: it's the tint
# color get_gift_color() below hands to push_event() so the browser
# overlay's flying gift animation can color itself (see
# recent_events / push_event()'s "color" field).
#
# Every gift uses this same tint, regardless of which gift it is.
# Set to None to fall back to the per-gift color listed in
# gift_images/_manifest.json instead.
GIFT_EVENT_COLOR = "#8A2BE2"  # violet

# Super Chats get their tint from YouTube's own tier color
# (reported by the browser extension - see last_superchat_color).


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

# Left-side "Tell or ask me anything." text on the overlay: True
# (default) rotates through left_texts every 10 seconds (see the
# timer page's JS below); False keeps it pinned to a single fixed
# line. Toggled from the Dashboard (/left-text/enable,
# /left-text/disable) so it can be flipped mid-stream.
left_text_scroll_enabled = True

# Whether the ENTIRE "Tell or ask me anything." box (background +
# text, id="left" in the overlay) is shown at all. Separate from
# left_text_scroll_enabled above, which only controls whether the
# text rotates or stays fixed on the box - this controls whether the
# box renders on the overlay at all. Toggled from the Dashboard
# (/left-box/enable, /left-box/disable).
left_box_enabled = True

# Whether the ENTIRE right-side box (timer + "Stream ends in..."
# message, id="right" in the overlay) is shown at all. Same idea as
# left_box_enabled above, but for the right-side box. Toggled from
# the Dashboard (/right-box/enable, /right-box/disable).
right_box_enabled = True

# Whether just the small message line above the timer digits
# (id="msg" in the overlay - "Stream ends in..." etc.) is shown.
# Separate from right_box_enabled above, which hides the WHOLE
# right box (timer included) - this only hides the message line,
# and the timer digits expand upward to fill the freed space
# (#time-row already has flex:1, so it fills #right on its own
# once #msg is taken out of the layout). Toggled from the
# Dashboard (/msg-box/enable, /msg-box/disable).
msg_box_enabled = True

# The actual lines the left-side text rotates through. Editable
# from the Dashboard (a textarea, one line per message - "how many
# messages" it cycles through is just how many lines you put there)
# instead of having to edit code. The first entry is always what's
# shown while scrolling is OFF. Saved to/loaded from STATE_FILE like
# everything else here, so edits survive a restart.
DEFAULT_LEFT_TEXTS = [
    "Tell or ask me anything.",
    "Type your question in chat!"
]
left_texts = list(DEFAULT_LEFT_TEXTS)

# How long (in seconds) each line in left_texts stays on screen
# before rotating to the next one. This list is PARALLEL to
# left_texts: left_text_durations[0] is how long left_texts[0] shows,
# left_text_durations[1] is how long left_texts[1] shows, and so on.
# Editable per-line from the Dashboard (a seconds box next to each
# line). Any line without a saved duration uses
# DEFAULT_LEFT_TEXT_SECONDS.
DEFAULT_LEFT_TEXT_SECONDS = 10
MIN_LEFT_TEXT_SECONDS = 1
MAX_LEFT_TEXT_SECONDS = 3600
left_text_durations = [DEFAULT_LEFT_TEXT_SECONDS] * len(left_texts)

# Look-and-feel of the left-side text (font family, size, bold,
# italic, color). Editable from the Dashboard so the user isn't
# stuck with whatever was hardcoded in CSS. Applied on the overlay
# page as inline styles pulled from /state (see left_text_style
# handling in the timer page's JS below).
DEFAULT_LEFT_TEXT_STYLE = {
    "font_family": "Arial, sans-serif",
    "font_size": 32.67,     # px
    "bold": True,
    "italic": False,
    "color": "#FFFFFF"
}
left_text_style = dict(DEFAULT_LEFT_TEXT_STYLE)


# ---------------- TIMER MESSAGE TEXT (the line above the digits) ----------------
# Same idea as left_texts/left_text_style above, but for the small
# line above the big digit timer (id="msg" on the overlay - default
# "Stream ends in..." / "Super Chat/Gift to add time"). It has its
# own separate set of lines for when the timer is LOCKED (default
# "Raiding streamer in..." / "Timer locked."), since that's already
# a special-case message. Both are editable from the Dashboard.
DEFAULT_MSG_TEXTS_UNLOCKED = [
    "Stream ends in...",
    "Super Chat/Gift to add time"
]
DEFAULT_MSG_TEXTS_LOCKED = [
    "Raiding streamer in...",
    "Timer locked."
]
msg_texts_unlocked = list(DEFAULT_MSG_TEXTS_UNLOCKED)
msg_texts_locked = list(DEFAULT_MSG_TEXTS_LOCKED)

DEFAULT_MSG_TEXT_STYLE = {
    "font_family": "Arial, sans-serif",
    "font_size": 16.03,     # px
    "bold": True,
    "italic": False,
    "color": "#FFFFFF"
}
msg_text_style = dict(DEFAULT_MSG_TEXT_STYLE)


def sanitize_text_style(raw, base):
    """
    Takes a dict of (possibly partial, possibly bad) style overrides
    and returns a full, safe style dict layered on top of `base`.
    Used for both left_text_style and msg_text_style. Unknown/
    invalid fields are ignored rather than raising, so one bad field
    doesn't reject the whole request.
    """

    result = dict(base)

    if not isinstance(raw, dict):
        return result

    if "font_family" in raw:
        # Strip characters that have no business in a font-family
        # value, mainly so this can't be used to break out of the
        # inline style attribute (quotes, braces, semicolons).
        family = re.sub(r'[^a-zA-Z0-9 ,\-\'"]', "", str(raw["font_family"])).strip()
        if family:
            result["font_family"] = family[:120]

    if "font_size" in raw:
        try:
            size = float(raw["font_size"])
            result["font_size"] = max(8, min(200, size))
        except (TypeError, ValueError):
            pass

    if "bold" in raw:
        result["bold"] = bool(raw["bold"])

    if "italic" in raw:
        result["italic"] = bool(raw["italic"])

    if "color" in raw:
        color = str(raw["color"]).strip()
        if re.fullmatch(r"#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?", color):
            result["color"] = color

    return result


def sanitize_durations(raw_durations, count):
    """
    Returns a list of exactly `count` per-line durations in seconds.
    Bad/missing/out-of-range entries fall back to (or are clamped
    toward) sane values instead of raising, so one bad number can
    never break the rotation.
    """

    if not isinstance(raw_durations, list):
        raw_durations = []

    result = []

    for i in range(count):
        try:
            value = float(raw_durations[i])
            if value != value:  # NaN
                raise ValueError
        except (IndexError, TypeError, ValueError):
            value = DEFAULT_LEFT_TEXT_SECONDS

        value = max(MIN_LEFT_TEXT_SECONDS, min(MAX_LEFT_TEXT_SECONDS, value))

        # Store whole numbers as ints (10 instead of 10.0).
        result.append(int(value) if value == int(value) else value)

    return result


def sanitize_text_lines(raw_texts, fallback):
    """
    Cleans a list of text lines (strips whitespace, drops empties).
    Returns `fallback` if the result would otherwise be empty, so a
    text block can never be saved with zero lines.
    """

    if not isinstance(raw_texts, list):
        return list(fallback)

    cleaned = [str(t).strip() for t in raw_texts if str(t).strip()]

    return cleaned or list(fallback)

last_tick = time.time()



# IMPORTANT:
# RLock allows the same thread to acquire the lock again.
# This prevents add_time() -> save_state() from deadlocking.
lock = threading.RLock()


# ---------------- SUPER CHAT COLOR (from browser extension) ----------------
# Most recent YouTube tier color reported by youtube.js/background.js
# for a Super Chat that just appeared in chat (see
# /superchat/color-update below). process_super_chat() - fired a
# moment later by SSN's webhook - picks this up if it arrives within
# SUPERCHAT_COLOR_MATCH_WINDOW_SECONDS, so the flying animation can be
# colored to match the real donation tier instead of a fixed color.
#
# Only the single most recent color is kept (not a queue matched by
# amount) because Super Chats normally land one at a time - unlike
# Gift Combos, they don't repeat rapidly for the same user.
last_superchat_color = {"color": None, "ts": 0.0}
superchat_color_lock = threading.Lock()

SUPERCHAT_COLOR_MATCH_WINDOW_SECONDS = 12


def take_recent_superchat_color():
    """Returns the most recently reported Super Chat tier color if it
    arrived within SUPERCHAT_COLOR_MATCH_WINDOW_SECONDS, then clears
    it so a later, unrelated Super Chat doesn't accidentally reuse it.
    Returns None if no recent color is available.
    """

    with superchat_color_lock:
        color = last_superchat_color["color"]
        ts = last_superchat_color["ts"]

        if color and (time.time() - ts) <= SUPERCHAT_COLOR_MATCH_WINDOW_SECONDS:
            last_superchat_color["color"] = None
            return color

        return None


_gift_image_manifest_cache = None


def get_gift_color(gift_name):
    """Returns the tint color for this gift's overlay animation.

    If GIFT_EVENT_COLOR is set, every gift returns that same
    fixed color regardless of which gift it is. If it's set to
    None, falls back to the per-gift color listed in
    gift_images/_manifest.json (see load_gift_image_manifest()).
    """

    if GIFT_EVENT_COLOR is not None:
        return GIFT_EVENT_COLOR

    global _gift_image_manifest_cache

    if _gift_image_manifest_cache is None:
        _gift_image_manifest_cache = load_gift_image_manifest()

    entry = _gift_image_manifest_cache.get(
        normalize_gift_name_key(gift_name)
    )

    if entry and entry.get("colors"):
        return entry["colors"][0]

    return None


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


def push_event(event_type, name, value, seconds, image_url=None, combo_count=1, color=None):
    """Record a gift/superchat event so the overlay can animate it.

    combo_count is which tap this is within an in-progress Gift Combo
    (1 for a normal, non-combo gift/superchat; 2, 3, 4... for the
    2nd, 3rd, 4th... rapid tap of the SAME gift by the SAME user).
    The overlay uses this to stamp an "xN" badge on the gift image
    instead of just replaying the same plain image every tap.

    color, when provided (currently only for Super Chats - see
    take_recent_superchat_color()), is YouTube's own tier color for
    this donation, e.g. "#F57F17". The overlay uses it to color the
    flying Super Chat animation instead of a fixed color.
    """

    global next_event_id

    with lock:
        if timer_locked and event_type in ("gift", "superchat"):
            # While the timer is locked, gifts/Super Chats still bank
            # their time (add_time() routes it into bank_seconds), but
            # the overlay should NOT play the gift/Super Chat image
            # animation for them - so just skip recording the event.
            return None

        event = {
            "id": next_event_id,
            "type": event_type,
            "name": name,
            "value": value,
            "seconds": seconds,
            "ts": time.time(),
            "image_url": image_url or None,
            "combo_count": int(combo_count) if combo_count else 1,
            "color": color or None,
        }

        next_event_id += 1

        recent_events.append(event)

        # Keep only the most recent events so this never grows forever.
        if len(recent_events) > 200:
            del recent_events[: len(recent_events) - 200]

    # Keyboard lighting no longer lives in this file - it's handled
    # entirely by keyboard_color_server.py, driven directly by
    # youtube.js/background.js via that server's
    # /overlay/active-message-color endpoint whenever a chat message
    # is clicked in the Live Chat Overlay extension.

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
            "locked": timer_locked,
            "left_text_scroll": left_text_scroll_enabled,
            "left_box_enabled": left_box_enabled,
            "right_box_enabled": right_box_enabled,
            "msg_box_enabled": msg_box_enabled,
            "left_texts": left_texts,
            "left_text_durations": left_text_durations,
            "left_text_style": left_text_style,
            "msg_texts_unlocked": msg_texts_unlocked,
            "msg_texts_locked": msg_texts_locked,
            "msg_text_style": msg_text_style
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
    global left_text_scroll_enabled
    global left_box_enabled
    global right_box_enabled
    global msg_box_enabled
    global left_texts
    global left_text_durations
    global left_text_style
    global msg_texts_unlocked
    global msg_texts_locked
    global msg_text_style

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

        left_text_scroll_enabled = bool(data.get("left_text_scroll", True))
        left_box_enabled = bool(data.get("left_box_enabled", True))
        right_box_enabled = bool(data.get("right_box_enabled", True))
        msg_box_enabled = bool(data.get("msg_box_enabled", True))

        left_texts = sanitize_text_lines(
            data.get("left_texts", DEFAULT_LEFT_TEXTS),
            DEFAULT_LEFT_TEXTS
        )

        left_text_durations = sanitize_durations(
            data.get("left_text_durations"),
            len(left_texts)
        )

        left_text_style = sanitize_text_style(
            data.get("left_text_style", {}),
            DEFAULT_LEFT_TEXT_STYLE
        )

        msg_texts_unlocked = sanitize_text_lines(
            data.get("msg_texts_unlocked", DEFAULT_MSG_TEXTS_UNLOCKED),
            DEFAULT_MSG_TEXTS_UNLOCKED
        )

        msg_texts_locked = sanitize_text_lines(
            data.get("msg_texts_locked", DEFAULT_MSG_TEXTS_LOCKED),
            DEFAULT_MSG_TEXTS_LOCKED
        )

        msg_text_style = sanitize_text_style(
            data.get("msg_text_style", {}),
            DEFAULT_MSG_TEXT_STYLE
        )

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
        # len(fraction) == 3 means the comma is a THOUSANDS separator
        # (e.g. "4,910" -> whole="4", fraction="910"), so the comma
        # needs to be stripped from the FULL value ("4910"), not just
        # from `whole` (which would silently throw the "910" away and
        # leave "4" - this was the bug that turned real Super Chats
        # like JPY 4,910 into 4.00). Any other fraction length means
        # it's a decimal comma instead (e.g. "4,91" -> "4.91").
        value = value.replace(",", "") if len(fraction) == 3 else value.replace(",", ".")
    elif "." in value:
        whole, fraction = value.rsplit(".", 1)
        # Same fix as above, mirrored for repeated dot-as-thousands
        # separators (e.g. "1.234.567" -> "1234567", not "1234").
        value = value.replace(".", "") if len(fraction) == 3 and value.count(".") > 1 else value

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

    color = take_recent_superchat_color()

    push_event("superchat", None, usd_amount, seconds, image_url, color=color)

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
COMBO_GROUP_WINDOW_SECONDS = 6

# key: (normalized_user, normalized_gift) -> {
#     "display_user", "display_gift",   - original casing, for logs/UI
#     "count", "jewels", "seconds",     - running totals
#     "seconds_per_tap", "jewels_per_tap",  - value of ONE tap, fixed
#         at creation time from SSN's real first-tap data. Used to
#         credit additional taps later that only the DOM-based combo
#         counter (see /combo/live-update) can see.
#     "last_time",
# }
combo_tracker = {}


def combo_key(user_key, gift_name):
    """Normalize a (user, gift) pair into a case/punctuation/@-
    insensitive lookup key, so the SAME combo is recognized whether
    it comes from SSN's webhook (e.g. user_key="ParasocialwithDon
    Benitez", gift_name="Go Team!") or from the browser extension
    reading YouTube's own pinned combo-counter DOM element (which may
    report slightly different casing/punctuation, e.g.
    "@ParasocialwithDonBenitez" / "go team").

    Reuses normalize_gift_name_key() for the gift side, since it
    already strips punctuation differences like "Go Team!" vs
    "Go Team" - the exact same mismatch that can happen between SSN
    and the DOM-based extension reading.
    """

    norm_user = (user_key or "").strip().lstrip("@").lower()
    norm_gift = normalize_gift_name_key(gift_name)
    return (norm_user, norm_gift)


def track_gift_combo(user_key, gift_name, jewels, seconds):
    """Record one gift tap toward a possible combo. Actual combo
    detection/logging happens later in combo_watcher(), once the
    burst of taps has gone quiet.

    Returns the tap's position within this combo so far (1 for the
    first tap, 2 for the second, etc.), so the caller can pass it
    along to push_event() for the overlay's "xN" combo badge.
    """

    now = time.time()
    key = combo_key(user_key, gift_name)

    with lock:
        entry = combo_tracker.get(key)

        if entry is None:
            combo_tracker[key] = {
                "display_user": user_key,
                "display_gift": gift_name,
                "count": 1,
                "jewels": jewels,
                "seconds": seconds,
                "jewels_per_tap": jewels,
                "seconds_per_tap": seconds,
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

        for key, entry in finished:
            if entry["count"] <= 1:
                continue

            user_key = entry["display_user"]
            gift_name = entry["display_gift"]

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
                combo_entry = combo_tracker.get(combo_key(user_key, gift_name))

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

        push_event(
            "gift", gift_name, jewels, seconds, image_url, combo_count,
            color=get_gift_color(gift_name),
        )

        print(
            f"JEWEL DONATION [SSN]: "
            f"{gift_name} -> "
            f"{jewels:g} Jewels ({source}) -> "
            f"+{seconds:.2f} seconds "
            f"[image: {image_source}]"
        )

        log_donation(
            f"EVENT: JEWEL GIFT\n"
            f"DETECTED VIA: SSN webhook\n"
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
            "last_event_id": last_event_id,
            "left_text_scroll_enabled": left_text_scroll_enabled,
            "left_box_enabled": left_box_enabled,
            "right_box_enabled": right_box_enabled,
            "msg_box_enabled": msg_box_enabled,
            "left_texts": left_texts,
            "left_text_durations": left_text_durations,
            "left_text_style": left_text_style,
            "msg_texts_unlocked": msg_texts_unlocked,
            "msg_texts_locked": msg_texts_locked,
            "msg_text_style": msg_text_style
        })


@app.route("/left-text/enable")
def left_text_enable():
    global left_text_scroll_enabled
    left_text_scroll_enabled = True
    save_state()
    return jsonify({"ok": True, "left_text_scroll_enabled": True})


@app.route("/left-text/disable")
def left_text_disable():
    global left_text_scroll_enabled
    left_text_scroll_enabled = False
    save_state()
    return jsonify({"ok": True, "left_text_scroll_enabled": False})


@app.route("/left-box/enable")
def left_box_enable():
    """Shows the entire left-side box (background + text) on the
    overlay again."""
    global left_box_enabled
    left_box_enabled = True
    save_state()
    return jsonify({"ok": True, "left_box_enabled": True})


@app.route("/left-box/disable")
def left_box_disable():
    """Hides the entire left-side box (background + text) from the
    overlay, not just the text - the box takes up no space while
    disabled."""
    global left_box_enabled
    left_box_enabled = False
    save_state()
    return jsonify({"ok": True, "left_box_enabled": False})


@app.route("/right-box/enable")
def right_box_enable():
    """Shows the entire right-side box (timer + message) on the
    overlay again."""
    global right_box_enabled
    right_box_enabled = True
    save_state()
    return jsonify({"ok": True, "right_box_enabled": True})


@app.route("/right-box/disable")
def right_box_disable():
    """Hides the entire right-side box (timer + message) from the
    overlay, not just the text - the box takes up no space while
    disabled."""
    global right_box_enabled
    right_box_enabled = False
    save_state()
    return jsonify({"ok": True, "right_box_enabled": False})


@app.route("/msg-box/enable")
def msg_box_enable():
    """Shows the "Stream ends in..." message line above the timer
    digits again."""
    global msg_box_enabled
    msg_box_enabled = True
    save_state()
    return jsonify({"ok": True, "msg_box_enabled": True})


@app.route("/msg-box/disable")
def msg_box_disable():
    """Hides just the message line above the timer digits - the
    timer digits themselves stay on and expand upward to fill the
    freed space."""
    global msg_box_enabled
    msg_box_enabled = False
    save_state()
    return jsonify({"ok": True, "msg_box_enabled": False})


@app.route("/left-text/set-texts", methods=["POST"])
def left_text_set_texts():
    """
    Replaces the full list of rotating left-side text lines, and
    (optionally) how many seconds each line stays on screen.
    Called from the Dashboard - the user decides how many
    lines/messages just by how many non-empty rows they fill in.

    Body: {"texts": ["line one", "line two", ...],
           "durations": [10, 5, ...]}

    "durations" is parallel to "texts" (same order). It is optional:
    if it's left out, each line keeps the default
    (DEFAULT_LEFT_TEXT_SECONDS). Values are clamped to
    MIN_LEFT_TEXT_SECONDS..MAX_LEFT_TEXT_SECONDS.
    """

    global left_texts
    global left_text_durations

    data = request.get_json(silent=True) or {}
    raw_texts = data.get("texts", [])
    raw_durations = data.get("durations")

    if not isinstance(raw_texts, list):
        return jsonify({
            "ok": False,
            "error": "\"texts\" must be a list of strings."
        }), 400

    # Line up each duration with its text BEFORE dropping empty
    # lines, so removing a blank row can't shift the other rows'
    # durations onto the wrong text.
    if not isinstance(raw_durations, list):
        raw_durations = []

    cleaned = []
    cleaned_raw_durations = []

    for i, t in enumerate(raw_texts):
        text = str(t).strip()
        if not text:
            continue
        cleaned.append(text)
        cleaned_raw_durations.append(
            raw_durations[i] if i < len(raw_durations) else None
        )

    if not cleaned:
        return jsonify({
            "ok": False,
            "error": "Need at least one non-empty line of text."
        }), 400

    left_texts = cleaned
    left_text_durations = sanitize_durations(
        cleaned_raw_durations,
        len(cleaned)
    )
    save_state()

    return jsonify({
        "ok": True,
        "left_texts": left_texts,
        "left_text_durations": left_text_durations
    })


@app.route("/left-text/set-style", methods=["POST"])
def left_text_set_style():
    """
    Updates the look of the left-side text (font family, size,
    bold, italic, color). Called from the Dashboard's style
    controls. Any subset of fields can be sent - fields left out
    keep their current value.

    Body: {"font_family": "...", "font_size": 53, "bold": true,
           "italic": false, "color": "#FFFFFF"}
    """

    global left_text_style

    data = request.get_json(silent=True) or {}

    left_text_style = sanitize_text_style(data, left_text_style)
    save_state()

    return jsonify({"ok": True, "left_text_style": left_text_style})


@app.route("/msg-text/set-texts", methods=["POST"])
def msg_text_set_texts():
    """
    Replaces the rotating lines shown above the digit timer. Both
    the "unlocked" set and the "locked" set (shown while the timer
    is locked) can be sent in the same request; either can be
    omitted to leave it unchanged.

    Body: {"unlocked": ["line one", ...], "locked": ["line one", ...]}
    """

    global msg_texts_unlocked
    global msg_texts_locked

    data = request.get_json(silent=True) or {}

    if "unlocked" in data:
        msg_texts_unlocked = sanitize_text_lines(
            data.get("unlocked"),
            msg_texts_unlocked
        )

    if "locked" in data:
        msg_texts_locked = sanitize_text_lines(
            data.get("locked"),
            msg_texts_locked
        )

    save_state()

    return jsonify({
        "ok": True,
        "msg_texts_unlocked": msg_texts_unlocked,
        "msg_texts_locked": msg_texts_locked
    })


@app.route("/msg-text/set-style", methods=["POST"])
def msg_text_set_style():
    """
    Updates the look of the text above the digit timer (font
    family, size, bold, italic, color). Same shape as
    /left-text/set-style.
    """

    global msg_text_style

    data = request.get_json(silent=True) or {}

    msg_text_style = sanitize_text_style(data, msg_text_style)
    save_state()

    return jsonify({"ok": True, "msg_text_style": msg_text_style})


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
    push_event(
        "gift", name, jewels, seconds, combo_count=combo_count,
        color=get_gift_color(name),
    )

    return jsonify({
        "ok": True,
        "name": name,
        "jewels": jewels,
        "seconds": seconds,
        "combo_count": combo_count
    })


@app.route("/combo/live-update", methods=["POST", "OPTIONS"])
def combo_live_update():
    """Receive a live combo-count update from the browser extension,
    which reads YouTube's own PINNED combo-counter DOM element
    (`.ytlsGiftAttributionItemViewModelComboCountText`, aria-label
    like "2 gift combo") directly off the popout live chat page.

    This exists because SSN's own webhook only ever sends ONE event
    for an entire real YouTube Gift Combo (confirmed via testing -
    see COMBO_GROUP_WINDOW_SECONDS notes above): YouTube hides the
    true tap count from SSN's data feed entirely. The extension,
    however, can see the REAL, live-updating count straight from
    YouTube's own UI - so we use it here to credit any additional
    taps SSN never told us about.

    Expects JSON body: {"user": "...", "gift": "...", "count": N}
    where N is the CURRENT total tap count YouTube is showing for
    this combo right now (e.g. 2 for "x2", 3 for "x3", ...). Safe to
    call repeatedly with the same or a stale/lower count - only the
    DELTA above what we've already credited is ever added.
    """

    if request.method == "OPTIONS":
        # CORS preflight - the after_request hook above adds the
        # actual allow-headers; just return an empty 204 here.
        return ("", 204)

    data = request.get_json(silent=True) or {}

    user_key = str(data.get("user") or "").strip()
    gift_name = str(data.get("gift") or "").strip()
    try:
        reported_count = int(data.get("count") or 0)
    except (TypeError, ValueError):
        reported_count = 0

    if not user_key or reported_count < 1:
        return jsonify({
            "ok": False,
            "error": "user and count (>=1) are required"
        }), 400

    key = combo_key(user_key, gift_name)
    delta = 0
    add_seconds = 0.0
    combo_count_for_badge = reported_count
    reused_jewels_per_tap = 0.0

    with lock:
        entry = combo_tracker.get(key)

        if entry is None:
            # Exact (user, gift) match failed - this can happen when
            # the browser extension can't parse the gift's name from
            # YouTube's own DOM (alt-text phrasing isn't identical
            # for every gift). Before giving up, check whether this
            # SAME USER already has ANY other combo actively running
            # right now (within the combo window) - if so, this
            # update almost certainly belongs to THAT combo, just
            # reported under a slightly different gift-name spelling.
            # This keeps combo crediting working correctly even when
            # gift-name extraction isn't perfect.
            norm_user, _ = key
            now = time.time()
            best_match_key = None
            best_match_time = -1

            for existing_key, existing_entry in combo_tracker.items():
                if existing_key[0] != norm_user:
                    continue
                if (now - existing_entry["last_time"]) > COMBO_GROUP_WINDOW_SECONDS:
                    continue
                if existing_entry["last_time"] > best_match_time:
                    best_match_time = existing_entry["last_time"]
                    best_match_key = existing_key

            if best_match_key is not None:
                key = best_match_key
                entry = combo_tracker[key]

        if entry is None:
            # Still nothing - SSN's first-tap event for this combo
            # hasn't arrived (or never will), and there's no other
            # active combo for this user to attach to either. Fall
            # back to the gift-name lookup table so we can still
            # credit time, using this update as a brand-new baseline
            # entry.
            normalized_name = normalize_gift_name_key(gift_name)
            jewels_per_tap = GIFT_JEWEL_VALUES_NORMALIZED.get(normalized_name)

            if jewels_per_tap is None:
                return jsonify({
                    "ok": False,
                    "error": (
                        f"No baseline value known yet for gift "
                        f"'{gift_name}' - waiting for SSN's own event "
                        f"first, or add it to GIFT_JEWEL_VALUES."
                    )
                }), 202

            seconds_per_tap = float(jewels_per_tap) * GIFT_SECONDS_PER_JEWEL
            entry = combo_tracker[key] = {
                "display_user": user_key,
                "display_gift": gift_name,
                "count": 0,
                "jewels": 0,
                "seconds": 0,
                "jewels_per_tap": jewels_per_tap,
                "seconds_per_tap": seconds_per_tap,
                "last_time": time.time(),
            }

        delta = reported_count - entry["count"]

        if delta > 0:
            add_seconds = delta * entry["seconds_per_tap"]
            reused_jewels_per_tap = entry["jewels_per_tap"]

            entry["count"] = reported_count
            entry["jewels"] += delta * entry["jewels_per_tap"]
            entry["seconds"] += add_seconds
            combo_count_for_badge = entry["count"]

        entry["last_time"] = time.time()

        # Use the entry's own authoritative display name/gift (set
        # when the combo was first created, usually from SSN's own
        # accurate first-tap data) for everything shown to the user
        # from here on - NOT the possibly-mis-extracted name reported
        # in this specific update, in case the fuzzy same-user
        # fallback above kicked in.
        display_user = entry["display_user"]
        display_gift = entry["display_gift"]

    if delta <= 0:
        # Nothing new (YouTube hasn't incremented past what we
        # already know) - last_time was still refreshed above so the
        # combo doesn't get closed out as "quiet" prematurely.
        return jsonify({"ok": True, "added_taps": 0, "count": reported_count})

    add_time(add_seconds)

    push_event(
        "gift",
        display_gift,
        reused_jewels_per_tap * delta,
        add_seconds,
        combo_count=combo_count_for_badge
    )

    print(
        f"COMBO LIVE UPDATE [OVERLAY EXTENSION]: {display_user} / {display_gift} -> "
        f"now x{reported_count} (+{delta} tap(s) from DOM) -> "
        f"+{add_seconds:.2f} seconds"
    )

    log_donation(
        f"EVENT: GIFT COMBO LIVE UPDATE\n"
        f"DETECTED VIA: Overlay extension (DOM combo counter)\n"
        f"USER: {display_user}\n"
        f"GIFT: {display_gift}\n"
        f"NEW COMBO COUNT: {reported_count}\n"
        f"TAPS CREDITED THIS UPDATE: {delta}\n"
        f"TIME ADDED THIS UPDATE: +{add_seconds:.2f} seconds\n"
        f"RESULT: SUCCESS"
    )

    return jsonify({
        "ok": True,
        "added_taps": delta,
        "count": reported_count,
        "seconds_added": add_seconds
    })


@app.route("/superchat/color-update", methods=["POST", "OPTIONS"])
def superchat_color_update():
    """Receive YouTube's own tier color for a Super Chat that just
    appeared in chat, sent automatically by youtube.js via
    background.js the instant the message lands (no click required -
    see the "LIVE SUPER CHAT COLOR DETECTION" section of youtube.js).

    Stored here so process_super_chat() - triggered a moment later by
    SSN's webhook, which is what actually adds the time and plays the
    animation - can pick it up via take_recent_superchat_color() and
    color the flying Super Chat animation to match.

    Expects JSON body: {"color": "#RRGGBB", "amount": "$5.00"}
    ("amount" is optional and only used for logging here.)
    """

    if request.method == "OPTIONS":
        # CORS preflight - the after_request hook above adds the
        # actual allow-headers; just return an empty 204 here.
        return ("", 204)

    data = request.get_json(silent=True) or {}

    color = str(data.get("color") or "").strip()
    amount = str(data.get("amount") or "").strip()

    if not color:
        return jsonify({"ok": False, "error": "color is required"}), 400

    with superchat_color_lock:
        last_superchat_color["color"] = color
        last_superchat_color["ts"] = time.time()

    print(f"[SUPERCHAT COLOR] Received {color} (amount: {amount or 'unknown'})")

    return jsonify({"ok": True})


@app.route("/test/superchat")
def test_superchat():

    if not ENABLE_TEST_PANEL:
        return jsonify({"ok": False, "error": "Test panel disabled"}), 403

    usd = request.args.get("usd", default=5, type=float)

    # Optional: preview with a specific tier color (hex, e.g.
    # "#F57C00"), same as what youtube.js would report for a real
    # Super Chat. Lets you test the colored animation on demand
    # instead of waiting for an actual donor - see the "Super Chat
    # tiers" and "Custom color" sections of /test-panel.
    color = request.args.get("color", default=None, type=str)

    seconds = usd * SUPERCHAT_SECONDS_PER_USD

    add_time(seconds)
    push_event("superchat", None, usd, seconds, color=color)

    return jsonify({"ok": True, "usd": usd, "seconds": seconds, "color": color})


@app.route("/test/superchat-currency")
def test_superchat_currency():
    """Test the REAL currency pipeline end-to-end: parse_donation()
    -> get_exchange_rate() -> process_super_chat(), using raw donation
    text exactly like what SSN's webhook would send (e.g. "¥4,910").

    Unlike /test/superchat (which just takes a USD amount directly),
    this exercises parse_monetary_amount()'s thousands-separator
    handling, so it's the right one to use when checking a currency
    conversion bug/fix.
    """

    if not ENABLE_TEST_PANEL:
        return jsonify({"ok": False, "error": "Test panel disabled"}), 403

    raw = request.args.get("raw", default="", type=str)

    if not raw:
        return jsonify({"ok": False, "error": "raw is required"}), 400

    # Optional: also test that a color received just before this
    # (like background.js would send) gets picked up by
    # process_super_chat() -> take_recent_superchat_color().
    color = request.args.get("color", default=None, type=str)
    if color:
        with superchat_color_lock:
            last_superchat_color["color"] = color
            last_superchat_color["ts"] = time.time()

    # Parse here too (read-only, no side effects) purely so we can
    # report back exactly what it resolved to - process_super_chat()
    # below does the actual work (adds time, pushes the animation
    # event, writes the log line).
    parsed = parse_donation(raw)

    if not parsed:
        return jsonify({"ok": False, "error": "Could not parse: " + raw}), 400

    usd_rate = get_exchange_rate(parsed["currency"])

    if usd_rate is None:
        return jsonify({
            "ok": False,
            "error": f"No exchange rate available for {parsed['currency']}"
        }), 400

    ok = process_super_chat(raw)

    return jsonify({
        "ok": ok,
        "raw": raw,
        "currency": parsed["currency"],
        "amount": parsed["amount"],
        "usd_rate": usd_rate,
        "usd_amount": parsed["amount"] * usd_rate,
    })


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
        push_event(
            "gift", name, jewels, seconds, combo_count=combo_count,
            color=get_gift_color(name),
        )
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
            push_event("gift", name, jewels, seconds, color=get_gift_color(name))
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
        .custom-sc {
            background: #2c2c2e;
            border-radius: 10px;
            padding: 12px;
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 10px;
        }
        .custom-sc label {
            font-size: 13px;
            color: #aaa;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .custom-sc input[type="number"] {
            width: 70px;
            padding: 6px 8px;
            border-radius: 6px;
            border: none;
            font-size: 14px;
        }
        .custom-sc input[type="color"] {
            width: 44px;
            height: 32px;
            padding: 0;
            border: none;
            border-radius: 6px;
            background: none;
            cursor: pointer;
        }
        .custom-sc button { background: #a34ed1; }
        .custom-sc button:active { background: #c46bff; }
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
            "COMBO GIFT DETECTED" summary. The Super Chat buttons below
            fire with a real tier color baked in, so you can see the
            colored animation without needing an actual donor.
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

        <h2>Super Chat tiers <span style="color:#777; font-weight:normal;">(YouTube's real tier colors)</span></h2>
        <div class="row">
            <button class="sc-btn" style="background:#1565C0" onclick="fireSuperchat(1, '#1565C0')">💙 $1 (blue)</button>
            <button class="sc-btn" style="background:#00B8D4" onclick="fireSuperchat(2, '#00B8D4')">💵 $2 (light blue)</button>
            <button class="sc-btn" style="background:#00A572; color:#111" onclick="fireSuperchat(5, '#00A572')">💚 $5 (green)</button>
            <button class="sc-btn" style="background:#F9A825; color:#111" onclick="fireSuperchat(10, '#F9A825')">💰 $10 (yellow)</button>
            <button class="sc-btn" style="background:#EF6C00" onclick="fireSuperchat(20, '#EF6C00')">🧡 $20 (orange)</button>
            <button class="sc-btn" style="background:#E91E8C" onclick="fireSuperchat(50, '#E91E8C')">🤑 $50 (magenta)</button>
            <button class="sc-btn" style="background:#D32F2F" onclick="fireSuperchat(150, '#D32F2F')">💸 $150 (red)</button>
        </div>
        <p class="hint" style="margin-top:8px;">
            These are approximations of YouTube's real per-tier colors,
            so you can preview the colored animation without needing
            an actual donor. Once your extension starts relaying real
            Super Chats, the ACTUAL color YouTube used for that
            specific donation will be used instead.
        </p>

        <h2>Custom color</h2>
        <div class="custom-sc">
            <label>Amount ($)
                <input type="number" id="customUsd" value="25" min="0.01" step="0.01">
            </label>
            <label>Color
                <input type="color" id="customColor" value="#F06292">
            </label>
            <button onclick="fireCustomSuperchat()">🎨 Fire custom Super Chat</button>
        </div>

        <h2>Currency testing <span style="color:#777; font-weight:normal;">(real parser + live exchange rate)</span></h2>
        <p class="hint">
            These go through the SAME parse_donation() / get_exchange_rate()
            pipeline a real Super Chat webhook uses - not a shortcut -
            so this is the place to check that a currency with
            comma/dot thousands separators (like ¥4,910) converts
            correctly. The result box below each button shows exactly
            what it was parsed as and converted to.
        </p>
        <div class="row">
            <button class="sc-btn" style="background:#333" onclick="fireCurrency('¥4,910')">🇯🇵 ¥4,910 (JPY)</button>
            <button class="sc-btn" style="background:#333" onclick="fireCurrency('₱12,500')">🇵🇭 ₱12,500 (PHP)</button>
            <button class="sc-btn" style="background:#333" onclick="fireCurrency('€1.234,56')">🇪🇺 €1.234,56 (EUR)</button>
            <button class="sc-btn" style="background:#333" onclick="fireCurrency('£2,500.75')">🇬🇧 £2,500.75 (GBP)</button>
            <button class="sc-btn" style="background:#333" onclick="fireCurrency('₩150,000')">🇰🇷 ₩150,000 (KRW)</button>
            <button class="sc-btn" style="background:#333" onclick="fireCurrency('₹25,000')">🇮🇳 ₹25,000 (INR)</button>
            <button class="sc-btn" style="background:#333" onclick="fireCurrency('IDR 1.000.000')">🇮🇩 Rp1.000.000 (IDR)</button>
            <button class="sc-btn" style="background:#333" onclick="fireCurrency('$10.50')">🇺🇸 $10.50 (USD)</button>
        </div>
        <div class="custom-sc" style="margin-top:10px;">
            <label style="min-width:140px;">Amount
                <input type="number" id="customAmount" value="10.50" min="0" step="0.01" style="width:100%; padding:6px 8px; border-radius:6px; border:none; font-size:14px;">
            </label>
            <label style="min-width:160px;">Currency
                <select id="customCurrency" style="width:100%; padding:6px 8px; border-radius:6px; border:none; font-size:14px;">
                    __CURRENCY_OPTIONS__
                </select>
            </label>
            <button onclick="fireCustomCurrency()">🌐 Fire</button>
        </div>
        <div id="currencyResult" class="hint" style="margin-top:8px; white-space:pre-wrap;"></div>

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

        async function fireSuperchat(usd, color){
            setStatus('Fired Super Chat: $' + usd + (color ? ' (' + color + ')' : ''));
            let url = '/test/superchat?usd=' + encodeURIComponent(usd);
            if (color) url += '&color=' + encodeURIComponent(color);
            await fetch(url);
        }

        async function fireCustomSuperchat(){
            const usd = document.getElementById('customUsd').value || 5;
            const color = document.getElementById('customColor').value;
            await fireSuperchat(usd, color);
        }

        async function fireCurrency(raw){
            setStatus('Fired Super Chat: ' + raw);
            const resultBox = document.getElementById('currencyResult');
            resultBox.textContent = 'Converting ' + raw + ' ...';
            try {
                const res = await fetch('/test/superchat-currency?raw=' + encodeURIComponent(raw));
                const data = await res.json();
                if (!data.ok){
                    resultBox.textContent = '❌ ' + raw + ' -> ' + (data.error || 'failed');
                    return;
                }
                resultBox.textContent =
                    '✅ ' + raw + '  ->  ' +
                    data.amount.toFixed(2) + ' ' + data.currency +
                    '  ->  $' + data.usd_amount.toFixed(2) + ' USD' +
                    '  (rate: ' + data.usd_rate.toFixed(8) + ')';
            } catch (e){
                resultBox.textContent = '❌ ' + raw + ' -> request failed: ' + e;
            }
        }

        async function fireCustomCurrency(){
            const amount = document.getElementById('customAmount').value || 0;
            const currency = document.getElementById('customCurrency').value;
            const raw = amount + ' ' + currency;
            await fireCurrency(raw);
        }

        async function fireSimulate(count){
            setStatus('Simulating ' + count + ' random events...');
            await fetch('/test/simulate?count=' + count);
        }
    </script>

    </body>
    </html>
    """

    currency_codes = list(OrderedDict.fromkeys(CURRENCY_SYMBOLS.values()))

    currency_options = "".join(
        f'<option value="{code}"{" selected" if code == "USD" else ""}>{code}</option>'
        for code in currency_codes
    )

    html = html.replace("__GIFT_ROWS__", gift_rows)
    html = html.replace("__CURRENCY_OPTIONS__", currency_options)

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

<meta name="viewport" content="width=device-width, initial-scale=1">

<title>Timer Dashboard</title>

<style>

:root {
    --bg: #121014;
    --panel: #1b1922;
    --panel-border: #2c2933;
    --accent: #8A2BE2;
    --accent-soft: rgba(138, 43, 226, 0.18);
    --text: #f2f1f5;
    --text-dim: #a8a5b3;
    --danger: #e0455a;
    --radius: 12px;
}

* {
    box-sizing: border-box;
}

body {
    font-family: -apple-system, "Segoe UI", Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 24px;
    max-width: 1100px;
    margin: 0 auto;
    line-height: 1.4;
}

header {
    margin-bottom: 24px;
}

header h1 {
    margin: 0 0 4px 0;
    font-size: 26px;
}

header p {
    margin: 0;
    color: var(--text-dim);
    font-size: 14px;
}

.grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 16px;
    margin-bottom: 16px;
}

.card {
    background: var(--panel);
    border: 1px solid var(--panel-border);
    border-radius: var(--radius);
    padding: 18px 20px;
}

.card.wide {
    grid-column: 1 / -1;
}

/* Puts the Left-Side Text and Right-Side Box cards next to each
   other instead of stacked, so the right-side controls aren't
   scrolled far below the left-side ones. Falls back to stacking on
   narrow screens. */
.card.dual-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 16px;
    padding: 0;
    background: none;
    border: none;
}

.card.dual-row > .card {
    margin: 0;
}

.card h2 {
    margin: 0 0 4px 0;
    font-size: 16px;
    display: flex;
    align-items: center;
    gap: 8px;
}

.card .hint {
    color: var(--text-dim);
    font-size: 13px;
    margin: 4px 0 14px 0;
}

.btn-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
}

button, .btn {
    background: #2a2733;
    color: var(--text);
    border: 1px solid var(--panel-border);
    border-radius: 8px;
    padding: 9px 14px;
    font-size: 14px;
    cursor: pointer;
    transition: background .15s ease, border-color .15s ease;
}

button:hover, .btn:hover {
    background: #35313f;
    border-color: var(--accent);
}

button.primary {
    background: var(--accent);
    border-color: var(--accent);
    color: white;
    font-weight: 600;
}

button.primary:hover {
    filter: brightness(1.1);
}

button.danger {
    border-color: var(--danger);
    color: #ffd3d8;
}

button.danger:hover {
    background: rgba(224, 69, 90, 0.15);
}

.field-label {
    display: block;
    font-size: 13px;
    color: var(--text-dim);
    margin-bottom: 6px;
}

textarea, input[type="text"], input[type="number"], select {
    width: 100%;
    background: #100f15;
    color: var(--text);
    border: 1px solid var(--panel-border);
    border-radius: 8px;
    padding: 9px 10px;
    font-size: 14px;
    font-family: inherit;
}

textarea:focus, input:focus, select:focus {
    outline: none;
    border-color: var(--accent);
}

input[type="color"] {
    width: 100%;
    height: 38px;
    padding: 3px;
    background: #100f15;
    border: 1px solid var(--panel-border);
    border-radius: 8px;
    cursor: pointer;
}

.checkbox-field {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 14px;
    padding-top: 22px;
}

.checkbox-field input {
    width: 18px;
    height: 18px;
}

.style-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 12px;
    margin-top: 14px;
}

.status-line {
    font-size: 13px;
    color: var(--text-dim);
    min-height: 18px;
    margin-top: 8px;
}

.status-line.ok {
    color: #7be08a;
}

.status-line.err {
    color: var(--danger);
}

.left-text-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
}

.left-text-row input.lt-text {
    flex: 1 1 auto;
    min-width: 0;
}

.left-text-row input.lt-secs {
    flex: 0 0 76px;
    width: 76px;
}

.left-text-row .lt-unit {
    color: var(--text-dim);
    font-size: 13px;
}

.left-text-row button {
    flex: 0 0 auto;
    padding: 8px 10px;
}

.preview-box {
    margin-top: 14px;
    padding: 18px;
    background: #100f15;
    border: 1px dashed var(--panel-border);
    border-radius: 10px;
    text-align: center;
    word-break: break-word;
}

.two-col {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 16px;
}

hr.divider {
    border: none;
    border-top: 1px solid var(--panel-border);
    margin: 18px 0;
}

</style>

</head>

<body>

<header>
    <h1>🎬 Stream Timer Dashboard</h1>
    <p>Controls for the timer overlay - text and font changes apply live, no restart needed.</p>
</header>

<div class="grid">

    <div class="card">
        <h2>▶️ Playback</h2>
        <p class="hint">Start, pause, or reset the countdown.</p>
        <div class="btn-row">
            <button class="primary" onclick="fetch('/start')">Start</button>
            <button onclick="fetch('/pause')">Pause</button>
            <button class="danger" onclick="fetch('/reset')">Reset</button>
        </div>
    </div>

    <div class="card">
        <h2>🔒 Lock</h2>
        <p class="hint">Locking freezes the timer and switches the message above it to the "locked" set below.</p>
        <div class="btn-row">
            <button onclick="fetch('/lock')">Lock</button>
            <button onclick="fetch('/unlock')">Unlock</button>
        </div>
    </div>

    <div class="card">
        <h2>🏦 Bank</h2>
        <p class="hint">Apply or clear time sitting in the bank.</p>
        <div class="btn-row">
            <button onclick="fetch('/apply_bank')">Apply Bank</button>
            <button onclick="fetch('/clear_bank')">Clear Bank</button>
        </div>
    </div>

    <div class="card">
        <h2>➖ Subtract Time</h2>
        <p class="hint">Manually remove time from the timer.</p>
        <div class="btn-row">
            <button onclick="fetch('/subtract/30')">-30 sec</button>
            <button onclick="fetch('/subtract/60')">-1 min</button>
            <button onclick="fetch('/subtract/150')">-2.5 min</button>
            <button onclick="fetch('/subtract/300')">-5 min</button>
            <button onclick="fetch('/subtract/600')">-10 min</button>
            <button onclick="fetch('/subtract/1500')">-25 min</button>
        </div>
    </div>

    <div class="card wide">
        <h2>🎁 Manual Super Chat Add Time (Numpad only)</h2>
        <p class="hint">
            Numpad 1 = +30s &nbsp;•&nbsp; Numpad 2 = +1 min &nbsp;•&nbsp;
            Numpad 3 = +2.5 min &nbsp;•&nbsp; Numpad 4 = +5 min &nbsp;•&nbsp;
            Numpad 5 = +10 min &nbsp;•&nbsp; Numpad 6 = +25 min.
            The regular number row does nothing.
        </p>
    </div>

</div>


<div class="card wide dual-row">

<div class="card">

    <h2>💬 Left-Side Text</h2>
    <p class="hint">
        The box next to the timer ("Tell or ask me anything.").
    </p>

    <label class="field-label">Box on/off (hides the entire box, background included)</label>
    <div class="btn-row">
        <button id="leftBoxOnBtn" onclick="setLeftBox(true)">Box ON</button>
        <button id="leftBoxOffBtn" onclick="setLeftBox(false)">Box OFF</button>
    </div>
    <div id="leftBoxStatus" class="status-line"></div>

    <hr class="divider">

    <p class="hint">
        When the box is ON: Scrolling ON rotates through the lines
        below, showing each one for the number of seconds you set next
        to it; Scrolling OFF stays fixed on the first line.
    </p>

    <div class="btn-row">
        <button onclick="fetch('/left-text/enable')">Enable Scrolling</button>
        <button onclick="fetch('/left-text/disable')">Disable Scrolling</button>
    </div>

    <hr class="divider">

    <div class="two-col">

        <div>
            <label class="field-label">Message lines + seconds each one stays on screen (add or remove as many as you want)</label>
            <div id="leftTextsRows"></div>
            <div class="btn-row" style="margin-top:8px;">
                <button onclick="leftTextsEditor.addRow()">+ Add line</button>
            </div>
            <p class="hint" style="margin-top:10px;">
                To break one message into two lines on the overlay
                WITHOUT making it a separate rotating message, type
                || (two pipe characters) where you want the break -
                e.g. "Type your question||in chat." Each row is its
                own rotating message.
            </p>
            <div class="btn-row" style="margin-top:10px;">
                <button class="primary" onclick="leftTextsEditor.save()">Save Text Lines</button>
            </div>
            <div id="leftTextsStatus" class="status-line"></div>
        </div>

        <div>
            <label class="field-label">Font style</label>
            <div class="style-grid">

                <div>
                    <label class="field-label">Font family</label>
                    <select id="leftFontFamily">
                        <option value="Arial, sans-serif">Arial</option>
                        <option value="'Helvetica Neue', Helvetica, sans-serif">Helvetica</option>
                        <option value="Georgia, serif">Georgia</option>
                        <option value="'Times New Roman', Times, serif">Times New Roman</option>
                        <option value="'Courier New', Courier, monospace">Courier New</option>
                        <option value="Verdana, sans-serif">Verdana</option>
                        <option value="Tahoma, sans-serif">Tahoma</option>
                        <option value="'Trebuchet MS', sans-serif">Trebuchet MS</option>
                        <option value="'Comic Sans MS', cursive, sans-serif">Comic Sans MS</option>
                        <option value="Impact, sans-serif">Impact</option>
                        <option value="custom">Custom (type below)...</option>
                    </select>
                </div>

                <div>
                    <label class="field-label">Custom font (optional)</label>
                    <input id="leftFontFamilyCustom" type="text" placeholder="e.g. 'Poppins', sans-serif">
                </div>

                <div>
                    <label class="field-label">Size (px)</label>
                    <input id="leftFontSize" type="number" min="8" max="200" step="1">
                </div>

                <div>
                    <label class="field-label">Color</label>
                    <input id="leftFontColor" type="color">
                </div>

                <div class="checkbox-field">
                    <input id="leftFontBold" type="checkbox"><label for="leftFontBold">Bold</label>
                </div>

                <div class="checkbox-field">
                    <input id="leftFontItalic" type="checkbox"><label for="leftFontItalic">Italic</label>
                </div>

            </div>

            <p id="leftStylePreview" class="preview-box">Tell or ask me anything.</p>

            <div class="btn-row" style="margin-top:10px;">
                <button class="primary" onclick="leftStyleEditor.save()">Save Font Style</button>
            </div>
            <div id="leftStyleStatus" class="status-line"></div>
        </div>

    </div>

</div>


<div class="card">

    <h2>⏰ Right-Side Box</h2>
    <p class="hint">
        The box with the "Stream ends in..." message and the timer digits.
    </p>

    <label class="field-label">Box on/off (hides the entire box, background included)</label>
    <div class="btn-row">
        <button id="rightBoxOnBtn" onclick="setRightBox(true)">Box ON</button>
        <button id="rightBoxOffBtn" onclick="setRightBox(false)">Box OFF</button>
    </div>
    <div id="rightBoxStatus" class="status-line"></div>

    <hr class="divider">

    <label class="field-label">"Stream ends in..." message on/off (timer digits expand up to fill the space when off)</label>
    <div class="btn-row">
        <button id="msgBoxOnBtn" onclick="setMsgBox(true)">Message ON</button>
        <button id="msgBoxOffBtn" onclick="setMsgBox(false)">Message OFF</button>
    </div>
    <div id="msgBoxStatus" class="status-line"></div>

</div>

</div>


<div class="card wide">

    <h2>⏱️ Timer Message Text</h2>
    <p class="hint">
        The line above the big digit timer. It rotates every 5 seconds and automatically
        switches to the "locked" set while the timer is locked.
    </p>

    <div class="two-col">

        <div>
            <label class="field-label">Lines while UNLOCKED (one per line)</label>
            <textarea id="msgUnlockedBox" rows="4"></textarea>
        </div>

        <div>
            <label class="field-label">Lines while LOCKED (one per line)</label>
            <textarea id="msgLockedBox" rows="4"></textarea>
        </div>

    </div>

    <div class="btn-row" style="margin-top:10px;">
        <button class="primary" onclick="saveMsgTexts()">Save Text Lines</button>
    </div>
    <div id="msgTextsStatus" class="status-line"></div>

    <hr class="divider">

    <label class="field-label">Font style</label>
    <div class="style-grid">

        <div>
            <label class="field-label">Font family</label>
            <select id="msgFontFamily">
                <option value="Arial, sans-serif">Arial</option>
                <option value="'Helvetica Neue', Helvetica, sans-serif">Helvetica</option>
                <option value="Georgia, serif">Georgia</option>
                <option value="'Times New Roman', Times, serif">Times New Roman</option>
                <option value="'Courier New', Courier, monospace">Courier New</option>
                <option value="Verdana, sans-serif">Verdana</option>
                <option value="Tahoma, sans-serif">Tahoma</option>
                <option value="'Trebuchet MS', sans-serif">Trebuchet MS</option>
                <option value="'Comic Sans MS', cursive, sans-serif">Comic Sans MS</option>
                <option value="Impact, sans-serif">Impact</option>
                <option value="custom">Custom (type below)...</option>
            </select>
        </div>

        <div>
            <label class="field-label">Custom font (optional)</label>
            <input id="msgFontFamilyCustom" type="text" placeholder="e.g. 'Poppins', sans-serif">
        </div>

        <div>
            <label class="field-label">Size (px)</label>
            <input id="msgFontSize" type="number" min="8" max="200" step="1">
        </div>

        <div>
            <label class="field-label">Color</label>
            <input id="msgFontColor" type="color">
        </div>

        <div class="checkbox-field">
            <input id="msgFontBold" type="checkbox"><label for="msgFontBold">Bold</label>
        </div>

        <div class="checkbox-field">
            <input id="msgFontItalic" type="checkbox"><label for="msgFontItalic">Italic</label>
        </div>

    </div>

    <p id="msgStylePreview" class="preview-box">Stream ends in...</p>

    <div class="btn-row" style="margin-top:10px;">
        <button class="primary" onclick="msgStyleEditor.save()">Save Font Style</button>
    </div>
    <div id="msgStyleStatus" class="status-line"></div>

</div>


<script>

// ---------------- shared helpers ----------------

function setLine(el, text, isError){
    el.textContent = text;
    el.classList.remove('ok', 'err');
    el.classList.add(isError ? 'err' : 'ok');
}

// ---------------- generic font-style editor ----------------
// One of these is created per customizable text element (left-side
// text, timer message text). Handles loading the current style from
// /state, live-previewing edits, and saving via POST.

function wireStyleEditor(prefix, endpoint, stateKey, fallbackText){

    const ids = {
        family: prefix + 'FontFamily',
        familyCustom: prefix + 'FontFamilyCustom',
        size: prefix + 'FontSize',
        color: prefix + 'FontColor',
        bold: prefix + 'FontBold',
        italic: prefix + 'FontItalic',
        preview: prefix + 'StylePreview',
        status: prefix + 'StyleStatus'
    };

    function currentFamily(){
        const custom = document.getElementById(ids.familyCustom).value.trim();
        if (custom) return custom;
        return document.getElementById(ids.family).value;
    }

    function refreshPreview(){
        const preview = document.getElementById(ids.preview);
        preview.style.fontFamily = currentFamily();
        preview.style.fontSize = (document.getElementById(ids.size).value || 24) + 'px';
        preview.style.color = document.getElementById(ids.color).value;
        preview.style.fontWeight = document.getElementById(ids.bold).checked ? 'bold' : 'normal';
        preview.style.fontStyle = document.getElementById(ids.italic).checked ? 'italic' : 'normal';
    }

    [ids.family, ids.familyCustom, ids.size, ids.color, ids.bold, ids.italic].forEach(id => {
        document.getElementById(id).addEventListener('input', refreshPreview);
    });

    async function load(){
        try {
            const res = await fetch('/state');
            const data = await res.json();
            const style = data[stateKey] || {};

            const familySelect = document.getElementById(ids.family);
            const knownFamily = Array.from(familySelect.options)
                .some(opt => opt.value === style.font_family);

            if (knownFamily){
                familySelect.value = style.font_family;
                document.getElementById(ids.familyCustom).value = '';
            } else if (style.font_family){
                familySelect.value = 'custom';
                document.getElementById(ids.familyCustom).value = style.font_family;
            }

            document.getElementById(ids.size).value = style.font_size || 24;
            document.getElementById(ids.color).value = style.color || '#ffffff';
            document.getElementById(ids.bold).checked = !!style.bold;
            document.getElementById(ids.italic).checked = !!style.italic;

            refreshPreview();
        } catch (e){
            setLine(document.getElementById(ids.status), 'Failed to load current style: ' + e, true);
        }
    }

    async function save(){
        const statusEl = document.getElementById(ids.status);

        const payload = {
            font_family: currentFamily(),
            font_size: Number(document.getElementById(ids.size).value) || 24,
            bold: document.getElementById(ids.bold).checked,
            italic: document.getElementById(ids.italic).checked,
            color: document.getElementById(ids.color).value
        };

        try {
            const res = await fetch(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json();

            if (data.ok){
                setLine(statusEl, 'Saved.', false);
            } else {
                setLine(statusEl, 'Error: ' + (data.error || 'unknown error'), true);
            }
        } catch (e){
            setLine(statusEl, 'Request failed: ' + e, true);
        }
    }

    load();

    return { save: save, refreshPreview: refreshPreview };
}

const leftStyleEditor = wireStyleEditor('left', '/left-text/set-style', 'left_text_style');
const msgStyleEditor = wireStyleEditor('msg', '/msg-text/set-style', 'msg_text_style');

// ---------------- left-side box on/off ----------------

async function setLeftBox(on){
    const statusEl = document.getElementById('leftBoxStatus');
    try {
        const res = await fetch(on ? '/left-box/enable' : '/left-box/disable');
        const data = await res.json();
        if (data.ok){
            setLine(statusEl, on ? 'Box is ON.' : 'Box is OFF (hidden on overlay).', false);
        } else {
            setLine(statusEl, 'Error: ' + (data.error || 'unknown error'), true);
        }
    } catch (e){
        setLine(statusEl, 'Request failed: ' + e, true);
    }
}

async function loadLeftBoxStatus(){
    const statusEl = document.getElementById('leftBoxStatus');
    try {
        const res = await fetch('/state');
        const data = await res.json();
        const on = data.left_box_enabled !== false;
        setLine(statusEl, on ? 'Box is currently ON.' : 'Box is currently OFF (hidden on overlay).', false);
    } catch (e){
        setLine(statusEl, 'Failed to load current state: ' + e, true);
    }
}

loadLeftBoxStatus();

// ---------------- right-side box on/off ----------------

async function setRightBox(on){
    const statusEl = document.getElementById('rightBoxStatus');
    try {
        const res = await fetch(on ? '/right-box/enable' : '/right-box/disable');
        const data = await res.json();
        if (data.ok){
            setLine(statusEl, on ? 'Box is ON.' : 'Box is OFF (hidden on overlay).', false);
        } else {
            setLine(statusEl, 'Error: ' + (data.error || 'unknown error'), true);
        }
    } catch (e){
        setLine(statusEl, 'Request failed: ' + e, true);
    }
}

async function loadRightBoxStatus(){
    const statusEl = document.getElementById('rightBoxStatus');
    try {
        const res = await fetch('/state');
        const data = await res.json();
        const on = data.right_box_enabled !== false;
        setLine(statusEl, on ? 'Box is currently ON.' : 'Box is currently OFF (hidden on overlay).', false);
    } catch (e){
        setLine(statusEl, 'Failed to load current state: ' + e, true);
    }
}

loadRightBoxStatus();

// ---------------- message-above-timer on/off ----------------

async function setMsgBox(on){
    const statusEl = document.getElementById('msgBoxStatus');
    try {
        const res = await fetch(on ? '/msg-box/enable' : '/msg-box/disable');
        const data = await res.json();
        if (data.ok){
            setLine(statusEl, on ? 'Message is ON.' : 'Message is OFF (timer expands up to fill the space).', false);
        } else {
            setLine(statusEl, 'Error: ' + (data.error || 'unknown error'), true);
        }
    } catch (e){
        setLine(statusEl, 'Request failed: ' + e, true);
    }
}

async function loadMsgBoxStatus(){
    const statusEl = document.getElementById('msgBoxStatus');
    try {
        const res = await fetch('/state');
        const data = await res.json();
        const on = data.msg_box_enabled !== false;
        setLine(statusEl, on ? 'Message is currently ON.' : 'Message is currently OFF (timer expands up to fill the space).', false);
    } catch (e){
        setLine(statusEl, 'Failed to load current state: ' + e, true);
    }
}

loadMsgBoxStatus();

// ---------------- left-side rotating text ----------------

const leftTextsEditor = (function(){

    const DEFAULT_SECONDS = 10;

    function rowsEl(){
        return document.getElementById('leftTextsRows');
    }

    function addRow(text, seconds){
        const row = document.createElement('div');
        row.className = 'left-text-row';

        const textInput = document.createElement('input');
        textInput.type = 'text';
        textInput.className = 'lt-text';
        textInput.placeholder = 'Message text';
        textInput.value = text || '';

        const secsInput = document.createElement('input');
        secsInput.type = 'number';
        secsInput.className = 'lt-secs';
        secsInput.min = '1';
        secsInput.max = '3600';
        secsInput.step = '0.5';
        secsInput.value = seconds || DEFAULT_SECONDS;

        const unit = document.createElement('span');
        unit.className = 'lt-unit';
        unit.textContent = 'sec';

        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.title = 'Remove this line';
        removeBtn.textContent = '\u2715';
        removeBtn.onclick = function(){ row.remove(); };

        row.appendChild(textInput);
        row.appendChild(secsInput);
        row.appendChild(unit);
        row.appendChild(removeBtn);

        rowsEl().appendChild(row);
    }

    async function load(){
        try {
            const res = await fetch('/state');
            const data = await res.json();

            const texts = data.left_texts || [];
            const durations = data.left_text_durations || [];

            rowsEl().innerHTML = '';

            texts.forEach(function(t, i){
                addRow(t, durations[i]);
            });
        } catch (e){
            setLine(document.getElementById('leftTextsStatus'), 'Failed to load current text: ' + e, true);
        }
    }

    async function save(){
        const statusEl = document.getElementById('leftTextsStatus');

        const texts = [];
        const durations = [];

        const rows = rowsEl().querySelectorAll('.left-text-row');

        for (const row of rows){
            const text = row.querySelector('.lt-text').value.trim();
            if (text.length === 0) continue;

            const secs = Number(row.querySelector('.lt-secs').value);

            if (!isFinite(secs) || secs < 1){
                setLine(statusEl, 'Every line needs a time of at least 1 second.', true);
                return;
            }

            texts.push(text);
            durations.push(secs);
        }

        if (texts.length === 0){
            setLine(statusEl, 'Enter at least one line.', true);
            return;
        }

        try {
            const res = await fetch('/left-text/set-texts', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ texts: texts, durations: durations })
            });
            const data = await res.json();

            if (data.ok){
                setLine(statusEl, 'Saved ' + data.left_texts.length + ' line(s).', false);
                load();
            } else {
                setLine(statusEl, 'Error: ' + (data.error || 'unknown error'), true);
            }
        } catch (e){
            setLine(statusEl, 'Request failed: ' + e, true);
        }
    }

    load();

    return {
        save: save,
        addRow: function(){ addRow('', DEFAULT_SECONDS); }
    };

})();

// ---------------- timer message text (unlocked + locked) ----------------

async function loadMsgTexts(){
    try {
        const res = await fetch('/state');
        const data = await res.json();
        document.getElementById('msgUnlockedBox').value = (data.msg_texts_unlocked || []).join('\\n');
        document.getElementById('msgLockedBox').value = (data.msg_texts_locked || []).join('\\n');
    } catch (e){
        setLine(document.getElementById('msgTextsStatus'), 'Failed to load current text: ' + e, true);
    }
}

async function saveMsgTexts(){
    const statusEl = document.getElementById('msgTextsStatus');

    const unlocked = document.getElementById('msgUnlockedBox').value
        .split('\\n').map(l => l.trim()).filter(l => l.length > 0);

    const locked = document.getElementById('msgLockedBox').value
        .split('\\n').map(l => l.trim()).filter(l => l.length > 0);

    if (unlocked.length === 0 || locked.length === 0){
        setLine(statusEl, 'Both boxes need at least one line.', true);
        return;
    }

    try {
        const res = await fetch('/msg-text/set-texts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ unlocked: unlocked, locked: locked })
        });
        const data = await res.json();

        if (data.ok){
            setLine(statusEl, 'Saved.', false);
        } else {
            setLine(statusEl, 'Error: ' + (data.error || 'unknown error'), true);
        }
    } catch (e){
        setLine(statusEl, 'Request failed: ' + e, true);
    }
}

loadMsgTexts();

</script>

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
    width: 229.65px;
    height: 70.88px;
    background: rgba(58, 58, 58, 0.9);
    color: white;

    display: flex;
    align-items: center;
    justify-content: center;

    text-align: center;

    padding: 0;
}

/* Only this inner text fades on rotation - #left itself (the
   background box) stays put, matching how #msg fades while its
   surrounding .box-bg stays static. Font family/size/weight/style/
   color are NOT set here - they're applied as inline styles from
   left_text_style (see applyLeftTextStyle() in the JS below), so
   they can be changed from the Dashboard without touching CSS. The
   values below are just the pre-JS fallback/first-paint look. */
#left-text {
    font-size: 32.67px;
    font-weight: 549;
    line-height: 0.95;

    letter-spacing: .5px;

    opacity: 1;

    transition: opacity .8s ease;
    width: 100%;
}

#right {
    width: 259.65px;
    height: 70.88px;

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

/* Super Chat flies in as a pure CSS "badge" - no ticket image at all -
   styled to match the same donation badge already built in
   youtube.js's donationHTML: a "SUPERCHAT" label bar with the
   YouTube play-button icon, on top of a $ amount bar underneath,
   both on a gradient built from the real tier color (ev.color).
   See spawnSuperchatAnimation() and buildSuperchatIconSvg() in the
   JS. Both bars are normal children of .superchat-wrap, which is
   what actually gets the appearAtStart/flyOnly animation - no
   separate animation needed for them. */
.superchat-wrap {
    pointer-events: none;
    filter: drop-shadow(0 0 14px var(--glow, #FFD700));
}

.superchat-card {
    position: relative;
    border-radius: 10px;
    overflow: hidden;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.45);
    pointer-events: none;

    /* Same font stack + weight as the SUPERCHAT badge in
       youtube.js/youtube.css - see --font-family and
       --highlight-chat-font-weight in youtube.css's :root, which is
       what the donationHTML badge actually inherits from its
       highlight-chat container. Kept here so both places render the
       exact same font/weight instead of Timer5.py falling back to
       its own page-wide "Arial, sans-serif" + bolder weights. */
    font-family: Arial, Helvetica, Geneva, Verdana, sans-serif;
}

.superchat-label-bar {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;

    padding: 9px 14px 8px;

    /* font-size is set from JS - see SUPERCHAT_LABEL_FONT_SIZE in
       the SIZE CONFIG block near the top of the <script> below. */
    font-weight: 600;
    letter-spacing: 0.3px;
    color: #fff;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.35);
}

.superchat-icon-box {
    background: #fff;
    border-radius: 6px;
    padding: 2px 3px;

    display: flex;
    align-items: center;
    justify-content: center;

    box-shadow: 0 1px 3px rgba(0, 0, 0, .35);
    flex-shrink: 0;
}

.superchat-amount {
    padding: 10px 14px 14px;
    text-align: center;

    white-space: nowrap;
    pointer-events: none;

    font-weight: 600;
    color: #111111;
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
    height: 12.33px;

    display: flex;
    justify-content: center;
    align-items: center;

    /* Font family/size/weight/style/color are NOT set here - they're
       applied as inline styles from msg_text_style (see
       applyMsgTextStyle() in the JS below), so they can be changed
       from the Dashboard without touching CSS. Values below are
       just the pre-JS fallback/first-paint look. */
    font-size: 16.03px;
    font-weight: 650;
    line-height: 1.2;

    text-align: center;

    white-space: pre-line;

    margin-top: 4.93px;
    margin-bottom: 0;

    opacity: 1;

    transition: opacity .8s ease;

    flex-shrink: 0;
}

#time {
    font-size: 67.8px;
    font-weight: 530;

    margin-top: -11.09px;

    transition: transform 0.15s ease, color 0.15s ease;
}

/* When the "Stream ends in..." message is hidden (see
   applyMsgBoxVisibility() below), #right gets the "msg-hidden"
   class. #time-row already grows to fill the freed space on its
   own (flex:1) - this just cancels #time's -11.09px margin-top,
   which was only there to nudge the digits up under the message
   line, so the digits land truly centered in the taller box
   instead of pinned toward the top. */
#right.msg-hidden #time {
    margin-top: 0;
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
    
    <!-- The "left" box (Tell or ask me anything.) now lives on its
         own page - see /left-overlay - so it can be added as a
         separate OBS browser source and positioned independently
         from the timer below. -->

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

// ============================================================
// SIZE CONFIG (animation sizes - safe to tweak)
// ============================================================
// Change the numbers below to resize things. Everything else in
// this file reads from these - you shouldn't need to touch
// anything past this block just to make things bigger/smaller.

// ---------------- GIFT IMAGE ----------------
// Width & height (in px) of the flying gift image.
const GIFT_IMAGE_SIZE = 201;

// ---------------- SUPER CHAT ----------------
// Overall width (in px) of the flying Super Chat card. The label
// bar and $ amount bar both stretch to match this automatically.
const SUPERCHAT_BADGE_WIDTH = 312;

// Everything below this line auto-scales with SUPERCHAT_BADGE_WIDTH
// above (same proportions they had at the original width=240) - you
// don't need to edit these directly, just change the width above.

// "SUPERCHAT" label text (25px at width=240).
const SUPERCHAT_LABEL_FONT_SIZE = SUPERCHAT_BADGE_WIDTH * (25 / 240);

// $ money value text, e.g. "$10.00" (44px at width=240).
const SUPERCHAT_VALUE_FONT_SIZE = SUPERCHAT_BADGE_WIDTH * (44 / 240);

// YouTube play-button icon (33x24px at width=240).
const SUPERCHAT_ICON_WIDTH = SUPERCHAT_BADGE_WIDTH * (33 / 240);
const SUPERCHAT_ICON_HEIGHT = SUPERCHAT_ICON_WIDTH * (24 / 33);


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

function applyTimerPop(big, giftName, explicitColor){
    const timeEl = document.getElementById('time');

    // explicitColor (used for Super Chats) takes priority over the
    // gift-name color lookup below - it's YouTube's own real tier
    // color for that specific donation (ev.color, same one used for
    // the flying Super Chat badge), so the digit "pop" flashes the
    // SAME color that triggered it (e.g. a red Super Chat -> red
    // digit pop). Gifts still use their own per-gift colors from
    // gift_images/_manifest.json, unaffected.
    let popColor = null;
    let popGlow = null;

    if (explicitColor){
        popColor = explicitColor;
        popGlow = explicitColor;
    } else {
        // ---- CURRENT: all gifts pop purple on the timer digits ----
        popColor = "#A020F0";
        popGlow = "#A020F0";

        // ---- OLD WAY (commented out, not deleted): pop color matched
        // each gift's own color(s) from gift_images/_manifest.json.
        // To restore this behavior later, delete/comment the 2 lines
        // above and uncomment the block below.
        //
        // const colors = getGiftPopColors(giftName);
        // if (colors){
        //     popColor = colors[0];
        //     popGlow = colors[1];
        // }
    }

    if (popColor){
        timeEl.style.setProperty('--pop-color', popColor);
        timeEl.style.setProperty('--pop-glow', popGlow);
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
    // Size is set in the SIZE CONFIG block near the top of this
    // <script> - see GIFT_IMAGE_SIZE.
    const size = GIFT_IMAGE_SIZE;
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


// ---------------- SUPER CHAT: PURE CSS BADGE (NO IMAGE) ----------------
// The flying Super Chat animation is a pure CSS/SVG badge, built to
// match the same donation badge already used in youtube.js's
// donationHTML: a "SUPERCHAT" label bar (with the YouTube play-button
// icon) on top of a $ amount bar underneath, both colored from the
// real tier color (ev.color) using the exact same color-mix recipe
// youtube.js uses. No ticket image/artwork is used at all.
//
// Same size for every tier (small/medium/large/huge no longer change
// how big the badge is - only the label bar color changes based on
// ev.color). Sizes are set in the SIZE CONFIG block near the top of
// this <script> - see SUPERCHAT_BADGE_WIDTH / SUPERCHAT_VALUE_FONT_SIZE.

const SUPERCHAT_TIER_DURATION = { small: 0.7, medium: 0.7, large: 0.7, huge: 0.7 };

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

// The same small YouTube "play button" icon used in youtube.js's
// donationHTML, next to the "SUPERCHAT" label. idSuffix keeps each
// flying badge's <defs> ids unique so multiple Super Chats flying in
// at once don't fight over the same gradient/filter ids.
function buildSuperchatIconSvg(idSuffix){
    return `
    <svg width="${SUPERCHAT_ICON_WIDTH}" height="${SUPERCHAT_ICON_HEIGHT}" viewBox="0 0 22 16" style="flex-shrink:0; display:block;">
        <defs>
            <linearGradient id="ytGrad${idSuffix}" x1="0%" y1="0%" x2="0%" y2="100%">
                <stop offset="0%" stop-color="#ff4d4d"/>
                <stop offset="55%" stop-color="#e50000"/>
                <stop offset="100%" stop-color="#a80000"/>
            </linearGradient>
            <linearGradient id="ytShine${idSuffix}" x1="0%" y1="0%" x2="0%" y2="100%">
                <stop offset="0%" stop-color="#fff" stop-opacity="0.55"/>
                <stop offset="45%" stop-color="#fff" stop-opacity="0"/>
            </linearGradient>
            <filter id="ytDrop${idSuffix}" x="-30%" y="-30%" width="160%" height="160%">
                <feDropShadow dx="0" dy="1" stdDeviation="0.8" flood-color="#000" flood-opacity="0.45"/>
            </filter>
        </defs>
        <rect width="22" height="16" rx="4" fill="url(#ytGrad${idSuffix})" filter="url(#ytDrop${idSuffix})"/>
        <rect x="0.6" y="0.6" width="20.8" height="14.8" rx="3.4" fill="none" stroke="rgba(255,255,255,0.35)" stroke-width="0.8"/>
        <rect width="22" height="8" rx="4" fill="url(#ytShine${idSuffix})"/>
        <polygon points="9,5 9,11 14.5,8" fill="#8c0000" opacity="0.4" transform="translate(0.4,0.6)"/>
        <polygon points="9,5 9,11 14.5,8" fill="#fff"/>
        <polygon points="9,5 9,7.6 11.8,6.3" fill="#ffffff" opacity="0.55"/>
    </svg>`;
}

function spawnSuperchatAnimation(ev){

    const tier = tierFromUsd(ev.value);
    const width = SUPERCHAT_BADGE_WIDTH;
    const duration = SUPERCHAT_TIER_DURATION[tier];

    // ev.color, when present, is YouTube's own real tier color for
    // THIS Super Chat (reported live by youtube.js/background.js -
    // see /superchat/color-update and take_recent_superchat_color()
    // on the Python side). Falls back to gold when it's missing,
    // e.g. for the /test/superchat and hotkey previews, which don't
    // go through the browser extension at all.
    const superchatColor = ev.color || '#FFD700';

    // Same color-mix recipe as youtube.js's donationHTML: a darker
    // shade for the "SUPERCHAT" label bar, a lighter metallic sheen
    // for the $ amount bar underneath.
    const topColor = `color-mix(in srgb, ${superchatColor} 65%, black)`;
    const shineTop = `color-mix(in srgb, ${superchatColor} 85%, white)`;
    const shineBottom = `color-mix(in srgb, ${superchatColor} 25%, white)`;
    const topGrad = `linear-gradient(270deg, ${topColor} 0%, ${shineTop} 49%, ${topColor} 100%)`;
    const bottomGrad = `linear-gradient(270deg, ${superchatColor} 0%, ${shineBottom} 49%, ${superchatColor} 100%)`;

    const fx = document.getElementById('fx-layer');
    const target = getFlyPositions();
    const startRot = Math.round((Math.random() - 0.5) * 20);

    const wrap = document.createElement('div');
    wrap.className = 'superchat-wrap';
    wrap.style.width = width + 'px';
    wrap.style.setProperty('--glow', superchatColor);

    const card = document.createElement('div');
    card.className = 'superchat-card';
    card.style.background = bottomGrad;

    const labelBar = document.createElement('div');
    labelBar.className = 'superchat-label-bar';
    labelBar.style.background = topGrad;
    labelBar.style.fontSize = SUPERCHAT_LABEL_FONT_SIZE + 'px';

    const iconBox = document.createElement('div');
    iconBox.className = 'superchat-icon-box';
    iconBox.innerHTML = buildSuperchatIconSvg('SC' + ev.id);

    const labelText = document.createElement('span');
    labelText.textContent = 'SUPERCHAT';

    labelBar.appendChild(iconBox);
    labelBar.appendChild(labelText);

    const amount = document.createElement('div');
    amount.className = 'superchat-amount';
    amount.textContent = formatSuperchatValue(ev.value);
    amount.style.fontSize = SUPERCHAT_VALUE_FONT_SIZE + 'px';

    card.appendChild(labelBar);
    card.appendChild(amount);
    wrap.appendChild(card);

    wrap.style.setProperty('--start-x', target.startX + 'px');
    wrap.style.setProperty('--start-y', target.startY + 'px');
    wrap.style.setProperty('--end-x', target.endX + 'px');
    wrap.style.setProperty('--end-y', target.endY + 'px');
    wrap.style.setProperty('--start-rot', startRot + 'deg');
    wrap.style.setProperty('--end-rot', '0deg');

    // Keep the Super Chat badge a STEADY size the whole time it
    // flies - peak-scale matches start-scale, so it never grows
    // mid-flight. It only shrinks away at the very end, right as it
    // lands.
    wrap.style.setProperty('--start-scale', '0.5');
    wrap.style.setProperty('--peak-scale', '0.5');
    wrap.style.setProperty('--end-scale', '0.12');

    fx.appendChild(wrap);

    const totalDuration = playHoldThenFly(wrap, duration, 0, {
        // popOnArrival removed - that's what caused the extra
        // inflate/grow right as it arrived. Now it just shrinks
        // away smoothly, same steady feel as gifts.
        burstColor: superchatColor,
        countdownText: 'Super Chat arriving',
        glitterTrail: true,
        glitterColor: superchatColor
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

function scheduleLanding(anim, giftName, explicitColor){
    if (!anim) return;

    pendingLandings++;

    setTimeout(() => {
        pendingLandings = Math.max(0, pendingLandings - 1);

        if (pendingLandings === 0){
            displayedSeconds = lastKnownSeconds;
            renderDisplayedSeconds();
        }

        if (EXPAND_TIMER_ON_INCREASE){
            applyTimerPop(!!anim.big, giftName, explicitColor);
        }
    }, Math.max(0, anim.duration) * 1000);
}


async function update(){

    let r = await fetch('/state?since=' + lastEventId);

    let d = await r.json();

    const timeEl = document.getElementById('time');

    lastKnownSeconds = d.seconds;

    // Cached for the left-text rotator (setInterval below) so it
    // doesn't need its own separate /state poll.
    leftScrollEnabled = d.left_text_scroll_enabled !== false;

    // Show/hide the ENTIRE left box (background + text), not just
    // the text inside it. display:none removes it from layout
    // entirely rather than just making it invisible.
    applyLeftBoxVisibility(d.left_box_enabled !== false);

    // Show/hide the ENTIRE right box (timer + message), same idea
    // as the left box above.
    applyRightBoxVisibility(d.right_box_enabled !== false);

    // Show/hide just the message line above the timer digits - the
    // digits expand upward on their own once it's hidden.
    applyMsgBoxVisibility(d.msg_box_enabled !== false);

    // Pick up any text-line edits made from the Dashboard. If the
    // list actually changed, snap the index back to 0 so it doesn't
    // point past the end of a shorter new list.
    let leftTextsChanged = false;
    let leftDurationsChanged = false;

    if (Array.isArray(d.left_texts) && d.left_texts.length){
        leftTextsChanged =
            d.left_texts.length !== leftTexts.length ||
            d.left_texts.some((t, i) => t !== leftTexts[i]);

        if (leftTextsChanged){
            leftTexts = d.left_texts;
            leftTextIndex = 0;
        }
    }

    // Pick up per-line duration edits made from the Dashboard.
    if (Array.isArray(d.left_text_durations) && d.left_text_durations.length){
        leftDurationsChanged =
            d.left_text_durations.length !== leftTextDurations.length ||
            d.left_text_durations.some((t, i) => t !== leftTextDurations[i]);

        if (leftDurationsChanged){
            leftTextDurations = d.left_text_durations;
        }
    }

    // Restart the countdown so the new timing takes effect right
    // away instead of after the old, already-running timeout.
    if (leftTextsChanged || leftDurationsChanged){
        scheduleLeftTextAdvance();
    }

    // Pick up any font style edits made from the Dashboard (family,
    // size, bold, italic, color).
    if (d.left_text_style){
        applyLeftTextStyle(d.left_text_style);
    }

    let handledByEvent = false;

    // Skip animating on the very first load so old/backlogged
    // events don't all fire at once when the overlay starts.
    if (!firstStateLoad && d.events && d.events.length){
        for (const ev of d.events){
            const anim = (ev.type === "superchat")
                ? spawnSuperchatAnimation(ev)
                : spawnGiftAnimation(ev);

            scheduleLanding(
                anim,
                ev.type === "superchat" ? null : ev.name,
                ev.type === "superchat" ? (ev.color || '#FFD700') : null
            );
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



// ---------------- TIMER MESSAGE TEXT (line above the digits) ----------------
// Rotates through msgTextsUnlocked/msgTextsLocked every 5 seconds,
// switching sets automatically when the timer locks/unlocks. Both
// lists (and the style below) are no longer hardcoded - they're
// kept in sync with /state, which the Dashboard's controls write to
// via /msg-text/set-texts and /msg-text/set-style. These fallbacks
// are only used for the brief moment before the first /state
// response arrives.
let msgTextsUnlocked = [
    "Stream ends in...",
    "Super Chat/Gift to add time"
];

let msgTextsLocked = [
    "Raiding streamer in...",
    "Timer locked."
];

function getFadeTexts(isLocked){
    return isLocked ? msgTextsLocked : msgTextsUnlocked;
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

// Last-applied style, so applyMsgTextStyle() can skip re-touching
// the DOM when nothing actually changed since the last /state poll.
let currentMsgTextStyle = null;

function applyMsgTextStyle(style){

    const el = document.getElementById("msg");

    if (!el || !style) return;

    const serialized = JSON.stringify(style);

    if (serialized === currentMsgTextStyle) return;

    currentMsgTextStyle = serialized;

    if (style.font_family) el.style.fontFamily = style.font_family;
    if (style.font_size) el.style.fontSize = style.font_size + "px";
    if (style.color) el.style.color = style.color;

    el.style.fontWeight = style.bold ? "bold" : "normal";
    el.style.fontStyle = style.italic ? "italic" : "normal";
}


// ---------------- "TELL OR ASK ME ANYTHING." LEFT TEXT ----------------
// Rotates through leftTexts every 10 seconds when enabled from the
// Dashboard (/left-text/enable, /left-text/disable). When disabled,
// pins the text to leftTexts[0] only. leftTexts itself is no longer
// hardcoded here - it's kept in sync with the server's /state
// response (see update() above), which the Dashboard's textarea
// writes to via /left-text/set-texts. This fallback is only used
// for the brief moment before the first /state response arrives.
let leftTexts = [
    "Tell or ask me anything.",
    "Type your question in chat!"
];

// Seconds each line in leftTexts stays on screen (parallel to
// leftTexts, edited per-line on the Dashboard).
let leftTextDurations = [10, 10];

let leftTextIndex = 0;
let leftScrollEnabled = true;

// Whether the entire #left box (background + text) should be
// shown at all. Toggled from the Dashboard (/left-box/enable,
// /left-box/disable) - separate from leftScrollEnabled above,
// which only affects whether the text inside it rotates.
let leftBoxVisible = true;

function applyLeftBoxVisibility(visible){
    if (visible === leftBoxVisible) return;
    leftBoxVisible = visible;

    const box = document.getElementById("left");
    if (!box) return;

    // display:none takes the box fully out of the layout (matches
    // "including the entire box", not just hiding its text/color).
    box.style.display = visible ? "flex" : "none";
}

// Whether the entire #right box (timer + message) should be shown
// at all. Toggled from the Dashboard (/right-box/enable,
// /right-box/disable).
let rightBoxVisible = true;

function applyRightBoxVisibility(visible){
    if (visible === rightBoxVisible) return;
    rightBoxVisible = visible;

    const box = document.getElementById("right");
    if (!box) return;

    // display:none takes the box fully out of the layout, same as
    // the left box above.
    box.style.display = visible ? "flex" : "none";
}

// Whether just the "Stream ends in..." message line (id="msg") is
// shown. When hidden, #time-row's existing flex:1 automatically
// expands it to fill #right on its own, and #right gets the
// "msg-hidden" class (see CSS above) so #time re-centers inside
// the taller space instead of staying pinned toward the top.
let msgBoxVisible = true;

function applyMsgBoxVisibility(visible){
    if (visible === msgBoxVisible) return;
    msgBoxVisible = visible;

    const box = document.getElementById("msg");
    if (!box) return;

    box.style.display = visible ? "flex" : "none";

    const rightBox = document.getElementById("right");
    if (rightBox) {
        rightBox.classList.toggle("msg-hidden", !visible);
    }
}

// Last-applied style, so applyLeftTextStyle() can skip re-touching
// the DOM when nothing actually changed since the last /state poll.
let currentLeftTextStyle = null;

function applyLeftTextStyle(style){

    const el = document.getElementById("left-text");

    if (!el || !style) return;

    const serialized = JSON.stringify(style);

    if (serialized === currentLeftTextStyle) return;

    currentLeftTextStyle = serialized;

    if (style.font_family) el.style.fontFamily = style.font_family;
    if (style.font_size) el.style.fontSize = style.font_size + "px";
    if (style.color) el.style.color = style.color;

    el.style.fontWeight = style.bold ? "bold" : "normal";
    el.style.fontStyle = style.italic ? "italic" : "normal";
}

// Converts a message's "||" markers into real <br> line breaks
// without letting any raw HTML in the message itself get
// interpreted - text is escaped first (via textContent), THEN the
// || markers are swapped in as actual <br> tags.
function renderLeftTextHtml(text){
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML.split('||').join('<br>');
}

function leftTextSwap(newText){

    const el =
        document.getElementById("left-text");

    if (!el) return;

    // fade out
    el.style.opacity = 0;

    setTimeout(() => {

        el.innerHTML = renderLeftTextHtml(newText);

        el.style.opacity = 1;

    }, 400);

}

// Rotates the left-side text. Each line stays up for its OWN
// duration (leftTextDurations[index], in seconds - set per line on
// the Dashboard), so a timeout is scheduled after every swap
// instead of using one fixed setInterval.
let leftTextTimer = null;

function leftTextDurationMs(index){
    const secs = Number(leftTextDurations[index]);
    return (isFinite(secs) && secs > 0 ? secs : 10) * 1000;
}

function scheduleLeftTextAdvance(){
    clearTimeout(leftTextTimer);

    // While scrolling is off, just re-check once a second so it
    // starts rotating again soon after being switched back on.
    const delay = leftScrollEnabled
        ? leftTextDurationMs(leftTextIndex)
        : 1000;

    leftTextTimer = setTimeout(advanceLeftText, delay);
}

function advanceLeftText(){

    if (!leftScrollEnabled){

        // Scrolling turned off from the Dashboard - make sure it's
        // pinned to the default line and stop advancing.
        if (leftTextIndex !== 0){
            leftTextIndex = 0;
            leftTextSwap(leftTexts[0]);
        }

    } else {

        leftTextIndex =
            (leftTextIndex + 1) %
            leftTexts.length;

        leftTextSwap(
            leftTexts[leftTextIndex]
        );

    }

    scheduleLeftTextAdvance();
}

scheduleLeftTextAdvance();


let currentLocked = false;

let activeTexts =
    getFadeTexts(false);


setInterval(async () => {

    let r = await fetch('/state');

    let d = await r.json();

    // Pick up any text-line edits made from the Dashboard for
    // either the unlocked or locked set.
    if (Array.isArray(d.msg_texts_unlocked) && d.msg_texts_unlocked.length){
        msgTextsUnlocked = d.msg_texts_unlocked;
    }

    if (Array.isArray(d.msg_texts_locked) && d.msg_texts_locked.length){
        msgTextsLocked = d.msg_texts_locked;
    }

    if (d.msg_text_style){
        applyMsgTextStyle(d.msg_text_style);
    }

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

    } else {

        // Keep pointed at the right list even if only its contents
        // changed (not the lock state) since the last poll.
        activeTexts = getFadeTexts(currentLocked);
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


@app.route("/left-overlay")
def left_overlay_page():

    return """
<!doctype html>

<html>

<head>

<meta charset="utf-8">

<title>Left Overlay</title>

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

    z-index: 9999;
    width: fit-content;
}

#left {
    width: 229.65px;
    height: 70.88px;
    background: rgba(58, 58, 58, 0.9);
    color: white;

    display: flex;
    align-items: center;
    justify-content: center;

    text-align: center;

    padding: 0;
}

/* Font family/size/weight/style/color are NOT set here - they're
   applied as inline styles from left_text_style (same as on the
   /timer page), so they can be changed from the Dashboard without
   touching CSS. The values below are just the pre-JS fallback. */
#left-text {
    font-size: 32.67px;
    font-weight: 549;
    line-height: 0.95;

    letter-spacing: .5px;

    opacity: 1;

    transition: opacity .8s ease;
}

</style>

</head>

<body>

<div id="wrap">
    <div id="left"><span id="left-text">Tell or ask me anything.</span></div>
</div>

<script>

// This page mirrors the "left" box logic that used to live on
// /timer. It polls the same /state endpoint the Dashboard writes
// to, so the Dashboard's Box ON/OFF, text lines, and style
// controls keep working exactly the same for this box, whether
// it's used here or on /timer.

let leftTexts = [
    "Tell or ask me anything.",
    "Type your question in chat!"
];

// Seconds each line in leftTexts stays on screen (parallel to
// leftTexts, edited per-line on the Dashboard).
let leftTextDurations = [10, 10];

let leftTextIndex = 0;
let leftScrollEnabled = true;
let leftBoxVisible = true;
let currentLeftTextStyle = null;

function applyLeftBoxVisibility(visible){
    if (visible === leftBoxVisible) return;
    leftBoxVisible = visible;

    const box = document.getElementById("left");
    if (!box) return;

    box.style.display = visible ? "flex" : "none";
}

function applyLeftTextStyle(style){

    const el = document.getElementById("left-text");

    if (!el || !style) return;

    const serialized = JSON.stringify(style);

    if (serialized === currentLeftTextStyle) return;

    currentLeftTextStyle = serialized;

    if (style.font_family) el.style.fontFamily = style.font_family;
    if (style.font_size) el.style.fontSize = style.font_size + "px";
    if (style.color) el.style.color = style.color;

    el.style.fontWeight = style.bold ? "bold" : "normal";
    el.style.fontStyle = style.italic ? "italic" : "normal";
}

// Converts a message's "||" markers into real <br> line breaks
// without letting any raw HTML in the message itself get
// interpreted - text is escaped first (via textContent), THEN the
// || markers are swapped in as actual <br> tags.
function renderLeftTextHtml(text){
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML.split('||').join('<br>');
}

function leftTextSwap(newText){

    const el = document.getElementById("left-text");

    if (!el) return;

    el.style.opacity = 0;

    setTimeout(() => {

        el.innerHTML = renderLeftTextHtml(newText);

        el.style.opacity = 1;

    }, 400);

}

// Rotates the left-side text. Each line stays up for its OWN
// duration (leftTextDurations[index], in seconds - set per line on
// the Dashboard), so a timeout is scheduled after every swap
// instead of using one fixed setInterval.
let leftTextTimer = null;

function leftTextDurationMs(index){
    const secs = Number(leftTextDurations[index]);
    return (isFinite(secs) && secs > 0 ? secs : 10) * 1000;
}

function scheduleLeftTextAdvance(){
    clearTimeout(leftTextTimer);

    // While scrolling is off, just re-check once a second so it
    // starts rotating again soon after being switched back on.
    const delay = leftScrollEnabled
        ? leftTextDurationMs(leftTextIndex)
        : 1000;

    leftTextTimer = setTimeout(advanceLeftText, delay);
}

function advanceLeftText(){

    if (!leftScrollEnabled){

        // Scrolling turned off from the Dashboard - make sure it's
        // pinned to the default line and stop advancing.
        if (leftTextIndex !== 0){
            leftTextIndex = 0;
            leftTextSwap(leftTexts[0]);
        }

    } else {

        leftTextIndex =
            (leftTextIndex + 1) %
            leftTexts.length;

        leftTextSwap(
            leftTexts[leftTextIndex]
        );

    }

    scheduleLeftTextAdvance();
}

scheduleLeftTextAdvance();

async function update(){

    let r = await fetch('/state');

    let d = await r.json();

    leftScrollEnabled = d.left_text_scroll_enabled !== false;

    applyLeftBoxVisibility(d.left_box_enabled !== false);

    let leftTextsChanged = false;
    let leftDurationsChanged = false;

    if (Array.isArray(d.left_texts) && d.left_texts.length){
        leftTextsChanged =
            d.left_texts.length !== leftTexts.length ||
            d.left_texts.some((t, i) => t !== leftTexts[i]);

        if (leftTextsChanged){
            leftTexts = d.left_texts;
            leftTextIndex = 0;
        }
    }

    // Pick up per-line duration edits made from the Dashboard.
    if (Array.isArray(d.left_text_durations) && d.left_text_durations.length){
        leftDurationsChanged =
            d.left_text_durations.length !== leftTextDurations.length ||
            d.left_text_durations.some((t, i) => t !== leftTextDurations[i]);

        if (leftDurationsChanged){
            leftTextDurations = d.left_text_durations;
        }
    }

    // Restart the countdown so the new timing takes effect right
    // away instead of after the old, already-running timeout.
    if (leftTextsChanged || leftDurationsChanged){
        scheduleLeftTextAdvance();
    }

    if (d.left_text_style){
        applyLeftTextStyle(d.left_text_style);
    }
}

setInterval(update, 1000);
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


def launch_keyboard_server():
    """
    Starts keyboard_color_server.py (the Corsair/iCUE keyboard-lighting
    server, port 5001) as its own separate process, in its own new
    console window, so it comes up automatically whenever timer.py
    is run.

    keyboard_color_server.py must be in the same folder as this script. This
    only launches the process - it does NOT shut it down when
    timer.py exits, so closing this window will leave the keyboard
    server's console window open on its own.
    """

    keyboard_script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "keyboard_color_server.py"
    )

    if not os.path.exists(keyboard_script):
        print(
            f"WARNING: keyboard_color_server.py not found at {keyboard_script} - "
            "keyboard lighting server was NOT started."
        )
        return

    try:
        subprocess.Popen(
            [sys.executable, keyboard_script],
            creationflags=subprocess.CREATE_NEW_CONSOLE
        )
        print("Launched keyboard_color_server.py in a new console window.")
    except Exception as e:
        print("Failed to launch keyboard_color_server.py:", e)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    load_state()
    load_exchange_rate_cache()

    launch_keyboard_server()

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
