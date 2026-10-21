"""Prove the change did not alter behaviour on a machine that HAS Claude.

A subscriber's config: explicit routing, Claude on PATH, quota to spare.
Spoken replies must stay on Claude until the weekly window is tight -- the
whole point of the pressure logic -- and the no-Claude shortcut must not fire.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

BRIDGE = Path(sys.argv[1])
home = Path(tempfile.mkdtemp(prefix="jarvis-withclaude-"))
(home / ".config" / "jarvis").mkdir(parents=True)
binn = home / "bin"
binn.mkdir()
(binn / "claude.cmd").write_text("@echo off\n", encoding="utf-8")
# A live-looking OAuth entry, so the login probe does not read as dead.
(home / ".claude").mkdir()
(home / ".claude" / ".credentials.json").write_text(json.dumps(
    {"claudeAiOauth": {"accessToken": "t", "expiresAt": 99999999999999}}),
    encoding="utf-8")
os.environ.update(USERPROFILE=str(home), HOME=str(home),
                  HOMEDRIVE="", HOMEPATH="", PATH=str(binn))

fails = []


def check(label, got, want):
    ok = want(got) if callable(want) else got == want
    print(("PASS  " if ok else "FAIL  ") + label + " -> " + repr(got))
    if not ok:
        fails.append(label)


spec = importlib.util.spec_from_file_location("bridge_with_claude", BRIDGE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ROUTING = {"enabled": True,
           "chat": ["nvidia/nemotron-3-super-120b-a12b"],
           "fast": ["poolside/laguna-xs-2.1"],
           "bg": ["nvidia/nemotron-3.5-lightning-30b-a3b"],
           "agent": ["nvidia/nemotron-3-super-120b-a12b"],
           "classification": ["poolside/laguna-xs-2.1"]}
(home / ".config" / "jarvis" / "nim-keys.json").write_text(
    json.dumps({"keys": [{"name": "k", "key": "nvapi-x"}], "routing": ROUTING}),
    encoding="utf-8")

check("claude CLI found", mod._claude_present(), True)
check("login reads as alive", mod._login_dead(), False)

print("\n--- quota healthy: spoken replies stay on Claude ---")
check("chat stays on Claude", mod._nim_model_for("chat", False, pressure=10.0), None)
check("a tool turn stays on Claude",
      mod._nim_model_for("fast", False, tools=True, pressure=10.0), None)
check("agent stays on Claude", mod._nim_model_for("agent", False, pressure=10.0), None)
print("   (fast and bg always went to NIM -- unchanged)")
check("fast goes to NIM", mod._nim_model_for("fast", False, pressure=10.0),
      ROUTING["fast"])
check("bg goes to NIM", mod._nim_model_for("bg", False, pressure=10.0),
      ROUTING["bg"])

print("\n--- window nearly gone: chat crosses over, as before ---")
check("chat moves to NIM at 92%",
      mod._nim_model_for("chat", False, pressure=92.0), ROUTING["fast"])
check("tool turns cross at 98%",
      mod._nim_model_for("fast", False, tools=True, pressure=98.0),
      ROUTING["chat"])

shutil.rmtree(home, ignore_errors=True)
print("\n" + (str(len(fails)) + " FAILED: " + ", ".join(fails) if fails
              else "all checks passed"))
sys.exit(1 if fails else 0)
