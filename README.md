# Jarvis

A local voice assistant that runs on your own machine. It listens, answers,
remembers, and can use the tools on your computer. The models can be your own
Claude subscription, free hosted ones, or nothing but what is installed
locally — that is the first thing you choose.

Nothing here phones home. Your keys stay in `~/.config/jarvis/`, which is
outside this repository and ignored by git.

---

## Start here

```bash
git clone <this repo>
cd jarvis
python -m pip install -r requirements.txt     # if present; see Troubleshooting
powershell -File restart-jarvis.ps1            # Windows
```

Then open **<http://127.0.0.1:8100/setup>**.

That page is the whole setup. Tick what you want, and anything that needs a key
has a button that opens the exact page which issues it — NVIDIA, OpenAI,
Google, Telegram and the rest. Paste the key, it is written to
`~/.config/jarvis/`, and the page shows a green dot when it is live.

**Nothing is required except the core**, and the core is four local things that
need no account at all. You can skip everything else and turn it on later from
**⚙ MORE → SETUP & KEYS** inside the app, or from **Settings → Integrations**.

---

## What you can connect

| | Needs | Cost |
|---|---|---|
| **Claude bridge** | `claude login` | included with a Claude subscription |
| **Whisper** speech-to-text | an NVIDIA GPU, ideally | free, runs locally |
| **Kokoro** voice | nothing | free, runs locally |
| **Ollama** embeddings | nothing | free, runs locally |
| NVIDIA NIM | a key | free tier, no card |
| OpenAI / Codex | a key | pay as you go |
| Google Gemini | a key | free tier |
| Telegram | a bot token | free |
| Phone calls (SIP) | an account | free |
| Fooocus, Everything, Open Notebook, God's Eye View | a download | free |

The full list with descriptions is `setup/catalogue.json`.

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
| `claude-bridge.py` | Routes model calls, serves the HUD and the setup page, port 8100 |
| `avatar/ui.html` | The HUD: conversation, machine state, settings |
| `setup/` | The setup page and the catalogue of integrations |
| `scripts/check-secrets.py` | Refuses to publish a credential |
| `restart-jarvis.ps1` | Starts or restarts bridge, backend, daemon, UI |
| `services/` | Optional third-party pieces, each its own download |

---

## Troubleshooting

**The setup page is empty.** The bridge is not running. `restart-jarvis.ps1
-Component bridge`, then check <http://127.0.0.1:8100/health>.

**A key saved but the dot stays grey.** Press *Re-check what's running*. For a
service rather than a key, the dot reports whether its port answers — so start
the service first.

**Node tools fail oddly on Windows.** Several want Node 24 or newer. Node 20
segfaults native modules on some machines, silently.

**It answers but will not speak.** Kokoro is probably down. The setup page says
so, and `restart-jarvis.ps1` brings it back.

---

## Licence and provenance

The code here is yours to read and change. The pieces under `services/` are
other people's projects, each under its own licence — God's Eye View, Fooocus,
Kokoro and the rest are linked from the setup page rather than vendored.
