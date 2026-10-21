# Jarvis

A local voice assistant that runs on your own machine. It listens, answers,
remembers, and can use the tools on your computer.

**You do not need a Claude subscription.** The brain is a choice between three
providers and Jarvis does not mind which: a Claude subscription if you have
one, or an NVIDIA NIM key, or a Google Gemini key — the last two have free
tiers that need no card. Pick one on the setup page and that is the decision
made. With no provider at all Jarvis starts, serves its pages and tells you
what is missing; it just cannot think yet.

Nothing here phones home. Your keys stay in `~/.config/jarvis/`, which is
outside this repository and ignored by git.

---

## Start here

```bash
git clone https://github.com/oliverror1409-ai/jarvis.git
cd jarvis
python start.py
```

That is the whole install. `start.py` checks the four packages the bridge
needs, offers to install them, starts the bridge and opens the setup page.
Python 3.10 or newer; nothing else is required up front.

```bash
python start.py --check          # say what is missing, change nothing
python start.py --port 8200      # if 8100 is taken
python start.py --no-browser     # over SSH, or on a headless box
```

The setup page is at **<http://127.0.0.1:8100/setup>**.

`restart-jarvis.ps1` is the other way in, and it does much more — a
virtualenv, the voice daemon, the watchdog, log rolling. It expects a machine
that has already been set up, and it will not run in a fresh clone. Use
`start.py` first.

Tick what you want. Anything needing a key has a button that opens the exact
page which issues it — NVIDIA, OpenAI, Google, Telegram and the rest. Paste
the key, it is written to `~/.config/jarvis/`, and the dot goes green when it
is live.

**The only thing you must choose is one model provider**, at the top of the
page. Everything under it is optional and can be turned on later from
**⚙ MORE → SETUP & KEYS** inside the app, or **Settings → Integrations**.

---

## Using it

Press **Save and continue** on the setup page and it opens the assistant. It
lives at **<http://127.0.0.1:8100/ui>** — that is the whole app, and the only
address worth bookmarking.

Type in the box at the bottom and press Enter. Replies arrive in the feed.
The left half is the face; the right half is the conversation, the machine
monitor and the settings.

It opens in **its own window** -- no address bar, no tabs, its own taskbar
icon -- rather than as a browser tab, using Edge or Chrome in app mode. It
gets a separate browser profile, so it never touches your tabs, extensions or
session, and closing it closes nothing else. `python start.py --tab` forces an
ordinary tab instead, and if no Chromium is installed it falls back to one.

### Installing it properly

In the Jarvis window, open the **...** menu and choose **Install Jarvis**
(Edge calls it *Apps -> Install this site as an app*; Chrome shows an install
icon in the address bar). It then behaves like any other installed program: a
Start menu entry, its own icon, pinnable to the taskbar, and no browser
furniture anywhere.

It still needs the bridge running underneath -- it is your machine doing the
work, not a website -- so start it with the shortcut below or `python
start.py`, then open the installed app.

### A desktop shortcut

So starting it is a double-click and not a command:

```
powershell -ExecutionPolicy Bypass -File make-shortcut.ps1
```

That puts **Jarvis** on your desktop with the right icon. Double-click it and
the bridge starts and the window opens; the small console window it leaves
behind is how you stop it. `-Remove` takes the shortcut away again, and
`-Port 8200` points it somewhere else.

**Without the voice daemon this is a text assistant**, and the HUD says so in
the bar across the top. Speech in (Whisper), speech out (Kokoro), the wake
word and the desktop tools all belong to a separate component that is not in
this repository yet. Everything you see in the HUD that reaches for it —
the MIC button, dictation, *Launch*, the file browser — will not do anything
until it is installed. The conversation itself does not need it.

Things worth knowing:

- The page holds the conversation, so **reloading starts a fresh one**. There
  is no server-side history in a bridge-only install.
- `Ctrl-C` in the terminal running `start.py` stops everything.
- Settings, including every integration you can turn on later, are behind
  **⚙ MORE** in the top right.

---

## What you can connect

**The brain — one of these, and only one is needed:**

| | Needs | Cost |
|---|---|---|
| NVIDIA NIM | a key | free tier, no card |
| Google Gemini | a key | free tier, no card |
| Claude bridge | `claude login` | included with a Claude subscription |
| OpenAI / Codex | a key | pay as you go |

