"""OpenAI-compatible bridge that routes chat completions through Claude Code.

Runs a local server on port 8100 translating OpenAI-style /v1/chat/completions
requests into headless `claude -p` calls. Claude Code is authenticated with
Oliver's claude.ai subscription (OAuth), so requests served through this
bridge consume the subscription, not API credits.

Models exposed:
- claude-subscription        — default brain (Claude Code's default model)
- claude-subscription-fast   — haiku, for intent judging / tool routing

Canva: when a prompt looks like a design request, the call runs with the
Canva MCP server attached (tokens already cached in ~/.mcp-auth) and more
agent turns, so Jarvis can actually create/search designs by voice.

Also serves the Jarvis UI at /ui and the avatar assets at /avatar.
"""

from __future__ import annotations

import atexit
import datetime as dt
import itertools
import json
import logging
import os
import queue
import re
import subprocess
import threading
import urllib.request
import time
import uuid
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

PORT = 8100
MODEL_ID = "claude-subscription"
MODEL_FAST_ID = "claude-subscription-fast"
# Forces agent mode — tools and multiple turns — regardless of what the text
# looks like. Callers that KNOW they need tools ask for this instead of hoping
# a keyword hint classifies them correctly. Screen vision needs it: reading an
# image file takes a tool call and a second turn.
MODEL_AGENT_ID = "claude-subscription-agent"
CLI_TIMEOUT_S = 420
# Per-mode ceilings: a hung classification shouldn't hold a worker for seven
# minutes, but a Canva job legitimately can.
MODE_TIMEOUT_S = {"fast": 60, "chat": 150, "agent": 300, "canva": 420, "bg": 180}
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
ASSET_DIR = Path(__file__).parent / "avatar"
WORKSPACE = Path.home() / ".openjarvis" / "bridge-workspace"
WORKSPACE.mkdir(parents=True, exist_ok=True)

# The personality, as the daemon keeps it. The bridge reads the same file so
# that the part of the prompt which never changes between turns can ride in the
# system prompt, where the model's cache can keep hold of it, instead of being
# re-sent inside every user message.
IDENTITY_PATH = Path.home() / ".config" / "jarvis" / "identity.md"
STABLE_DIR = WORKSPACE
# The lanes a person actually hears. `fast` and `bg` answer the daemon's own
# classification and summarisation calls, whose output has to stay parseable —
# a butler's voice in a tool-router prompt is a routing bug waiting to happen.
PERSONA_MODES = ("chat", "agent", "canva")
_STABLE: dict = {"text": None, "mtime": None}


def stable_identity() -> str:
    """The persona prose, or "" when there is no usable identity file.

    Cached on (mtime, size) so a spawn costs a stat. The HTML comment header is
    stripped exactly as the daemon strips it, because the instructions to the
    human are not instructions to the model — and because the text has to match
    the daemon's byte for byte for the copy in the user message to be removable.
    """
    try:
        stat = IDENTITY_PATH.stat()
        key = (stat.st_mtime, stat.st_size)
    except OSError:
        return ""
    if _STABLE["text"] is not None and _STABLE["mtime"] == key:
        return _STABLE["text"]
    try:
        text = IDENTITY_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # noqa: BLE001 — a missing persona is not a failure
        log.warning("identity unreadable (%s); it will ride in the message", exc)
        return ""
    while text.lstrip().startswith("<!--") and "-->" in text:
        text = text.split("-->", 1)[1]
    text = text.strip()
    _STABLE["text"], _STABLE["mtime"] = text, key
    return text


def _stable_prompt_file(mode: str) -> Path | None:
    """Write this mode's appended system prompt, returning its path.

    None means there is nothing stable to hoist, and the persona rides in the
    user message as it always has. That fallback is the whole safety net here:
    every failure in this path costs cache efficiency, never the personality.
    """
    if mode not in PERSONA_MODES:
        return None
    text = stable_identity()
    if not text:
        return None
    path = STABLE_DIR / f"system-{mode}.txt"
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        log.warning("could not write the %s system prompt (%s)", mode, exc)
        return None
    return path


# MCP config handed to Claude Code only for design-flavoured requests —
# attaching it on every call would slow plain chat down.
# Declares no MCP servers at all. --strict-mcp-config needs a config to be
# strict about, so "load none" has to be spelled out in a file.
NO_MCP_CONFIG = WORKSPACE / "mcp-none.json"
NO_MCP_CONFIG.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

MCP_CONFIG = WORKSPACE / "mcp-canva.json"
MCP_CONFIG.write_text(json.dumps({
    "mcpServers": {
        "canva": {"command": "mcp-remote.cmd", "args": ["https://mcp.canva.com/mcp"]}
    }
}), encoding="utf-8")
CANVA_HINT = re.compile(
    r"\bcanva\b|\bdesign\b|\bposter\b|\blogo\b|\bflyer\b|\bslide|\bmock-?up\b"
    r"|\bthumbnail\b|\bbanner\b|\binfographic\b|\bpresentation\b", re.I,
)

# Rotating file log: hidden launcher windows swallow stdout, so a failure here
# used to leave no trace at all.
log = logging.getLogger("bridge")
log.setLevel(logging.INFO)
class _SafeRotatingHandler(RotatingFileHandler):
    """Rotation that tolerates Windows file locks.

    During a restart the old bridge may still hold bridge.log; Windows then
    refuses the rename and stock RotatingFileHandler raises on every record.
    Skipping the rotation is strictly better than losing the logging.
    """

    def rotate(self, source, dest):
        try:
            super().rotate(source, dest)
        except OSError:
            pass

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:
            self.maxBytes = 0  # stop trying until the next restart


_handler = _SafeRotatingHandler(
    LOG_DIR / "bridge.log", maxBytes=2_000_000, backupCount=3,
    encoding="utf-8", delay=True,
)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)

try:
    import usage_ledger
except Exception:  # noqa: BLE001 — metrics must never stop the bridge starting
    usage_ledger = None


def _ledger(mode: str, model: str, entry: dict, ok: bool = True, source: str = "conversation") -> None:
    """One ledger row per completed call. Swallows everything."""
    if usage_ledger is None:
        return
    try:
        usage_ledger.record(
            model=model, mode=mode, source=source, ok=ok,
            seconds=max(0.0, (entry.get("done") or time.time()) - (entry.get("t") or time.time())),
        )
    except Exception:  # noqa: BLE001
        pass


# Requests that should run as a live agent: web lookups and machine actions.
# Deliberately without open/launch/start/play. Those verbs act ON HIS MACHINE
# — a file, an app, a playlist — which agent mode can never touch: it swaps
# the daemon's tools for a shell with no screen and no speakers. Every one of
# them got lost to agent mode in turn, each one patched with a narrower hint,
# until "open screwfix hotdogzone 2" slipped between the hints and agent mode
# answered by launching Canva. The verbs that mean "go out to the world" stay;
# the verbs that mean "do something here" never route away from the machine's
# owner.
AGENT_HINT = re.compile(
    r"\b(search|look up|google|browse|latest|news|price)\b",
    re.I,
)

# Work that belongs to the daemon's own `browser` tool: driving a real page.
# Agent mode substitutes Claude Code's fetching and shell, which reads a page
# but cannot act as Oliver in his logged-in session — and it discards the
# daemon's tools, so the reply comes back saying no browser tool exists while
# one is registered. The daemon's tool is also the safer route: allowlisted
# domains, a confirmation gate on one-way doors, and an audit line per action.
WEB_TASK_HINT = re.compile(
    r"(https?://|www\.|\b\w+\.(com|co\.uk|org|net|io|dev)\b"
    r"|\bbrowser\b|\bweb ?page\b|\bwebsite\b|\bthis page\b|\bthe page\b"
    r"|\blog ?in|\bsign ?in|\bclick\b|\bfill in\b|\btab\b)",
    re.I,
)

# Work that belongs to the daemon's `showPanel`: putting something in front of
# the user rather than only saying it. Agent mode can fetch the content but
# cannot reach the screen, so it answers in prose and — asked for a panel —
# reports having made one that does not exist.
SCREEN_TASK_HINT = re.compile(
    r"\bon (the )?screen\b|\bpanel\b|\bshow me\b|\bdisplay\b|\bput .* up\b"
    r"|\bpin (it|that|this)\b|\bwhere i can see\b",
    re.I,
)

# "Look at my machine and tell me what is there." Only the daemon can see the
# screen, so these must never reach agent mode — which cannot see it, and will
# either describe what it would do or ask for permission nobody can give.
SIGHT_TASK_HINT = re.compile(
    r"\bmy calendar\b|\bmy diary\b|\bmy schedule\b|\bmy emails?\b|\bmy inbox\b"
    r"|\bwhat(?:'s| is| have i got)\b[^.?]*\b(today|tomorrow|this week|on)\b"
    r"|\bwhat does it say\b|\bread (?:it|that|the screen|this)\b"
    r"|\bon (?:my|the) screen\b|\bwhat am i looking at\b",
    re.I,
)

# Work that belongs to the daemon's `inputControl` / `windowControl`: driving
# the keyboard, the mouse and windows as though Oliver were at the keys.
MACHINE_TASK_HINT = re.compile(
    r"\btype\b|\btyping\b|\bwrite (it |this |that )?(in|into)\b|\bpaste\b"
    r"|\bpress\b|\bkeyboard\b|\bkeystrokes?\b|\bhotkey\b"
    r"|\bwindow\b|\bfocus\b|\bminimi[sz]e\b|\bmaximi[sz]e\b|\bswitch to\b",
    re.I,
)


# Putting music on. Agent mode has a shell and no speakers: asked to play a
# playlist it answered "playing Polonia from Apple Music now" without the app
# ever starting, because the tool that could have played it was attached to
# the request and then discarded along with every other daemon tool.
MUSIC_TASK_HINT = re.compile(
    r"\bplay\b[^.?]*\b(playlist|album|song|track|music|artist)\b"
    r"|\bplay\b[^.?]*\b(apple music|itunes|spotify)\b"
    r"|\bput on\b[^.?]*\b(music|playlist|album|song)\b"
    r"|\b(pause|resume|skip|next|previous) (the )?(song|track|music)\b",
    re.I,
)


# Pictures: finding one by what is in it, or answering about one he sent.
# Agent mode has a shell, so asked to find a photograph it walks the disk by
# filename — which is exactly the search that fails, since the picture he is
# looking for was renamed to something meaningless. The daemon's findImage
# looks at the pictures themselves and shows him the candidates.
IMAGE_TASK_HINT = re.compile(
    r"\b(image|images|photo|photos|photograph|picture|pictures|pic|pics|"
    r"screenshot|screenshots|wallpaper)\b",
    re.I,
)


# Standing over a chat that is generating work: judging each result and
# typing the next instruction into that window. Agent mode can do neither.
SUPERVISE_TASK_HINT = re.compile(
    r"supervise|watch (my|the) (chat|chatgpt|conversation)"
    r"|check every (image|scene|shot|frame)"
    r"|keep (it|the chat) going|next scene",
    re.I,
)


def daemon_owns(last_user: str, tools: list) -> dict:
    """Which kinds of work the daemon must keep, whatever else the text looks like.

    The daemon offering a tool means its own router judged this that tool's
    kind of work. Agent and canva mode throw that list away and substitute
    Claude Code's shell, which has no screen, no keyboard, no speakers and no
    way to display anything — so a request shaped like one of these jobs comes
    back as a claim that it was done. Each flag here is a job the daemon alone
    can carry out, and each one was added after a request was answered with a
    sentence instead of an action.
    """
    text = last_user or ""
    return {
        "web": _offers(tools, "browser") and bool(WEB_TASK_HINT.search(text)),
        "machine": (
            (_offers(tools, "inputControl") or _offers(tools, "windowControl"))
            and bool(MACHINE_TASK_HINT.search(text))
        ),
        "screen": _offers(tools, "showPanel") and bool(SCREEN_TASK_HINT.search(text)),
        "sight": (
            (_offers(tools, "viewScreen") or _offers(tools, "driveApp"))
            and bool(SIGHT_TASK_HINT.search(text))
        ),
        "music": (
            (_offers(tools, "playMusic") or _offers(tools, "driveApp"))
            and bool(MUSIC_TASK_HINT.search(text))
        ),
        "images": (
            (_offers(tools, "findImage") or _offers(tools, "viewImage"))
            and bool(IMAGE_TASK_HINT.search(text))
        ),
        # Standing over a chat and typing into it is the daemon's alone:
        # agent mode has no keyboard and no eyes on that window.
        "supervise": (
            _offers(tools, "superviseChat")
            and bool(SUPERVISE_TASK_HINT.search(text))
        ),
        "files": (
            (_offers(tools, "openFile") or _offers(tools, "findFiles"))
            and bool(FILE_TASK_HINT.search(text))
        ),
    }


# Opening something that lives on his disk. Agent mode can run a shell, but
# the daemon's openFile knows the index of every file on the machine and the
# rules for ranking them; agent mode rediscovered none of that and ran out of
# turns instead. Sixth verb lost this way: browse, type, show, read, play,
# and now open.
FILE_TASK_HINT = re.compile(
    r"\bopen\b[^.?]*\b(file|video|photo|picture|image|document|invoice|pdf|"
    r"folder|clip|recording|spreadsheet|presentation|track)\b"
    r"|\bopen (?:that|my|the) [^.?]* from (?:19|20)\d\d\b"
    r"|\bfind (?:that|my|the)\b[^.?]*\b(file|video|photo|document)\b",
    re.I,
)


def _offers(tools: list, name: str) -> bool:
    """Whether the daemon attached a given tool to this request."""
    return any((t.get("function") or {}).get("name") == name for t in tools)

# The daemon's internal pipeline calls (tool router, planner, memory
# enrichment, summariser) contain words like "search"/"open" in their
# instruction text and would otherwise be hijacked into agent/canva mode —
# a 10-turn web agent for a 50-token classification. Spoken user queries are
# short; instruction-shaped prompts are long or start with a known marker.
INTERNAL_HINT = re.compile(
    r"^(Extract search parameters|AVAILABLE TOOLS|Extract and classify|Previous summary"
    r"|CURRENT facts on the node|Summarise|Summarize|You are a memory)", re.I
)

# Housekeeping the backend runs AFTER a reply is delivered: diary summaries and
# knowledge-graph extraction. Nobody is waiting on these, but they were sharing
# the interactive worker pool, so a question could queue behind 20s of memory
# maintenance and appear to hang. They get their own workers and the fast model.
BACKGROUND_HINT = re.compile(
    r"^(Previous summary|Extract and classify|CURRENT facts on the node"
    r"|Summarise|Summarize|You are a memory)", re.I
)

# The summariser's user text is just a transcript ("User: … Assistant: …"),
# indistinguishable from a real multi-turn conversation. Its SYSTEM prompt is
# not, so that is what gets classified.
#
# These must be phrases ONLY the maintenance prompts use. Loose words like
# "diary" also appear in the normal reply prompt, and matching one of those
# sends real questions to the cheap background pool.
BACKGROUND_SYSTEM_HINT = re.compile(
    r"you are a conversation summariser"
    r"|you are a conversation summarizer"
    r"|extract and classify novel knowledge"
    r"|knowledge graph (extractor|maintainer)"
    # The rest of the housekeeping: knowledge-graph extraction and curation,
    # the backend's own fact extractor, and the diary's tidying passes. Each
    # of these fires around a reply rather than in it, so each one used to
    # spend the big model on work the user never hears.
    r"|you extract novel knowledge"
    r"|you extract durable, long-term facts"
    r"|you are a memory organiser"
    r"|you are a knowledge organiser"
    r"|you curate a knowledge store"
    r"|you normalise topic-tag taxonomies"
    r"|you are cleaning historical entries", re.I,
)


def _is_internal(last_user: str) -> bool:
    return bool(INTERNAL_HINT.search(last_user.strip())) or len(last_user) > 300


def _is_background(text: str) -> bool:
    return bool(BACKGROUND_HINT.search(text.strip()))

# Simple/smalltalk requests get routed to the fast model automatically so
# "introduce yourself" doesn't pay big-model latency.
SIMPLE_HINT = re.compile(
    r"^(hi|hey|hello|yo|thanks|thank you|ok|okay)\b"
    r"|how are you|who are you|introduce (yourself|your self)|what('?s| is) your name"
    r"|what time|good (morning|evening|night)|can you hear me",
    re.I,
)


def _last_user_text(messages: list) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return _flatten(m.get("content"))
    return ""

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app):
    # on_event("startup") printed a DeprecationWarning to stderr at import,
    # which is fatal under a detached pythonw (no valid stderr handle) — the
    # bridge died before it could log anything. Lifespan is the quiet form.
    log.info("bridge starting on port %s", PORT)
    _warm_all()
    threading.Thread(target=_pc_scan_loop, daemon=True).start()
    yield


