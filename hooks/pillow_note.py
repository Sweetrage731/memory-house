#!/usr/bin/env python3
"""Pillow note: a Claude Code PreCompact hook.

A compaction replaces the conversation with a summary that the model writes in its own words. Summaries
paraphrase, and a paraphrase can turn "back off the GPU for a minute" into "NEVER USE THE GPU". Seconds before
every compaction, this hook copies the stretch that is about to be summarized into a plain note, word for word:

  - the user's last messages, including the ones sent while Claude was busy (and a scheduled task's
    prompt, marked as written ahead of time)
  - what Claude said last before each of them
  - the task list, if the session has one
  - the files Claude wrote or edited

The note goes to <memory dir>/pillow/pillow-<date>-<time>-<session>.md. Right after the compaction,
after_compact.py tells Claude to read it first.

The hook also prints a few instructions for the summary itself. Claude Code uses a PreCompact hook's plain
output as extra instructions for the compaction summary. They ask it to quote the user, to keep temporary things
temporary, and never to invent rules.

Where the memory dir is (first match wins; a relative path starts at the project folder):
  1. --memory-dir PATH
  2. the MEMORY_HOUSE_DIR environment variable
  3. <project folder>/memory, where the project folder is $CLAUDE_PROJECT_DIR or the hook's "cwd"

Other options:
  --messages N        how many of the user's last messages to copy (default 30)
  --keep N            how many pillow notes to keep (default 30; 0 keeps them all)
  --no-summary-hints  save the note, but print nothing for the summary

It never blocks a compaction. On any error it writes one line to <temp dir>/memory-house/errors.log and exits 0.
Standard library only. Python 3.8 or newer, on Windows, macOS or Linux.

Try it by hand with any transcript from ~/.claude/projects/<project>/:
  echo '{"hook_event_name": "PreCompact", "transcript_path": "/path/to/session.jsonl", "session_id": "test"}' \\
      | python3 pillow_note.py --memory-dir /tmp/test-house
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
import time

READ_LIMIT = 64 * 1024 * 1024    # read at most the last 64 MB of the transcript
MESSAGE_LIMIT = 4000             # characters per user message; a longer one is cut, and the cut is marked
CLOSING_LIMIT = 1500             # characters per closing message from Claude
CLOSINGS_KEPT = 4
FILES_KEPT = 30
WAKE_MESSAGES = 10               # messages handed to after_compact.py for its inline copy
WAKE_LIMIT = 800
SESSION_FILE_DAYS = 30

# Text that Claude Code or another program put into a "user" turn. None of it is the user's own words.
MACHINE_STARTS = (
    "<task-notification", "<scheduled-task", "<local-command", "<command-name", "<command-message",
    "<command-args", "<bash-input", "<bash-stdout", "<bash-stderr", "<cross-session-message", "<agent-message",
    "<artifact-content-authored-by-others", "<user-prompt-submit-hook", "<side-sessions-event",
    "<ci-monitor-event", "Caveat: The messages below", "[Request interrupted",
    "This session is being continued from a previous", "Another Claude session sent a message",
)
# Turns that came from something other than the person at the keyboard.
NOT_HUMAN_TURNS = ("peer", "task_notification", "task-notification", "scheduled", "host_synthetic")
INJECTED = re.compile(r"<(system-reminder|user-prompt-submit-hook)\b[^>]*>.*?</\1>", re.S)
PASTE_TAG = re.compile(r"</?pasted_content\b[^>]*>")
# Bulky bookkeeping lines that never hold a message. Anything not listed here is read.
SKIP_TYPES = (b"api-request", b"file-history", b"queue-operation", b"bridge-session", b"last-prompt",
              b"frame-link", b"agent-name", b"atis-latch", b"tools-used", b"cost-state")
LEADING_TYPE = re.compile(rb'^\{"type":"([^"]{1,60})"')
MID_TASK = "sent while Claude was working"
SCHEDULED = "a scheduled task's prompt, written ahead of time"
ISO_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$")


# ---------- small helpers ----------

def log_error(where):
    """One line per failure, so a hook that stays silent can still be debugged. Never raises."""
    try:
        exc = sys.exc_info()[1]
        folder = os.path.join(tempfile.gettempdir(), "memory-house")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "errors.log")
        if os.path.exists(path) and os.path.getsize(path) > 200000:
            os.remove(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s: %s: %s\n" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), where,
                                          type(exc).__name__, str(exc)[:300]))
    except Exception:
        pass


def native(path):
    """On Windows, let a path longer than the old 260-character limit be opened too."""
    if os.name != "nt" or not path:
        return path
    full = os.path.abspath(path)
    if len(full) < 240 or full.startswith("\\\\?\\"):
        return path
    return "\\\\?\\UNC\\" + full[2:] if full.startswith("\\\\") else "\\\\?\\" + full


def project_dir(hook):
    return os.environ.get("CLAUDE_PROJECT_DIR") or hook.get("cwd") or os.getcwd()


def memory_dir(hook, flag=None):
    """Where the house is. A relative path starts at the project folder."""
    root = project_dir(hook)
    chosen = flag or os.environ.get("MEMORY_HOUSE_DIR")
    if chosen:
        return os.path.abspath(os.path.join(root, os.path.expanduser(chosen)))
    return os.path.abspath(os.path.join(root, "memory"))


def safe_name(session_id):
    return re.sub(r"[^A-Za-z0-9_-]", "", str(session_id or ""))[:80] or "unknown"


def parse_time(ts):
    """A transcript timestamp ("2026-03-14T22:02:11.123Z") as an aware datetime, or None."""
    m = ISO_TIME.match(str(ts or "").strip())
    if not m:
        return None
    base, frac, zone = m.groups()
    zone = "+00:00" if zone in (None, "Z") else zone
    if len(zone) == 5:
        zone = zone[:3] + ":" + zone[3:]
    try:
        return dt.datetime.fromisoformat("%s.%s%s" % (base, (frac or "0")[:6].ljust(6, "0"), zone))
    except ValueError:
        return None


def stamp(moment):
    """A friendly local time like "Mar 14, 9:58 PM"."""
    return "%s %d, %s" % (moment.strftime("%b"), moment.day, moment.strftime("%I:%M %p").lstrip("0"))


def when(ts):
    moment = parse_time(ts)
    if moment is None:
        return ""
    try:
        return stamp(moment.astimezone())
    except (ValueError, OverflowError, OSError):
        return ""


def cut(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[... %d more characters not copied here; the transcript has them all]" % (
        len(text) - limit)


def quote(text):
    """The text as a Markdown block quote, line by line, unchanged."""
    return "\n".join("> " + line if line.strip() else ">" for line in text.split("\n"))


# ---------- whose words are these? ----------

def pieces_of(content):
    """A message's content as pieces of text: a plain string, or a list of blocks (pictures marked)."""
    if isinstance(content, str):
        return [content]
    pieces = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            pieces.append(str(block.get("text") or ""))
        elif kind == "image":
            pieces.append("[image]")
        elif kind == "document":
            pieces.append("[document]")
    return pieces


