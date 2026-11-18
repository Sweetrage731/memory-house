#!/usr/bin/env python3
"""Shoulder tap: a Claude Code hook for PostToolUse and UserPromptSubmit.

Claude can't feel its context filling up. When it fills, Claude Code compacts the conversation into a summary.
This hook checks how full the context is after every tool call and on every message the user sends. When it
crosses a mark, it taps Claude once (extra context that Claude sees) and tells the user too (one short line):

  about 70% full: pack at the next pause (save new notes, update the index)
  about 80%: pack now, before starting anything long
  about 88%: last call, finish packing
  about 93%: time to compact; Claude says it's packed and suggests /compact

Each mark taps once. After a compaction (or /clear) the context is small again, and the marks start over.

How "full" is measured: the newest reply in the session's transcript records its token usage (input, cache
reads and writes, output). Their sum is the size of the context at that moment. The context window it's
compared with comes from, first match wins:
  1. --window or MEMORY_HOUSE_CONTEXT_WINDOW, in tokens (for example 200000, 200k or 1M)
  2. CLAUDE_CODE_AUTO_COMPACT_WINDOW, if you set that for Claude Code itself
  3. a guess from the model's name: 200,000 for older models (Opus 4.6 and Sonnet 4.6 without [1m],
     Haiku 4.5, Claude 4.5 and earlier), 1,000,000 for current ones
If the context ever grows past that number, the window must be bigger, so it switches to 1,000,000.
If you moved the point where compaction happens (with /autocompact or autoCompactWindow in your settings), set
MEMORY_HOUSE_CONTEXT_WINDOW to the same number so the marks line up.

The marks: --taps or MEMORY_HOUSE_TAPS, default "70,80,88,93".

It never blocks or slows anything: it gives up after one second, prints nothing on any error (it writes one
line to <temp dir>/memory-house/errors.log instead), and exits 0.
Proof that it runs: <temp dir>/memory-house[-<user id>]/shoulder-tap/heartbeat-<session>.txt is rewritten on
every check. Standard library only. Python 3.8 or newer, on Windows, macOS or Linux.

Try it by hand:
  echo '{"hook_event_name": "PostToolUse", "transcript_path": "/path/to/session.jsonl", "session_id": "test"}' \\
      | python3 shoulder_tap.py --window 200000
"""
import argparse
import json
import os
import re
import sys
import tempfile
import threading
import time

READ_LIMIT = 4 * 1024 * 1024       # the newest usage is near the end; 4 MB is plenty
DEADLINE = 1.0                     # seconds; after this the hook quits silently
DEFAULT_TAPS = "70,80,88,93"
RESET_DROP = 0.6                   # context fell below 60% of what it was at the last tap: start over
EVENTS_WITH_CONTEXT = ("PostToolUse", "UserPromptSubmit")
USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")

START = ("SHOULDER TAP (automatic, from the memory-house hook): your context is about {p}% full. When it fills, "
         "Claude Code will replace this conversation with a summary. Pack at the next natural pause, without "
         "waiting to be asked: save anything new and lasting as dated notes (with the user's exact words for "
         "anything they asked for), add a dated library entry for what happened and what is still open, update "
         "the open-loops line in MEMORY.md, then tell the user in one line that you packed. Then carry on.",
         "Shoulder tap: Claude's context is about {p}% full. It will pack its notes at the next pause.")
NOW = ("SHOULDER TAP (automatic): your context is about {p}% full. If you haven't packed yet, pack now, before "
       "starting anything long (big reads, long tool runs). If you have, add what happened since.",
       "Shoulder tap: Claude is about {p}% full and is packing now.")
LAST_CALL = ("SHOULDER TAP (automatic): your context is about {p}% full, and compaction is close. Finish packing "
             "now: the open-loops line first, then notes and the library. Keep replies and tool output short "
             "until it comes.",
             "Shoulder tap: Claude is about {p}% full. Last call to pack.")
COMPACT_SOON = ("SHOULDER TAP (automatic): your context is about {p}% full, and automatic compaction is very near. "
                "Finish packing, tell the user in one line that you are packed, and suggest they run /compact at "
                "the next pause: a compaction at a quiet moment is safer than one in the middle of a task. Keep "
                "replies short until then.",
                "Shoulder tap: Claude is about {p}% full. Time to compact (/compact) once it says it's packed.")


_writing = threading.Lock()


def give_up():
    if _writing.acquire(blocking=False):  # never quit halfway through writing the answer
        os._exit(0)