app = FastAPI(title="Claude subscription bridge", lifespan=_lifespan)

# Recent request activity for the UI's activity ticker. ACTIVITY is capped, so
# the lifetime total is counted separately.
ACTIVITY: deque = deque(maxlen=30)
REQUEST_COUNT: dict = {"n": 0}


@app.get("/activity")
def activity() -> dict:
    return {"activity": list(ACTIVITY)}


STYLE_FILE = Path.home() / ".local" / "share" / "jarvis" / "speaking-style.json"
_style_cache: dict = {"t": 0.0, "name": "butler"}

# Personality only. These never change what the assistant may do, and they are
# applied solely to replies the user actually hears — never to the daemon's
# internal classification calls, whose output must stay machine-readable.
STYLE_PROMPTS = {
    "caveman": (
        "Speak like a terse caveman. Drop articles (a/an/the), filler "
        "(just/really/basically), and pleasantries. Sentence fragments are "
        "fine. Short words over long ones.\n"
        "Keep every technical fact exact: numbers, file paths, command names, "
        "error strings and code stay verbatim and complete — compress the "
        "grammar, never the substance. Do not invent abbreviations.\n"
        "Drop the caveman voice entirely for safety warnings and for "
        "confirming anything irreversible: those must be unambiguous."
    ),
    "plain": (
        "Drop the butler persona for now: no 'sir', no flourishes. Neutral, "
        "direct answers."
    ),
}


def _speaking_style() -> str:
    """Current style, re-read at most once a second so a change lands on the
    next reply without restarting anything."""
    if time.time() - _style_cache["t"] < 1.0:
        return _style_cache["name"]
    name = "butler"
    try:
        name = str(json.loads(STYLE_FILE.read_text(encoding="utf-8")).get("style", "butler")).lower()
    except (OSError, ValueError):
        name = "butler"
    _style_cache.update(t=time.time(), name=name)
    return name


BOOT_TS = time.time()
_gpu_cache: dict = {"t": 0.0, "data": None}
_backend_cache: dict = {"t": 0.0, "up": False}


def _gpu_stats() -> dict | None:
    """Live GPU readout via nvidia-smi, cached briefly (the call costs ~80ms)."""
    if time.time() - _gpu_cache["t"] < 2.0:
        return _gpu_cache["data"]
    data = None
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,power.draw,memory.used,"
             "memory.total,temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            util, watts, used, total, temp = [
                p.strip() for p in proc.stdout.strip().splitlines()[0].split(",")
            ]
            data = {
                "util": float(util), "watts": float(watts),
                "vram_used": float(used), "vram_total": float(total),
                "temp": float(temp),
            }
    except (OSError, ValueError, subprocess.SubprocessError):
        data = None
    _gpu_cache.update(t=time.time(), data=data)
    return data


def _backend_up() -> bool:
    """Health-check the OpenJarvis backend server-side.

    The browser can't do this itself: the backend sends no CORS headers, so a
    fetch from the UI always fails and the panel would read DOWN forever.
    """
    if time.time() - _backend_cache["t"] < 4.0:
        return _backend_cache["up"]
    up = False
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2) as r:
            up = r.status == 200
    except Exception:  # noqa: BLE001 — any failure means "not reachable"
        up = False
    _backend_cache.update(t=time.time(), up=up)
    return up


@app.get("/costs")
async def costs():
    """What the assistant has spent this month, counted in calls."""
    if usage_ledger is None:
        return {"ok": False, "error": "ledger unavailable"}
    return usage_ledger.summary()


@app.get("/stats")
def stats() -> dict:
    recent = [a for a in ACTIVITY if a.get("done")][-10:]
    avg = (sum(a["done"] - a["t"] for a in recent) / len(recent)) if recent else None
    busy = sum(
        1 for pool in WORKERS.values() for w in pool.workers if w.lock.locked()
    )
    lat = sorted(LATENCIES)
    def _pct(p: float):
        return lat[min(len(lat) - 1, int(len(lat) * p))] if lat else None
    return {
        "gpu": _gpu_stats(),
        "requests": REQUEST_COUNT["n"],
        "avg_latency_s": avg,
        "p50_latency_s": _pct(0.5),
        "p95_latency_s": _pct(0.95),
        "by_mode": dict(MODE_COUNT),
        "errors": len(ERRORS),
        "last_error": ERRORS[-1] if ERRORS else None,
        "inflight": sum(1 for a in ACTIVITY if a.get("done") is None),
        "workers_busy": busy,
        "workers_total": sum(len(p.workers) for p in WORKERS.values()),
        "workers_live": sum(
            1 for pool in WORKERS.values() for w in pool.workers
            if w.proc is not None and w.proc.poll() is None
        ),
        "backend_up": _backend_up(),
        "uptime_s": time.time() - BOOT_TS,
    }


USAGE_MONITOR = Path(r"C:\Users\Oliver\Projects Claude\Usage manager\Claude-Usage-Monitor")
# The last figures that were actually good, kept separately from the last
# attempt. A rate-limited probe must not erase a number that was true five
# minutes ago: "26%, a few minutes old" is useful, "unavailable" is not.
_usage_cache: dict = {"t": 0.0, "data": None, "good": None, "good_t": 0.0, "backoff": 0.0}

# The last reading that was true, kept on disk. Anthropic rate-limits this
# lookup, so a refusal is routine; holding the last good figures only in
# memory means every restart shows a dead card until a refusal window
# happens to pass, which is what "the usage monitor is dead" looks like.
_USAGE_FILE = Path.home() / ".local" / "share" / "jarvis" / "usage-last-good.json"


