#!/usr/bin/env python3
r"""Print the "hooks" block for Claude Code's settings.json, with this computer's real paths filled in.

  python3 print_settings.py                              all four hook entries
  python3 print_settings.py --memory-dir ~/notes/house   keep the house in one fixed folder
  python3 print_settings.py --no-shoulder-tap            only the pillow note and the wake-up check

Optional, for a computer shared by more than one AI (each AI's hooks fire in every window, so these keep
them to their own AI's windows):
  --model opus                                   the hooks act only in windows whose newest reply came from
                                                 a model whose id contains "opus" (same as MEMORY_HOUSE_MODEL)
  --skip-dirs "C:\Users\you\Documents\OtherAI"   the hooks do nothing in windows started in the other AI's
                                                 folder; separate several with ; (same as
                                                 MEMORY_HOUSE_SKIP_DIRS)
They go into every hook's "args", like --memory-dir, so two AIs can each have their own entries.

It uses the Python you run it with, and the hooks folder next to this tools folder. It only prints. Paste the
result into ~/.claude/settings.json (every project) or <project>/.claude/settings.json (one project). If the
file already has a "hooks" section, add these entries to it instead of replacing what is there.

Each hook runs through a one-line guard: Python starts, checks that the script exists, and only then runs it.
Without the guard, a wrong path makes Python exit with code 2, and Claude Code reads exit code 2 from a
UserPromptSubmit hook as "block this message" (and from PreCompact as "block the compaction"). With the guard,
a wrong path just does nothing.

Python runs the guard with -I (isolated mode). Claude Code runs hooks inside the project folder, and without
-I, a file there with the same name as one of Python's own modules (a json.py, say) would run instead of the
real one. Standard library only. Python 3.8 or newer.
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
             "args": ["-I", "-c", GUARD, os.path.join(HOOKS, script)] + extra, "timeout": timeout}
    if status:
        entry["statusMessage"] = status
    return entry


def block(memory_dir=None, shoulder_tap=True, model=None, skip_dirs=None):
    whose = (["--model", model] if model else []) + (["--skip-dirs", skip_dirs] if skip_dirs else [])
    extra = (["--memory-dir", memory_dir] if memory_dir else []) + whose
    hooks = {
        "PreCompact": [{"hooks": [hook("pillow_note.py", extra, 30, "Saving the pillow note")]}],
        "SessionStart": [{"matcher": "compact", "hooks": [hook("after_compact.py", extra, 10)]}],
    }
    if shoulder_tap:
        hooks["PostToolUse"] = [{"matcher": "*", "hooks": [hook("shoulder_tap.py", whose, 5)]}]
        hooks["UserPromptSubmit"] = [{"hooks": [hook("shoulder_tap.py", whose, 5)]}]
    return {"hooks": hooks}


def main():
    parser = argparse.ArgumentParser(description="Print the memory-house hooks block for settings.json.")
    parser.add_argument("--memory-dir", help="a fixed memory folder (default: each project's ./memory)")
    parser.add_argument("--no-shoulder-tap", action="store_true", help="leave out the shoulder tap")
    parser.add_argument("--model", help='optional: the hooks act only in windows whose newest reply came from a '
                                        'model whose id contains this, e.g. "opus"')
    parser.add_argument("--skip-dirs", help="optional: other AIs' folders, separated by ;. The hooks do nothing "
                                            "in windows started in them")
    a = parser.parse_args()
    for name in ("pillow_note.py", "after_compact.py", "shoulder_tap.py"):
        if not os.path.isfile(os.path.join(HOOKS, name)):
            sys.exit("Can't find %s in %s. Keep the tools and hooks folders together." % (name, HOOKS))
    memory_dir = os.path.abspath(os.path.expanduser(a.memory_dir)) if a.memory_dir else None
    skip_dirs = ";".join(os.path.abspath(os.path.expanduser(d.strip()))
                         for d in (a.skip_dirs or "").split(";") if d.strip())
    print(json.dumps(block(memory_dir, not a.no_shoulder_tap, (a.model or "").strip() or None, skip_dirs or None),
                     indent=2))


if __name__ == "__main__":
    main()
