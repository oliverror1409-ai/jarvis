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