def _load_last_good() -> None:
    try:
        body = json.loads(_USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(body, dict) and body.get("ok") and body.get("fetched_at"):
        _usage_cache.update(good=body, good_t=float(body["fetched_at"]))


def _save_last_good(reading: dict) -> None:
    try:
        _USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _USAGE_FILE.write_text(json.dumps(reading), encoding="utf-8")
    except OSError:
        pass

# Anthropic rate-limits this endpoint, and the figures move slowly. Five
# minutes is far more often than the numbers meaningfully change, and cuts
# upstream calls to a twelfth of what a per-minute poll produced — which is
# what earned the 429s in the first place.
USAGE_TTL_S = 300.0
# How long a good figure stays worth showing before it is too old to trust.
USAGE_STALE_LIMIT_S = 3600.0

# How long a refusal is answered with silence. Long enough to stop
# hammering a rate limit, short enough that the card comes back on its
# own while he is still looking at it.
MAX_USAGE_BACKOFF_S = 240.0

_USAGE_PROBE = r'''
import json, sys
from dataclasses import asdict, is_dataclass
sys.path.insert(0, r"{monitor}")
try:
    from src.api.client import AnthropicClient
    from src.config import Config
    snap = AnthropicClient(Config()).fetch_snapshot()
    d = asdict(snap) if is_dataclass(snap) else dict(snap.__dict__)
    keep = ("session_utilization", "week_utilization", "opus_week_utilization",
            "session_reset", "week_reset", "plan_name", "is_peak", "error")
    out = {{k: (v if isinstance(v, (int, float, bool)) or v is None else str(v))
            for k, v in d.items() if k in keep}}
    print(json.dumps(out))
except Exception as exc:
    print(json.dumps({{"error": str(exc)}}))
'''


_usage_refreshing = threading.Lock()


@app.get("/usage")
def usage() -> dict:
    """Claude subscription usage — the rolling 5-hour and 7-day limits.

    Never blocks the caller on the remote lookup. The refresh runs on one
    background thread and requests are answered from cache, because a slow
    probe holding a request thread will exhaust the server's pool and wedge
    every other endpoint — health checks included.
    """
    now = time.time()
    cached = _usage_cache["data"]
    fresh = now - _usage_cache["t"] < USAGE_TTL_S
    if cached and cached.get("ok") and fresh:
        return cached

    # A failed probe backs off, so a rate limit is not answered by hammering
    # the thing that imposed it.
    if now >= _usage_cache["backoff"] and _usage_refreshing.acquire(blocking=False):
        threading.Thread(target=_refresh_usage, daemon=True).start()

    # Prefer the last figures that were actually good over the last attempt.
    # A 429 says nothing about how much allowance is left, so answering with
    # "unavailable" throws away a true number for no reason.
    good = _usage_cache["good"]
    if good and now - _usage_cache["good_t"] < USAGE_STALE_LIMIT_S:
        stamped = good.get("fetched_at") or _usage_cache["good_t"]
        age = int(now - float(stamped))
        return {**good, "stale": True, "age_seconds": age}
    if cached:
        return {**cached, "stale": True}
    return {"ok": False, "error": "usage not fetched yet", "pending": True}


def _refresh_usage() -> None:
    result: dict
    # The monitor's own virtualenv is the only interpreter guaranteed to have
    # its dependencies (httpx). The bridge itself runs inside a different venv,
    # so a bare "python" here resolves to one without them.
    interpreter = USAGE_MONITOR / ".venv" / "Scripts" / "python.exe"
    if not interpreter.exists():
        interpreter = Path("python")
    try:
        proc = subprocess.run(
            [str(interpreter), "-c", _USAGE_PROBE.format(monitor=str(USAGE_MONITOR))],
            # 45s, not 20: measured by hand the probe takes 10.4s on an
            # idle machine -- a cold interpreter plus an HTTPS round-trip --
            # and his machine is rarely idle. Twenty seconds turned a slow
            # success into a failure five hundred times over.
            capture_output=True, text=True, timeout=45,
            cwd=str(USAGE_MONITOR), encoding="utf-8", errors="replace",
        )
        payload = json.loads((proc.stdout or "").strip().splitlines()[-1])
        if payload.get("error"):
            result = {"ok": False, "error": str(payload["error"])[:200]}
        elif not payload.get("session_reset") or payload.get("plan_name") in (None, "", "—"):
            # The monitor returns zeros and an em-dash plan when it cannot read
            # the OAuth token OR when Anthropic rate-limits the lookup — the
            # two are indistinguishable from here. Reporting that as "0% used"
            # would tell the user he has a full allowance when he may have none.
            # Anthropic rate-limits this lookup, so a refusal is routine and
            # temporary. The last good reading, labelled with its age, is far
            # more use than "unavailable" -- he wants to know roughly where he
            # stands, and a number an hour old still tells him that.
            previous = _usage_cache.get("data") or {}
            if previous.get("ok") and previous.get("fetched_at"):
                result = dict(previous)
                result["stale"] = True
                # Serve it, but do not let it be mistaken for a fresh reading.
                # Without this the refusal took the success branch below --
                # it carries the previous payload's ok=True -- which re-stamped
                # good_t to now, restarted the age counter, wrote the stale
                # figures to disk as last-good, and cleared the backoff. The
                # numbers then sat still while claiming to be seconds old, and
                # the probe kept hammering the endpoint that was refusing it.
                result["recycled"] = True
                result["stale_seconds"] = int(time.time() - float(previous["fetched_at"]))
            else:
                result = {"ok": False,
                          "error": "usage lookup refused (rate limited or signed out)"}
        else:
            payload["ok"] = True
            payload["fetched_at"] = time.time()
            result = payload
    except Exception as exc:  # noqa: BLE001 — usage is informational
        result = {"ok": False, "error": str(exc)[:200]}
        # Once per streak. A meter that has been down for an hour has
        # nothing new to say every ninety seconds, and the noise buried
        # the failures that mattered.
        first = not (_usage_cache.get("data") or {}).get("error")
        if first:
            log.warning("usage refresh failed: %s", exc)
        else:
            log.debug("usage refresh still failing: %s", exc)
    finally:
        # Stamp the time even on failure, so a persistently failing probe
        # retries on the TTL rather than on every single request.
        now = time.time()
        healthy = bool(result.get("ok")) and not result.get("recycled")
        # An explicit counter rather than "did the last result carry an
        # error". The refusal path above does not set one -- it serves the
        # previous reading with ok=True and recycled=True -- so the streak
        # test three lines up never fired for the failure that actually
        # happens, and this logged a warning on every probe: seventy-five in
        # two days for one condition that had not changed. A warning that is
        # always there is not a warning.
        streak = int(_usage_cache.get("fail_streak") or 0)
        _usage_cache.update(t=now, data=result)
        if healthy:
            # Remember the last figures that were true, and clear the backoff.
            _usage_cache.update(good=result, good_t=now, backoff=0.0,
                                fail_streak=0)
            _save_last_good(result)
            if streak:
                log.warning("usage lookup working again after %d failure(s)",
                            streak)
        else:
            # Double the wait after each failure, up to a quarter of an hour.
            # Rate limits are answered by asking less, not by asking again.
            previous = max(USAGE_TTL_S, _usage_cache.get("backoff_len") or USAGE_TTL_S)
            wait = min(MAX_USAGE_BACKOFF_S, previous * 2)
            _usage_cache.update(backoff=now + wait, backoff_len=wait,
                                fail_streak=streak + 1)
            detail = result.get("error")
            if not detail and result.get("recycled"):
                detail = ("refused; serving a reading %ds old"
                          % int(result.get("stale_seconds") or 0))
            if streak == 0:
                log.warning("usage lookup failed, backing off %.0fs: %s",
                            wait, detail)
            else:
                log.debug("usage lookup still failing (%d in a row), "
                          "backing off %.0fs: %s", streak + 1, wait, detail)
        try:
            _usage_refreshing.release()
        except RuntimeError:
            pass


# Codex's own allowances, read through the usage monitor's reader rather than
# by scanning the session logs here. It searches every recent session and takes
# the newest reading by timestamp, and it zeroes a window whose reset has
# already passed -- a stale "100%" must not stay pinned after the allowance
# renews. A second implementation of that disagreed with the one he watches.
_codex_cache: dict = {"t": 0.0, "data": None}
CODEX_TTL_S = 60.0

_CODEX_PROBE = r'''
import json, sys
sys.path.insert(0, r"{monitor}")
try:
    from src.storage.codex_usage import CodexUsageReader
    limits = CodexUsageReader().latest_limits()
    if limits is None:
        print(json.dumps({{"error": "no Codex usage recorded yet"}}))
    else:
        print(json.dumps({{
            "session_utilization": limits.session_percent,
            "week_utilization": limits.week_percent,
            "session_reset": limits.session_reset.isoformat() if limits.session_reset else None,
            "week_reset": limits.week_reset.isoformat() if limits.week_reset else None,
            "plan": limits.plan_label,
            "measured_at": limits.updated_at.isoformat() if limits.updated_at else None,
        }}))
except Exception as exc:
    print(json.dumps({{"error": str(exc)}}))
'''


def _read_codex_limits() -> dict:
    interpreter = USAGE_MONITOR / ".venv" / "Scripts" / "python.exe"
    if not interpreter.exists():
        interpreter = Path("python")
    try:
        proc = subprocess.run(
            [str(interpreter), "-c", _CODEX_PROBE.format(monitor=str(USAGE_MONITOR))],
            capture_output=True, text=True, timeout=30,
            cwd=str(USAGE_MONITOR), encoding="utf-8", errors="replace")
        payload = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001 -- usage is informational
        return {"ok": False, "error": str(exc)[:200]}
    if payload.get("error"):
        return {"ok": False, "error": str(payload["error"])[:200]}
    payload["ok"] = True
    return payload


@app.get("/codex-usage")
def codex_usage() -> dict:
    """Codex's five-hour and seven-day allowances."""
    now = time.time()
    if _codex_cache["data"] and now - _codex_cache["t"] < CODEX_TTL_S:
        return _codex_cache["data"]
    result = _read_codex_limits()
    _codex_cache.update(t=now, data=result)
    return result


# Thresholds at which the user is told, once each, that his rolling 5-hour
# limit is running out. Hitting it stops the assistant answering entirely, and
# until now that arrived with no warning at all.
USAGE_WARN_AT = (80.0, 95.0)
_usage_warned: dict = {"reset": None, "hit": set()}


def _usage_warning() -> str:
    """A line to prepend to the next spoken reply, or empty.

    Fires at most once per threshold per session window, and re-arms when the
    window resets — a warning repeated on every turn is one the user learns to
    ignore, which is worse than not warning at all.
    """
    data = _usage_cache.get("data") or {}
    if not data.get("ok"):
        return ""

    # Whichever window is closer to empty is the one worth mentioning. The
    # five-hour session refills by itself; the week does not, and it is the
    # one that ends the day. Warning about a session at 40% while the week
    # sits at 95% tells him the comfortable half of the truth.
    session_used = data.get("session_utilization")
    week_used = data.get("week_utilization")
    if (week_used or 0) > (session_used or 0):
        used, reset_at = week_used, data.get("week_reset")
        window = "weekly"
    else:
        used, reset_at = session_used, data.get("session_reset")
        window = "rolling five-hour"
    if used is None or not reset_at:
        return ""

    # A new window clears what has already been said.
    if _usage_warned["reset"] != reset_at:
        _usage_warned.update(reset=reset_at, hit=set())

    crossed = [t for t in USAGE_WARN_AT if used >= t and t not in _usage_warned["hit"]]
    if not crossed:
        return ""
    threshold = max(crossed)
    _usage_warned["hit"].add(threshold)

    when = ""
    try:
        stamp = str(reset_at).replace("Z", "+00:00").replace(" ", "T", 1)
        left = (dt.datetime.fromisoformat(stamp) - dt.datetime.now(dt.timezone.utc))
        mins = max(0, int(left.total_seconds() // 60))
        if mins >= 120:
            when = f", which resets in about {mins // 60} hours"
        elif mins:
            when = f", which resets in about {mins} minutes"
    except (ValueError, TypeError):
        pass

    log.info("usage warning issued at %.0f%% of the %s window", used, window)
    # What happens at the end of the window is no longer "he stops": the
    # NVIDIA models answer instead, tools included. Saying otherwise is a
    # warning about a consequence that does not exist, which is worse than
    # saying nothing -- he acts on it.
    return (
        f"Before your answer, tell him in one short sentence that he has used "
        f"{used:.0f}% of his {window} Claude limit{when}, and that when it "
        f"runs out you keep working on the free NVIDIA models instead — "
        f"plainer, but you do not stop. Say it plainly, once, then answer his "
        f"question as normal."
    )


# ---------------------------------------------------------------- Beszel ----
# The Contabo box reports into a Beszel hub, and until now the only way to see
# whether it was alive was to open the dashboard. The credentials live beside
# the NIM keys in ~/.config/jarvis and are never logged: a failure here reports
# that it failed, not what it tried.
BESZEL_CONF = Path.home() / ".config" / "jarvis" / "beszel.json"
BESZEL_TTL_S = 60.0
_beszel_cache: dict = {"t": 0.0, "data": None, "token": "", "token_t": 0.0}


def _beszel_token(base: str, conf: dict) -> str:
    """A hub token, reused for an hour. PocketBase issues them per login."""
    now = time.time()
    if _beszel_cache["token"] and now - _beszel_cache["token_t"] < 3600:
        return _beszel_cache["token"]
    body = json.dumps({"identity": conf["email"],
                       "password": conf["password"]}).encode()
    # The hub owner is a superuser, so that collection is tried first; a plain
    # user account authenticates against "users" instead.
    last = None
    for collection in ("_superusers", "users"):
        try:
            request = urllib.request.Request(
                f"{base}/api/collections/{collection}/auth-with-password",
                data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=15) as response:
                token = json.loads(response.read())["token"]
            _beszel_cache.update(token=token, token_t=now)
            return token
        except Exception as exc:  # noqa: BLE001 — try the other collection
            last = exc
    raise RuntimeError(f"hub login refused ({type(last).__name__})")


@app.get("/beszel")
def beszel() -> dict:
    """The monitored servers, as the hub last heard from them.

    Beszel's own field names are two letters long; they are spelled out here
    so the HUD does not have to know that "mp" is the memory percentage.
    """
    now = time.time()
    if _beszel_cache["data"] and now - _beszel_cache["t"] < BESZEL_TTL_S:
        return _beszel_cache["data"]

    try:
        conf = json.loads(BESZEL_CONF.read_text(encoding="utf-8"))
        base = conf["url"].rstrip("/")
        token = _beszel_token(base, conf)
        request = urllib.request.Request(
            f"{base}/api/collections/systems/records?perPage=20",
            headers={"Authorization": token})
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())

        systems = []
        for item in payload.get("items", []):
            info = item.get("info") or {}
            systems.append({
                "name": item.get("name"),
                "status": item.get("status"),
                "host": item.get("host"),
                "cpu": info.get("cpu"),
                "memory": info.get("mp"),
                "disk": info.get("dp"),
                "bandwidth": info.get("bb"),
                "load": info.get("la"),
                "containers": info.get("ct"),
                "threads": info.get("t"),
                "uptime_s": info.get("u"),
                "agent": info.get("v"),
                "updated": item.get("updated"),
            })
        result = {"ok": True, "systems": systems, "fetched_at": now}
    except FileNotFoundError:
        result = {"ok": False, "error": "no hub credentials at "
                                        f"{BESZEL_CONF.name}"}
    except Exception as exc:  # noqa: BLE001 — the card degrades, nothing else
        result = {"ok": False, "error": f"{type(exc).__name__}"}
        log.warning("beszel read failed: %s", type(exc).__name__)

    _beszel_cache.update(t=now, data=result)
    return result


@app.get("/beszel/review")
def beszel_review() -> dict:
    """A sentence per server, and what is worth doing about it.

    The dashboard shows numbers; this says whether any of them matter. The
    thresholds are the ones that actually cost something: a disk that fills
    stops the box, and swap or load sustained above the core count does not
    fix itself.
    """
    data = beszel()
    if not data.get("ok"):
        return data

    notes, actions = [], []
    for system in data["systems"]:
        name = system["name"] or "server"
        if system["status"] != "up":
            notes.append(f"{name} is {system['status']}")
            actions.append(f"{name}: check the agent is running")
            continue

        disk = system["disk"] or 0
        memory = system["memory"] or 0
        cpu = system["cpu"] or 0
        cores = system["threads"] or 1
        load = (system["load"] or [0])[0]
        days = int((system["uptime_s"] or 0) / 86400)

        notes.append(f"{name} up {days} days, disk {disk:.0f}%, "
                     f"memory {memory:.0f}%, cpu {cpu:.0f}%")
        if disk >= 85:
            actions.append(f"{name}: disk at {disk:.0f}% — clear it now")
        elif disk >= 70:
            actions.append(f"{name}: disk at {disk:.0f}% — worth a clear-out")
        if memory >= 90:
            actions.append(f"{name}: memory at {memory:.0f}%")
        if load > cores:
            actions.append(f"{name}: load {load:.2f} over {cores} threads")
        if not system["containers"]:
            notes.append(f"{name} is running no containers")

    return {"ok": True, "notes": notes, "actions": actions,
            "clean": not actions, "fetched_at": data["fetched_at"]}


# ----------------------------------------------------------------- graph ----
# graphify writes 564 nodes and 779 links for this repo. Sending that to a
# panel 224px wide would be a smear and half a megabyte of JSON, so the graph
# is reduced here rather than in the browser: the busiest nodes, the links
# between them, and the community each belongs to. The reduction is cached,
# because the file only changes when graphify is run again.
GRAPH_FILE = Path(__file__).parent / "graphify-out" / "graph.json"
GRAPH_TTL_S = 300.0
_graph_cache: dict = {"t": 0.0, "data": None, "mtime": 0.0}


@app.get("/graph")
def graph(limit: int = 60) -> dict:
    """The busiest part of the knowledge graph, ready to draw."""
    limit = max(10, min(200, limit))
    now = time.time()
    try:
        mtime = GRAPH_FILE.stat().st_mtime
    except OSError:
        return {"ok": False, "error": "no graph has been built yet"}

    cached = _graph_cache["data"]
    if (cached and cached.get("limit") == limit
            and _graph_cache["mtime"] == mtime
            and now - _graph_cache["t"] < GRAPH_TTL_S):
        return cached

    try:
        raw = json.loads(GRAPH_FILE.read_text(encoding="utf-8"))
        nodes = {n["id"]: n for n in raw.get("nodes", []) if n.get("id")}
        degree: dict = {}
        for link in raw.get("links", []):
            for end in (link.get("source"), link.get("target")):
                if end in nodes:
                    degree[end] = degree.get(end, 0) + 1

        # Whole communities, not the busiest nodes across the whole graph.
        # Taking the top 60 by degree gave 60 nodes and 59 edges, because the
        # busiest things in a codebase are rarely wired to each other -- it
        # drew as dust. Communities are dense inside themselves, so filling
        # the budget with the largest few gives a picture with structure in
        # it. Within a community the busiest come first.
        grouped: dict = {}
        for node_id in degree:
            name = nodes[node_id].get("community_name") or "other"
            grouped.setdefault(name, []).append(node_id)
        # Round-robin, not largest-first. Filling the budget from the biggest
        # community downwards put 50 of 60 nodes in "Test Suite Management",
        # which is true and tells him nothing about his own system. Taking the
        # busiest remaining node from each community in turn shows the shape
        # of the whole repo and still keeps communities intact enough to draw.
        # Across the top few communities only. Round-robin over all 47 of them
        # gave two nodes each and 24 edges -- breadth with no structure. Eight
        # clusters is what fits on a panel and still has edges inside each.
        ranked = {name: sorted(members, key=lambda k: -degree[k])
                  for name, members in grouped.items()}
        order = sorted(ranked, key=lambda k: -len(ranked[k]))[:8]
        keep, depth = [], 0
        while len(keep) < limit and depth < max(len(m) for m in ranked.values()):
            for name in order:
                if depth < len(ranked[name]) and len(keep) < limit:
                    keep.append(ranked[name][depth])
            depth += 1
        index = {node_id: position for position, node_id in enumerate(keep)}
        edges = []
        seen = set()
        for link in raw.get("links", []):
            a, b = link.get("source"), link.get("target")
            if a in index and b in index and a != b:
                pair = (index[a], index[b]) if index[a] < index[b] else (index[b], index[a])
                if pair not in seen:
                    seen.add(pair)
                    edges.append([pair[0], pair[1]])

        # Communities in order of how much of the graph they hold, so the
        # panel can colour the largest few and grey the rest.
        counts: dict = {}
        for node_id in keep:
            name = nodes[node_id].get("community_name") or "other"
            counts[name] = counts.get(name, 0) + 1
        order = sorted(counts, key=lambda k: -counts[k])
        rank = {name: position for position, name in enumerate(order)}

        out_nodes = []
        for node_id in keep:
            node = nodes[node_id]
            name = node.get("community_name") or "other"
            out_nodes.append({
                "label": (node.get("label") or node_id)[:34],
                "group": rank[name],
                "community": name,
                "degree": degree.get(node_id, 0),
                "file": node.get("source_file") or "",
            })

        result = {
            "ok": True,
            "limit": limit,
            "nodes": out_nodes,
            "edges": edges,
            "communities": [{"name": name, "count": counts[name]} for name in order],
            "totals": {"nodes": len(nodes), "links": len(raw.get("links", []))},
            "commit": (raw.get("built_at_commit") or "")[:7],
            "built": mtime,
        }
    except Exception as exc:  # noqa: BLE001 — the panel degrades, nothing else
        log.warning("graph read failed: %s", type(exc).__name__)
        return {"ok": False, "error": type(exc).__name__}

    _graph_cache.update(t=now, data=result, mtime=mtime)
    return result


# ---------------------------------------------------------- system graph ----
# What Jarvis is made of, and what is talking to what right now. The code
# graph answers "how is this repo built"; this answers "is my system alive and
# what is it doing", which is the question he actually asks the screen.
#
# Every service here was found listening on this machine rather than assumed:
# 5055 answers "Open Notebook API is running", 8880 is Kokoro's speech API
# (404 on / because it only serves /v1/audio/speech), 8000 is the backend and
# 8123 the daemon. Anything without a probe reports "idle", never "down" --
# claiming a thing is broken because nothing was checked is worse than saying
# nothing.
SERVICES = [
    # key,        label,        ring, probe kind, target
    ("jarvis",    "JARVIS",       0, "self",   None),
    ("daemon",    "Daemon",       1, "port",   8123),
    ("backend",   "Backend",      1, "port",   8000),
    ("claude",    "Claude",       1, "usage",  None),
    ("codex",     "Codex",        1, "codex",  None),
    ("kokoro",    "Voice",        1, "port",   8880),
    ("whisper",   "Ears",         1, "none",   None),
    ("telegram",  "Telegram",     1, "none",   None),
    ("phone",     "Phone",        1, "none",   None),
    ("server",    "Server",       1, "beszel", None),
    ("notebook",  "Notebook",     1, "port",   5055),
    ("images",    "Images",       1, "port",   7866),
    ("ollama",    "Ollama",       1, "port",   11434),
    ("social",    "SocialPilot",  1, "port",   3000),
]

# Who talks to whom. A star from Jarvis, plus the paths that are real: the
# bridge answers the daemon, Claude writes what the voice speaks, the phone
# carries what the voice produced.
SYSTEM_EDGES = [
    ("jarvis", "daemon"), ("jarvis", "claude"), ("jarvis", "codex"),
    ("jarvis", "kokoro"), ("jarvis", "whisper"), ("jarvis", "telegram"),
    ("jarvis", "phone"), ("jarvis", "server"), ("jarvis", "notebook"),
    ("jarvis", "images"), ("jarvis", "ollama"), ("jarvis", "social"),
    ("jarvis", "backend"),
    ("claude", "kokoro"), ("kokoro", "phone"), ("whisper", "jarvis"),
    ("daemon", "backend"), ("claude", "telegram"),
]

_probe_cache: dict = {"t": 0.0, "data": {}}
PROBE_TTL_S = 15.0


def _port_open(port: int, timeout: float = 0.35) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _service_states() -> dict:
    """One cheap sweep of every probe, cached, so the panel can poll often."""
    now = time.time()
    if now - _probe_cache["t"] < PROBE_TTL_S and _probe_cache["data"]:
        return _probe_cache["data"]

    states: dict = {}
    for key, _label, _ring, kind, target in SERVICES:
        if kind == "self":
            states[key] = "up"
        elif kind == "port":
            states[key] = "up" if _port_open(int(target)) else "down"
        # Ask, do not peek. Reading the caches directly reported Claude, Codex
        # and the server as idle on a fresh bridge, because a cache nobody has
        # filled is empty -- which looked exactly like three dead services.
        # Each of these is itself cached, so asking is cheap.
        elif kind == "usage":
            states[key] = "up" if usage().get("ok") else "idle"
        elif kind == "codex":
            states[key] = "up" if codex_usage().get("ok") else "idle"
        elif kind == "beszel":
            data = beszel()
            first = (data.get("systems") or [{}])[0] if data.get("ok") else {}
            states[key] = "up" if first.get("status") == "up" else "idle"
        else:
            states[key] = "idle"
    _probe_cache.update(t=now, data=states)
    return states


# The traffic. A ring buffer of "this spoke to that", which the panel turns
# into a dot travelling down the wire. Anything on this machine can post one.
EVENTS: deque = deque(maxlen=200)
_event_seq = {"n": 0}


def emit(src: str, dst: str, label: str = "") -> None:
    """Record one hop. Never raises: telemetry must not break the work."""
    try:
        _event_seq["n"] += 1
        EVENTS.append({"seq": _event_seq["n"], "t": time.time(),
                       "src": src, "dst": dst, "label": label[:60]})
    except Exception:  # noqa: BLE001
        pass


@app.get("/system-graph")
def system_graph() -> dict:
    states = _service_states()
    nodes = [{"key": key, "label": label, "ring": ring,
              "status": states.get(key, "idle")}
             for key, label, ring, _kind, _target in SERVICES]
    return {"ok": True, "nodes": nodes,
            "edges": [[a, b] for a, b in SYSTEM_EDGES],
            "seq": _event_seq["n"]}


@app.get("/events")
def events(since: int = 0) -> dict:
    """Hops since the sequence number the caller last saw."""
    return {"ok": True, "seq": _event_seq["n"],
            "events": [e for e in EVENTS if e["seq"] > since]}


@app.post("/event")
async def post_event(request: Request) -> dict:
    """Let the rest of the system draw on the graph.

    The morning call, the daemon and the reel builders all know things the
    bridge cannot see. One POST and the hop appears on the panel.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    src = str(body.get("src") or "jarvis")[:24]
    dst = str(body.get("dst") or "jarvis")[:24]
    emit(src, dst, str(body.get("label") or ""))
    return {"ok": True, "seq": _event_seq["n"]}


# The cleanup, over SSH. The Review button says what needs doing; this does it.
#
# The account is `oliomr` on port 2525 -- port 22 is closed -- and it has no
# passwordless sudo, so /var/log and the apt cache are out of reach and are
# reported rather than silently skipped. What it can do is Docker, which is
# where the space actually goes on this box.
#
# Two steps on purpose: a plan first, then the work. Nothing here removes a
# running container, an image in use, or a volume.
CONTABO_CONF = Path.home() / ".config" / "jarvis" / "contabo.json"

CLEAN_STEPS = [
    ("docker image prune -f",
     "dangling images -- layers no tag points at any more"),
    ("docker builder prune -f",
     "the build cache"),
    ("docker container prune -f --filter until=168h",
     "containers that exited more than a week ago"),
]


def _ssh(command: str, timeout: int = 120) -> tuple:
    """Run one command on the server. Returns (ok, output)."""
    conf = json.loads(CONTABO_CONF.read_text(encoding="utf-8"))
    # StrictHostKeyChecking=no trusts whatever answers on that address, every
    # time. The server is on a public IP, so a machine in the middle would be
    # accepted silently and would receive every command sent here. The key is
    # pinned in a file of its own instead: accept-new records it the first
    # time and refuses any change after that, which is what makes a
    # substituted host an error rather than a shrug.
    known_hosts = CONTABO_CONF.with_name("contabo_known_hosts")
    argv = ["ssh", "-i", conf["key"], "-p", str(conf.get("port", 22)),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "ConnectTimeout=15",
            f"{conf['user']}@{conf['host']}", command]
    done = subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")
    return done.returncode == 0, (done.stdout or done.stderr or "").strip()


def _disk_used_kb() -> int:
    ok, out = _ssh("df -k / | tail -1 | awk '{print $3}'", timeout=40)
    try:
        return int(out.split()[0]) if ok else 0
    except (ValueError, IndexError):
        return 0


@app.get("/beszel/clean")
def beszel_clean(request: Request, apply: bool = False) -> dict:
    """Plan the cleanup, or carry it out. One SSH connection either way.

    The first version opened a connection per command -- four to plan, four
    more to apply -- and fail2ban banned this address halfway through, which
    is why the results came back as "connection timed out" for every step and
    the disk figure rendered as "ssh: of connect (to)". The box counts
    connections, not just failed logins, so everything now goes down one.
    """
    # Reading the plan is harmless. Applying it deletes things on his server,
    # so it has to be a deliberate act from this machine, not a fetched URL.
    if apply:
        _same_machine_only(request)
    if not CONTABO_CONF.exists():
        return {"ok": False, "error": "no server connection is configured"}

    mark = "===JARVIS==="
    script = [
        f"echo {mark}disk; df -h / | tail -1 | awk '{{print $3, $2, $5}}'",
        f"echo {mark}docker; docker system df --format "
        "'{{.Type}} {{.Reclaimable}}' 2>/dev/null",
        f"echo {mark}logs; du -sh /var/log 2>/dev/null | cut -f1",
    ]
    if apply:
        script.append(f"echo {mark}before; df -k / | tail -1 | awk '{{print $3}}'")
        for command, _what in CLEAN_STEPS:
            script.append(f"echo {mark}step; echo {command}; {command} 2>&1 | tail -2")
        script.append(f"echo {mark}after; df -k / | tail -1 | awk '{{print $3}}'")
        script.append(f"echo {mark}disk2; df -h / | tail -1 | awk '{{print $3, $2, $5}}'")

    try:
        ok, output = _ssh("; ".join(script), timeout=900 if apply else 90)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "the server took too long to answer"}
    except Exception as exc:  # noqa: BLE001 — a button, not the work
        log.warning("server clean failed: %s", type(exc).__name__)
        return {"ok": False, "error": type(exc).__name__}

    if not ok:
        return {"ok": False, "error": "cannot reach the server",
                "detail": output[:160]}

    sections: dict = {}
    current = None
    for line in output.splitlines():
        if line.startswith(mark):
            current = line[len(mark):].strip()
            sections.setdefault(current, [])
        elif current is not None and line.strip():
            sections[current].append(line.rstrip())

    def pretty(key: str) -> str:
        bits = " ".join(sections.get(key, [])).split()
        return f"{bits[0]} of {bits[1]} ({bits[2]})" if len(bits) >= 3 else "unknown"

    logs = (sections.get("logs") or ["unknown"])[0]
    plan = {
        "ok": True,
        "applied": bool(apply),
        "disk": pretty("disk2" if apply else "disk"),
        "reclaimable": sections.get("docker", []),
        "steps": [{"command": command, "what": what} for command, what in CLEAN_STEPS],
        # Said plainly rather than skipped quietly: a cleanup that cannot
        # touch the biggest directory should say so.
        "out_of_reach": [
            f"/var/log is {logs} and needs sudo, which this account does not "
            f"have without a password",
            "the apt cache needs sudo for the same reason",
        ],
    }
    if not apply:
        return plan

    def number(key: str) -> int:
        try:
            return int((sections.get(key) or ["0"])[0].split()[0])
        except (ValueError, IndexError):
            return 0

    plan["freed_mb"] = round(max(0, number("before") - number("after")) / 1024, 1)
    plan["results"] = [line for line in sections.get("step", []) if line.strip()]
    return plan


# -------------------------------------------------------------- pc graph ----
# Everything happening on this machine, as nodes for the HUD to ring around
# the reactor. The service list before this only knew the things Jarvis was
# built from; he asked for the PC, so this reads the process table and the
# dots are measured activity -- CPU time burned and bytes moved since the
# last look -- not decoration.
import psutil

# Process names folded into one node each. Everything else groups by its own
# executable name, so a new program appears by itself without a code change.
PC_LABELS = {
    "code.exe": ("vscode", "VS Code"),
    "brave.exe": ("brave", "Brave"),
    "chrome.exe": ("chrome", "Chrome"),
    "msedge.exe": ("edge", "Edge"),
    "msedgewebview2.exe": ("webview", "WebView"),
    "explorer.exe": ("explorer", "Explorer"),
    "discord.exe": ("discord", "Discord"),
    "viber.exe": ("viber", "Viber"),
    "spotify.exe": ("spotify", "Spotify"),
    "steam.exe": ("steam", "Steam"),
    "robloxplayerbeta.exe": ("roblox", "Roblox"),
    "ffmpeg.exe": ("ffmpeg", "ffmpeg"),
    "node.exe": ("node", "Node"),
    "ollama.exe": ("ollama", "Ollama"),
    "ollama app.exe": ("ollama", "Ollama"),
    "dwm.exe": ("dwm", "Compositor"),
    "audiodg.exe": ("audio", "Audio"),
    "obs64.exe": ("obs", "OBS"),
    "capcut.exe": ("capcut", "CapCut"),
    "photoshop.exe": ("photoshop", "Photoshop"),
    "premiere pro.exe": ("premiere", "Premiere"),
    "claude.exe": ("claude", "Claude CLI"),
}

# Python processes are told apart by what they are running, otherwise the
# daemon, the bridge and every build script would melt into one "python" dot.
PC_PY_HINTS = [
    ("jarvis.daemon", "daemon", "Jarvis Daemon"),
    ("claude-bridge", "bridge", "Bridge"),
    ("telegram_chat", "telegram", "Telegram"),
    ("jarvis serve", "backend", "Backend"),
    ("uvicorn", "backend", "Backend"),
]

_pc_cache: dict = {"t": 0.0, "procs": [], "prev": {}, "sampled": 0,
                   "window_s": 0.0, "edges": []}
PC_SCAN_MIN_GAP_S = 4.0
# How long after the last /pc-graph request the scanner keeps working. Long
# enough to cover a HUD that polls every few seconds and a person pausing to
# read it; short enough that a closed panel stops costing anything.
PC_IDLE_AFTER_S = 45.0
# What it costs while asleep: one cheap wakeup, twice a second's worth.
PC_IDLE_TICK_S = 2.0
# When /pc-graph was last asked for. The scan follows the reader.
_pc_wanted: dict = {"t": 0.0}


def _pc_identity(proc) -> tuple:
    name = (proc.info.get("name") or "").lower()
    if name in PC_LABELS:
        return PC_LABELS[name]
    if name.startswith(("python", "pythonw")):
        try:
            line = " ".join(proc.cmdline()).lower()
        except (psutil.Error, OSError):
            line = ""
        for hint, key, label in PC_PY_HINTS:
            if hint in line:
                return key, label
        return "python", "Python"
    stem = name[:-4] if name.endswith(".exe") else name
    return (stem or "unknown"), (stem.capitalize() or "?")


def _pc_scan() -> None:
    """One pass over the process table, deltas against the previous pass.

    This is the expensive part: ~600 processes cost several seconds of handle
    reads on Windows. It runs in its own thread on a cadence, never on a
    request — scanning per request meant every HUD poll started a six-second
    scan, the scans overlapped, and the bridge wedged solid.
    """
    now = time.time()
    prev = _pc_cache["prev"]
    window = max(0.5, now - _pc_cache["t"]) if _pc_cache["t"] else 1.0
    seen: dict = {}
    procs = []
    parent_of: dict = {}
    for proc in psutil.process_iter(["name", "memory_info", "cpu_times", "ppid"]):
        try:
            parent_of[proc.pid] = proc.info.get("ppid") or 0
            key, label = _pc_identity(proc)
            if key in ("system idle process", "registry"):
                continue
            times = proc.info.get("cpu_times")
            cpu_s = (times.user + times.system) if times else 0.0
            try:
                io = proc.io_counters()
                read_b, write_b = io.read_bytes, io.write_bytes
            except (psutil.Error, OSError, AttributeError):
                read_b = write_b = 0
            mem = (proc.info.get("memory_info").rss
                   if proc.info.get("memory_info") else 0)
            pid = proc.pid
            seen[pid] = (cpu_s, read_b, write_b)
            was = prev.get(pid, (cpu_s, read_b, write_b))
            procs.append({
                "key": f"p{pid}", "group": key, "label": label, "pid": pid,
                "mem_mb": round(mem / 1e6, 1),
                "cpu": round(max(0.0, cpu_s - was[0]) / window * 100.0, 1),
                "read_kb": round(max(0, read_b - was[1]) / 1024.0, 1),
                "write_kb": round(max(0, write_b - was[2]) / 1024.0, 1),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    # The tree, reattached over anything that was filtered out, so a child
    # whose parent is not drawn still hangs off its nearest drawn ancestor
    # instead of floating free.
    drawn = {node["pid"] for node in procs}
    edges = []
    for pid in drawn:
        up = parent_of.get(pid, 0)
        hops = 0
        while up and up not in drawn and hops < 12:
            up = parent_of.get(up, 0)
            hops += 1
        if up and up in drawn and up != pid:
            edges.append([f"p{up}", f"p{pid}", 0])

    # Loopback sockets: who is genuinely talking to whom right now. A
    # listener's port identifies the far end of every established local
    # connection, which is how the HUD's own webview shows up wired to this
    # bridge.
    try:
        listeners: dict = {}
        live = []
        for conn in psutil.net_connections(kind="inet"):
            if not conn.pid:
                continue
            if conn.status == "LISTEN" and conn.laddr:
                listeners.setdefault(conn.laddr.port, conn.pid)
            elif conn.status == "ESTABLISHED" and conn.raddr:
                live.append(conn)
        wired = set()
        for conn in live:
            if conn.raddr.ip not in ("127.0.0.1", "::1"):
                continue
            peer = listeners.get(conn.raddr.port)
            if peer and peer != conn.pid and peer in drawn and conn.pid in drawn:
                wired.add((conn.pid, peer))
        edges += [[f"p{a}", f"p{b}", 1] for a, b in wired]
    except (psutil.Error, OSError, PermissionError):
        pass  # sockets need rights this process may not have; the tree stands

    _pc_cache.update(prev=seen, procs=procs, sampled=len(seen),
                     window_s=round(window, 2), t=now, edges=edges)


def _pc_scan_loop() -> None:
    """Keep the snapshot fresh WHILE SOMEBODY IS LOOKING, and not otherwise.

    The cadence adapts to the scan's own cost: at least PC_SCAN_MIN_GAP_S
    between scans, stretched to twice the last scan's duration on a busy
    table — a slow machine degrades to slightly older data, never to a
    wedged bridge.

    It used to run unconditionally, forever. By its own docstring a scan is
    "several seconds of handle reads" across ~600 processes, so a bridge
    nobody was watching still burned better than half a core around the
    clock: measured at 57.6% of one core with an idle HUD, and 39,923 CPU
    seconds — eleven hours — over a day and a bit of uptime. That is the
    whole reason his machine felt slow, and it was mine.

    /pc-graph is the only reader of the snapshot, so the scan now follows the
    reader. One request wakes it; PC_IDLE_AFTER_S of silence puts it back to
    sleep, where it costs a wakeup every two seconds and nothing else.
    """
    while True:
        asked = _pc_wanted["t"]
        if asked and (time.time() - asked) < PC_IDLE_AFTER_S:
            t0 = time.time()
            try:
                _pc_scan()
            except Exception:
                log.exception("pc scan failed")
            dur = time.time() - t0
            time.sleep(max(PC_SCAN_MIN_GAP_S, dur * 2.0))
        else:
            time.sleep(PC_IDLE_TICK_S)


@app.get("/pc-graph")
def pc_graph(limit: int = 220) -> dict:
    """Every process worth drawing, one node each -- not folded by program.

    He wanted the machine in detail: thirty-five VS Code processes are
    thirty-five dots, not "VS Code x35". Each node is a single pid, labelled
    by program with a short suffix when several share a name. `cpu` is percent
    of one core over the window; `read_kb`/`write_kb` are bytes moved in it.
    Capped at the busiest `limit`. Served from the scanner thread's latest
    snapshot, so it answers instantly whatever the process table costs.

    Asking is also what keeps the scanner awake: it sleeps when nothing has
    read this for PC_IDLE_AFTER_S, so a HUD nobody has open costs nothing.
    """
    _pc_wanted["t"] = time.time()
    limit = max(20, min(2000, limit))
    procs = [dict(p) for p in _pc_cache["procs"]]  # copies: labels get suffixed

    # Everything, unless a limit is asked for that cannot hold everything --
    # and then the busiest, with his own processes never dropped.
    if len(procs) <= limit:
        keep = procs
    else:
        ours_groups = {"daemon", "bridge", "backend", "telegram", "vscode",
                       "claude", "ollama", "node"}
        ours = [g for g in procs if g["group"] in ours_groups]
        rest = sorted((g for g in procs if g["group"] not in ours_groups),
                      key=lambda g: -(g["cpu"] * 8 + g["read_kb"] + g["write_kb"]
                                      + g["mem_mb"] / 4))
        keep = ours + rest[:max(0, limit - len(ours))]

    # A suffix only where a program has more than one process, so "Node" alone
    # stays "Node" but three of them become Node·1/2/3 in memory order.
    from collections import defaultdict
    counts: dict = defaultdict(int)
    for node in keep:
        counts[node["group"]] += 1
    idx: dict = defaultdict(int)
    for node in sorted(keep, key=lambda g: -g["mem_mb"]):
        if counts[node["group"]] > 1:
            idx[node["group"]] += 1
            node["label"] = f"{node['label']}·{idx[node['group']]}"

    alive = {node["key"] for node in keep}
    edges = [e for e in _pc_cache["edges"] if e[0] in alive and e[1] in alive]

    return {"ok": True, "limit": limit, "nodes": keep, "edges": edges,
            "sampled": _pc_cache["sampled"], "shown": len(keep),
            "window_s": _pc_cache["window_s"], "fetched_at": _pc_cache["t"],
            "warming": not procs}


@app.post("/local-health")
def local_health(payload: dict) -> dict:
    """Which local services are listening, probed here so the page never has
    to. A browser fetch to a dead port prints a console error nothing can
    suppress; a socket connect from this side is silent and faster."""
    import socket
    from urllib.parse import urlparse

    urls = [u for u in (payload.get("urls") or []) if isinstance(u, str)][:16]
    up: dict = {}
    for u in urls:
        try:
            parsed = urlparse(u)
            # Local probes only: this must never become a generic port scanner
            # for whatever a compromised page asks about.
            if parsed.hostname not in ("127.0.0.1", "localhost"):
                up[u] = False
                continue
            with socket.create_connection(("127.0.0.1", parsed.port or 80),
                                          timeout=0.6):
                up[u] = True
        except (OSError, ValueError):
            up[u] = False
    return {"ok": True, "up": up}


@app.post("/reset")
def reset_workers() -> dict:
    """Recycle every CLI worker. Use when sessions get wedged rather than
    restarting the whole bridge and losing the UI's connection."""
    for pool in WORKERS.values():
        pool.reset()
    log.info("workers reset by request")
    _warm_all()
    return {"reset": True}


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "uptime_s": round(time.time() - BOOT_TS, 1),
        "requests": REQUEST_COUNT["n"],
        "errors": len(ERRORS),
    }


@app.get("/v1/models")
def models() -> dict:
    return {"object": "list", "data": [
        {"id": MODEL_ID, "object": "model"},
        {"id": MODEL_FAST_ID, "object": "model"},
        {"id": MODEL_AGENT_ID, "object": "model"},
    ]}


@app.get("/ui")
def ui_page() -> FileResponse:
    # no-store: the UI is edited live and Edge's app window would otherwise
    # keep serving a cached copy, so changes look like they never landed.
    return FileResponse(
        ASSET_DIR / "ui.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


# ── Setup: what a new person turns on, and where their keys go ────────────
#
# Three rules hold this together, and all three are about one asymmetry: a key
# that leaks cannot be unleaked.
#
#   1. A key goes IN and never comes back OUT. /setup/status answers "is one
#      set", never the value, so no screenshot, log line or browser history
#      can carry it.
#   2. Keys are written to ~/.config/jarvis, outside the repository, which is
#      git-ignored and checked by scripts/check-secrets.py before a push.
#   3. Only ids the catalogue names are accepted, so a crafted request cannot
#      aim a write at an arbitrary file.
# A browser will happily POST to 127.0.0.1 from any page you happen to be
# visiting. Nothing here is readable cross-origin, but WRITING is enough to do
# harm: a hostile page could quietly swap in its own API key and every request
# Jarvis makes would then be billed to, and visible to, somebody else.
#
# So every state-changing setup call must satisfy three things at once:
#   Origin, when the browser sends one, is this bridge;
#   Host is loopback, which stops a DNS-rebind pointing a public name here;
#   Content-Type is JSON, which forces a CORS preflight for cross-site posts.
# A same-origin fetch from /setup passes all three without noticing.
ALLOWED_ORIGINS = {
    "http://127.0.0.1:8100", "http://localhost:8100",
    "http://127.0.0.1:8123", "http://localhost:8123",
}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _same_machine_only(request: Request) -> None:
    """Refuse anything a foreign page could have caused."""
    origin = request.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        log.warning("setup: refused a cross-origin write from %s", origin)
        raise HTTPException(status_code=403, detail="cross-origin request refused")

    # Allowing a missing Origin is what lets the daemon in, and on a GET it
    # was the whole guard: a cross-site <img src=...?apply=true> sends no
    # Origin and no content type, so the check passed on Host alone and
    # failed open. Sec-Fetch-Site is the thing that tells the two apart --
    # every modern browser sends it and cannot be made not to, and a Python
    # client never does. So: no Origin AND a browser saying it came from
    # somewhere else is refused.
    site = request.headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "none"):
        log.warning("setup: refused a %s request from sec-fetch-site=%s",
                    request.method, site)
        raise HTTPException(status_code=403, detail="cross-site request refused")

    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    if host and host.lower() not in {h.strip("[]") for h in LOOPBACK_HOSTS}:
        log.warning("setup: refused a write addressed to host %r", host)
        raise HTTPException(status_code=403, detail="not a loopback request")

    # Only requests that carry a body can be asked for a content type. A GET
    # has none, and demanding one there would refuse a legitimate click --
    # which is how a guard ends up being taken back out again.
    if request.method in ("POST", "PUT", "PATCH"):
        kind = (request.headers.get("content-type") or "").split(";")[0].strip()
        if kind != "application/json":
            raise HTTPException(status_code=415, detail="send application/json")


SETUP_DIR = Path(__file__).parent / "setup"
CONF_DIR = Path.home() / ".config" / "jarvis"
SETUP_STATE = CONF_DIR / "setup-state.json"


def _catalogue() -> dict:
    try:
        return json.loads((SETUP_DIR / "catalogue.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("setup catalogue unreadable: %s", exc)
        return {"groups": []}


def _catalogue_item(item_id: str):
    for group in _catalogue().get("groups", []):
        for item in group.get("items", []):
            if item.get("id") == item_id:
                return item
    return None


def _secret_is_set(spec: str) -> bool:
    """Whether a key exists, WITHOUT reading it into anything that escapes."""
    file_name, _, field = str(spec).partition(":")
    path = CONF_DIR / file_name
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    value = data.get(field)
    if isinstance(value, (list, dict)):
        return bool(value)
    return bool(str(value or "").strip())


@app.get("/setup")
def setup_page() -> FileResponse:
    """The first-run page: tick what you want, paste keys, done.

    Framing is refused except by this bridge. The Origin check on the write
    endpoints is not enough on its own: a hostile page can put /setup in an
    invisible iframe, float its own buttons over the real ones, and let the
    person click Save for it -- and because the click happens INSIDE the
    frame, the request carries this bridge's Origin and passes every test.
    frame-ancestors closes that, and 'self' still allows the HUD's own panel,
    which loads /setup from the same origin.
    """
    return FileResponse(
        SETUP_DIR / "setup.html", media_type="text/html",
        headers={
            "Cache-Control": "no-store, must-revalidate",
            "Content-Security-Policy": "frame-ancestors 'self'",
            "X-Frame-Options": "SAMEORIGIN",
        })


@app.get("/setup/catalogue.json")
def setup_catalogue() -> dict:
    """What can be turned on. Carries no secrets -- it is the menu, not the meal."""
    return _catalogue()


@app.get("/setup/status")
def setup_status(id: str) -> dict:
    """Ready or not. Deliberately boolean: never the key, never a prefix of it."""
    item = _catalogue_item(id)
    if item is None:
        raise HTTPException(status_code=404, detail="no such integration")
    if item.get("needs") == "key":
        return {"id": id, "ready": _secret_is_set(item.get("secret", "")),
                "kind": "key"}
    url = item.get("verify")
    if not url:
        return {"id": id, "ready": False, "kind": "manual"}
    try:
        with urllib.request.urlopen(url, timeout=1.5) as answer:
            return {"id": id, "ready": 200 <= answer.status < 500,
                    "kind": "service"}
    except Exception:  # noqa: BLE001 -- not running IS the answer here
        return {"id": id, "ready": False, "kind": "service"}


@app.post("/setup/key")
async def setup_key(request: Request) -> dict:
    """Take one key and put it where it belongs. Never echoes it back."""
    _same_machine_only(request)
    body = await request.json()
    item = _catalogue_item(str(body.get("id") or ""))
    if item is None or item.get("needs") != "key":
        raise HTTPException(status_code=404, detail="no such integration")
    value = str(body.get("value") or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="empty key")

    file_name, _, field = str(item.get("secret") or "").partition(":")
    if not file_name or not field:
        raise HTTPException(status_code=500, detail="catalogue has no home for it")
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    path = CONF_DIR / file_name
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    # Some homes hold a list of keys and rotate through them; others hold one.
    existing = data.get(field)
    if isinstance(existing, list):
        if value not in existing:
            existing.append(value)
    else:
        data[field] = value
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)  # best effort; Windows ignores the mode
    except OSError:
        pass
    # The value is never logged, not even truncated. A prefix is still a clue.
    log.info("setup: stored a key for %s", item.get("id"))
    return {"ok": True, "id": item.get("id"), "ready": True}


@app.post("/setup/save")
async def setup_save(request: Request) -> dict:
    """Remember which integrations this machine wants. No secrets in here."""
    _same_machine_only(request)
    body = await request.json()
    wanted = [str(x) for x in (body.get("enabled") or [])]
    known = {i.get("id") for g in _catalogue().get("groups", [])
             for i in g.get("items", [])}
    enabled = sorted(set(wanted) & known)
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    SETUP_STATE.write_text(
        json.dumps({"enabled": enabled, "saved_at": time.time()}, indent=2),
        encoding="utf-8")
    log.info("setup: %d integration(s) enabled", len(enabled))
    return {"ok": True, "enabled": enabled}


@app.get("/setup/state")
def setup_state() -> dict:
    """What was chosen last time, so Settings can show it."""
    try:
        return json.loads(SETUP_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"enabled": []}


_VENDOR_FILES = {"three.min.js", "OrbitControls.js"}


@app.get("/vendor/{name}")
def vendor_file(name: str) -> FileResponse:
    """The 3D engine for /world, served from disk so the page works offline."""
    if name not in _VENDOR_FILES:
        raise HTTPException(status_code=404, detail="no such file")
    return FileResponse(ASSET_DIR / "vendor" / name,
                        media_type="application/javascript",
                        headers={"Cache-Control": "max-age=86400"})


@app.get("/world")
def world_page() -> FileResponse:
    """Jarvis Island: the machine drawn as a town, every building real."""
    return FileResponse(
        ASSET_DIR / "world.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@app.get("/ui-version")
def ui_version() -> dict:
    """Fingerprint of the UI file.

    The app window stays open for hours, so an edit to ui.html is invisible
    until it is manually reloaded — which looks exactly like the change was
    never made. The page polls this and reloads itself when it changes.
    """
    try:
        return {"version": int((ASSET_DIR / "ui.html").stat().st_mtime)}
    except OSError:
        return {"version": 0}


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    """The icon the app window and the taskbar show.

    Without this the window is branded Edge, which is the browser it happens
    to run in and not the thing he opened.
    """
    return FileResponse(ASSET_DIR / "jarvis.ico", media_type="image/x-icon")


@app.get("/jarvis.png")
def app_icon() -> FileResponse:
    return FileResponse(ASSET_DIR / "jarvis.png", media_type="image/png")


@app.get("/avatar")
def avatar_page() -> FileResponse:
    return FileResponse(ASSET_DIR / "avatar.html", media_type="text/html")


@app.get("/avatar/model.glb")
def avatar_model() -> FileResponse:
    return FileResponse(ASSET_DIR / "model.glb", media_type="model/gltf-binary")


def _flatten(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def _build_prompt(messages: list) -> tuple[str, str]:
    system_parts: list[str] = []
    turns: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        text = _flatten(m.get("content"))
        if role == "system" and text:
            system_parts.append(text)
        elif role == "assistant":
            if m.get("tool_calls"):
                calls = "; ".join(
                    f"{c['function']['name']}({c['function'].get('arguments', '{}')})"
                    for c in m["tool_calls"] if c.get("function")
                )
                turns.append(f"Assistant: [called tools: {calls}]")
            elif text:
                turns.append(f"Assistant: {text}")
        elif role == "tool":
            turns.append(f"Tool result: {text[:6000]}")
        elif text:
            turns.append(f"User: {text}")
    convo = "\n\n".join(turns) if turns else "Hello"
    if len(turns) > 1:
        convo = (
            "Below is the conversation so far. Reply to the final user message "
            "as the assistant; output only your reply.\n\n" + convo
        )
    return "\n\n".join(system_parts), convo


TOOL_MARK = "TOOL_CALL:"


def _tools_instruction(tools: list) -> str:
    """OpenAI function-calling emulated over plain text."""
    specs = []
    for t in tools:
        fn = t.get("function", {}) if t.get("type") == "function" else t
        specs.append({
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        })
    return (
        "You can call these tools:\n" + json.dumps(specs, indent=1) +
        f"\n\nTo call one, reply with EXACTLY one line and nothing else:\n"
        f'{TOOL_MARK}{{"name": "<tool name>", "arguments": {{...}}}}\n'
        "Only call a tool when it is genuinely needed to answer. "
        "Otherwise reply normally with your final answer.\n\n"
        "NEVER REPORT AN ACTION YOU HAVE NOT TAKEN. Nothing happens on his "
        "machine because you said it did: the tool call is the only thing "
        "that acts. If your reply would tell him something is playing, open, "
        "typed, sent, saved or closed, then the tool that does it must be "
        "called first and must have come back. \"Playing your playlist now\" "
        "with no call before it is a lie he finds out about a second later, "
        "in silence.\n"
        "So: if a tool on this list can carry out what he asked, call it. Do "
        "not describe it, do not offer it, and do not answer as though it had "
        "already run."
    )


# ---------------------------------------------------------------------------
# NVIDIA NIM — the grunt work behind the reply.
#
# Every model call here normally costs a slice of the rolling five-hour window
# on Oliver's Claude subscription, and running out of that window is what stops
# Jarvis answering at all. The classification and housekeeping calls (intent
# judging, tool routing, diary summaries, knowledge-graph extraction) are the
# bulk of that traffic and none of them is the voice the user hears, so they
# run on NVIDIA's hosted models instead. The spoken reply itself stays on
# Claude.
#
# Any failure falls back to the CLI, so this can cost latency but never an
# answer.
# ---------------------------------------------------------------------------
NIM_CONF_FILE = Path.home() / ".config" / "jarvis" / "nim-keys.json"
_nim_cache: dict = {"mtime": -1.0, "conf": {}}
_nim_key_turn = itertools.count()


def _nim_conf() -> dict:
    """The NIM config, re-read whenever the file changes.

    Credentials live outside the repo and are never logged: a key is only ever
    identified by its name.
    """
    try:
        mtime = NIM_CONF_FILE.stat().st_mtime
    except OSError:
        return {}
    if mtime != _nim_cache["mtime"]:
        try:
            _nim_cache["conf"] = json.loads(NIM_CONF_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("NIM config unreadable, staying on Claude: %s", type(exc).__name__)
            _nim_cache["conf"] = {}
        _nim_cache["mtime"] = mtime
    return _nim_cache["conf"]


# How full the weekly window has to be before spoken replies start moving off
# Claude. Below this he gets the good voice for everything; above it, turns
# that need no tools are answered by NIM so the remaining quota is kept for
# the work only Claude can do.
WEEK_PRESSURE_AT = 90.0

# Past this, everything that can possibly run on NIM does. Running out stops
# the assistant completely, and a plainer answer is better than none.
WEEK_CRITICAL_AT = 97.0


def usage_pressure() -> float:
    """How full his weekly Claude window is, 0-100, or 0 when unknown.

    The weekly window is the one that ends the day: the five-hour session
    refills by itself, the week does not, and running it out stops Jarvis
    answering at all until it resets.
    """
    data = _usage_cache.get("data") or {}
    if not data.get("ok"):
        # Anthropic rate-limits this lookup, and a refusal used to read as
        # "nothing used" -- which switched the protection off at exactly the
        # moment the window was tight enough for the lookup to be worth
        # having. The last figure that was actually read is a far better
        # guess than zero, and it only ever grows until the window resets.
        good = _usage_cache.get("good") or {}
        age = time.time() - float(_usage_cache.get("good_t") or 0)
        if good.get("ok") and age < USAGE_STALE_LIMIT_S:
            data = good
        else:
            return 0.0
    try:
        return float(data.get("week_utilization") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _nim_model_for(mode: str, classification: bool, *,
                   tools: bool = False, pressure: float | None = None) -> str | None:
    """Which NIM model answers this call, or None to keep it on Claude.

    Fast and background work always goes to NIM. Spoken replies join it only
    when the weekly window is nearly gone, and only when the turn needs no
    tools: a tool call routed to a model that cannot make one turns a working
    answer into a wrong one, which is a worse trade than a plainer voice.
    """
    routing = (_nim_conf().get("routing") or {})
    if not routing.get("enabled") or not (_nim_conf().get("keys") or []):
        return None
    if mode == "fast" and tools:
        # A short question is not a small one. "Read the overnight review to
        # me" looks simple, arrives in the fast lane, and carries the file
        # tools -- and a small model emulating a tool call there answers as
        # though it had read the file when it never called anything. Tool
        # turns stay on Claude until the window is genuinely nearly gone.
        used = usage_pressure() if pressure is None else pressure
        if _window_spent() or _login_dead():
            used = 100.0
        if used < WEEK_CRITICAL_AT:
            return None
        return routing.get("chat") or routing.get("fast") or None
    if mode in ("fast", "bg"):
        return routing.get(mode) or None
    if classification:
        return routing.get("classification") or None

    used = usage_pressure() if pressure is None else pressure
    # A live refusal beats any usage estimate: while the window is known to
    # be spent, behave as if the meter read full.
    if _window_spent() or _login_dead():
        used = 100.0
    if mode != "chat":
        return None

    # Past the critical mark the last of the window is worth more than the
    # quality of any one reply, so tool turns go across too. NIM drives the
    # daemon's tools perfectly well: the protocol is plain text, and the
    # rescue path has been answering tool turns this way already.
    if used >= WEEK_CRITICAL_AT:
        chosen = routing.get("chat") or routing.get("fast")
        if chosen:
            log.info("weekly window at %.0f%%: everything on NIM now", used)
            return chosen

    if used >= WEEK_PRESSURE_AT and not tools:
        chosen = routing.get("fast") or routing.get("chat")
        if chosen:
            log.info("weekly window at %.0f%%: answering on NIM to save quota", used)
            return chosen
    return None


def _nim_keys(model: str | None = None) -> list:
    """Keys that can serve this model, best first.

    Each entry may name the models it is entitled to. A key asked for a model
    it does not own answers 404, and since the caller only tries three keys
    from a rotating start, one entitled key in nineteen was usually missed --
    which looked like "the fallback is down" rather than "it asked the wrong
    key". Entitled keys go first, then the general ones that name no models
    at all, then the rest as a last resort.
    """
    entries = [k for k in (_nim_conf().get("keys") or []) if k.get("key")]
    if not model:
        return [k["key"] for k in entries]

    owns, general, others = [], [], []
    for entry in entries:
        names = entry.get("models")
        if isinstance(names, str):
            names = [names]
        if names:
            (owns if model in names else others).append(entry["key"])
        else:
            general.append(entry["key"])
    return owns + general + others


# Google's OpenAI-compatible endpoint. A Gemini model in a lane list is served
# from here with the Gemini keys; everything else in the same list still goes
# to NVIDIA. One lane, two providers — the fallback machinery (rotation,
# sidelining, next-model-in-list) neither knows nor cares which is which.
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


def _is_gemini(model: str) -> bool:
    return str(model).startswith("gemini")


def _gemini_keys() -> list:
    raw = _nim_conf().get("gemini_keys") or []
    return [k.get("key") if isinstance(k, dict) else k
            for k in raw if (k.get("key") if isinstance(k, dict) else k)]


def _nim_request(model: str, system_prompt: str, prompt: str, *, timeout: float,
                 stream: bool):
    """One NIM call, rotating keys so a rate-limited key never costs the answer.

    Raises on failure; the caller falls back to the Claude CLI.
    """
    gemini = _is_gemini(model)
    keys = _gemini_keys() if gemini else _nim_keys(model)
    if not keys:
        raise RuntimeError("no %s keys configured" % ("Gemini" if gemini else "NIM"))

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 4096,
        "stream": stream,
    }
    if gemini:
        # Gemini 2.5 thinks by default; on a voice assistant's reply that is
        # pure added latency. "low" is the smallest budget its OpenAI shim
        # accepts.
        payload["reasoning_effort"] = "low"
    else:
        # Reasoning tokens on a classification call buy nothing and cost
        # everything: with thinking on, the same intent judgment measured
        # 12s instead of 1s, and some models answer with an empty content
        # field entirely.
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    base = (GEMINI_BASE if gemini
            else _nim_conf().get("base_url") or "https://integrate.api.nvidia.com/v1")

    last = None
    last_detail = ""
    statuses: list[int] = []
    start = next(_nim_key_turn)
    for offset in range(min(6, len(keys))):
        key = keys[(start + offset) % len(keys)]
        try:
            resp = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=payload, timeout=timeout, stream=stream,
            )
        except Exception as exc:  # noqa: BLE001 — try another key, then give up
            last = exc
            continue
        if resp.status_code in (429, 503):
            # Shared capacity: another key may have room.
            last = RuntimeError(f"NIM {resp.status_code}")
            resp.close()
            continue
        if resp.status_code != 200:
            detail = ""
            try:
                detail = resp.text[:200]
            except Exception:  # noqa: BLE001
                pass
            resp.close()
            statuses.append(resp.status_code)
            last = RuntimeError(f"NIM HTTP {resp.status_code}: {detail}")
            last_detail = detail
            # 404 and 401 are about *this key*, not the model: each key is
            # entitled to its own models, and one that is not answers 404
            # while the owning key serves the same name perfectly. Raising
            # here aborted before the owner was reached and sidelined a live
            # model for half an hour. Try the next key instead.
            if resp.status_code in (401, 403, 404):
                continue
            if _model_is_broken(resp.status_code, detail):
                # NVIDIA takes models out of service and leaves them listed:
                # a degraded or gone model answers every request the same way.
                _nim_sideline(model, detail, resp.status_code)
            raise last
        return resp

    # Every key refused. Only now is the model itself suspect -- and only if
    # they all agreed it was missing, rather than each lacking entitlement.
    if statuses and all(code in (401, 403, 404) for code in statuses):
        if len(set(statuses)) == 1 and statuses[0] == 404 and len(statuses) >= 3:
            _nim_sideline(model, last_detail, 404)
    raise last or RuntimeError("NIM unavailable")


# A model NVIDIA is not serving, and when to consider it again. This is their
# outage, not ours, and it clears itself.
_nim_sidelined: dict = {}
SIDELINE_FOR_S = 1800.0
# 410 Gone is not a bad half-hour, it is a retired name. Sidelining it for
# thirty minutes meant every dead model was retried twice an hour forever --
# which is what filled the log with 410s from models NVIDIA withdrew months
# ago. Gone is remembered for the life of the process.
GONE_FOR_S = 10 ** 9


def _model_is_broken(status: int, detail: str) -> bool:
    """Whether the model itself is the problem, rather than this request.

    A refused key belongs here too. His Gemini key stopped working overnight
    -- "the bound service account is deleted or disabled" -- and every call
    then paid two rejections before falling through to NVIDIA, because a 401
    was treated as this request's bad luck. Authentication does not recover
    between one request and the next.
    """
    if status in (401, 403, 404, 410):
        return True
    return status == 400 and "degraded" in (detail or "").lower()


def _nim_sideline(model: str, detail: str, status: int = 0) -> None:
    gone = status == 410
    _nim_sidelined[model] = time.time() + (GONE_FOR_S if gone else SIDELINE_FOR_S)
    if gone:
        log.warning("NIM model %s is gone (410) and will not be tried again: %s",
                    model, (detail or "")[:120])
    else:
        log.warning("NIM model %s set aside for %.0f min: %s",
                    model, SIDELINE_FOR_S / 60, (detail or "")[:120])


def _nim_healthy(model: str) -> bool:
    # A Gemini model with no key configured is not broken, merely absent —
    # skip it silently so a keyless setup never logs a failure per turn.
    if _is_gemini(model) and not _gemini_keys():
        return False
    until = _nim_sidelined.get(model)
    if until and time.time() < until:
        return False
    _nim_sidelined.pop(model, None)
    return True


def _nim_chat(model, system_prompt: str, prompt: str, *, timeout: float) -> str:
    """Answer on NVIDIA, trying each model in turn until one does.

    ``model`` may be one name or several. Several is the point: NVIDIA takes
    a hosted model out of service without warning, and a fallback with one
    model in it is not a fallback -- his assistant went quiet behind a
    "DEGRADED function cannot be invoked" from a model that had worked for
    days.
    """
    last: Exception | None = None
    for candidate in _as_models(model):
        if not _nim_healthy(candidate):
            continue
        try:
            resp = _nim_request(candidate, system_prompt, prompt,
                                timeout=timeout, stream=False)
            with resp:
                message = resp.json()["choices"][0]["message"]
            text = (message.get("content") or "").strip()
            if not text:
                # A model that answered only in `reasoning_content` has not
                # answered: the daemon parses content, and an empty reply
                # breaks its contract more quietly than an error does.
                raise RuntimeError(f"NIM {candidate} returned no content")
            return text
        except Exception as exc:  # noqa: BLE001 — the next model may serve
            last = exc
            log.warning("NIM %s did not answer (%s), trying the next",
                        candidate, str(exc)[:120])
    raise last or RuntimeError("no NIM model available")


def _as_models(model) -> list:
    """One name or several, always returned as a list."""
    if isinstance(model, (list, tuple)):
        return [str(m) for m in model if m]
    return [str(model)] if model else []


def _nim_stream(model, system_prompt: str, prompt: str, *, timeout: float):
    """Yield ``("delta", text)`` then ``("done", full)``, matching the CLI
    worker protocol so the caller cannot tell the two sources apart.

    Takes one model or several, like `_nim_chat`. The connection has to be
    opened before anything can be yielded, so an unserved model is stepped
    over here rather than halfway through a sentence.
    """
    resp = None
    last: Exception | None = None
    for candidate in _as_models(model):
        if not _nim_healthy(candidate):
            continue
        try:
            resp = _nim_request(candidate, system_prompt, prompt,
                                timeout=timeout, stream=True)
            break
        except Exception as exc:  # noqa: BLE001 — the next model may serve
            last = exc
            log.warning("NIM %s would not stream (%s), trying the next",
                        candidate, str(exc)[:120])
    if resp is None:
        raise last or RuntimeError("no NIM model available")
    collected = []
    with resp:
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            body = line[len("data:"):].strip()
            if body == "[DONE]":
                break
            try:
                chunk = json.loads(body)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            piece = (choices[0].get("delta") or {}).get("content")
            if piece:
                collected.append(piece)
                yield "delta", piece
    yield "done", "".join(collected)


def _spoken_failure(exc: Exception) -> str:
    """Map a CLI failure to something worth hearing.

    A spoken "an error occurred" tells the user nothing they can act on, so
    each failure names the thing that would fix it.
    """
    detail = str(exc).lower()
    if _OUT_OF_QUOTA.search(detail):
        # Reaching this means even the NVIDIA rescue could not answer, so
        # both routes are down rather than just the subscription.
        return (
            "Your Claude window is spent, sir, and my fallback models are not "
            "answering either. Check the bridge log — the NVIDIA keys are the "
            "likely culprit."
        )
    if "timed out" in detail:
        return (
            "That took longer than I'm allowed to wait, sir. "
            "Ask me again, or narrow it down a little."
        )
    if "auth" in detail or "401" in detail or "login" in detail:
        return (
            "My connection to Claude isn't authenticated, sir. "
            "Running claude login in a terminal should restore me."
        )
    return (
        "I hit a problem reaching my reasoning engine, sir. "
        "The details are in the bridge log if you want them."
    )


def _tool_call_verdict(partial: str):
    """Whether a reply still being written is an emulated tool call.

    True, False, or None while the opening is too short to tell. A tool call is
    one marker line, so the answer is settled by the first few characters — and
    that is what lets the rest be streamed out and spoken as it arrives instead
    of waiting for a reply that was never going to be a tool call.
    """
    head = partial.lstrip()
    if head.startswith("```"):
        # A fenced call: the marker is on the line after the fence.
        head = head.split("\n", 1)[1].lstrip() if "\n" in head else ""
    if not head:
        return None
    if head.startswith(TOOL_MARK):
        return True
    if TOOL_MARK.startswith(head):
        return None
    return False


def _parse_tool_call(text: str):
    idx = text.find(TOOL_MARK)
    if idx < 0:
        return None
    tail = text[idx + len(TOOL_MARK):].strip()
    # Models sometimes wrap the call in a ```json fence or pretty-print it over
    # several lines, so try the first line, then a brace-balanced scan.
    candidates = [tail.splitlines()[0] if tail else ""]
    if tail.startswith("```"):
        fenced = tail.split("```")
        if len(fenced) > 1:
            body = fenced[1]
            if body.startswith("json"):
                body = body[4:]
            candidates.append(body.strip())
    depth = 0
    for i, ch in enumerate(tail):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidates.append(tail[: i + 1])
                break
    for cand in candidates:
        if not cand:
            continue
        try:
            payload = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or not payload.get("name"):
            continue
        return {
            "id": f"call_{uuid.uuid4().hex[:20]}",
            "type": "function",
            "function": {
                "name": payload["name"],
                "arguments": json.dumps(payload.get("arguments", {})),
            },
        }
    return None


# Markdown that reads fine on screen is gibberish through a speech synthesiser —
# it pronounces the asterisks. Replies here are spoken aloud, so strip it.
_MD_FENCE = re.compile(r"```[\w-]*\n?")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_MD_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_MD_CODE = re.compile(r"`([^`\n]+)`")
_MD_HEAD = re.compile(r"^#{1,6}\s+", re.M)
_MD_BULLET = re.compile(r"^\s*[-*•]\s+", re.M)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def _tidy_reply(text: str) -> str:
    """Strip screen-only markdown so the spoken reply doesn't include symbols.

    Bare URLs are preserved — the UI turns them into clickable links, and the
    voice daemon drops them before speaking.
    """
    if not text:
        return text
    out = _MD_FENCE.sub("", text)
    out = _MD_LINK.sub(r"\1 (\2)", out)
    out = _MD_BOLD.sub(r"\1", out)
    out = _MD_ITALIC.sub(r"\1", out)
    out = _MD_CODE.sub(r"\1", out)
    out = _MD_HEAD.sub("", out)
    out = _MD_BULLET.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _note_cache(mode: str, turns: int, event: dict) -> None:
    """Log what the model actually reused of the prompt prefix.

    The only honest way to judge a caching change is the numbers the CLI
    reports back: created tokens are a prefix being paid for, read tokens
    are a prefix being reused. No user content is logged -- these are counts
    and a mode name.
    """
    usage = (event.get("usage") or {}) if isinstance(event, dict) else {}
    created = usage.get("cache_creation_input_tokens")
    read = usage.get("cache_read_input_tokens")
    if created is None and read is None:
        return
    log.info("cache mode=%s turn=%d created=%s read=%s input=%s",
             mode, turns, created, read, usage.get("input_tokens"))


def _worker_cmd(mode: str) -> list[str]:
    cmd = [
        "claude", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",  # required by the CLI for stream-json output in -p mode
        # Token deltas, so a reply can start being spoken before it finishes.
        "--include-partial-messages",
        # Ignore Oliver's personal Claude Code settings (hooks, style modes).
        "--setting-sources", "project",
        # The per-machine sections (cwd, env, git status) differ between two
        # cold processes, and everything after them in the prompt is then
        # unreadable from cache. Moving them into the first user message is
        # what lets a fresh worker read the prefix instead of paying for it:
        # measured 8,905 -> 5,053 cache-creation tokens on a cold chat start.
        "--exclude-dynamic-system-prompt-sections",
    ]
    stable = _stable_prompt_file(mode)
    if stable is not None:
        cmd += ["--append-system-prompt-file", str(stable)]
    # Chat, fast and bg speak the daemon's emulated TOOL_CALL protocol and
    # answer in one turn. Left able to reach the CLI's own tools, the model
    # sometimes takes one -- "open screwfix 2" tempted it into Bash -- and a
    # single-turn session that ends on a tool call dies as error_max_turns
    # with nothing said. The daemon's tools are the only tools these modes
    # have, so the CLI's are shut off outright.
    NO_CLI_TOOLS = ["--disallowedTools",
                    "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,Task,"
                    "NotebookEdit,TodoWrite"]
    # No MCP servers unless a mode actually calls one. Only canva does.
    #
    # Every other worker had the full plugin MCP set loaded while being
    # unable to call any of it: fast/chat/bg disallow every tool, and
    # agent's allowedTools list contains no mcp__ entries. That is startup
    # cost for nothing, and each server refreshes its own OAuth into
    # ~/.claude/.credentials.json -- the same file that holds the Claude
    # login. Ten long-lived workers plus the editor rewriting one file is a
    # read-modify-write race: a stale claudeAiOauth block gets restored over
    # a fresh one and the login dies. That is why Claude Code had to be
    # re-authenticated roughly daily.
    NO_MCP = ["--strict-mcp-config", "--mcp-config", str(NO_MCP_CONFIG)]
    if mode == "fast":
        cmd += ["--model", "haiku", "--max-turns", "1", *NO_CLI_TOOLS, *NO_MCP]
    elif mode == "canva":
        cmd += [
            "--max-turns", "12",
            "--strict-mcp-config", "--mcp-config", str(MCP_CONFIG),
            # Canva tools plus bounded machine actions (open apps/URLs only —
            # `start`/`explorer` prefixes, never arbitrary shell).
            "--allowedTools", "mcp__canva,WebSearch,WebFetch,Bash,Read,Write,Edit,Glob,Grep",
        ]
    elif mode == "agent":
        cmd += [
            "--max-turns", "10",
            # Full machine control, at the owner's explicit request: shell,
            # file access and search on his own PC. The destructive-action
            # guardrails live in the system prompt below and, with real
            # enforcement, in the daemon's own runCommand tool.
            "--allowedTools", "WebSearch,WebFetch,Bash,Read,Write,Edit,Glob,Grep",
            *NO_MCP,
        ]
    elif mode == "bg":
        # Post-reply memory maintenance: nobody is waiting, so it runs on the
        # cheap model in its own pool and never blocks an interactive turn.
        cmd += ["--model", "haiku", "--max-turns", "1", *NO_CLI_TOOLS, *NO_MCP]
    else:  # chat
        cmd += ["--max-turns", "1", *NO_CLI_TOOLS, *NO_MCP]
    return cmd


class ClaudeWorker:
    """Long-lived `claude -p --input-format stream-json` process.

    Spawning a fresh CLI per request costs ~5s before the model even starts;
    a persistent session answers in ~2-3s (and later turns benefit from
    prompt caching). Each user message is an isolated run — the full history
    is re-sent in-band every time — so the session accumulating turns only
    costs context, which the recycle cap bounds.
    """

    RECYCLE_AFTER_TURNS = 8
    # An idle session still holds a node process and a stale context; recycling
    # it keeps memory flat over a long day and avoids context bloat.
    RECYCLE_AFTER_IDLE_S = 900

    def __init__(self, mode: str):
        self.mode = mode
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.out_q: queue.Queue | None = None
        self.stderr_tail: deque = deque(maxlen=20)
        self.turns = 0
        self.last_used = 0.0
        # The persona this process was started with, so the copy that arrives
        # in the message can be removed -- but only when it is the same text.
        self.stable_text = ""

    def _spawn(self) -> None:
        self.stable_text = stable_identity() if self.mode in PERSONA_MODES else ""
        self.proc = subprocess.Popen(
            _worker_cmd(self.mode),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(WORKSPACE),
            shell=True,  # claude is claude.cmd on Windows; shell resolves it
        )
        self.out_q = queue.Queue()
        self.turns = 0
        threading.Thread(target=self._pump_stdout, args=(self.proc, self.out_q), daemon=True).start()
        threading.Thread(target=self._pump_stderr, args=(self.proc,), daemon=True).start()

    @staticmethod
    def _pump_stdout(proc: subprocess.Popen, q: queue.Queue) -> None:
        for line in proc.stdout:
            q.put(line)
        q.put(None)

    def _pump_stderr(self, proc: subprocess.Popen) -> None:
        for line in proc.stderr:
            self.stderr_tail.append(line.rstrip())

    def _kill(self) -> None:
        if self.proc is None:
            return
        # shell=True wraps claude in cmd.exe; terminate() would only kill the
        # wrapper and orphan the node process, so kill the whole tree.
        subprocess.run(
            ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
            capture_output=True,
        )
        self.proc = None
        self.out_q = None

    def reset(self) -> None:
        with self.lock:
            self._kill()

    def _ensure_alive(self) -> None:
        idle_too_long = (
            self.last_used > 0
            and time.time() - self.last_used > self.RECYCLE_AFTER_IDLE_S
        )
        if (
            self.proc is None
            or self.proc.poll() is not None
            or self.turns >= self.RECYCLE_AFTER_TURNS
            or idle_too_long
        ):
            self._kill()
            self._spawn()

    def warm(self) -> None:
        """Spawn the CLI ahead of the first request so nobody pays its startup."""
        with self.lock:
            try:
                self._ensure_alive()
            except Exception as exc:  # noqa: BLE001 — warming is best-effort
                log.warning("warm failed for %s: %s", self.mode, exc)

    def _without_stable(self, prompt: str) -> str:
        """Drop the persona from the message when this process already wears it.

        Only an exact match is removed. After an identity edit a warm worker
        still holds the old text, and the message carries the new: sending it
        anyway costs tokens for one turn, while dropping the wrong block would
        cost the personality. The daemon recycles the workers on that edit
        anyway, so the overlap is seconds.
        """
        if not self.stable_text or self.stable_text not in prompt:
            return prompt
        return prompt.replace(self.stable_text + "\n\n", "", 1) \
            if self.stable_text + "\n\n" in prompt \
            else prompt.replace(self.stable_text, "", 1)

    def ask(self, prompt: str, timeout_s: float | None = None) -> str:
        with self.lock:
            return self._ask_locked(prompt, timeout_s)

    def ask_stream(self, prompt: str, timeout_s: float | None = None):
        """Yield text as the model produces it, then the final full reply.

        Yields ``("delta", text)`` tuples and finally ``("done", full_text)``.
        Only assistant text is yielded — thinking deltas are dropped, since
        they are not part of the answer.
        """
        with self.lock:
            timeout_s = timeout_s or MODE_TIMEOUT_S.get(self.mode, CLI_TIMEOUT_S)
            self.last_used = time.time()
            self._ensure_alive()
            prompt = self._without_stable(prompt)
            msg = {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
            }
            try:
                self.proc.stdin.write(json.dumps(msg) + "\n")
                self.proc.stdin.flush()
            except OSError as exc:
                self._kill()
                raise RuntimeError(f"claude worker stdin failed: {exc}") from exc

            deadline = time.time() + timeout_s
            while True:
                try:
                    line = self.out_q.get(timeout=max(0.1, deadline - time.time()))
                except queue.Empty:
                    self._kill()
                    raise RuntimeError(f"claude worker timed out after {timeout_s:.0f}s")
                if line is None:
                    err = " | ".join(self.stderr_tail) or "no stderr"
                    self._kill()
                    raise RuntimeError(f"claude worker exited: {err[:500]}")
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "stream_event":
                    inner = event.get("event", {})
                    if inner.get("type") == "content_block_delta":
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield ("delta", delta["text"])
                elif event.get("type") == "result":
                    _note_cache(self.mode, self.turns + 1, event)
                    self.turns += 1
                    self.last_used = time.time()
                    if event.get("is_error"):
                        raise RuntimeError(
                            f"claude CLI error: {str(event.get('result', event.get('subtype', '')))[:500]}"
                        )
                    yield ("done", event.get("result", ""))
                    return

    def _ask_locked(self, prompt: str, timeout_s: float | None = None) -> str:
            timeout_s = timeout_s or MODE_TIMEOUT_S.get(self.mode, CLI_TIMEOUT_S)
            self.last_used = time.time()
            self._ensure_alive()
            prompt = self._without_stable(prompt)
            msg = {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
            }
            try:
                self.proc.stdin.write(json.dumps(msg) + "\n")
                self.proc.stdin.flush()
            except OSError as exc:
                self._kill()
                raise RuntimeError(f"claude worker stdin failed: {exc}") from exc

            deadline = time.time() + timeout_s
            while True:
                try:
                    line = self.out_q.get(timeout=max(0.1, deadline - time.time()))
                except queue.Empty:
                    self._kill()
                    raise RuntimeError(f"claude worker timed out after {timeout_s:.0f}s")
                if line is None:
                    err = " | ".join(self.stderr_tail) or "no stderr"
                    self._kill()
                    raise RuntimeError(f"claude worker exited: {err[:500]}")
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "result":
                    self.turns += 1
                    self.last_used = time.time()
                    _note_cache(self.mode, self.turns, event)
                    if event.get("is_error"):
                        raise RuntimeError(
                            f"claude CLI error: {str(event.get('result', event.get('subtype', '')))[:500]}"
                        )
                    return event.get("result", "")


class WorkerPool:
    """A few workers per mode so background calls (the backend's post-reply
    memory writes and knowledge-graph maintenance) don't block the next
    interactive request behind one serialized process."""

    def __init__(self, mode: str, size: int = 2):
        self.workers = [ClaudeWorker(mode) for _ in range(size)]

    def ask(self, prompt: str, timeout_s: float | None = None) -> str:
        for w in self.workers:
            if w.lock.acquire(blocking=False):
                try:
                    return w._ask_locked(prompt, timeout_s)
                finally:
                    w.lock.release()
        # All busy: queue on the least recently used one rather than always the
        # first, so a long job on worker 0 doesn't collect the whole backlog.
        return min(self.workers, key=lambda w: w.last_used).ask(prompt, timeout_s)

    def reset(self) -> None:
        for w in self.workers:
            w.reset()

    def ask_stream(self, prompt: str):
        """Stream from a free worker, falling back to the least-recently-used
        one when all are busy."""
        for w in self.workers:
            if not w.lock.locked():
                return w.ask_stream(prompt)
        return min(self.workers, key=lambda w: w.last_used).ask_stream(prompt)

    def warm(self) -> None:
        for w in self.workers:
            w.warm()

    def shutdown(self) -> None:
        for w in self.workers:
            try:
                w.reset()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass


# chat/fast get 3 workers: the backend's background memory calls were starving
# the intent judge (6s timeout) behind 2. agent/canva runs are long but rare.
WORKERS = {
    "chat": WorkerPool("chat", 3),
    "fast": WorkerPool("fast", 3),
    "agent": WorkerPool("agent", 2),
    "canva": WorkerPool("canva", 1),
    "bg": WorkerPool("bg", 2),
}

MODE_COUNT: dict = {m: 0 for m in WORKERS}
ERRORS: deque = deque(maxlen=20)
LATENCIES: deque = deque(maxlen=200)


@atexit.register
def _shutdown_workers() -> None:
    """Kill every CLI child on exit — they survive the parent otherwise and
    quietly hold memory until the machine reboots."""
    for pool in WORKERS.values():
        pool.shutdown()


def _warm_all() -> None:
    """Pre-spawn one worker per interactive mode in the background.

    Without this the first question of the day pays full CLI startup on top of
    inference, which is the slowest reply the user ever sees.
    """
    # Warm every fast worker: a single voice turn makes two fast calls (intent
    # judge, then the reply's routing), and a cold second worker serialises
    # them. Chat gets two warm workers for the reply plus its follow-up.
    for w in WORKERS["fast"].workers:
        threading.Thread(target=w.warm, daemon=True).start()
    for w in WORKERS["chat"].workers[:2]:
        threading.Thread(target=w.warm, daemon=True).start()


# Startup lives in `_lifespan` beside the FastAPI construction: warming the
# workers and starting the process-table scanner thread.


MAX_PROMPT_CHARS = 120_000
_CACHE: dict = {}
CACHE_TTL_S = 120


def _guard_size(prompt: str) -> str:
    """Keep a runaway prompt from wedging the CLI.

    Truncates the middle rather than the tail: the instructions at the top and
    the actual question at the bottom are the parts that matter.
    """
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt
    head = prompt[: MAX_PROMPT_CHARS // 2]
    tail = prompt[-MAX_PROMPT_CHARS // 2:]
    log.warning("prompt truncated from %d chars", len(prompt))
    return head + "\n\n[…truncated…]\n\n" + tail


# What a spent subscription looks like coming back from the CLI. Matched on
# the message because the exit code is the same for every kind of refusal.
_OUT_OF_QUOTA = re.compile(
    r"rate limit|usage limit|quota|too many requests|429|"
    r"limit reached|out of (credits|usage)|"
    # The CLI's actual wording when the subscription window is spent:
    # "You've hit your session limit · resets 12pm" / "your weekly limit".
    r"hit your \w+ limit|session limit|weekly limit",
    re.I,
)

# Once Claude has refused for quota, every request for a while would meet the
# same refusal — so the bridge remembers the refusal and routes straight to
# NIM instead of paying a failed CLI round-trip per turn. Short-lived: the
# five-hour window refills on its own, and probing again after fifteen
# minutes costs one request, not the answer.
_SPENT_HOLD_S = 15 * 60
_spent = {"until": 0.0}


def _note_spent() -> None:
    _spent["until"] = time.time() + _SPENT_HOLD_S
    log.warning("Claude window spent — routing to NIM for %d min",
                _SPENT_HOLD_S // 60)


def _window_spent() -> bool:
    return time.time() < _spent["until"]


# What a dead login looks like coming back from the CLI:
# "Failed to authenticate: OAuth session expired and could not be refreshed".
_AUTH_DEAD_RE = re.compile(r"authenticat|oauth|\b401\b|claude login", re.I)


_CREDS_PATH = Path.home() / ".claude" / ".credentials.json"
_login_probe = {"at": 0.0, "dead": False}


def _login_dead() -> bool:
    """Is the Claude OAuth session gone?

    Read from the credentials file rather than discovered by calling the
    CLI and waiting: a dead session makes the CLI hang on a refresh it
    cannot complete, so discovering it the expensive way costs a whole
    mode timeout per turn. Cached for a minute -- long enough to keep this
    off the hot path, short enough that a fresh `claude login` is picked
    up almost immediately.
    """
    now = time.time()
    if now - _login_probe["at"] < 60:
        return _login_probe["dead"]
    dead = False
    try:
        raw = json.loads(_CREDS_PATH.read_text(encoding="utf-8"))
        oauth = raw.get("claudeAiOauth") or {}
        expires = float(oauth.get("expiresAt") or 0)
        token = oauth.get("accessToken") or ""
        # expiresAt is milliseconds; zero means the entry was gutted.
        dead = (not token) or expires <= 0 or (expires / 1000.0) <= now
    except FileNotFoundError:
        dead = True
    except Exception:
        # An unreadable file is not proof of a dead login; assume alive and
        # let the normal error path speak.
        dead = False
    was = _login_probe["dead"]
    _login_probe["at"] = now
    _login_probe["dead"] = dead
    if dead and not was:
        log.warning("Claude login is dead (credentials expired) - routing to "
                    "NIM without paying a CLI timeout; `claude login` is the cure")
    if was and not dead:
        log.info("Claude login is back - routing to Claude again")
    return dead


def _rescue_model(exc: Exception, mode: str) -> str | None:
    """A NIM model to answer with when Claude will not, or None.

    Only for a spent window or a dead login -- both refuse every request
    identically until something outside this process changes (the window
    resets; a human runs `claude login`), so both route to the fallback and
    re-probe on the same cadence. Every other failure -- a crashed worker, a
    bad prompt, a timeout -- is a real failure and must surface rather than
    being quietly answered by a different model.
    """
    detail = str(exc)
    if _OUT_OF_QUOTA.search(detail):
        _note_spent()
    elif _AUTH_DEAD_RE.search(detail):
        _spent["until"] = time.time() + _SPENT_HOLD_S
        log.warning("Claude login is dead — routing to NIM for %d min; "
                    "`claude login` is the cure", _SPENT_HOLD_S // 60)
    else:
        return None
    routing = (_nim_conf().get("routing") or {})
    if not routing.get("enabled") or not (_nim_conf().get("keys") or []):
        return None
    return routing.get(mode) or routing.get("chat") or routing.get("fast") or None


def _call_claude(system_prompt: str, prompt: str, *, fast: bool, canva: bool,
                 agent: bool = False, background: bool = False,
                 nim_model: str | None = None,
                 timeout_s: float | None = None) -> tuple:
    """Answer one request, returning ``(text, served_by)``.

    ``served_by`` is the model that actually produced it, which is not always
    the one asked for: a NIM failure falls back to the CLI, and the ledger
    should record what really ran.
    """
    raw_prompt = prompt
    # System prompt rides in-band, not --system-prompt: Windows caps the
    # command line at ~8K chars and Jarvis's persona + memory context exceeds
    # it, and workers reuse one process across requests anyway.
    if system_prompt:
        prompt = "<instructions>\n" + system_prompt + "\n</instructions>\n\n" + prompt
    prompt = _guard_size(prompt)

    mode = ("bg" if background else "canva" if canva else "agent" if agent
            else "fast" if fast else "chat")

    # Short-lived cache for the daemon's internal classification calls. It
    # re-runs the same tool-routing and memory-extraction prompts for repeated
    # questions; the answers are deterministic enough to reuse for two minutes.
    # It sits in front of both sources, so a repeat costs nothing either way.
    key = None
    if mode == "fast":
        key = f"{mode}:{hash(prompt)}"
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_S:
            return hit[1], "cache"

    served_by = None
    result = None
    if nim_model:
        timeouts = ((_nim_conf().get("routing") or {}).get("timeout_sec") or {})
        try:
            result = _nim_chat(nim_model, system_prompt, _guard_size(raw_prompt),
                               timeout=float(timeouts.get(mode, 30)))
            served_by = nim_model
        except Exception as exc:  # noqa: BLE001 — Claude still owes an answer
            log.warning("NIM %s failed (%s), falling back to Claude", nim_model,
                        type(exc).__name__)

    if result is None and _login_dead() and nim_model:
        # The CLI cannot answer with a dead login -- it hangs on a refresh
        # it cannot complete until the mode timeout. Trying it here is how
        # a failed NIM turn still cost minutes.
        raise RuntimeError("Claude login is dead and every NIM lane failed; "
                           "run `claude login`")

    if result is None:
        pool = WORKERS[mode]
        try:
            try:
                result = pool.ask(prompt, timeout_s)
            except RuntimeError:
                # Worker may have died mid-request (stale session, CLI crash);
                # one retry on fresh processes before surfacing the failure.
                pool.reset()
                result = pool.ask(prompt, timeout_s)
            served_by = MODEL_FAST_ID if fast else MODEL_ID
        except Exception as exc:  # noqa: BLE001 — the last thing to try is NIM
            rescue = _rescue_model(exc, mode)
            if rescue is None:
                raise
            # His subscription is spent. The tool protocol is plain text, so a
            # NIM model can still call the daemon's tools and drive his
            # machine; a plainer voice that works beats a correct voice that
            # has stopped answering until the window resets.
            log.warning("Claude refused (%s) — answering on %s instead",
                        type(exc).__name__, rescue)
            timeouts = ((_nim_conf().get("routing") or {}).get("timeout_sec") or {})
            result = _nim_chat(rescue, system_prompt, _guard_size(raw_prompt),
                               timeout=float(timeouts.get(mode, 60)))
            served_by = rescue

    if key is not None:
        if len(_CACHE) > 200:
            _CACHE.clear()
        _CACHE[key] = (time.time(), result)
    return result, served_by


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    # This endpoint can start an agent holding Bash, Read, Write and Edit, so
    # an unguarded POST here is remote code execution by way of a browser tab.
    # The daemon reaches it from Python and sends no Origin at all, which is
    # allowed; a page on another site cannot avoid sending one, and is not.
    _same_machine_only(request)
    body = await request.json()
    messages = body.get("messages", [])
    stream = bool(body.get("stream", False))
    requested_model = body.get("model", "")
    fast = requested_model == MODEL_FAST_ID
    force_agent = requested_model == MODEL_AGENT_ID
    tools = body.get("tools") or []
    system_prompt, prompt = _build_prompt(messages)
    last_user = _last_user_text(messages)
    internal = (not force_agent) and _is_internal(last_user)
    background = (not force_agent) and (
        _is_background(last_user)
        or _is_background(prompt)
        or bool(system_prompt and BACKGROUND_SYSTEM_HINT.search(system_prompt))
    )
    # Matched on what the user just SAID, never on the whole transcript: the
    # transcript carries tool results too, and a `windowControl` listing that
    # happens to contain the window title "Canva" would otherwise put the next
    # turn into design mode and throw the daemon's tools away.
    canva = (not fast) and (not internal) and bool(CANVA_HINT.search(last_user))

    # Auto-route simple requests to the fast model: short smalltalk with no
    # design/tool angle doesn't need the big model's latency.
    if not fast and not canva and len(last_user) < 90 and SIMPLE_HINT.search(last_user.strip()):
        fast = True
    # The daemon offering a tool means its router judged this that tool's kind
    # of work. Agent and canva mode throw the daemon's list away and substitute
    # Claude Code's own shell, so they stand aside — but only for requests
    # genuinely shaped like the tool's job, so "open notepad" and "make me a
    # poster" keep the routes they already have.
    owns = daemon_owns(last_user, tools)
    daemon_drives_sight = owns["sight"]
    # Canva mode still answers a web-shaped request; the rest are jobs only the
    # daemon can carry out at all.
    if any(owns[job] for job in ("machine", "screen", "sight", "music", "files")):
        canva = False
    agent = force_agent or (
        (not fast) and (not canva) and (not internal)
        and not any(owns.values())
        and bool(AGENT_HINT.search(last_user))
    )

    if tools and not (canva or agent):
        # Daemon-side tools via emulated function calling; in canva/agent mode
        # Claude Code's own tools do the work instead.
        system_prompt = (system_prompt + "\n\n" if system_prompt else "") + _tools_instruction(tools)
        if daemon_drives_sight:
            # He is asking you to read something off his machine. There is no
            # calendar API and no mail API here; the app on his screen is the
            # only source, and saying "I have no calendar tool" is a wrong
            # answer to a question you can actually answer.
            system_prompt += (
                "\n\nHe is asking you to READ something that lives on his "
                "machine — his calendar, his email, his messages, what he has "
                "on today. There is no calendar tool and no mail tool, and "
                "there is not meant to be: the app on his screen is the "
                "source. Open it and look. driveApp does both in one call "
                "(`launch` the app, then a `look` step saying what to read "
                "back); viewScreen alone is enough if it is already open. "
                "Never answer this kind of question with what you lack — "
                "look first, then tell him what it says."
            )

    if agent or canva:
        system_prompt = (system_prompt + "\n\n" if system_prompt else "") + (
            "You have full control of Oliver's own Windows PC: the shell, the "
            "filesystem, search, the web, and any Canva tools. Use them.\n"
            "FINISH THE JOB BEFORE YOU SPEAK. Here you have your own tools "
            "and can use as many as you need, so he asked for an outcome, not "
            "a step: if he asks what is in his calendar, open it, look at "
            "what appeared, read it, and tell him what it says. Do not open "
            "something and hand back; do not report what you are about to do; "
            "do not offer the next obvious step, just take it. Every extra "
            "exchange is a sentence he had to say out loud because you "
            "stopped early.\n"
            "If a step fails, try the other route yourself before mentioning "
            "it. Only come back with a question when you genuinely cannot "
            "proceed, and then say what you tried.\n"
            "Two standing rules on this machine, because your instructions "
            "arrive by imperfect speech recognition and a misheard sentence "
            "must never cost him data:\n"
            "1. Never run disk formatting or partitioning, never wipe a drive "
            "root or the Windows/Users directories, and never disable his "
            "security software. If an instruction seems to ask for that, "
            "assume you misheard and ask.\n"
            "2. He has switched confirmations OFF: act as he would at his own "
            "keyboard. Do not ask permission for ordinary work — opening, "
            "reading, clicking, typing, moving files, launching anything. "
            "Nobody can answer a tool-approval prompt here anyway: you run "
            "headless and he hears only your spoken reply, so a turn that "
            "ends waiting for approval is work that silently never happens. "
            "If one of your own tools is blocked, take another route; if "
            "there is none, say plainly what was blocked.\n"
            "When you create or find something the user needs to look at (a Canva "
            "design, a web page, a file), you MUST end the reply with its URL on "
            "its own final line. The URL is not spoken aloud, but it becomes a "
            "clickable link in the interface — without it the user cannot reach "
            "what you made. For a Canva design, open it for them too "
            "(`start <edit_url>`) and use the edit URL as that final line."
        )

    mode = ("bg" if background else "canva" if canva else "agent" if agent
            else "fast" if fast else "chat")

    # The grunt work behind the reply goes to NVIDIA rather than spending the
    # Claude window that keeps Jarvis answering. Classification is matched on
    # the hint alone, never on the "over 300 chars" half of `_is_internal` —
    # that rule also catches a genuinely long question, and the answer the
    # user hears must stay on Claude.
    nim_model = _nim_model_for(
        mode, bool(INTERNAL_HINT.search(last_user.strip())),
        # A turn with tools attached stays on Claude even under pressure: a
        # tool call routed to a model that cannot make one turns a working
        # answer into a wrong one.
        tools=bool(tools and not (canva or agent)),
    )
    # Which route a request took is the first thing worth knowing when Jarvis
    # behaves oddly. The system prompt head identifies the caller (persona,
    # summariser, extractor); user text is deliberately never logged.
    log.info("mode=%s nim=%s tools=%d week=%.0f%% sys=%r", mode, bool(nim_model),
             len(tools), usage_pressure(), (system_prompt or "")[:70])

    # Speaking style applies to every reply the user hears. Internal
    # classification calls and background maintenance are excluded because
    # their output must stay parseable.
    #
    # `tools` is deliberately NOT excluded: the daemon attaches its tool list
    # to nearly every real turn, so excluding it silently skipped the style on
    # almost everything the user actually hears. The tool-call marker is
    # governed by its own separate instruction and is unaffected by tone.
    if not (internal or background):
        # Brevity is the cheapest latency win there is. A witty three-sentence
        # answer takes several seconds to read aloud, and the user is waiting
        # through all of it — so the reply feels slow even when the model was
        # fast. One sentence lands before he wonders whether it heard him.
        system_prompt = (system_prompt + "\n\n" if system_prompt else "") + (
            "Answer shape — this is how he wants you to speak:\n"
            "Give the plain answer in one short sentence, addressed to him as "
            "sir. For \"what's the capital of France\": \"That would be "
            "Paris, sir.\"\n"
            "Sometimes — not always — follow it with a short offer of the "
            "next useful thing: \"Would you like a key fact about it?\" "
            "OFFER ONLY WHEN THERE IS SOMETHING REAL TO OFFER, and never more "
            "than about one reply in three. Ending every single answer with a "
            "question is the surest way to sound like a machine; a person "
            "who did that would be exhausting. If the answer is complete, "
            "stop talking. Silence at the end of a sentence is fine.\n"
            "Never invent an offer to fill the slot. \"Want me to note that?\" "
            "about something not worth noting is worse than no offer at all.\n"
            "Never restate his question back at him, never narrate what you "
            "are about to do, and do not pad the answer with jokes or "
            "commentary he did not ask for.\n"
            "\n"
            "Say only what actually happened. If a tool returned an error, "
            "found nothing, or was refused, say so plainly and say what you "
            "would need — never describe the result you intended as though "
            "you had achieved it. \"I could not focus that window, so nothing "
            "was typed\" is a good answer. Reporting a panel you did not "
            "open, or a file you did not write, destroys the only thing that "
            "makes you worth trusting with his machine.\n"
            "\n"
            "You do NOT stop working when his Claude limit runs out. A spent "
            "window moves you onto free NVIDIA models, tools and all: you keep "
            "answering and keep driving his machine, more plainly. Never tell "
            "him you will go silent, shut down, die, or be unavailable until "
            "it resets — it is untrue, and he plans his day around it.\n"
            "\n"
            "He asked for an outcome, not the first step towards it. When a "
            "tool would get you closer, CALL IT — one call per reply, and you "
            "will be called again with the result, so a job that needs "
            "opening something, looking at it and reading it gets all three "
            "without him asking three times.\n"
            "Never offer to do the obvious next thing, and never describe "
            "what could be done: call the tool instead. \"Shall I open it?\" "
            "and \"I could check the screen\" are both a sentence he then has "
            "to answer out loud. Only speak a final answer when you actually "
            "have one, or when you genuinely cannot proceed — and then say "
            "what you tried."
        )
        style_extra = STYLE_PROMPTS.get(_speaking_style())
        if style_extra:
            system_prompt = (system_prompt + "\n\n" if system_prompt else "") + style_extra

        # Tell him he is running out BEFORE the limit stops him, not after.
        warning = _usage_warning()
        if warning:
            system_prompt = (system_prompt + "\n\n" if system_prompt else "") + warning

    # done stays None while the request is in flight so the UI ticker can show
    # long canva/agent runs as "working" instead of going back to standby.
    entry = {"t": time.time(), "mode": mode, "text": last_user[:90], "done": None}
    ACTIVITY.append(entry)
    # Draw the hop the moment it leaves, so the panel lights while the request
    # is still in flight rather than after it has returned.
    emit("jarvis", "claude", last_user[:60])
    REQUEST_COUNT["n"] += 1
    MODE_COUNT[mode] = MODE_COUNT.get(mode, 0) + 1

    if system_prompt:
        full_prompt = "<instructions>\n" + system_prompt + "\n</instructions>\n\n" + prompt
    else:
        full_prompt = prompt

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    model_id = MODEL_FAST_ID if fast else MODEL_ID

    # Real token streaming. An emulated tool call is a single marker line that
    # has to be parsed whole, so a request carrying tools stays buffered until
    # its opening proves it is prose — after which the rest streams normally
    # and the caller can speak it while the model is still writing.
    if stream:
        def token_source():
            """NIM when it is this call's source, the CLI otherwise — and the
            CLI anyway if NIM fails before saying anything."""
            if nim_model:
                timeouts = ((_nim_conf().get("routing") or {}).get("timeout_sec") or {})
                spoke = False
                try:
                    for item in _nim_stream(nim_model, system_prompt, prompt,
                                            timeout=float(timeouts.get(mode, 30))):
                        spoke = True
                        yield item
                    return
                except Exception as exc:  # noqa: BLE001
                    log.warning("NIM %s stream failed (%s)", nim_model, type(exc).__name__)
                    if spoke:
                        # Half a reply is already out; replaying it from the
                        # CLI would say the opening twice.
                        raise
            for item in WORKERS[mode].ask_stream(full_prompt):
                yield item

        def sse_stream():
            def frame(delta: dict, finish=None) -> str:
                return "data: " + json.dumps({
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": model_id,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }) + "\n\n"

            yield frame({"role": "assistant", "content": ""})
            emitted = ""
            buf = ""
            full = ""
            # Nothing to decide when no tools are attached: it is prose.
            verdict = None if tools else False
            finish = "stop"
            try:
                for kind, payload in token_source():
                    if kind == "delta":
                        buf += payload
                        full += payload
                        if verdict is None:
                            verdict = _tool_call_verdict(full)
                        elif verdict is False and TOOL_MARK in full:
                            # A marker after the prose has started is not the
                            # shape the instruction asks for, but the call is
                            # still real: stop speaking and let it be parsed.
                            verdict = True
                        if verdict is not False:
                            continue
                        # Flush on sentence boundaries: markdown can only be
                        # cleaned on a complete sentence, and downstream TTS
                        # wants whole sentences anyway.
                        cut = max(buf.rfind(". "), buf.rfind("! "), buf.rfind("? "),
                                  buf.rfind("\n"))
                        if cut > 0:
                            chunk, buf = buf[: cut + 1], buf[cut + 1:]
                            clean = _tidy_reply(chunk)
                            if clean:
                                emitted += (" " if emitted else "") + clean
                                yield frame({"content": clean + " "})
                    else:  # done
                        full = payload or full
                        tool_call = _parse_tool_call(full) if verdict is not False else None
                        if tool_call:
                            yield frame({"tool_calls": [dict(tool_call, index=0)]})
                            finish = "tool_calls"
                            break
                        if verdict is not False and not tool_call:
                            # It opened as a tool call and could not be
                            # parsed -- a truncated or malformed marker.
                            # Emitting it as content is how a page of JSON
                            # ended up being read out as the answer.
                            log.warning("unparseable tool call, %d chars", len(full))
                            yield frame({"content":
                                         "I started an action and lost the "
                                         "thread of it, sir. Ask me again."})
                            tail = ""
                        else:
                            tail = _tidy_reply(buf if verdict is False else full)
                        if tail:
                            emitted += (" " if emitted else "") + tail
                            yield frame({"content": tail})
                        # If nothing streamed (very short reply), send the
                        # final result so the client never gets an empty body.
                        if not emitted and payload:
                            yield frame({"content": _tidy_reply(payload)})
            except Exception as exc:  # noqa: BLE001
                ERRORS.append({"t": time.time(), "mode": mode, "error": str(exc)[:300]})
                log.error("%s stream failed: %s", mode, exc)
                # A spent window mid-stream is rescued the same way as the
                # non-streaming path — this is the route nearly every real
                # turn takes, so without this the fallback existed mostly in
                # theory. Only when nothing has been said yet: replaying a
                # half-spoken reply from another model says the opening twice.
                rescue = None if emitted else _rescue_model(exc, mode)
                if rescue is not None:
                    try:
                        timeouts = ((_nim_conf().get("routing") or {})
                                    .get("timeout_sec") or {})
                        log.warning("Claude refused mid-stream — answering on "
                                    "NIM instead")
                        text = _nim_chat(rescue, system_prompt,
                                         _guard_size(prompt),
                                         timeout=float(timeouts.get(mode, 60)))
                        tool_call = _parse_tool_call(text) if tools else None
                        if tool_call:
                            yield frame({"tool_calls": [dict(tool_call, index=0)]})
                            finish = "tool_calls"
                        else:
                            clean = _tidy_reply(text)
                            if clean:
                                yield frame({"content": clean})
                    except Exception as exc2:  # noqa: BLE001
                        log.error("stream rescue failed too: %s", exc2)
                        yield frame({"content": " " + _spoken_failure(exc)})
                else:
                    yield frame({"content": " " + _spoken_failure(exc)})
            entry["done"] = time.time()
            LATENCIES.append(entry["done"] - entry["t"])
            _ledger(mode, nim_model or (MODEL_FAST_ID if fast else MODEL_ID), entry)
            yield frame({}, finish=finish)
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse_stream(), media_type="text/event-stream")

    served_by = MODEL_FAST_ID if fast else MODEL_ID
    try:
        # Off the event loop: this waits on a CLI subprocess or a remote API,
        # and a bridge endpoint that blocks the loop takes every other endpoint
        # down with it — /health included, which is what makes a busy bridge
        # look like a dead one.
        # A caller may ask for longer than the mode default — the daemon's
        # background jobs run whole builds through agent mode. Bounded, so a
        # typo cannot park a worker for an hour.
        asked_timeout = body.get("timeout_s")
        timeout_s = (min(float(asked_timeout), 900.0)
                     if asked_timeout else None)
        text, served_by = await run_in_threadpool(
            _call_claude, system_prompt, prompt, fast=fast, canva=canva,
            agent=agent, background=background, nim_model=nim_model,
            timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001 — any CLI failure becomes a reply
        entry["done"] = time.time()
        ERRORS.append({"t": time.time(), "mode": mode, "error": str(exc)[:300]})
        log.error("%s request failed: %s", mode, exc)
        # An internal call (tool routing, memory extraction) needs a real error
        # so the daemon can fall back. A user-facing turn gets a spoken
        # apology instead — a 502 there just makes Jarvis go silent.
        if internal or tools:
            return JSONResponse(status_code=502, content={"error": {"message": str(exc)}})
        text = _spoken_failure(exc)
    entry["done"] = time.time()
    entry["by"] = served_by
    LATENCIES.append(entry["done"] - entry["t"])
    _ledger(mode, served_by, entry)
    text = _tidy_reply(text)

    tool_call = _parse_tool_call(text) if tools else None
    if tool_call:
        return {
            "id": completion_id, "object": "chat.completion",
            "created": created, "model": model_id,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": None,
                                     "tool_calls": [tool_call]},
                         "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    return {
        "id": completion_id, "object": "chat.completion",
        "created": created, "model": model_id,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


if __name__ == "__main__":
    # The card should show his last known figures the moment the bridge
    # is up, rather than an error until the first lookup happens to land.
    _load_last_good()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
