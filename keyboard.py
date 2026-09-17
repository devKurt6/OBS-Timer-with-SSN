# Standalone server for Corsair/iCUE keyboard lighting.
#
# This used to live inside Test.py (Timer_combo4.py). It has been
# split out so it can be started/stopped/updated independently of
# the timer server - the two no longer share a process.
#
# What it does:
#   Lights the keyboard to match whichever chat message is currently
#   shown in the Live Chat Overlay browser extension (youtube.js):
#     - a Gift message   -> always GIFT_KEYBOARD_COLOR (violet)
#     - a Super Chat      -> YouTube's own tier color for that message
#     - anything else      (plain text, membership post, etc.) -> white
#     - no message selected -> keyboard off
#
# How it's wired up:
#   youtube.js -> background.js (ACTIVE_MESSAGE_COLOR message) ->
#   POST /overlay/active-message-color on this server.
#
#   background.js needs to point its ACTIVE_MESSAGE_COLOR handler at
#   this server's own port (see KEYBOARD_SERVER_BASE in
#   background.js) since it's a separate process from Test.py.
#
# Requirements for the lighting itself to actually work:
#   - iCUE 4.31+ installed and running
#   - "Enable SDK" / third-party device control turned on in
#     iCUE's Settings -> General
#   - `pip install cuesdk` `pip install flask`
#
# Run this alongside Test.py (Timer_combo4.py) as its own process:
#   python keyboard_color_server.py

from flask import Flask, request, jsonify
import threading
import queue
import time
import re

try:
    from cuesdk import (
        CueSdk,
        CorsairDeviceFilter,
        CorsairDeviceType,
        CorsairError,
        CorsairLedColor,
    )
except ImportError:
    CueSdk = None


app = Flask(__name__)


