"""Prove the bridge routes every lane when there is no Claude CLI.

Run against a fake home holding one hosted key and no routing block -- which
is what the setup page actually writes -- with the Claude CLI removed from
PATH. Nothing here touches the real config.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

BRIDGE = Path(sys.argv[1])
home = Path(tempfile.mkdtemp(prefix="jarvis-noclaude-"))
(home / ".config" / "jarvis").mkdir(parents=True)
os.environ.update(USERPROFILE=str(home), HOME=str(home),
                  HOMEDRIVE="", HOMEPATH="",
                  PATH=str(home))  # nothing on PATH, so no claude

fails = []


def check(label, got, want):
    ok = got == want if not callable(want) else want(got)
    print(("PASS  " if ok else "FAIL  ") + label + " -> " + repr(got))
    if not ok:
        fails.append(label)


spec = importlib.util.spec_from_file_location("bridge_under_test", BRIDGE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

conf = home / ".config" / "jarvis" / "nim-keys.json"
MODES = ["chat", "fast", "bg", "agent"]

print("\n--- no Claude, no keys: must refuse clearly, never route ---")
conf.write_text(json.dumps({"keys": []}), encoding="utf-8")
check("claude CLI absent", mod._claude_present(), False)
check("login reads as dead", mod._login_dead(), True)
check("no fallback available", mod._have_fallback(), False)
for m in MODES:
    check("no key: " + m + " routes nowhere",
          mod._nim_model_for(m, False, pressure=0.0), None)

print("\n--- no Claude, one NIM key, no routing block ---")
conf.write_text(json.dumps({"keys": [{"name": "friend", "key": "nvapi-x"}]}),
                encoding="utf-8")
check("fallback available", mod._have_fallback(), True)
for m in MODES:
    check(m + " gets a model", mod._nim_model_for(m, False, pressure=0.0),
          lambda got: bool(got))
check("a tool-carrying fast turn still routes",
      mod._nim_model_for("fast", False, tools=True, pressure=0.0),
      lambda got: bool(got))
check("classification routes",
      mod._nim_model_for("fast", True, pressure=0.0), lambda got: bool(got))

print("\n--- no Claude, one Gemini key only ---")
conf.write_text(json.dumps({"gemini_keys": [{"name": "g", "key": "AIza-x"}]}),
                encoding="utf-8")
check("fallback available on Gemini alone", mod._have_fallback(), True)
for m in MODES:
    check(m + " gets a Gemini model",
          mod._nim_model_for(m, False, pressure=0.0),
          lambda got: bool(got) and all(str(x).startswith("gemini")
                                        for x in (got if isinstance(got, list) else [got])))

print("\n--- an explicit routing block still wins ---")
conf.write_text(json.dumps({"keys": [{"name": "f", "key": "nvapi-x"}],
                            "routing": {"chat": ["mine/own-model"]}}),
                encoding="utf-8")
check("chat uses the named model",
      mod._nim_model_for("chat", False, pressure=0.0), ["mine/own-model"])
check("routing.enabled false is still obeyed",
      (conf.write_text(json.dumps({"keys": [{"name": "f", "key": "k"}],
                                   "routing": {"enabled": False,
                                               "chat": ["m"]}}),
                       encoding="utf-8"),
       mod._nim_model_for("chat", False, pressure=0.0))[1], None)

shutil.rmtree(home, ignore_errors=True)
print("\n" + (str(len(fails)) + " FAILED: " + ", ".join(fails) if fails
              else "all checks passed"))
sys.exit(1 if fails else 0)
