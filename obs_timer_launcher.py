"""
OBS script: starts your timer.py automatically when OBS starts,
and stops it again when OBS closes.

Setup (one time):
  1. OBS > Tools > Scripts > "Python Settings" tab: point OBS at your
     Python install folder (needed for any Python script in OBS).
  2. Scripts tab > "+" > pick this file (obs_timer_launcher.py).
  3. Fill in your SSN Session ID in the script's settings on the right.
  Scripts stay loaded between OBS launches, so from now on the timer
  starts by itself every time you open OBS.
"""

import os
import shutil
import socket
import subprocess

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
# Button callbacks
# ------------------------------------------------------------------
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
            "OBS closes.")


def script_defaults(settings):
    obs.obs_data_set_default_string(settings, "timer_path", DEFAULT_TIMER_PATH)
    obs.obs_data_set_default_bool(settings, "auto_start", True)
    obs.obs_data_set_default_bool(settings, "show_console", True)
    obs.obs_data_set_default_bool(settings, "stop_on_exit", True)


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

    return props


def script_update(settings):
    read_settings(settings)


def _delayed_autostart():
    obs.timer_remove(_delayed_autostart)
    start_timer()


def script_load(settings):
    read_settings(settings)
    if cfg["auto_start"]:
        # Small delay so OBS finishes loading before we launch anything.
        obs.timer_add(_delayed_autostart, 1500)


def script_unload():
    if cfg["stop_on_exit"]:
        stop_timer()
