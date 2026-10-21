#!/usr/bin/env python3
"""Start Jarvis.

The one command a fresh clone needs. It checks the four packages the bridge
imports, installs them on request, starts the bridge and opens the setup page.

`restart-jarvis.ps1` is the other way in and does far more -- a virtualenv, the
voice daemon, the watchdog, log rolling -- but it expects a machine that has
already been set up. This is for the first five minutes.

    python start.py              # start, in its own window
    python start.py --tab        # in an ordinary browser tab instead
    python start.py --port 8200   # somewhere else
    python start.py --no-browser  # headless, or over SSH
    python start.py --check       # say what is missing and stop
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE = HERE / "claude-bridge.py"

# Import name on the left, pip name on the right: they differ often enough to
# be worth writing both down.
NEEDED = [("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
          ("psutil", "psutil"), ("requests", "requests")]

MIN_PYTHON = (3, 10)

# Chromium's --app= gives a plain window: no address bar, no tabs, its own
# taskbar button and its own icon. It is the difference between "a localhost
# page I have to keep finding" and something that behaves like an application.
# Edge ships with Windows, so the first entry nearly always exists.
APP_BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def app_browser() -> str | None:
    """A Chromium that can open a bare window, or None."""
    for path in APP_BROWSERS:
        if Path(path).exists():
            return path
    return shutil.which("chrome") or shutil.which("chromium") \
        or shutil.which("msedge")


def open_as_app(url: str) -> bool:
    """Open `url` as its own window. False if no suitable browser was found.

    Each install gets its own profile directory, so the window does not
    inherit the user's tabs, extensions or session, and closing it does not
    close their browsing.
    """
    exe = app_browser()
    if not exe:
        return False
    profile = Path.home() / ".config" / "jarvis" / "window"
    try:
        profile.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([exe, "--app=" + url,
                          "--user-data-dir=" + str(profile),
                          "--no-first-run", "--no-default-browser-check"])
        return True
    except OSError:
        return False


def missing() -> list[str]:
    return [pip for mod, pip in NEEDED if importlib.util.find_spec(mod) is None]


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def install(packages: list[str]) -> bool:
    print("Installing: " + ", ".join(packages))
    cmd = [sys.executable, "-m", "pip", "install", *packages]
    return subprocess.call(cmd) == 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Start Jarvis.")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--tab", action="store_true",
                    help="open in an ordinary browser tab, not its own window")
    ap.add_argument("--check", action="store_true",
                    help="report what is missing and exit")
    args = ap.parse_args()

    if sys.version_info < MIN_PYTHON:
        print("Python %d.%d or newer is needed; this is %d.%d."
              % (*MIN_PYTHON, *sys.version_info[:2]))
        return 1

    if not BRIDGE.exists():
        print("claude-bridge.py is not next to this script. Run it from "
              "inside the clone.")
        return 1

    gaps = missing()
    if args.check:
        print("Python %d.%d - fine" % sys.version_info[:2])
        print("packages: " + ("all four present" if not gaps
                              else "missing " + ", ".join(gaps)))
        print("port %d: %s" % (args.port,
                               "free" if port_free(args.port) else "in use"))
        return 0 if not gaps else 1

    if gaps:
        print("Jarvis needs: " + ", ".join(gaps))
        # Non-interactive shells (CI, a service wrapper) get a clear failure
        # rather than a prompt nobody can answer.
        if not sys.stdin or not sys.stdin.isatty():
            print("Run: python -m pip install -r requirements.txt")
            return 1
        if input("Install them now? [Y/n] ").strip().lower() in ("", "y", "yes"):
            if not install(gaps):
                print("pip failed. Try: python -m pip install -r "
                      "requirements.txt")
                return 1
        else:
            return 1

    if not port_free(args.port):
        print("Port %d is already in use. Either Jarvis is running already -- "
              "open http://127.0.0.1:%d/setup -- or pick another port with "
              "--port." % (args.port, args.port))
        return 1

    env = dict(os.environ, JARVIS_BRIDGE_PORT=str(args.port))
    url = "http://127.0.0.1:%d/setup" % args.port
    print("Jarvis is starting. Setup page: " + url)
    print("Nothing is configured yet -- that page is where you choose what to "
          "connect. Ctrl-C here stops it.")

    proc = subprocess.Popen([sys.executable, str(BRIDGE)], env=env, cwd=str(HERE))
    if not args.no_browser:
        # The bridge binds in well under a second, but a browser that arrives
        # first shows a connection error and people read that as broken.
        try:
            proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            if args.tab or not open_as_app(url):
                webbrowser.open(url)
        else:
            print("The bridge exited immediately. Its reason is above.")
            return proc.returncode or 1
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