def text_of(content):
    return "\n".join(p for p in pieces_of(content) if p)


def users_words(content):
    """The user's own words in a message's content, once the bits Claude Code added are taken out.
    Each piece is judged on its own, so a machine note next to a real message doesn't hide the message."""
    kept = []
    for piece in pieces_of(content):
        piece = PASTE_TAG.sub("", INJECTED.sub("", piece.replace("\r\n", "\n"))).strip()
        if piece and not piece.startswith(MACHINE_STARTS):
            kept.append(piece)
    return "\n".join(kept)


def from_a_person(origin):
    """Older Claude Code leaves "origin" out. Newer versions say who sent the turn."""
    return not isinstance(origin, dict) or origin.get("kind") in (None, "human")


def typed_message(entry):
    """The user's own words in a "user" entry; "" for tool results, reminders and other machine turns."""
    if entry.get("type") != "user":
        return ""
    if any(entry.get(k) for k in ("isMeta", "isCompactSummary", "isSidechain", "isVisibleInTranscriptOnly")):
        return ""
    if not from_a_person(entry.get("origin")) or entry.get("turnOrigin") in NOT_HUMAN_TURNS:
        return ""
    return users_words((entry.get("message") or {}).get("content"))


def scheduled_prompt(entry):
    """A scheduled task's prompt: the user's own words, written ahead of time."""
    if entry.get("type") != "user":
        return ""
    if any(entry.get(k) for k in ("isMeta", "isCompactSummary", "isSidechain", "isVisibleInTranscriptOnly")):
        return ""
    pieces = [PASTE_TAG.sub("", INJECTED.sub("", p.replace("\r\n", "\n"))).strip()
              for p in pieces_of((entry.get("message") or {}).get("content"))]
    text = "\n".join(p for p in pieces if p)
    if entry.get("turnOrigin") == "scheduled" or text.startswith("<scheduled-task"):
        return text
    return ""