Tick more than one and they cover for each other: Claude answers, and the
hosted models take over when its window is spent or its login has lapsed.

**Voice and memory — all local, all free, all optional:**

| | Needs | Cost |
|---|---|---|
| **Whisper** speech-to-text | an NVIDIA GPU, ideally | free, runs locally |
| **Kokoro** voice | nothing | free, runs locally |
| **Ollama** embeddings | nothing | free, runs locally |

**Everything else:**

| | Needs | Cost |
|---|---|---|
| Telegram | a bot token | free |
| Phone calls (SIP) | an account | free |
| ntfy notifications | nothing | free |
| Fooocus, Everything, Open Notebook, God's Eye View | a download | free |

The full list with descriptions is `setup/catalogue.json`.

### Running it without Claude

Worth being concrete, because it is the common case. Get a key at
<https://build.nvidia.com> (sign in, open any model, *Get API Key*), paste it
into **NVIDIA NIM** on the setup page, and that is it — no model names to
choose, no config file to edit. Jarvis notices the Claude CLI is absent and
sends every request to the hosted models instead.

`tests/test_no_claude.py` is the proof, and runs against an empty config with
the CLI removed from `PATH`:

```bash
python tests/test_no_claude.py claude-bridge.py     # no Claude installed
python tests/test_with_claude.py claude-bridge.py   # and with it, unchanged
```

---

## Your keys are not in here

This matters enough to be explicit about.

- Every credential lives in `~/.config/jarvis/`, **never in the repository**.
- `.gitignore` refuses `.env`, `*.key`, `*.pem`, `config.json`, `nim-keys.json`
  and the rest.
- `config.example.json` is the template: every key and every personal detail
  (location, phone, chat id) is blank.
- **`python scripts/check-secrets.py`** reads what git would actually publish
  and refuses if a credential is in it. Install it as a pre-push hook with
  `python scripts/check-secrets.py --install-hook` and it runs itself.

The setup page takes a key in and never gives one back: `/setup/status` answers
only whether one is set, and nothing is logged, not even a prefix. Writes are
refused unless they come from this machine, so a web page you happen to be
visiting cannot quietly swap your API key for its own.

If a key ever does reach a public repository, **rotate it**. Rewriting history
is not a fix; assume anything pushed was scraped within the minute.

---

## The parts

| | |
|---|---|
| `start.py` | Checks what is missing, installs it, starts the bridge |
| `claude-bridge.py` | Routes model calls, serves the HUD and the setup page |
| `requirements.txt` | The four packages the bridge imports |
| `avatar/ui.html` | The HUD: conversation, machine state, settings |
| `setup/` | The setup page and the catalogue of integrations |
| `scripts/check-secrets.py` | Refuses to publish a credential |
| `make-shortcut.ps1` | Puts Jarvis on the desktop with its icon |
| `tests/` | Runnable checks, including the two no-Claude ones above |
| `restart-jarvis.ps1` | Full restart of bridge, backend, daemon, UI — needs a set-up machine |
| `services/` | Optional third-party pieces, each its own download |

---

## Troubleshooting

**The setup page is empty, or the browser says it cannot connect.** The bridge
is not running. `python start.py`, then check
<http://127.0.0.1:8100/health>.

**`python start.py` says a package is missing and pip fails.** Install them
yourself with `python -m pip install -r requirements.txt`. On Linux, a distro
Python may need `python3 -m venv .venv` first.

**It answers "I have no model to think with yet".** Working as intended: no
provider is configured. Add a NIM or Gemini key on the setup page.

**Port 8100 is already in use.** Either Jarvis is already running — open the
setup page — or `python start.py --port 8200`.

**A key saved but the dot stays grey.** Press *Re-check what's running*. For a
service rather than a key, the dot reports whether its port answers — so start
the service first.

**Node tools fail oddly on Windows.** Several want Node 24 or newer. Node 20
segfaults native modules on some machines, silently.

**It answers but will not speak.** Kokoro is probably down. The setup page
says so.

---

## Licence and provenance

The code here is yours to read and change. The pieces under `services/` are
other people's projects, each under its own licence — God's Eye View, Fooocus,
Kokoro and the rest are linked from the setup page rather than vendored.
