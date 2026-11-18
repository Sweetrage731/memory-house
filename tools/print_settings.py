#!/usr/bin/env python3
"""Print the "hooks" block for Claude Code's settings.json, with this computer's real paths filled in.

  python3 print_settings.py                              all four hook entries
  python3 print_settings.py --memory-dir ~/notes/house   keep the house in one fixed folder
  python3 print_settings.py --no-shoulder-tap            only the pillow note and the wake-up check

It uses the Python you run it with, and the hooks folder next to this tools folder. It only prints. Paste the
result into ~/.claude/settings.json (every project) or <project>/.claude/settings.json (one project). If the
file already has a "hooks" section, add these entries to it instead of replacing what is there.

Each hook runs through a one-line guard: Python starts, checks that the script exists, and only then runs it.
Without the guard, a wrong path makes Python exit with code 2, and Claude Code reads exit code 2 from a
UserPromptSubmit hook as "block this message" (and from PreCompact as "block the compaction"). With the guard,
a wrong path just does nothing. Standard library only. Python 3.8 or newer.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.abspath(os.path.join(HERE, "..", "hooks"))
GUARD = ("import os,runpy,sys;sys.argv=sys.argv[1:];"
         "os.path.isfile(sys.argv[0]) and runpy.run_path(sys.argv[0],run_name='__main__')")


def hook(script, extra, timeout, status=None):
    entry = {"type": "command", "command": sys.executable,
             "args": ["-c", GUARD, os.path.join(HOOKS, script)] + extra, "timeout": timeout}
    if status:
        entry["statusMessage"] = status
    return entry


def block(memory_dir=None, shoulder_tap=True):
    extra = ["--memory-dir", memory_dir] if memory_dir else []
    hooks = {
        "PreCompact": [{"hooks": [hook("pillow_note.py", extra, 30, "Saving the pillow note")]}],
        "SessionStart": [{"matcher": "compact", "hooks": [hook("after_compact.py", extra, 10)]}],
    }
    if shoulder_tap:
        hooks["PostToolUse"] = [{"matcher": "*", "hooks": [hook("shoulder_tap.py", [], 5)]}]
        hooks["UserPromptSubmit"] = [{"hooks": [hook("shoulder_tap.py", [], 5)]}]
    return {"hooks": hooks}


def main():
    parser = argparse.ArgumentParser(description="Print the memory-house hooks block for settings.json.")
    parser.add_argument("--memory-dir", help="a fixed memory folder (default: each project's ./memory)")
    parser.add_argument("--no-shoulder-tap", action="store_true", help="leave out the shoulder tap")
    a = parser.parse_args()
    for name in ("pillow_note.py", "after_compact.py", "shoulder_tap.py"):
        if not os.path.isfile(os.path.join(HOOKS, name)):
            sys.exit("Can't find %s in %s. Keep the tools and hooks folders together." % (name, HOOKS))
    memory_dir = os.path.abspath(os.path.expanduser(a.memory_dir)) if a.memory_dir else None
    print(json.dumps(block(memory_dir, not a.no_shoulder_tap), indent=2))


if __name__ == "__main__":
    main()
