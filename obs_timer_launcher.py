"""
OBS script that:
  1. starts your timer.py automatically when OBS starts (and stops it
     when OBS closes), and
  2. presses the timer's "Start" (countdown) button for you the moment
     you press "Start Streaming", and
  3. refreshes your timer Browser Sources (http://127.0.0.1:5000/...)
     once timer.py is up, so they display without a manual refresh, and
  4. automatically plays a video file (mp4, mkv, mov, ...) the moment
     you press "Start Streaming". It opens in Windows' default video
     player (not inside OBS).

Setup (one time):
  1. OBS > Tools > Scripts > "Python Settings" tab: point OBS at your
     Python install folder (needed for any Python script in OBS).
  2. Scripts tab > "+" > pick this file (obs_timer_launcher.py).
  3. Fill in your SSN Session ID in the script's settings on the right.
  5. (Optional) Pick your video file with the "Video file" Browse button.
  Scripts stay loaded between OBS launches, so from now on the timer
  starts by itself every time you open OBS.
"""

import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request

import obspython as obs

# ------------------------------------------------------------------
# Defaults (all of these can be changed in the OBS script settings)
# ------------------------------------------------------------------
DEFAULT_TIMER_PATH = r"C:\Users\kurtd\Documents\client\OBS-Timer-with-SSN\timer.py"
TIMER_PORT = 5000  # timer.py's Flask server port

# ------------------------------------------------------------------
# State
# ------------------------------------------------------------------
timer_process = None
log_handle = None

cfg = {
    "timer_path": DEFAULT_TIMER_PATH,
    "python_exe": "",
    "session_id": "",
    "auto_start": True,
    "show_console": True,
    "stop_on_exit": True,
    "timer_autostart": True,
    "refresh_on_launch": True,
    "refresh_on_stream": True,
    "video_enabled": True,
    "video_path": "",
    "video_delay": 0,
}


def log(message, level=None):
    obs.script_log(level if level is not None else obs.LOG_INFO,
                   "[Timer Launcher] " + message)


def read_settings(settings):
    cfg["timer_path"] = obs.obs_data_get_string(settings, "timer_path") or DEFAULT_TIMER_PATH
    cfg["python_exe"] = obs.obs_data_get_string(settings, "python_exe")
    cfg["session_id"] = obs.obs_data_get_string(settings, "session_id").strip()
    cfg["auto_start"] = obs.obs_data_get_bool(settings, "auto_start")
    cfg["show_console"] = obs.obs_data_get_bool(settings, "show_console")
    cfg["stop_on_exit"] = obs.obs_data_get_bool(settings, "stop_on_exit")
    cfg["timer_autostart"] = obs.obs_data_get_bool(settings, "timer_autostart")
    cfg["refresh_on_launch"] = obs.obs_data_get_bool(settings, "refresh_on_launch")
    cfg["refresh_on_stream"] = obs.obs_data_get_bool(settings, "refresh_on_stream")
    cfg["video_enabled"] = obs.obs_data_get_bool(settings, "video_enabled")
    cfg["video_path"] = obs.obs_data_get_string(settings, "video_path")
    cfg["video_delay"] = obs.obs_data_get_double(settings, "video_delay")


def find_python():
    """Python used to RUN timer.py (not OBS's own embedded one)."""
    if cfg["python_exe"] and os.path.isfile(cfg["python_exe"]):
        return cfg["python_exe"]
    return shutil.which("python") or shutil.which("py")


def port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", TIMER_PORT)) == 0


def is_running():
    return timer_process is not None and timer_process.poll() is None


# ------------------------------------------------------------------
# Start / stop
# ------------------------------------------------------------------
def start_timer():
    global timer_process, log_handle

    if is_running():
        log("Timer is already running.")
        return

    if port_in_use():
        log("Port %d is already in use - timer.py is probably already "
            "running, so not starting a second copy." % TIMER_PORT,
            obs.LOG_WARNING)
        return

    timer_path = cfg["timer_path"]
    if not os.path.isfile(timer_path):
        log("timer.py not found at: " + timer_path, obs.LOG_ERROR)
        return

    if not cfg["session_id"]:
        log("SSN Session ID is empty - enter it in this script's settings. "
            "(timer.py refuses to start without it.)", obs.LOG_ERROR)
        return

    python_exe = find_python()
    if not python_exe:
        log("Could not find Python. Set the 'Python for timer.py' path in "
            "the script settings.", obs.LOG_ERROR)
        return

    cmd = [python_exe, "-u", timer_path, cfg["session_id"]]
    work_dir = os.path.dirname(timer_path)  # timer.py saves its files relative to here

    kwargs = {"cwd": work_dir}

    if cfg["show_console"]:
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    else:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        log_handle = open(os.path.join(work_dir, "obs_timer_launcher.log"),
                          "a", encoding="utf-8", errors="replace")
        kwargs["stdout"] = log_handle
        kwargs["stderr"] = subprocess.STDOUT

    try:
        timer_process = subprocess.Popen(cmd, **kwargs)
        log("Started timer.py (PID %d)." % timer_process.pid)
    except Exception as error:
        timer_process = None
        log("Failed to start timer.py: %s" % error, obs.LOG_ERROR)