def tokens_from(text, minimum=1000):
    """'200000', '200k' or '1M' as a number of tokens, or None."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*$", str(text or ""))
    if not m:
        return None
    n = int(float(m.group(1)) * {"": 1, "k": 1000, "m": 1000000}[m.group(2).lower()])
    return n if n >= minimum else None


def model_window(model):
    """The context window a model runs with by default, guessed from its name."""
    name = str(model or "").lower()
    if "[1m]" in name:
        return 1000000
    if os.environ.get("CLAUDE_CODE_DISABLE_1M_CONTEXT", "").strip().lower() in ("1", "true", "yes"):
        return 200000
    m = re.search(r"claude-(?:(opus|sonnet|haiku|fable)-(\d+)(?:-(\d{1,2}))?(?!\d)|(\d)(?:-\d)?-(?:opus|sonnet|haiku))",
                  name)
    if not m:
        return 1000000                        # unknown or newer: current models default to 1M
    if m.group(4):
        return 200000                         # claude-3-5-sonnet and the like
    family, major, minor = m.group(1), int(m.group(2)), int(m.group(3) or 0)
    if family == "fable" or major >= 5:
        return 1000000
    if major < 4 or (major == 4 and minor <= 6):
        return 200000                         # Opus 4.6 and Sonnet 4.6 reach 1M only with [1m]
    return 1000000


def window_for(model, flag):
    explicit = tokens_from(flag) or tokens_from(os.environ.get("MEMORY_HOUSE_CONTEXT_WINDOW"))
    if explicit:
        return explicit
    natural = model_window(model)
    auto = tokens_from(os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"), minimum=1)
    if auto:
        return min(max(auto, 100000), 1000000, natural)   # Claude Code clamps it the same way
    return natural


def native(path):
    """On Windows, let a path longer than the old 260-character limit be opened too."""
    if os.name != "nt" or not path:
        return path
    full = os.path.abspath(path)
    if len(full) < 240 or full.startswith("\\\\?\\"):
        return path
    return "\\\\?\\UNC\\" + full[2:] if full.startswith("\\\\") else "\\\\?\\" + full


def latest_usage(path):
    """(tokens, model) at the newest reply in the main conversation, or (None, None) when a compaction came
    after it (the context is small again and the next reply will say how small)."""
    with open(native(path), "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - READ_LIMIT))
        lines = f.read().split(b"\n")
    if size > READ_LIMIT:
        lines = lines[1:]                     # the seek cut the first line in half
    for raw in reversed(lines):
        maybe_boundary = b"compact_boundary" in raw
        if not maybe_boundary and b'"usage"' not in raw:
            continue
        try:
            entry = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
            return None, None
        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        msg = entry.get("message") or {}
        if msg.get("model") == "<synthetic>":
            continue
        usage = msg.get("usage") or {}
        total = 0
        for key in USAGE_KEYS:
            try:
                total += int(usage.get(key) or 0)
            except (TypeError, ValueError):
                pass
        if total > 0:
            return total, str(msg.get("model") or "")
    return None, None


def taps_from(text):
    marks = sorted({int(x) for x in re.findall(r"\d+", str(text or "")) if 0 < int(x) < 100})
    return marks or [int(x) for x in DEFAULT_TAPS.split(",")]


def message_for(index, count):
    if index == count - 1 and count > 1:
        return COMPACT_SOON
    if index == count - 2 and count >= 3:
        return LAST_CALL
    return START if index == 0 else NOW


def state_dir():
    base = "memory-house"
    if hasattr(os, "getuid"):
        base += "-%d" % os.getuid()           # shared temp folders on Linux: one folder per user
    return os.path.join(tempfile.gettempdir(), base, "shoulder-tap")


def options(argv=None):
    parser = argparse.ArgumentParser(description="Shoulder tap (Claude Code PostToolUse / UserPromptSubmit hook)")
    parser.add_argument("--window", help="context window in tokens, e.g. 200000, 200k or 1M")
    parser.add_argument("--taps", help='the marks in percent, e.g. "70,80,88,93"')
    return parser.parse_known_args(argv)[0]   # unknown options are ignored, never fatal


def main():
    threading.Timer(DEADLINE, give_up).start()
    opts = options()
    if sys.stdin.isatty():
        print(__doc__)
        return
    hook = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    if not isinstance(hook, dict) or hook.get("agent_id"):
        return                                # a subagent's tool call: its context isn't the conversation's
    event = hook.get("hook_event_name") or "PostToolUse"
    if event not in EVENTS_WITH_CONTEXT:
        return
    path = hook.get("transcript_path") or ""
    if not path or not os.path.isfile(native(path)):
        return
    tokens, model = latest_usage(path)
    if tokens is None:
        return
    window = window_for(model, opts.window)
    if tokens > window:
        window = max(window, 1000000)         # it went past the guess, so the window is bigger
    pct = int(tokens * 100 / window)
    folder = state_dir()
    os.makedirs(folder, exist_ok=True)
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(hook.get("session_id") or "session"))[:80] or "session"
    with open(os.path.join(folder, "heartbeat-%s.txt" % sid), "w", encoding="utf-8") as f:
        f.write("%s %s %d%% (%d of %d tokens, %s)\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), event, pct, tokens,
                                                         window, model or "model unknown"))
    state_file = os.path.join(folder, sid + ".json")
    try:
        with open(state_file, encoding="utf-8") as f:
            state = json.load(f)
        told, told_tokens = int(state.get("told") or 0), int(state.get("tokens") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        told, told_tokens = 0, 0
    if told and tokens < told_tokens * RESET_DROP:
        told = 0                              # compacted or cleared since the last tap: start over
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump({"told": 0, "tokens": tokens}, f)
    marks = taps_from(opts.taps or os.environ.get("MEMORY_HOUSE_TAPS"))
    due = [m for m in marks if pct >= m > told]
    if not due:
        return
    mark = due[-1]                            # several at once (a big jump): only the highest one speaks
    to_claude, to_user = message_for(marks.index(mark), len(marks))
    answer = json.dumps({"systemMessage": to_user.format(p=pct),
                         "hookSpecificOutput": {"hookEventName": event, "additionalContext": to_claude.format(p=pct)}},
                        ensure_ascii=True)
    if not _writing.acquire(blocking=False):
        return                                # out of time: say nothing now, and tap on the next check
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({"told": mark, "tokens": tokens}, f)
    sys.stdout.write(answer)
    sys.stdout.flush()


def log_error(where):
    """One line per failure, in the same errors.log as the other hooks. Never raises."""
    try:
        exc = sys.exc_info()[1]
        folder = os.path.join(tempfile.gettempdir(), "memory-house")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "errors.log")
        if os.path.exists(path) and os.path.getsize(path) > 200000:
            os.remove(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s: %s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), where, type(exc).__name__,
                                          str(exc)[:300]))
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass
    except BaseException:
        log_error("shoulder_tap")
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(0)
