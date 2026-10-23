#!/usr/bin/env python3
"""Start Jarvis.

The one command a fresh clone needs. It checks the four packages the bridge
imports, installs them on request, starts the bridge and opens the setup page.

`restart-jarvis.ps1` is the other way in and does far more -- a virtualenv, the
voice daemon, the watchdog, log rolling -- but it expects a machine that has
already been set up. This is for the first five minutes.

    python start.py              # start, and open it
    python start.py --background # leave it running with no console window
    python start.py --stop       # stop one started that way
    python start.py --window     # in a bare window, once installed
    python start.py --shortcut   # put it on the desktop, console-free
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
import time
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


# Where a windowless start writes what it would otherwise have printed.
LOG = Path.home() / ".config" / "jarvis" / "start.log"


def tell(message: str, *, bad: bool = False, silent: bool = False) -> None:
    """Say something to whoever started this, however they started it.

    From a terminal that is a print. From the desktop shortcut there is no
    terminal at all, so it has to be a message box or it is not said.
    """
    print(message)
    if not silent or os.name != "nt":
        return
    try:
        import ctypes
        # MB_ICONERROR (0x10) or MB_ICONINFORMATION (0x40), always on top.
        ctypes.windll.user32.MessageBoxW(
            None, message, "Jarvis", 0x40000 | (0x10 if bad else 0x40))
    except Exception:  # noqa: BLE001 -- a failed dialog must not mask the error
        pass


def log_tail(lines: int = 12) -> str:
    try:
        text = LOG.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    rows = [r for r in text.splitlines() if r.strip()]
    return "\n".join(rows[-lines:])


def windowless_python() -> str:
    """pythonw.exe where there is one: the same interpreter, no console.

    Falls back to the console build, which still works -- the window is then
    hidden rather than absent, which is what the shortcut does anyway.
    """
    exe = Path(sys.executable)
    for name in ("pythonw.exe", "pythonw"):
        candidate = exe.with_name(name)
        if candidate.exists():
            return str(candidate)
    return sys.executable


def desktop() -> Path:
    """The desktop, asked of Windows rather than guessed.

    ~/Desktop is wrong on any machine with a redirected or OneDrive desktop,
    which is most of them now.
    """
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            # CSIDL_DESKTOPDIRECTORY = 0x10, SHGFP_TYPE_CURRENT = 0
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0:
                return Path(buf.value)
        except Exception:  # noqa: BLE001 -- fall through to the guess
            pass
    return Path.home() / "Desktop"


def make_shortcut(port: int, remove: bool = False) -> int:
    """Write a desktop shortcut that uses THIS interpreter.

    Which is the whole point: the py launcher picks whichever Python it
    thinks newest, and that is regularly not the one holding the packages.
    """
    if os.name != "nt":
        tell("Desktop shortcuts are a Windows thing. On Linux or macOS, run "
             "`python start.py --background`.")
        return 1
    link = desktop() / "Jarvis.lnk"
    if remove:
        try:
            link.unlink()
            tell("Removed " + str(link))
        except FileNotFoundError:
            tell("No shortcut at " + str(link) + " - nothing to remove.")
        return 0

    gaps = missing()
    if gaps:
        tell("Not making a shortcut yet: this Python is missing "
             + ", ".join(gaps) + ".\n\nRun this first:\n"
               "    python -m pip install -r requirements.txt", bad=True)
        return 1

    target = windowless_python()
    icon = HERE / "avatar" / "jarvis.ico"
    try:
        import win32com.client  # noqa: F401  -- rarely present; VBScript below
        raise ImportError
    except ImportError:
        # No pywin32 on a fresh machine, so the shell is asked directly.
        import subprocess as sp
        import tempfile
        vbs = tempfile.NamedTemporaryFile("w", suffix=".vbs", delete=False,
                                          encoding="utf-8")
        vbs.write(
            'Set s = CreateObject("WScript.Shell")\n'
            'Set l = s.CreateShortcut("%s")\n' % str(link).replace('"', '""')
            + 'l.TargetPath = "%s"\n' % str(target).replace('"', '""')
            + 'l.Arguments = "%s"\n' % (
                '""%s"" --port %d --background' % (BRIDGE.parent / "start.py", port))
            + 'l.WorkingDirectory = "%s"\n' % str(HERE).replace('"', '""')
            + ('l.IconLocation = "%s"\n' % str(icon).replace('"', '""')
               if icon.exists() else "")
            + 'l.Description = "Start Jarvis"\n'
            'l.Save\n')
        vbs.close()
        try:
            r = sp.run(["cscript", "//nologo", vbs.name],
                       capture_output=True, text=True)
            if r.returncode != 0:
                tell("Could not create the shortcut:\n"
                     + (r.stderr or r.stdout or "cscript failed"), bad=True)
                return 1
        finally:
            try:
                os.unlink(vbs.name)
            except OSError:
                pass

    tell("Shortcut created: " + str(link)
         + "\nIt uses this Python:\n  " + target
         + "\n\nDouble-click it to start Jarvis. No console window is left "
           "behind.\nTo stop it: QUIT inside the app, or "
           "`python start.py --stop`.")
    return 0


def health_of(port: int) -> dict | None:
    """What the bridge on this port says about itself, or None."""
    try:
        import json as _json
        import urllib.request
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/health" % port, timeout=2) as r:
            if r.status != 200:
                return None
            return _json.loads(r.read() or b"{}")
    except Exception:  # noqa: BLE001 -- anything at all means "not ours"
        return None


def running_on(port: int) -> bool:
    """Is a Jarvis bridge already answering here?

    Asks rather than assumes: something else may hold the port, and telling
    somebody Jarvis is running when it is not wastes their afternoon.
    """
    return health_of(port) is not None


def disk_fingerprint() -> str:
    """The same hash the bridge computes, over the file as it is now."""
    try:
        import hashlib
        return hashlib.sha256(BRIDGE.read_bytes()).hexdigest()[:16]
    except OSError:
        return "unknown"


def is_stale(port: int) -> bool:
    """Is the bridge on this port running code we have since changed?

    An older bridge reports no fingerprint at all; that is stale too, since
    it predates this check.
    """
    info = health_of(port)
    if info is None:
        return False
    running = info.get("code")
    if not running:
        return True
    return running != disk_fingerprint()


def ask_it_to_stop(port: int) -> bool:
    import json as _json
    import urllib.request
    req = urllib.request.Request(
        "http://127.0.0.1:%d/shutdown" % port,
        data=b"{}", method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return _json.loads(r.read() or b"{}").get("stopping", False)
    except Exception:  # noqa: BLE001
        return False


def install(packages: list[str]) -> bool:
    print("Installing: " + ", ".join(packages))
    cmd = [sys.executable, "-m", "pip", "install", *packages]
    return subprocess.call(cmd) == 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Start Jarvis.")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--window", action="store_true",
                    help="open in a bare window instead of a tab. A window has "
                         "no toolbar, so the browser cannot offer to install "
                         "Jarvis from one -- install it first, then launch it "
                         "from the Start menu")
    ap.add_argument("--check", action="store_true",
                    help="report what is missing and exit")
    ap.add_argument("--background", action="store_true",
                    help="start it and return, leaving no console window")
    ap.add_argument("--stop", action="store_true",
                    help="stop a bridge that is already running")
    ap.add_argument("--shortcut", action="store_true",
                    help="put a Jarvis shortcut on the desktop")
    ap.add_argument("--remove-shortcut", action="store_true",
                    help="take that shortcut away again")
    ap.add_argument("--restart", action="store_true",
                    help="stop a running Jarvis first, then start this one")
    args = ap.parse_args()

    if args.shortcut or args.remove_shortcut:
        return make_shortcut(args.port, remove=args.remove_shortcut)

    # A pull changes the files; it cannot change a process already running.
    # Restarting here is what the person asked for by running this at all.
    if (args.restart or is_stale(args.port)) and running_on(args.port):
        if is_stale(args.port) and not args.restart:
            tell("A different version of Jarvis is already running. "
                 "Restarting it so your update takes effect.")
        ask_it_to_stop(args.port)
        for _ in range(20):
            if not running_on(args.port):
                break
            time.sleep(0.5)
        else:
            tell("The running Jarvis would not stop. End pythonw.exe in Task "
                 "Manager, then try again.", bad=True)
            return 1

    if args.stop:
        if not running_on(args.port):
            print("Nothing is running on port %d." % args.port)
            return 0
        if ask_it_to_stop(args.port):
            print("Jarvis is stopping.")
            return 0
        print("It would not stop. Close its window, or end python.exe in "
              "Task Manager.")
        return 1

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

    if gaps and args.background:
        tell("Jarvis is missing: " + ", ".join(gaps)
             + "\n\nOpen a terminal in this folder and run:\n"
               "    python -m pip install -r requirements.txt",
             bad=True,
             silent=not (sys.stdout and sys.stdout.isatty()))
        return 1
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
        # Starting it twice is the likeliest reason to be here, and it is not
        # an error -- the thing he wanted is already running.
        if running_on(args.port):
            url = "http://127.0.0.1:%d/ui" % args.port
            # Stale copies were restarted above, so this one is current.
            print("Jarvis is already running, and up to date. Opening it.")
            if not args.no_browser:
                if not (args.window and open_as_app(url)):
                    webbrowser.open(url)
            return 0
        tell("Port %d is in use by something that is not Jarvis.\n\n"
             "Close whatever is using it, or start Jarvis on another port."
             % args.port, bad=True,
             silent=args.background and not (sys.stdout and sys.stdout.isatty()))
        return 1

    env = dict(os.environ, JARVIS_BRIDGE_PORT=str(args.port))
    # pythonw has no stdout to print to, which is exactly how a failed
    # double-click became "nothing happened".
    quiet = args.background and not (sys.stdout and sys.stdout.isatty())
    url = "http://127.0.0.1:%d/setup" % args.port
    print("Jarvis is starting. Setup page: " + url)
    # How to stop it depends entirely on how it was started, and saying
    # "Ctrl-C here" to somebody running it detached is simply wrong.
    if not args.background:
        print("Nothing is configured yet -- that page is where you choose "
              "what to connect. Ctrl-C here stops it.")

    if args.background:
        # Detached and windowless: it outlives this process, and there is no
        # console to leave sitting on the taskbar. /shutdown is how it stops,
        # which `--stop` and the HUD's QUIT both use.
        flags = 0
        if os.name == "nt":
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        # Its output goes to a file rather than DEVNULL: with no console,
        # discarding stderr is discarding the only explanation there will be.
        try:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            sink = open(LOG, "w", encoding="utf-8", errors="replace")
        except OSError:
            sink = subprocess.DEVNULL
        try:
            subprocess.Popen([windowless_python(), str(BRIDGE)], env=env,
                             cwd=str(HERE), creationflags=flags,
                             stdin=subprocess.DEVNULL,
                             stdout=sink, stderr=subprocess.STDOUT,
                             close_fds=True)
        except OSError as exc:
            tell("Jarvis could not start:\n\n" + str(exc), bad=True, silent=quiet)
            return 1
        finally:
            if sink is not subprocess.DEVNULL:
                sink.close()
        for _ in range(40):                 # up to 20s for a cold first start
            if running_on(args.port):
                break
            time.sleep(0.5)
        else:
            why = log_tail()
            tell("Jarvis did not start.\n\n"
                 + (why or "Nothing was written to the log.")
                 + "\n\nThe full log is at:\n" + str(LOG)
                 + "\n\nRunning `python start.py` in a terminal shows this "
                   "as it happens.", bad=True, silent=quiet)
            return 1
        tell("Jarvis is running in the background: " + url
             + "\nStop it with: python start.py --stop   (or QUIT in the app)")
        if not args.no_browser:
            if not (args.window and open_as_app(url)):
                webbrowser.open(url)
        return 0

    proc = subprocess.Popen([sys.executable, str(BRIDGE)], env=env, cwd=str(HERE))
    if not args.no_browser:
        # The bridge binds in well under a second, but a browser that arrives
        # first shows a connection error and people read that as broken.
        try:
            proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            if not (args.window and open_as_app(url)):
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
