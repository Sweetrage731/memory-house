#!/usr/bin/env python3
"""Window words: one conversation's words, one Markdown file per day, for a private backup.

A transcript (~/.claude/projects/<project>/<session>.jsonl) holds everything that passed through the window:
pictures, tool output, file contents. It can grow to gigabytes, too big for a git repository, and all of it is
private. The words alone are small: the user's messages (including the ones sent while Claude was working) and
Claude's replies. This writes them as one file per day, with each message's local time, and hides anything that
looks like a key, token or password. A finished day's file never changes, so git stores each day once.

  python3 window_words.py ~/.claude/projects/<project>/<session>.jsonl ~/private-backup/window-words
  python3 window_words.py <transcript> <folder> --user Sam --claude Claude --title "My main window"

Keep the whole transcript somewhere else too (another computer or a drive): see "Keeping the window itself" in the
README. Push these files only to a PRIVATE repository: they are your conversations, word for word.
Keep this file next to the hooks folder: it uses hooks/pillow_note.py to tell the user's words from the machine's.
Standard library only. Python 3.8 or newer.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "hooks"))
import pillow_note as house  # noqa: E402  (one place decides whose words are whose)

# Anything that looks like a key or token is hidden before it is written.
SECRETS = [re.compile(p, f) for p, f in [
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
    (r"\b(?:sk|pk|rk)[-_](?:ant[-_])?(?:live[-_]|test[-_]|proj[-_])?[A-Za-z0-9_\-]{16,}", 0),
    (r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})", 0),
    (r"\bxox[abposr]-[A-Za-z0-9\-]{10,}", 0),
    (r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", 0),
    (r"\bAIza[0-9A-Za-z_\-]{30,}", 0),
    (r"\b(?:hf|r8|npm|whsec|glpat)[_-][A-Za-z0-9_\-]{16,}", 0),
    (r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}", 0),
    (r"\bbearer\s+[A-Za-z0-9_\-.=+/]{12,}", re.I),
    (r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)", 0),
    (r"(?<=[?&])(?:key|token|api_key|apikey|access_token|auth|sig|signature|secret|password)=[^&\s]+", re.I),
    (r"\b[0-9a-fA-F]{32,}\b", 0),
]]
# "password: ...", "api key is ..." -> keep the label, hide the value. Passwords always; the others when the value
# looks machine-made, so ordinary sentences survive.
LABELLED = re.compile(r"(?i)\b(passw(?:or)?d|passcode|api[_\- ]?key|secret|token)(\s*(?:[:=]|\bis\b)\s*)"
                      r"([^\s\"'`,;<>()\[\]]{6,})")


def scrub(text):
    for pattern in SECRETS:
        text = pattern.sub("[hidden]", text)

    def labelled(m):
        label, value = m.group(1).lower(), m.group(3)
        if label.startswith("pass") or re.search(r"\d", value) or len(value) >= 20:
            return m.group(1) + m.group(2) + "[hidden]"
        return m.group(0)
    return LABELLED.sub(labelled, text)


def messages(path):
    """(timestamp, "user" or "claude", text, sent_mid_task) for each message, oldest first."""
    seen = {}
    with open(house.native(path), "rb") as f:
        for raw in f:
            kind = house.LEADING_TYPE.match(raw)
            if kind and kind.group(1).startswith(house.SKIP_TYPES):
                continue
            try:
                entry = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            ts = entry.get("timestamp") or (entry.get("attachment") or {}).get("timestamp") or ""
            who, mid, text = "user", False, house.typed_message(entry)
            if not text:
                mid, text = True, house.queued_message(entry)
            if not text:
                who, mid, text = "claude", False, house.claude_text(entry)
            if not text:
                continue
            if who == "user":       # a message sent mid-task is recorded twice: queued, then delivered
                key = " ".join(text.split())[:300]
                moment, before = house.parse_time(ts), seen.get(key)
                if moment and before and abs((moment - before).total_seconds()) < 6 * 3600:
                    continue
                if moment:
                    seen[key] = moment
            yield ts, who, text, mid


def by_day(path, user, claude):
    days = {}
    for ts, who, text, mid in messages(path):
        moment = house.parse_time(ts)
        if moment is None:
            continue
        local = moment.astimezone()
        name = user if who == "user" else claude
        if mid:
            name += ", while %s worked" % claude
        line = "**%s · %s**\n\n%s\n" % (local.strftime("%I:%M %p").lstrip("0"), name, scrub(text))
        days.setdefault(local.strftime("%Y-%m-%d"), []).append(line)
    return days


def title_of(path):
    """The window's name, if the app saved one."""
    title = ""
    with open(house.native(path), "rb") as f:
        for raw in f:
            if b'"custom-title"' in raw[:200]:
                try:
                    title = json.loads(raw.decode("utf-8", "replace")).get("customTitle") or title
                except ValueError:
                    pass
    return title


def write(days, folder, title):
    os.makedirs(folder, exist_ok=True)
    changed = 0
    for day, lines in sorted(days.items()):
        date = dt.date(*map(int, day.split("-")))
        body = "# %s, %s %d, %d\n\n%s" % (title, date.strftime("%A, %B"), date.day, date.year, "\n".join(lines))
        path = os.path.join(folder, day + ".md")
        try:
            with open(path, encoding="utf-8") as f:
                old = f.read()
        except OSError:
            old = None
        if old != body:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            changed += 1
    return changed


def main():
    ap = argparse.ArgumentParser(description="One conversation's words, one Markdown file per day.")
    ap.add_argument("transcript", help="the conversation's .jsonl file in ~/.claude/projects/<project>/")
    ap.add_argument("folder", help="where the day files go (inside a PRIVATE repository, if you push it)")
    ap.add_argument("--user", default="User", help="the name for the user's messages")
    ap.add_argument("--claude", default="Claude", help="the name for Claude's messages")
    ap.add_argument("--title", help="the heading on each day (default: the window's name, or 'Conversation')")
    args = ap.parse_args()
    if not os.path.isfile(args.transcript):
        sys.exit("No transcript at " + args.transcript)
    title = args.title or title_of(args.transcript) or "Conversation"
    days = by_day(args.transcript, args.user, args.claude)
    changed = write(days, args.folder, title)
    size = sum(len(line.encode("utf-8")) for lines in days.values() for line in lines)
    print("%d days, %d KB of words; %d day files written to %s" % (len(days), size // 1024, changed, args.folder))


if __name__ == "__main__":
    main()
