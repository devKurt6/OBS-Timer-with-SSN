from flask import Flask, request, jsonify
import time, threading, json, os, re

try:
    import keyboard
except ImportError:
    keyboard = None

app = Flask(__name__)

# ================= CONFIG =================

# Super Chats are MANUAL ONLY now. The bot will not add time from Super Chat amounts/currencies.
GIFT_SECONDS_PER_JEWEL = 0.5

# Global hotkeys for manually adding Super Chat time.
# IMPORTANT: these are NUMERIC KEYPAD scan codes only.
# This avoids the Python keyboard library treating top-row numbers like numpad numbers.
# Windows scan codes: numpad 1-6 are 79, 80, 81, 75, 76, 77.
# Top-row 1-6 are different scan codes and are intentionally ignored.
NUMPAD_SCAN_CODE_SECONDS = {
    79: 30,  # Numpad 1 / dark blue: 30 seconds
    80: 60,  # Numpad 2 / light blue: 1 minute
    81: 150,  # Numpad 3 / green: 2.5 minutes
    75: 300,  # Numpad 4 / yellow: 5 minutes
    76: 600,  # Numpad 5 / orange: 10 minutes
    77: 1500,  # Numpad 6 / magenta: 25 minutes
}

# No regular-number hotkeys. No subtract hotkeys.
SUBTRACT_HOTKEYS = {}

STATE_FILE = "timer_state.json"

# ================= STATE =================

timer_seconds = 0.0
bank_seconds = 0.0

timer_running = False
timer_locked = False

last_tick = time.time()

seen_ids = set()
lock = threading.Lock()


# ================= SAVE / LOAD =================

def save_state():
    with lock:
        data = {
            "timer_seconds": timer_seconds,
            "bank_seconds": bank_seconds,
            "running": timer_running,
            "locked": timer_locked
        }

    with open(STATE_FILE, "w") as f:
        json.dump(data, f)


def load_state():
    global timer_seconds, bank_seconds, timer_running, timer_locked

    if not os.path.exists(STATE_FILE):
        return

    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)

        timer_seconds = float(data.get("timer_seconds", 0))
        bank_seconds = float(data.get("bank_seconds", 0))

        timer_running = False
        timer_locked = bool(data.get("locked", False))

    except:
        pass


# ================= TIMER CORE =================

def add_time(sec):
    global timer_seconds, bank_seconds

    with lock:
        if timer_locked:
            bank_seconds += sec
        else:
            timer_seconds += sec

    save_state()


def subtract_time(sec):
    global timer_seconds

    with lock:
        timer_seconds = max(0, timer_seconds - sec)

    save_state()


def tick():
    global timer_seconds, last_tick

    while True:
        time.sleep(0.2)

        with lock:
            now = time.time()
            delta = now - last_tick
            last_tick = now

            if timer_running and timer_seconds > 0:
                timer_seconds = max(0, timer_seconds - delta)

        save_state()


# ================= INPUT =================

@app.route("/", methods=["POST"])
def event():
    data = request.get_json(force=True, silent=True) or {}

    event_id = data.get("meta", {}).get("messageId") or data.get("id")
    if event_id:
        if event_id in seen_ids:
            return "OK"
        seen_ids.add(event_id)

    if data.get("event") == "jeweldonation":
        try:
            gift = data["meta"]["youtubeGift"]
            jewels = float(gift.get("jewelsAmount", 0))
            add_time(jewels * GIFT_SECONDS_PER_JEWEL)
        except:
            pass
        return "OK"

    # Super Chats are manual only.
    # Do NOT parse hasDonation amounts or currencies here.
    # This prevents foreign-currency Super Chats from adding the wrong time.
    if data.get("hasDonation"):
        print("SUPER CHAT RECEIVED - manual hotkey required:", data.get("hasDonation"))
        return "OK"

    return "OK"


# ================= STATE =================

@app.route("/state")
def state():
    with lock:
        return jsonify({
            "seconds": timer_seconds,
            "bank_seconds": bank_seconds,
            "running": timer_running,
            "locked": timer_locked
        })


# ================= CONTROLS =================

@app.route("/start")
def start():
    global timer_running, last_tick
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
    add_time(float(sec))
    return "added"


@app.route("/subtract/<sec>")
def subtract(sec):
    subtract_time(float(sec))
    return "subtracted"


@app.route("/apply_bank")
def apply_bank():
    global timer_seconds, bank_seconds
    with lock:
        timer_seconds += bank_seconds
    save_state()
    return "applied"


@app.route("/clear_bank")
def clear_bank():
    global bank_seconds
    with lock:
        bank_seconds = 0
    save_state()
    return "cleared"


# ================= DASHBOARD =================

@app.route("/dashboard")
def dashboard():
    return """
<!doctype html>
<html>
<head>
<title>Timer Dashboard</title>
<style>
body { font-family: Arial; background:#111; color:white; padding:20px; }
button { margin:5px; padding:10px; }
</style>
</head>
<body>

<h2>Timer Dashboard</h2>

<h3>Controls</h3>
<button onclick="fetch('/start')">Start</button>
<button onclick="fetch('/pause')">Pause</button>
<button onclick="fetch('/reset')">Reset</button>

<h3>Lock</h3>
<button onclick="fetch('/lock')">Lock</button>
<button onclick="fetch('/unlock')">Unlock</button>

<h3>Bank</h3>
<button onclick="fetch('/apply_bank')">Apply Bank</button>
<button onclick="fetch('/clear_bank')">Clear Bank</button>

<h3>Manual Super Chat Add Time</h3>
<p>Numeric keypad only: Numpad 1 = +30s, Numpad 2 = +1 min, Numpad 3 = +2.5 min, Numpad 4 = +5 min, Numpad 5 = +10 min, Numpad 6 = +25 min.</p>
<p>The regular number row does nothing.</p>

<h3>Subtract Time</h3>
<button onclick="fetch('/subtract/30')">-30 sec</button>
<button onclick="fetch('/subtract/60')">-1 min</button>
<button onclick="fetch('/subtract/150')">-2.5 min</button>
<button onclick="fetch('/subtract/300')">-5 min</button>
<button onclick="fetch('/subtract/600')">-10 min</button>
<button onclick="fetch('/subtract/1500')">-25 min</button>

</body>
</html>
"""