def stop_timer():
    global timer_process, log_handle

    if is_running():
        pid = timer_process.pid
        # /T also closes anything timer.py launched itself (e.g. the
        # keyboard colour server).
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except Exception:
            try:
                timer_process.terminate()
            except Exception:
                pass
        log("Stopped timer.py (PID %d)." % pid)

    timer_process = None

    if log_handle is not None:
        try:
            log_handle.close()
        except Exception:
            pass
        log_handle = None


# ------------------------------------------------------------------
# Refresh the timer's Browser Sources
# ------------------------------------------------------------------
_refresh_state = {"tries": 0}


def refresh_timer_sources():
    """Presses 'Refresh cache of current page' on every Browser Source
    whose URL points at timer.py (127.0.0.1 / localhost on its port).
    That covers /timer, /left-overlay, /image-overlay, etc."""
    refreshed = 0
    needle = ":%d" % TIMER_PORT

    sources = obs.obs_enum_sources()
    if sources is not None:
        for source in sources:
            if obs.obs_source_get_unversioned_id(source) != "browser_source":
                continue

            settings = obs.obs_source_get_settings(source)
            url = obs.obs_data_get_string(settings, "url")
            obs.obs_data_release(settings)

            if needle in url and ("127.0.0.1" in url or "localhost" in url):
                props = obs.obs_source_properties(source)
                button = obs.obs_properties_get(props, "refreshnocache")
                if button is not None:
                    obs.obs_property_button_clicked(button, source)
                    refreshed += 1
                obs.obs_properties_destroy(props)

        obs.source_list_release(sources)

    if refreshed:
        log("Refreshed %d timer Browser Source(s)." % refreshed)
    else:
        log("No Browser Source pointing at 127.0.0.1:%d was found to refresh."
            % TIMER_PORT, obs.LOG_WARNING)


def _poll_then_refresh():
    _refresh_state["tries"] += 1

    if port_in_use():
        obs.timer_remove(_poll_then_refresh)
        refresh_timer_sources()
    elif _refresh_state["tries"] >= 40:
        obs.timer_remove(_poll_then_refresh)
        log("timer.py never came up on port %d, so nothing was refreshed."
            % TIMER_PORT, obs.LOG_ERROR)


def refresh_when_timer_ready():
    """Waits (without freezing OBS) until timer.py answers on its port,
    then refreshes the Browser Sources."""
    obs.timer_remove(_poll_then_refresh)
    _refresh_state["tries"] = 0
    obs.timer_add(_poll_then_refresh, 1000)


# ------------------------------------------------------------------
# Start the timer countdown when the stream starts
# ------------------------------------------------------------------
def _press_timer_start():
    """Same as clicking the Start button on the dashboard (GET /start).
    Runs in a background thread so OBS never freezes; retries for a few
    seconds in case timer.py is still booting."""
    url = "http://127.0.0.1:%d/start" % TIMER_PORT

    for attempt in range(8):
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                response.read()
            log("Timer countdown started.")
            return
        except Exception as error:
            last_error = error
            time.sleep(1.5)

    log("Could not press the timer's Start button (%s). Is timer.py "
        "running?" % last_error, obs.LOG_ERROR)


def start_timer_countdown():
    threading.Thread(target=_press_timer_start, daemon=True).start()


# ------------------------------------------------------------------
# Auto-play video when the stream starts
# ------------------------------------------------------------------
def play_video():
    path = cfg["video_path"]

    if not path or not os.path.isfile(path):
        log("Video file not found: '%s'. Pick it in the script settings."
            % path, obs.LOG_ERROR)
        return

    # Opens the file with Windows' default video player.
    try:
        os.startfile(path)
        log("Opened video in the default player: " + path)
    except Exception as error:
        log("Could not open video: %s" % error, obs.LOG_ERROR)


def _delayed_video_play():
    obs.timer_remove(_delayed_video_play)
    play_video()


def schedule_video_play():
    obs.timer_remove(_delayed_video_play)
    delay_ms = max(100, int(cfg["video_delay"] * 1000))
    obs.timer_add(_delayed_video_play, delay_ms)


