#!/usr/bin/env python3
"""Attic search: find old conversation by its words, with dates. It only reads.

Claude Code keeps every conversation as a transcript on disk (~/.claude/projects/<project>/<session>.jsonl).
A compaction takes the old words out of Claude's context, but not off the disk. This finds the messages that
contain all of your words, newest first: the user's own messages and Claude's replies, and your memory notes.

  python3 attic_search.py gpu render                 messages with both words, in this project
  python3 attic_search.py "use the GPU again" --who user
                                                     the user's messages only; quotes keep a phrase together
  python3 attic_search.py gpu --all-projects         every project on this computer
  python3 attic_search.py gpu -n 30                  more results (default 10)

Options: --project DIR (default: the folder you're in), --memory-dir DIR (default: <project>/memory),
--no-notes, --who user|claude|both. Capital letters don't matter.

Claude Code deletes transcripts older than cleanupPeriodDays (30 days unless you change it). For a longer
attic, set "cleanupPeriodDays" in ~/.claude/settings.json, for example to 365.
Keep this file next to the hooks folder: it uses hooks/pillow_note.py to tell the user's words from the
machine's. Standard library only. Python 3.8 or newer.
"""
import argparse
import glob
import json
import os
import re
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "hooks"))
import pillow_note as house  # noqa: E402  (one place decides whose words are whose)


def claude_home():
    return os.path.abspath(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))


def first_cwd(folder):
    """The working folder recorded at the top of a project's newest transcript."""
    files = sorted(glob.glob(os.path.join(folder, "*.jsonl")), key=lambda p: os.path.getmtime(house.native(p)),
                   reverse=True)
    for path in files[:1]:
        try:
            with open(house.native(path), "rb") as f:
                for _, raw in zip(range(200), f):
                    if b'"cwd"' in raw:
                        try:
                            return json.loads(raw.decode("utf-8", "replace")).get("cwd")
                        except (ValueError, AttributeError):
                            continue
        except OSError:
            pass
    return None


def project_folder(project):
    """~/.claude/projects/<project>: the path with every character other than letters and digits made a '-'."""
    root = os.path.join(claude_home(), "projects")
    if not os.path.isdir(root):
        return None
    wanted = os.path.abspath(project)
    slug = re.sub(r"[^A-Za-z0-9]", "-", wanted)
    if os.path.isdir(house.native(os.path.join(root, slug))):
        return os.path.join(root, slug)
    names = sorted(os.listdir(root))
    for name in names:
        if name.lower() == slug.lower():
            return os.path.join(root, name)
    for name in names:                        # a long path may have been shortened: ask the transcripts
        cwd = first_cwd(os.path.join(root, name))
        if cwd and os.path.normcase(os.path.abspath(cwd)) == os.path.normcase(wanted):
            return os.path.join(root, name)
    return None


def flat(text):
    return " ".join(str(text or "").split()).lower()


def search_transcripts(files, terms, who):
    words = [w.encode("utf-8") for t in terms for w in t.lower().split()
             if all(32 < ord(c) < 127 for c in w) and '"' not in w and "\\" not in w]
    hits, seen = [], set()
    for path in files:
        session = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(house.native(path), "rb") as f:
                for raw in f:
                    low = raw.lower()
                    if any(w not in low for w in words):
                        continue
                    try:
                        entry = json.loads(raw.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    speaker = "user"
                    text = (house.typed_message(entry) or house.queued_message(entry)
                            or house.scheduled_prompt(entry))
                    if not text:
                        speaker, text = "claude", house.claude_text(entry)
                    if not text or who not in ("both", speaker):
                        continue
                    body = flat(text)
                    if not all(flat(t) in body for t in terms):
                        continue
                    ts = entry.get("timestamp") or (entry.get("attachment") or {}).get("timestamp") or ""
                    key = (ts, speaker, body[:200])
                    if key not in seen:
                        seen.add(key)
                        hits.append((ts, speaker, session, text))
        except OSError:
            continue
    hits.sort(key=lambda h: house.parse_time(h[0]).timestamp() if house.parse_time(h[0]) else 0, reverse=True)
    return hits


def search_notes(folder, terms):
    hits = []
    for path in sorted(glob.glob(os.path.join(folder, "**", "*.md"), recursive=True)):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if not all(flat(t) in flat(text) for t in terms):
            continue
        date = re.search(r"^date:\s*(\d{4}-\d{2}-\d{2})", text, re.M)
        lines = [line.strip() for line in text.splitlines()
                 if line.strip() and any(flat(t) in flat(line) for t in terms)]
        hits.append((date.group(1) if date else "", os.path.relpath(path, folder), lines[:3]))
    return hits


def snippet(text, terms, width=420):
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    low = text.lower()
    spots = [low.find(t.lower().split()[0]) for t in terms if t.split()]
    start = max(0, min([s for s in spots if s >= 0] or [0]) - width // 4)
    piece = text[start:start + width]
    return ("..." if start else "") + piece + ("..." if start + width < len(text) else "")


def main():
    parser = argparse.ArgumentParser(description="Search old Claude Code conversations and memory notes by words.")
    parser.add_argument("terms", nargs="+", help="words to find (all must appear); quote a phrase")
    parser.add_argument("-n", type=int, default=10, help="how many conversation results to show")
    parser.add_argument("--who", choices=("user", "claude", "both"), default="both")
    parser.add_argument("--project", default=os.getcwd(), help="the project folder (default: here)")
    parser.add_argument("--all-projects", action="store_true", help="search every project on this computer")
    parser.add_argument("--memory-dir", help="the memory folder (default: <project>/memory)")
    parser.add_argument("--no-notes", action="store_true", help="skip the memory notes")
    a = parser.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    terms = [t for t in a.terms if t.strip()]

    if not a.no_notes:
        notes_dir = os.path.abspath(os.path.join(a.project, os.path.expanduser(a.memory_dir or "memory")))
        if os.path.isdir(notes_dir):
            found = search_notes(notes_dir, terms)
            print("In the memory notes (%s): %d" % (notes_dir, len(found)))
            for date, name, lines in found:
                print("  %s  %s" % (date or "(no date)", name))
                for line in lines:
                    print(textwrap.indent(textwrap.fill(line, 100), "      "))
            print()

    if a.all_projects:
        files = glob.glob(os.path.join(claude_home(), "projects", "*", "*.jsonl"))
        where = "every project"
    else:
        folder = project_folder(a.project)
        if not folder:
            print("No transcripts found for %s. Try --all-projects, or --project with the folder you started "
                  "Claude Code in." % os.path.abspath(a.project))
            return
        files = glob.glob(os.path.join(folder, "*.jsonl"))
        where = folder
    hits = search_transcripts(files, terms, a.who)
    print("In old conversations (%s): %d match%s, newest first" % (where, len(hits), "" if len(hits) == 1 else "es"))
    for ts, speaker, session, text in hits[:max(1, a.n)]:
        moment = house.parse_time(ts)
        label = moment.astimezone().strftime("%Y-%m-%d %H:%M") if moment else "time unknown"
        print()
        print("%s  %-6s  session %s" % (label, speaker, session[:8]))
        print(textwrap.indent(textwrap.fill(snippet(text, terms), 100), "    "))
    if len(hits) > a.n:
        print("\n(%d more; use -n to see them)" % (len(hits) - a.n))


if __name__ == "__main__":
    main()