# ================= TIMER UI (ONLY FIX: CENTERING) =================

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
    justify-content: flex-start; /* Don't spread them apart */
    gap: 57px;                   /* Space between left and right */

    width: fit-content;          /* Wrap only around the content */
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
    height: 20px;          /* Always reserve space for two lines */

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
    margin-top: 0;      /* Move timer upward */
}
</style>
</head>

<body>

<div id="wrap">
    <!--<div id="left">Tell or ask me anything.</div> -->

    <div id="right">
       <!-- <div id="msg">Stream ends in...</div> -->
        <div id="time">00:00:00</div>
    </div>
</div>

<script>
function fmt(s){
    s = Math.max(0, Math.floor(s));
    let h = Math.floor(s/3600);
    let m = Math.floor((s%3600)/60);
    let sec = s%60;

    return String(h).padStart(2,'0') + ":" +
           String(m).padStart(2,'0') + ":" +
           String(sec).padStart(2,'0');
}

async function update(){
    let r = await fetch('/state');
    let d = await r.json();

    document.getElementById('time').innerText = fmt(d.seconds);
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
    const el = document.getElementById("msg");

    // fade out
    el.style.opacity = 0;

    setTimeout(() => {
        el.innerText = newText;
        el.style.opacity = 1;
    }, 400);
}

let currentLocked = false;
let activeTexts = getFadeTexts(false);

setInterval(async () => {
    let r = await fetch('/state');
    let d = await r.json();

    // detect lock change
    if (d.locked !== currentLocked){
        currentLocked = d.locked;
        activeTexts = getFadeTexts(currentLocked);
        index = 0;
    }

    index = (index + 1) % activeTexts.length;
    fadeTextSwap(activeTexts[index]);

}, 5000);

setInterval(update, 500);
update();
</script>

</body>
</html>
"""


# ================= GLOBAL HOTKEYS =================

def register_hotkeys():
    if keyboard is None:
        print("Global hotkeys NOT enabled: install the keyboard module with: pip install keyboard")
        return

    # Some Windows/Python-keyboard setups treat "num 1" like regular "1".
    # So we use physical scan codes to reject top-row numbers, AND we reject
    # navigation key names so arrows/Home/End/Page keys cannot add time.
    last_press_by_scan_code = {}
    debounce_seconds = 0.25

    allowed_digit_names_by_scan_code = {
        79: {"1", "num 1", "numpad 1"},
        80: {"2", "num 2", "numpad 2"},
        81: {"3", "num 3", "numpad 3"},
        75: {"4", "num 4", "numpad 4"},
        76: {"5", "num 5", "numpad 5"},
        77: {"6", "num 6", "numpad 6"},
    }

    navigation_names = {
        "left", "right", "up", "down",
        "home", "end", "page up", "page down",
        "insert", "delete",
    }

    def on_key_event(event):
        if event.event_type != "down":
            return

        seconds = NUMPAD_SCAN_CODE_SECONDS.get(event.scan_code)
        if seconds is None:
            return

        key_name = (event.name or "").lower()

        # Arrow/navigation keys can share scan codes with the numpad on Windows.
        # They must never add time.
        if key_name in navigation_names:
            return

        # Accept only the expected digit name for this numpad scan code.
        # This keeps top-row numbers rejected by scan code and navigation keys rejected by name.
        if key_name not in allowed_digit_names_by_scan_code.get(event.scan_code, set()):
            print(f"Ignored numpad-like scan_code={event.scan_code} name={event.name!r}")
            return

        now = time.time()
        last_press = last_press_by_scan_code.get(event.scan_code, 0)
        if now - last_press < debounce_seconds:
            return

        last_press_by_scan_code[event.scan_code] = now
        add_time(seconds)
        print(f"NUMPAD HOTKEY scan_code={event.scan_code} name={event.name!r} -> +{seconds}s")

    try:
        keyboard.hook(on_key_event)

        for scan_code, seconds in NUMPAD_SCAN_CODE_SECONDS.items():
            print(f"Registered NUMPAD scan code {scan_code} -> +{seconds}s")

        if SUBTRACT_HOTKEYS:
            print(
                "WARNING: SUBTRACT_HOTKEYS is not empty, but subtract hotkeys are intentionally disabled in this version.")

        print("Global numpad add hotkeys enabled. Regular number row should do nothing.")
    except Exception as e:
        print("Global hotkeys failed to start:", e)
        print("Try running Command Prompt as Administrator, or use the dashboard buttons instead.")


# ================= RUN =================

if __name__ == "__main__":
    load_state()
    last_tick = time.time()
    threading.Thread(target=tick, daemon=True).start()
    register_hotkeys()
    app.run(host="127.0.0.1", port=5000, debug=True)