def on_frontend_event(event):
    if event == obs.OBS_FRONTEND_EVENT_STREAMING_STARTED:
        log("Stream started.")
        if cfg["refresh_on_stream"]:
            refresh_when_timer_ready()
        if cfg["timer_autostart"]:
            start_timer_countdown()
        if cfg["video_enabled"] and cfg["video_path"]:
            schedule_video_play()


# ------------------------------------------------------------------
# Button callbacks
# ------------------------------------------------------------------
def on_refresh_now_clicked(props, prop):
    refresh_timer_sources()
    return False


def on_test_video_clicked(props, prop):
    play_video()
    return False


def on_start_clicked(props, prop):
    start_timer()
    return False


def on_stop_clicked(props, prop):
    stop_timer()
    return False


def on_restart_clicked(props, prop):
    stop_timer()
    start_timer()
    return False


# ------------------------------------------------------------------
# OBS script hooks
# ------------------------------------------------------------------
def script_description():
    return ("<b>Timer Launcher</b><br>"
            "Starts timer.py automatically when OBS opens and stops it when "
            "OBS closes. When you start streaming it also presses the "
            "timer's Start button and can auto-play a video file.")


def script_defaults(settings):
    obs.obs_data_set_default_string(settings, "timer_path", DEFAULT_TIMER_PATH)
    obs.obs_data_set_default_bool(settings, "auto_start", True)
    obs.obs_data_set_default_bool(settings, "show_console", True)
    obs.obs_data_set_default_bool(settings, "stop_on_exit", True)
    obs.obs_data_set_default_bool(settings, "timer_autostart", True)
    obs.obs_data_set_default_bool(settings, "refresh_on_launch", True)
    obs.obs_data_set_default_bool(settings, "refresh_on_stream", True)
    obs.obs_data_set_default_bool(settings, "video_enabled", True)


def script_properties():
    props = obs.obs_properties_create()

    obs.obs_properties_add_path(props, "timer_path", "timer.py location",
                                obs.OBS_PATH_FILE, "Python (*.py)", None)
    obs.obs_properties_add_text(props, "session_id", "SSN Session ID",
                                obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_path(props, "python_exe",
                                "Python for timer.py (optional)",
                                obs.OBS_PATH_FILE, "python.exe (*.exe)", None)
    obs.obs_properties_add_bool(props, "auto_start", "Start timer when OBS opens")
    obs.obs_properties_add_bool(props, "show_console", "Show the console window")
    obs.obs_properties_add_bool(props, "stop_on_exit", "Stop timer when OBS closes")

    obs.obs_properties_add_button(props, "start_btn", "Start now", on_start_clicked)
    obs.obs_properties_add_button(props, "stop_btn", "Stop", on_stop_clicked)
    obs.obs_properties_add_button(props, "restart_btn", "Restart", on_restart_clicked)

    # ---- browser source refresh ----
    obs.obs_properties_add_bool(props, "refresh_on_launch",
                                "Refresh timer Browser Sources after timer.py starts")
    obs.obs_properties_add_bool(props, "refresh_on_stream",
                                "Refresh timer Browser Sources when stream starts")
    obs.obs_properties_add_button(props, "refresh_now_btn",
                                  "Refresh Browser Sources now",
                                  on_refresh_now_clicked)

    # ---- timer countdown ----
    obs.obs_properties_add_bool(props, "timer_autostart",
                                "Start timer countdown when stream starts")

    # ---- auto-play video ----
    obs.obs_properties_add_bool(props, "video_enabled",
                                "Auto-play video when stream starts")
    obs.obs_properties_add_path(
        props, "video_path", "Video file",
        obs.OBS_PATH_FILE,
        "Video files (*.mp4 *.mkv *.mov *.avi *.webm *.flv *.m4v);;All files (*.*)",
        None)

    obs.obs_properties_add_float(props, "video_delay",
                                 "Delay after stream starts (seconds)",
                                 0, 600, 0.5)
    obs.obs_properties_add_button(props, "test_video_btn", "Test video now",
                                  on_test_video_clicked)

    return props


def script_update(settings):
    read_settings(settings)


def _delayed_autostart():
    obs.timer_remove(_delayed_autostart)
    start_timer()
    if cfg["refresh_on_launch"]:
        refresh_when_timer_ready()


def script_load(settings):
    read_settings(settings)
    obs.obs_frontend_add_event_callback(on_frontend_event)
    if cfg["auto_start"]:
        # Small delay so OBS finishes loading before we launch anything.
        obs.timer_add(_delayed_autostart, 1500)


def script_unload():
    obs.obs_frontend_remove_event_callback(on_frontend_event)
    obs.timer_remove(_delayed_video_play)
    obs.timer_remove(_poll_then_refresh)
    if cfg["stop_on_exit"]:
        stop_timer()
