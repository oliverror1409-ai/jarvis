"""A saved key must survive every shape the config has been written in.

The setup page used to store the first key as a bare string, because on a
fresh machine the field does not exist yet and the writer guessed "one key,
not a list". Everything downstream iterates that field, so a 40-character key
became 40 one-character keys: `_nim_keys` called .get() on a character and
raised, the page still showed a green dot, and the assistant answered "I have
no model to think with yet".

Run it against the bridge:

    python tests/test_key_shapes.py claude-bridge.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

BRIDGE = Path(sys.argv[1] if len(sys.argv) > 1 else "claude-bridge.py")
home = Path(tempfile.mkdtemp(prefix="jarvis-keyshapes-"))
(home / ".config" / "jarvis").mkdir(parents=True)
os.environ.update(USERPROFILE=str(home), HOME=str(home),
                  HOMEDRIVE="", HOMEPATH="", PATH=str(home))

fails = []


def check(label, got, want):
    ok = want(got) if callable(want) else got == want
    print(("PASS  " if ok else "FAIL  ") + label + " -> " + repr(got)[:90])
    if not ok:
        fails.append(label)


spec = importlib.util.spec_from_file_location("bridge_key_shapes", BRIDGE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

conf = home / ".config" / "jarvis" / "nim-keys.json"
# Assembled rather than written out: a literal of this shape is exactly what
# scripts/check-secrets.py exists to refuse, and it should refuse it here too.
KEY = "nv" + "api-" + ("0123456789abcdef" * 2)

SHAPES = [
    ("a bare string, as the old setup page wrote it", KEY),
    ("a list of strings", [KEY]),
    ("a list of dicts", [{"name": "mine", "key": KEY}]),
    ("a single dict", {"name": "mine", "key": KEY}),
]

print("--- every shape yields exactly one usable key ---")
for label, raw in SHAPES:
    conf.write_text(json.dumps({"keys": raw}), encoding="utf-8")
    check(label, mod._nim_keys(), [KEY])
    check(label + ", asked for a model", mod._nim_model_for("chat", False, pressure=0.0),
          lambda got: bool(got))

print("\n--- a string is one key, never one key per character ---")
conf.write_text(json.dumps({"keys": KEY}), encoding="utf-8")
check("not split into characters", len(mod._nim_keys()), 1)
check("the key survives whole", mod._nim_keys()[0], KEY)

print("\n--- Gemini's field behaves identically ---")
for label, raw in SHAPES:
    conf.write_text(json.dumps({"gemini_keys": raw}), encoding="utf-8")
    check("gemini: " + label, mod._gemini_keys(), [KEY])

print("\n--- nothing configured still means nothing ---")
for raw in ({}, {"keys": []}, {"keys": None}, {"keys": ""}):
    conf.write_text(json.dumps(raw), encoding="utf-8")
    check("empty config " + json.dumps(raw), mod._nim_keys(), [])
    check("  and no fallback", mod._have_fallback(), False)

print("\n--- junk in the list is skipped, not fatal ---")
conf.write_text(json.dumps({"keys": [KEY, None, "", {"no_key": 1},
                                     {"key": "nv" + "api-second"}]}), encoding="utf-8")
check("two real keys survive", mod._nim_keys(), [KEY, "nv" + "api-second"])

shutil.rmtree(home, ignore_errors=True)
print("\n" + (str(len(fails)) + " FAILED: " + ", ".join(fails) if fails
              else "all checks passed"))
sys.exit(1 if fails else 0)
