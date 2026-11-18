#!/usr/bin/env python3
"""Stale check: list memory that may be out of date. It only lists. It never changes anything.

  python3 stale_check.py                         the memory folder of the project you're in
  python3 stale_check.py --memory-dir PATH
  python3 stale_check.py --today 2026-04-01      pretend it's another day

What it looks for:
  - notes whose "check_by" date (in the frontmatter) has passed
  - notes still marked "status: active" whose "ends" date has passed
  - lines in MEMORY.md that say "until", "check by" or "ends" with a date that has passed
  - the "Open loops (updated YYYY-MM-DD)" line in MEMORY.md, when it is more than 7 days old
The library is history, so it isn't checked. Dates are written YYYY-MM-DD.
Standard library only. Python 3.8 or newer.
"""
import argparse
import datetime as dt
import glob
import os
import re
import sys

DATE = r"(\d{4}-\d{2}-\d{2})"
OPEN_LOOPS_DAYS = 7


def day(text):
    try:
        return dt.date.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def frontmatter(text):
    """The simple "key: value" lines between the first two --- lines. A " # comment" at the end is dropped."""
    m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    fields = {}
    for line in (m.group(1).splitlines() if m else []):
        kv = re.match(r"^\s*([A-Za-z_][\w-]*)\s*:\s*(.*?)\s*(?:\s#.*)?$", line)
        if kv:
            fields[kv.group(1).lower()] = kv.group(2).strip().strip("\"'")
    return fields


def check(folder, today):
    found = []
    for path in sorted(glob.glob(os.path.join(folder, "**", "*.md"), recursive=True)):
        name = os.path.relpath(path, folder)
        if name.split(os.sep)[0] == "pillow" or os.path.basename(path) in ("MEMORY.md", "library.md"):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                fields = frontmatter(f.read(4000))
        except OSError:
            continue
        check_by = day(fields.get("check_by"))
        if check_by and check_by < today:
            found.append("%s: its check_by date (%s) has passed. Is it still true?" % (name, check_by))
        ends = day(fields.get("ends"))
        if ends and ends < today and fields.get("status", "active").lower() == "active":
            found.append("%s: it was meant to end on %s but is still marked active." % (name, ends))
    index = os.path.join(folder, "MEMORY.md")
    if os.path.isfile(index):
        with open(index, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        for number, line in enumerate(lines, 1):
            label = " ".join(re.sub(r"[*_`\[\]]", "", line).split())[:70]
            loops = re.search(r"Open loops \(updated " + DATE + r"\)", line, re.I)
            if loops and day(loops.group(1)) and (today - day(loops.group(1))).days > OPEN_LOOPS_DAYS:
                found.append("MEMORY.md line %d: the open-loops line is %d days old. Bring it up to date." % (
                    number, (today - day(loops.group(1))).days))
            if re.match(r"^\s*[-*]?\s*(ENDED|DONE)\b", line):
                continue
            for m in re.finditer(r"\b(until|check by|check_by|ends)\s*:?\s*" + DATE, line, re.I):
                when = day(m.group(2))
                if when and when < today:
                    found.append("MEMORY.md line %d: \"%s...\" says %s %s, and that has passed." % (
                        number, label, m.group(1), when))
    return found


def main():
    parser = argparse.ArgumentParser(description="List memory that may be out of date (read-only).")
    parser.add_argument("--memory-dir", default="memory", help="the memory folder (default: ./memory)")
    parser.add_argument("--today", help="pretend today is this date (YYYY-MM-DD)")
    a = parser.parse_args()
    folder = os.path.abspath(os.path.expanduser(a.memory_dir))
    today = day(a.today) if a.today else dt.date.today()
    if today is None:
        sys.exit("--today needs a date like 2026-04-01")
    if not os.path.isdir(folder):
        sys.exit("No memory folder at %s. Use --memory-dir to point at it." % folder)
    found = check(folder, today)
    print("Stale check for %s, %s:" % (folder, today))
    print("\n".join("  - " + line for line in found) if found else "  nothing looks out of date")


if __name__ == "__main__":
    main()
