"""Refuse to publish if anything that looks like a credential is tracked.

Run it before pushing, and wire it as a pre-push hook with
`python scripts/check-secrets.py --install-hook`.

The repository is already the right shape -- every real key lives in
~/.config/jarvis, outside it, which is why there was nothing to clean up when
this was written. The danger is the first .env somebody drops beside the code,
so this reads what git would actually publish, not what is lying around.

It is deliberately noisy about shapes and quiet about files it cannot read:
a missed key is unrecoverable once pushed, a false alarm costs ten seconds.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Shapes that are a credential and nothing else. Each is anchored tightly
# enough that minified JavaScript does not trip it -- an early version matched
# the word "Bearer" inside a React bundle and buried the real signal.
PATTERNS = [
    ("OpenAI key", re.compile(r"\bsk-(?:proj|svcacct)?-?[A-Za-z0-9_-]{32,}")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{32,}")),
    ("NVIDIA NIM key", re.compile(r"\bnvapi-[A-Za-z0-9_-]{32,}")),
    ("Google API key", re.compile(r"\bAIzaSy[A-Za-z0-9_-]{30,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[0-9]{10,}-[A-Za-z0-9-]{10,}")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("Hardcoded password", re.compile(
        r"""(?i)\b(?:password|passwd|secret)\s*[=:]\s*["'][^"'\s]{8,}["']""")),
]

# Files that are allowed to contain a shape, because teaching what a key looks
# like is their whole job. Kept short on purpose: every entry is a blind spot.
ALLOW = (
    "scripts/check-secrets.py",
    "setup/catalogue.json",
    ".env.example",
    "config.example.json",
)

# Names that should never be tracked at all, whatever is inside them.
FORBIDDEN_NAMES = re.compile(
    r"(^|/)(\.env(\..+)?|.*secrets?\.json|credentials\.json|nim-keys\.json|"
    r"you-key\.json|contabo\.json|beszel\.json|config\.json)$")

SKIP_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".mp4", ".mov", ".webm", ".ico",
               ".pdf", ".zip", ".glb", ".woff", ".woff2", ".ttf", ".mp3", ".wav",
               ".min.js", ".min.css", ".map", ".pyc", ".exe", ".dll", ".ipa")


def tracked_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                         capture_output=True, text=True)
    return [p for p in out.stdout.split("\0") if p]


def scan():
    problems = []
    for rel in tracked_files():
        if rel in ALLOW:
            continue
        if FORBIDDEN_NAMES.search(rel) and not rel.endswith(".example.json"):
            problems.append((rel, 0, "forbidden filename",
                             "this file holds credentials and must not be tracked"))
            continue
        low = rel.lower()
        if low.endswith(SKIP_SUFFIX):
            continue
        path = ROOT / rel
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable: a key is not hiding in it as text
        for n, line in enumerate(text.splitlines(), 1):
            if len(line) > 2000:
                continue  # a bundled one-liner, not somebody's config
            for label, rx in PATTERNS:
                hit = rx.search(line)
                if hit:
                    shown = hit.group(0)
                    masked = shown[:6] + "..." + shown[-4:] if len(shown) > 14 else "..."
                    problems.append((rel, n, label, masked))
    return problems


def install_hook():
    hooks = ROOT / ".git" / "hooks"
    if not hooks.is_dir():
        print("no .git/hooks here")
        return 1
    hook = hooks / "pre-push"
    hook.write_text(
        "#!/bin/sh\n"
        "# Added by scripts/check-secrets.py --install-hook\n"
        'exec python "$(git rev-parse --show-toplevel)/scripts/check-secrets.py"\n',
        encoding="utf-8")
    try:
        hook.chmod(0o755)
    except OSError:
        pass  # Windows does not need the bit, and git honours the hook anyway
    print(f"pre-push hook written to {hook}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--install-hook", action="store_true",
                    help="refuse pushes that would publish a credential")
    args = ap.parse_args()
    if args.install_hook:
        return install_hook()

    problems = scan()
    if not problems:
        n = len(tracked_files())
        print(f"  clean: {n} tracked files, no credential shapes, "
              f"no credential filenames.")
        return 0

    print("  REFUSING: these would be published.\n")
    for rel, line, label, detail in problems:
        where = f"{rel}:{line}" if line else rel
        print(f"   {where}\n      {label}: {detail}")
    print("\n  Move the value into ~/.config/jarvis/ and read it from there,")
    print("  or add the file to .gitignore. Never rewrite history as the fix:")
    print("  a key that has been pushed is burned and must be rotated.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