def rides_with_tool_results(entry):
    """True when the user's words came in alongside tool results, which means Claude was mid-task."""
    content = (entry.get("message") or {}).get("content")
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                             for b in content)


def queued_message(entry):
    """A message the user sent while Claude was busy. Claude Code files these as "queued_command" attachments."""
    a = entry.get("attachment") if entry.get("type") == "attachment" else None
    if not isinstance(a, dict) or a.get("type") != "queued_command" or a.get("isMeta"):
        return ""
    if a.get("commandMode") not in (None, "prompt") or not from_a_person(a.get("origin")):
        return ""
    return users_words(a.get("prompt"))


def claude_text(entry):
    """Claude's visible words in an "assistant" entry: no thinking, no tool calls."""
    if entry.get("type") != "assistant" or entry.get("isSidechain") or entry.get("isApiErrorMessage"):
        return ""
    msg = entry.get("message") or {}
    if msg.get("model") == "<synthetic>":
        return ""
    blocks = msg.get("content")
    if isinstance(blocks, str):
        return blocks.strip()
    return "\n".join(str(b.get("text") or "") for b in blocks or []
                     if isinstance(b, dict) and b.get("type") == "text").strip()


# ---------- the task list ----------

class TaskList:
    """Follows whichever task tool the session used: TodoWrite, TaskCreate and TaskUpdate, or the Claude desktop
    app's update_plan. Newer models often keep no written list at all; then this stays empty."""

    def __init__(self):
        self.items, self.order, self.waiting, self.source = {}, [], {}, ""

    def replace(self, rows, source):
        self.items, self.order, self.waiting, self.source = {}, [], {}, source
        for i, row in enumerate(rows, 1):
            self.items[str(i)] = row
            self.order.append(str(i))

    def step(self, inp):
        """update_plan without a new list: {"advance": true}, or {"done": [2], "start": 3} (1-based)."""
        rows = [self.items[k] for k in self.order]
        if inp.get("advance"):
            current = next((r for r in rows if r["status"] == "in_progress"), None)
            if current:
                current["status"] = "completed"
            upcoming = next((r for r in rows if r["status"] == "pending"), None)
            if upcoming:
                upcoming["status"] = "in_progress"
        for k in inp.get("done") or []:
            if isinstance(k, int) and 1 <= k <= len(rows):
                rows[k - 1]["status"] = "completed"
        k = inp.get("start")
        if isinstance(k, int) and 1 <= k <= len(rows):
            rows[k - 1]["status"] = "in_progress"

    def tool(self, name, inp, use_id):
        if name == "TodoWrite" and isinstance(inp.get("todos"), list):
            self.replace([{"content": str(t.get("content") or t.get("activeForm") or ""),
                           "status": str(t.get("status") or "pending")}
                          for t in inp["todos"] if isinstance(t, dict)], "TodoWrite")
        elif name == "update_plan":
            if isinstance(inp.get("items"), list) and inp["items"]:
                self.replace([{"content": str(i.get("content") or ""), "status": str(i.get("status") or "pending")}
                              for i in inp["items"] if isinstance(i, dict)], "the task list")
            else:
                self.step(inp)
        elif name == "TaskCreate":
            key = "new-%d" % (len(self.order) + 1)
            self.items[key] = {"content": str(inp.get("subject") or inp.get("description") or ""),
                               "status": "pending"}
            self.order.append(key)
            if use_id:
                self.waiting[use_id] = key
            self.source = "TaskCreate and TaskUpdate"
        elif name == "TaskUpdate":
            key = str(inp.get("taskId") or inp.get("id") or "")
            if not key:
                return
            if key not in self.items:
                self.items[key] = {"content": "task #" + key, "status": "?"}
                self.order.append(key)
            if inp.get("status") == "deleted":
                self.items.pop(key, None)
                self.order.remove(key)
                return
            if inp.get("status"):
                self.items[key]["status"] = str(inp["status"])
            if inp.get("subject"):
                self.items[key]["content"] = str(inp["subject"])
            self.source = "TaskCreate and TaskUpdate"

    def results(self, entry):
        """A TaskCreate result says which number the new task got, for example "Task #3 created"."""
        if not self.waiting:
            return
        content = (entry.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            key = self.waiting.pop(block.get("tool_use_id"), None)
            if not key:
                continue
            number, extra = None, entry.get("toolUseResult")
            if isinstance(extra, dict):
                task = extra.get("task") if isinstance(extra.get("task"), dict) else extra
                number = task.get("id") or task.get("taskId")
            if number is None:
                m = re.search(r"#(\d+)", text_of(block.get("content")))
                number = m.group(1) if m else None
            if number is not None and str(number) not in self.items:
                self.items[str(number)] = self.items.pop(key)
                self.order[self.order.index(key)] = str(number)

    def rows(self):
        return [dict(self.items[k]) for k in self.order]


# ---------- reading the transcript ----------

def read_entries(path):
    """Parsed transcript entries from the last READ_LIMIT bytes, oldest first."""
    with open(native(path), "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        if size > READ_LIMIT:
            f.seek(size - READ_LIMIT)
            f.readline()                      # the seek landed mid-line; skip that partial line
        else:
            f.seek(0)
        for raw in f:
            if not raw.strip():
                continue
            m = LEADING_TYPE.match(raw)
            if m and m.group(1).startswith(SKIP_TYPES):
                continue
            try:
                entry = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(entry, dict):
                yield entry


def gather(path):
    """Everything since the last compaction: the user's messages, Claude's closing words, files touched.
    The task list and the conversation title carry across compactions."""
    got = {"messages": [], "closings": [], "files": [], "custom_title": "", "ai_title": ""}
    tasks, pending = TaskList(), None
    for entry in read_entries(path):
        kind = entry.get("type")
        if kind == "system" and entry.get("subtype") == "compact_boundary":
            got["messages"], got["closings"], got["files"], pending = [], [], [], None
            continue
        if kind == "custom-title":
            got["custom_title"] = str(entry.get("customTitle") or got["custom_title"])
            continue
        if kind == "ai-title":
            got["ai_title"] = str(entry.get("aiTitle") or got["ai_title"])
            continue
        ts, how = entry.get("timestamp") or "", ""
        text = typed_message(entry)
        if text and rides_with_tool_results(entry):
            how = MID_TASK
        if not text:
            text = queued_message(entry)
            if text:
                how = MID_TASK
                ts = (entry.get("attachment") or {}).get("timestamp") or ts
        if not text:
            text = scheduled_prompt(entry)
            how = SCHEDULED if text else ""
        if text:
            if pending:
                got["closings"].append(pending)
                pending = None
            last = got["messages"][-1] if got["messages"] else None
            if (last and last[1] == text and MID_TASK in (last[2], how) and abs_seconds(last[0], ts) < 300):
                continue                      # the same message recorded twice (queued, then delivered)
            got["messages"].append((ts, text, how))
            continue
        if kind == "user":
            tasks.results(entry)
        elif kind == "assistant" and not entry.get("isSidechain"):
            said = claude_text(entry)
            if said:
                pending = (ts, said, False)
            content = (entry.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name") or "").rsplit("__", 1)[-1]
                inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                    target = inp.get("file_path") or inp.get("notebook_path")
                    if target:
                        got["files"].append(str(target))
                else:
                    tasks.tool(name, inp, block.get("id"))
    if pending:
        got["closings"].append((pending[0], pending[1], True))
    newest_first = []
    for target in reversed(got["files"]):
        if target not in newest_first:
            newest_first.append(target)
    got["files"] = newest_first[:FILES_KEPT]
    got["tasks"], got["task_source"] = tasks.rows(), tasks.source
    got["title"] = got["custom_title"] or got["ai_title"]
    return got


def abs_seconds(a, b):
    ta, tb = parse_time(a), parse_time(b)
    if ta is None or tb is None:
        return 0
    return abs((tb - ta).total_seconds())


# ---------- writing the note ----------

def note_text(hook, got, now, count):
    sid = str(hook.get("session_id") or "unknown")
    trigger = str(hook.get("trigger") or "")
    how = {"auto": "automatic, because the conversation was full",
           "manual": "manual, because someone ran /compact"}.get(trigger, "trigger unknown")
    msgs = got["messages"][-count:] if count > 0 else []
    dropped = len(got["messages"]) - len(msgs)
    w = ["# Pillow note: %s %d %d, %s" % (now.strftime("%b"), now.day, now.year,
                                          now.strftime("%I:%M %p").lstrip("0")), "",
         "Saved by the memory-house PreCompact hook seconds before a compaction (%s)." % how,
         "Session: `%s`" % sid]
    if got["title"]:
        w.append("Conversation: %s" % got["title"])
    if hook.get("custom_instructions"):
        w.append("Focus asked for with /compact: %s" % " ".join(str(hook["custom_instructions"]).split()))
    w += ["",
          "Everything below was copied from the transcript as it was. Nothing in it is summarized.",
          "After the compaction: read this first, compare it with the summary, put back anything missing or",
          "warped, and tell the user in one line whether anything was lost.",
          "", "## The user's last messages, word for word (oldest first)", ""]
    if dropped > 0:
        w += ["(%d earlier message%s since the last compaction %s in the transcript.)" % (
            dropped, "" if dropped == 1 else "s", "is" if dropped == 1 else "are"), ""]
    if not msgs:
        w += ["(No messages from the user since the last compaction.)", ""]
    for number, (ts, text, how) in enumerate(msgs, dropped + 1):
        head = "**%d. %s**" % (number, when(ts) or "time unknown")
        if how:
            head += " (%s)" % how
        w += [head, "", quote(cut(text, MESSAGE_LIMIT)), ""]
    w += ["## What Claude said just before each new message, and last of all (oldest first)", ""]
    closings = got["closings"][-CLOSINGS_KEPT:]
    if not closings:
        w += ["(Nothing yet.)", ""]
    for ts, text, latest in closings:
        head = "**%s**" % (when(ts) or "time unknown")
        if latest:
            head += " (the latest; it may have been mid-task)"
        w += [head, "", quote(cut(text, CLOSING_LIMIT)), ""]
    if got["tasks"]:
        w += ["## Task list (from %s; it can be out of date, so check before redoing a step)" % got["task_source"],
              ""]
        w += ["- [%s] %s" % (t["status"], t["content"]) for t in got["tasks"]]
        w.append("")
    else:
        w += ["## Task list", "", "(No written task list in this session.)", ""]
    w += ["## Files Claude wrote or edited (newest first)", ""]
    w += ["- `%s`" % p for p in got["files"]] or ["(None.)"]
    w += ["", "## Where the full record is", "",
          "The transcript is still on disk, word for word: `%s`" % (hook.get("transcript_path") or "?"), ""]
    return "\n".join(w)


def write_file(path, text):
    """Write the whole file or nothing: a half-written note is worse than none."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def save(hook, opts):
    """Write the pillow note and its small companion file. Returns the note's path, or None."""
    transcript = hook.get("transcript_path") or ""
    if not transcript or not os.path.isfile(native(transcript)):
        return None
    time.sleep(0.3)                           # the transcript is written a moment behind; let it catch up
    got = gather(transcript)
    now = dt.datetime.now().astimezone()
    house = memory_dir(hook, opts.memory_dir)
    folder = os.path.join(house, "pillow")
    sessions = os.path.join(folder, ".sessions")
    first_time = not os.path.isdir(folder)
    os.makedirs(sessions, exist_ok=True)
    if first_time:
        write_file(os.path.join(folder, ".gitignore"),
                   "# Pillow notes hold your words word for word. Keep them out of git.\n"
                   "# Delete this file only if this folder lives in a PRIVATE repository.\n*\n")
    sid = safe_name(hook.get("session_id"))
    note = os.path.join(folder, "pillow-%s-%s.md" % (now.strftime("%Y-%m-%d-%H%M%S"), sid[:8]))
    write_file(note, note_text(hook, got, now, opts.messages))
    companion = {
        "note": note, "session_id": str(hook.get("session_id") or ""), "saved_at": now.isoformat(),
        "trigger": hook.get("trigger") or "", "transcript_path": transcript,
        "messages": [{"when": when(ts), "text": cut(text, WAKE_LIMIT), "mid_task": how == MID_TASK, "how": how}
                     for ts, text, how in got["messages"][-WAKE_MESSAGES:]],
        "tasks": got["tasks"], "files": got["files"][:10],
    }
    write_file(os.path.join(sessions, sid + ".json"), json.dumps(companion, ensure_ascii=False, indent=1))
    tidy(folder, sessions, opts.keep)
    return note


def tidy(folder, sessions, keep):
    """Keep the newest `keep` notes (0 keeps all) and drop companion files older than a month."""
    if keep > 0:
        notes = sorted(n for n in os.listdir(folder) if n.startswith("pillow-") and n.endswith(".md"))
        for old in notes[:-keep]:
            os.remove(os.path.join(folder, old))
    limit = time.time() - SESSION_FILE_DAYS * 86400
    for name in os.listdir(sessions):
        path = os.path.join(sessions, name)
        if name.endswith(".json") and os.path.getmtime(path) < limit:
            os.remove(path)


def summary_hints(note):
    """Plain text that Claude Code passes to the compaction as extra instructions."""
    lines = [
        "Instructions for this summary, from the memory-house PreCompact hook:",
        "1. When the user gave an instruction, preference, correction or decision, quote their exact words in "
        "quotation marks. Do not rewrite them as rules in your own words.",
        "2. Keep temporary things temporary. If the user said \"for a minute\", \"for now\", \"until ...\" or "
        "similar, say so, and include any later message that ended or changed it (for example \"you can use it "
        "again\"). An instruction the user lifted is not a current rule.",
        "3. Do not state any rule, ban or preference that the user did not say. If something is your own "
        "inference, label it \"(my inference)\".",
        "4. Keep open tasks, promises, decisions and file paths that are still pending.",
    ]
    if note:
        lines.append("5. End the summary with this line: The user's last messages are saved word for word in the "
                     "pillow note at %s. Read it first and check this summary against it." % note)
    return "\n".join(lines) + "\n"


def options(argv=None):
    parser = argparse.ArgumentParser(description="Pillow note (Claude Code PreCompact hook)")
    parser.add_argument("--memory-dir", help="the memory folder (default: <project>/memory)")
    parser.add_argument("--messages", type=int, default=30, help="how many of the user's messages to copy")
    parser.add_argument("--keep", type=int, default=30, help="how many pillow notes to keep (0 keeps all)")
    parser.add_argument("--no-summary-hints", action="store_true", help="print nothing for the summary")
    return parser.parse_known_args(argv)[0]   # unknown options are ignored, never fatal


def main():
    opts = options()
    if sys.stdin.isatty():
        print(__doc__)
        return
    hook = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    if not isinstance(hook, dict):
        return
    if hook.get("hook_event_name") not in (None, "PreCompact") or hook.get("agent_id"):
        return                                # wrong event, or a subagent compacting its own context
    note = None
    try:
        note = save(hook, opts)
    except Exception:
        log_error("pillow_note")
    if not opts.no_summary_hints:
        sys.stdout.buffer.write(summary_hints(note).encode("utf-8"))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass
    except BaseException:
        log_error("pillow_note")
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(0)