@app.after_request
def _allow_extension_requests(response):
    """Permit the browser-extension overlay (running on youtube.com,
    relaying through its background service worker) to POST to this
    local server. Harmless for a personal/local-only tool - this
    server only listens on 127.0.0.1 to begin with.

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

# ---------------- ICUE (KEYBOARD LIGHTING) ----------------
# Set to False to disable this feature entirely (e.g. if iCUE
# or the cuesdk package isn't installed - the server will still
# start and answer requests, it just won't touch any hardware).
ICUE_ENABLED = True

# Every gift lights the keyboard this same color, regardless of
# which gift it is (Super Chats use YouTube's real tier color,
# reported directly by youtube.js in each request instead).
GIFT_KEYBOARD_COLOR = "#8A2BE2"  # violet

# The keyboard color for a plain chat message (no gift, no Super
# Chat), and the color used when no message is currently selected
# in the Live Chat Overlay extension ("off" = black = LEDs
# effectively off).
ICUE_MESSAGE_COLOR = "#FFFFFF"  # white
ICUE_OFF_COLOR = "#000000"

# Which host/port this server listens on. background.js's
# ACTIVE_MESSAGE_COLOR handler must point at the same values.
KEYBOARD_SERVER_HOST = "127.0.0.1"
KEYBOARD_SERVER_PORT = 5001


# ============================================================
# ICUE (KEYBOARD LIGHTING)
# ============================================================

icue_sdk = None
icue_keyboard_device_id = None
icue_lock = threading.Lock()

# Cached per-device LED layout, fetched once at connect time so we
# don't re-query it on every single color change.
icue_led_positions = None

# Set by _icue_state_changed once the session actually reaches
# CSS_Connected. connect() returns as soon as the request is
# *accepted*, not once iCUE has actually finished the handshake -
# querying devices before this event fires reliably returns
# CE_NotConnected even though everything else is fine.
icue_connected_event = threading.Event()

# Every gift/Super Chat color goes through this queue instead of
# spawning its own thread - see apply_keyboard_color_async() and
# _icue_worker_loop() below for why.
icue_color_queue = queue.Queue()
icue_worker_started = False


def _icue_state_changed(evt):
    print(f"[ICUE] Session state -> {evt.state}")
    if "Connected" in str(evt.state) and "NotConnected" not in str(evt.state) and "Connecting" not in str(evt.state):
        icue_connected_event.set()


def init_icue():
    """Connects to iCUE's SDK and finds the first keyboard.

    Safe to call even if iCUE isn't running or the cuesdk package
    isn't installed - it just prints a warning and leaves the
    keyboard-lighting feature inactive. Nothing else in this server
    depends on this succeeding (it will still answer HTTP requests
    either way, it just won't be able to light anything).
    """

    global icue_sdk, icue_keyboard_device_id, icue_led_positions

    if not ICUE_ENABLED:
        print("[ICUE] Disabled (ICUE_ENABLED = False).")
        return

    if CueSdk is None:
        print(
            "[ICUE] 'cuesdk' package not installed - "
            "keyboard lighting disabled. Run: pip install cuesdk"
        )
        return

    try:
        try:
            import cuesdk as _cuesdk_pkg
            print(f"[ICUE] cuesdk package version: {getattr(_cuesdk_pkg, '__version__', 'unknown')}")
        except Exception:
            pass

        sdk = CueSdk()
        err = sdk.connect(_icue_state_changed)
        print(f"[ICUE] connect() returned: {err}")

        if err != CorsairError.CE_Success:
            print(f"[ICUE] connect() failed: {err}")
            return

        # Wait for the session to actually finish connecting before
        # asking for anything else - connect() only means the
        # request was accepted, not that the handshake is done yet.
        if not icue_connected_event.wait(timeout=10):
            print(
                "[ICUE] Timed out waiting for CSS_Connected - iCUE "
                "may still be starting up. Try again in a few seconds."
            )
            return

        details, details_err = sdk.get_session_details()
        print(f"[ICUE] get_session_details() -> err={details_err}, details={details}")

        # Diagnostic: ask for ALL devices first (no filter), so we
        # can tell a permissions problem (nothing comes back at all)
        # apart from a keyboard-filter problem (other devices show
        # up but the keyboard doesn't).
        all_devices, all_err = sdk.get_devices(
            CorsairDeviceFilter(device_type_mask=CorsairDeviceType.CDT_All)
        )
        print(
            f"[ICUE] get_devices(ALL) err={all_err}, "
            f"found={len(all_devices) if all_devices else 0}: "
            f"{[ (d.device_id, getattr(d, 'device_type', '?')) for d in (all_devices or []) ]}"
        )

        # Give iCUE a moment to finish the handshake before we ask
        # it for devices.
        devices = None
        last_err = None
        for attempt in range(20):
            devices, err = sdk.get_devices(
                CorsairDeviceFilter(
                    device_type_mask=CorsairDeviceType.CDT_Keyboard
                )
            )
            last_err = err
            if err == CorsairError.CE_Success and devices:
                break
            time.sleep(0.25)

        print(f"[ICUE] get_devices() last err={last_err}, found={len(devices) if devices else 0}")

        if not devices:
            print(
                "[ICUE] No keyboard found. Make sure iCUE is running "
                "and 'Enable SDK' is turned on in iCUE Settings -> "
                "General. (See the err code above the line - if it "
                "reads CE_Success with 0 devices, iCUE is reachable "
                "but isn't reporting the K95 to the SDK layer; if "
                "it's anything else, that's the actual failure.)"
            )
            return

        device_id = devices[0].device_id
        print(f"[ICUE] Found device: {sdk.get_device_info(device_id)}")

        if hasattr(sdk, "get_led_positions"):
            leds, err = sdk.get_led_positions(device_id)
        elif hasattr(sdk, "get_led_positions_by_device_index"):
            leds, err = sdk.get_led_positions_by_device_index(0)
        else:
            print(
                "[ICUE] Neither get_led_positions() nor "
                "get_led_positions_by_device_index() exist on this "
                "cuesdk version. Available methods: "
                + ", ".join(m for m in dir(sdk) if not m.startswith("_"))
            )
            return

        if err != CorsairError.CE_Success or not leds:
            print(f"[ICUE] Could not read LED positions: {err}")
            return

        with icue_lock:
            icue_sdk = sdk
            icue_keyboard_device_id = device_id
            icue_led_positions = leds

        print(f"[ICUE] Connected. Keyboard device_id={device_id}, {len(leds)} LEDs")

        global icue_worker_started
        if not icue_worker_started:
            icue_worker_started = True
            threading.Thread(target=_icue_worker_loop, daemon=True).start()

    except Exception as e:
        print("[ICUE] Failed to initialize:", e)


def _get_led_id(led):
    """The LED-id field on the position object has been named
    differently across cuesdk versions (led_id / ledId / id). Try
    the known names instead of hardcoding one that might not match
    this install."""

    for attr in ("led_id", "ledId", "id"):
        if hasattr(led, attr):
            return getattr(led, attr)

    raise AttributeError(
        f"Could not find a LED-id field on position object. "
        f"Available attributes: {[a for a in dir(led) if not a.startswith('_')]}"
    )


def _make_led_color(led_id, r, g, b):
    """CorsairLedColor's exact constructor signature (RGB vs RGBA,
    positional vs keyword) has varied across cuesdk versions. Try
    the common shapes instead of hardcoding one."""

    for attempt in (
        lambda: CorsairLedColor(led_id, r, g, b, 255),
        lambda: CorsairLedColor(led_id, r, g, b, a=255),
        lambda: CorsairLedColor(led_id, r, g, b),
    ):
        try:
            return attempt()
        except TypeError:
            continue

    raise TypeError("No matching CorsairLedColor(...) signature found.")


def _parse_color(color):
    """Parses a color string into ((r, g, b), format_label), or
    (None, None) if it doesn't match a known format.

    Accepts:
      - hex: '#F57F17', 'F57F17'      -> format_label 'hex'
      - rgb():  'rgb(245, 127, 23)'   -> format_label 'rgb'
      - rgba(): 'rgba(245, 127, 23, 1)' -> format_label 'rgba'

    YouTube's own tier colors (what tierColor carries for Super
    Chats) come through as rgba(...), e.g. 'rgba(0,229,255,1)' -
    not hex - so both formats have to be handled here.
    """

    if not color:
        return None, None

    color = color.strip()

    rgb_match = re.match(
        r"(rgba?)\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*[\d.]+\s*)?\)",
        color,
        re.IGNORECASE,
    )
    if rgb_match:
        try:
            format_label = rgb_match.group(1).lower()
            r, g, b = (int(round(float(v))) for v in rgb_match.groups()[1:])
            return (
                (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))),
                format_label,
            )
        except ValueError:
            return None, None

    hex_color = color.lstrip("#")
    if len(hex_color) == 6:
        try:
            return (
                (
                    int(hex_color[0:2], 16),
                    int(hex_color[2:4], 16),
                    int(hex_color[4:6], 16),
                ),
                "hex",
            )
        except ValueError:
            return None, None

    return None, None


def set_keyboard_color(color):
    """Sets every LED on the keyboard to color (e.g. '#F57F17' or
    'rgba(245, 127, 23, 1)').

    Runs whatever color was most recently reported for a gift or
    Super Chat, and leaves the keyboard on that color until the
    next one comes in (matches "keyboard shows the color of the
    last super chat / gift"). Only reached once ICUE is enabled and
    a color has already been queued - see apply_keyboard_color_async()
    for the "was a color received at all" logging.
    """

    rgb, format_label = _parse_color(color)

    if rgb is None:
        print(f"[ICUE] Could not re-parse queued color: {color!r}, skipping")
        return

    r, g, b = rgb

    if (
        icue_sdk is None
        or icue_led_positions is None
        or icue_keyboard_device_id is None
    ):
        print(
            f"[ICUE] Skipping {color!r} (RGB {r},{g},{b}) - "
            f"keyboard not connected."
        )
        return

    try:
        with icue_lock:
            colors = [
                _make_led_color(_get_led_id(led), r, g, b)
                for led in icue_led_positions
            ]
            # Using the direct, synchronous set_led_colors() here
            # instead of set_led_colors_buffer() + the async flush -
            # the async flush's callback was getting garbage
            # collected by Python after the call returned while
            # iCUE still held a pointer to it, crashing the process
            # on the next completion. This method needs no callback
            # at all, so that failure mode doesn't exist here.
            err = icue_sdk.set_led_colors(icue_keyboard_device_id, colors)
            if err is not None and str(err) != "CorsairError.CE_Success":
                print(f"[ICUE] set_led_colors returned: {err}")

    except Exception as e:
        print("[ICUE] Failed to set keyboard color:", e)


def apply_keyboard_color_async(color):
    """Fire-and-forget wrapper so a slow SDK call never delays the
    Flask request that triggered it.

    Always logs what was received and how it parsed (hex vs
    rgb/rgba, and the resolved RGB), even if ICUE is disabled or no
    color came through at all - this is the one place every color
    coming from the browser extension passes through, so it's the
    right spot to answer "did a color arrive, and what was it".

    IMPORTANT: past the logging, this only enqueues the color - it
    does NOT spawn a new thread per event. cuesdk is a ctypes
    binding straight into Corsair's native DLL, which is not safe
    to call from multiple threads concurrently (two overlapping
    calls can crash the whole Python process, not just raise a
    catchable exception). A single dedicated worker thread (started
    in init_icue) drains this queue one color at a time, so the
    native SDK is never touched from more than one thread at once.
    """

    if not color:
        print("[ICUE] Color received: none (empty/missing) - ignoring")
        return

    rgb, format_label = _parse_color(color)
    if rgb is None:
        print(f"[ICUE] Color received: {color!r} -> COULD NOT PARSE, ignoring")
        return

    r, g, b = rgb
    print(
        f"[ICUE] Color received: {color!r} "
        f"(format={format_label}) -> RGB({r}, {g}, {b})"
    )

    if not ICUE_ENABLED:
        print("[ICUE] Not applying to keyboard - ICUE_ENABLED is False.")
        return

    icue_color_queue.put(color)


def _icue_worker_loop():
    """Runs for the lifetime of the program on its own thread. This
    is the ONLY thread allowed to call into icue_sdk after startup -
    every gift/Super Chat just drops a color into icue_color_queue
    instead of touching the SDK directly."""

    while True:
        hex_color = icue_color_queue.get()

        # If several colors piled up while we were busy (a burst of
        # gifts), skip straight to the most recent one instead of
        # flashing through every color in order - only matters for
        # bursts, harmless otherwise.
        while True:
            try:
                hex_color = icue_color_queue.get_nowait()
            except queue.Empty:
                break

        set_keyboard_color(hex_color)


# ============================================================
# ROUTES
# ============================================================

@app.route("/overlay/active-message-color", methods=["POST", "OPTIONS"])
def overlay_active_message_color():
    """Lights the keyboard to match whichever chat message is
    currently shown in the Live Chat Overlay extension (youtube.js).

    Sent by youtube.js via background.js every time a message is
    clicked to show/hide in the overlay - see the "ACTIVE MESSAGE
    KEYBOARD COLOR" section of youtube.js.

    Expects JSON body:
      {"status": "shown", "messageType": "gift", "tierColor": ""}
      {"status": "shown", "messageType": "superchat", "tierColor": "#RRGGBB"}
      {"status": "shown", "messageType": "message", "tierColor": ""}
      {"status": "hidden"}

    status="hidden" (no message currently selected) turns the
    keyboard off. messageType="gift" always uses GIFT_KEYBOARD_COLOR
    (violet) regardless of which gift. messageType="superchat" uses
    the real tierColor YouTube reported. Anything else (a plain
    text message, a membership post, etc.) lights the keyboard
    white.
    """

    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(silent=True) or {}

    status = str(data.get("status") or "").strip().lower()
    message_type = str(data.get("messageType") or "").strip().lower()
    tier_color = str(data.get("tierColor") or "").strip()

    if status == "hidden":
        apply_keyboard_color_async(ICUE_OFF_COLOR)
        print("[OVERLAY COLOR] No message selected -> keyboard off")
        return jsonify({"ok": True})

    if message_type == "gift":
        apply_keyboard_color_async(GIFT_KEYBOARD_COLOR)
        print(f"[OVERLAY COLOR] Gift message shown -> {GIFT_KEYBOARD_COLOR}")
    elif message_type == "superchat" and tier_color:
        apply_keyboard_color_async(tier_color)
        print(f"[OVERLAY COLOR] Super Chat message shown -> {tier_color}")
    else:
        apply_keyboard_color_async(ICUE_MESSAGE_COLOR)
        print(f"[OVERLAY COLOR] Regular message shown -> {ICUE_MESSAGE_COLOR}")

    return jsonify({"ok": True})


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    threading.Thread(
        target=init_icue,
        daemon=True
    ).start()

    app.run(
        host=KEYBOARD_SERVER_HOST,
        port=KEYBOARD_SERVER_PORT,
        debug=False
    )
