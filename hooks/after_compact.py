#!/usr/bin/env python3
"""Wake-up check: a Claude Code SessionStart hook, for the "compact" matcher.

Right after a compaction, Claude wakes up holding only a summary. This hook puts a short message in front of it:
read the newest pillow note first (pillow_note.py saved it seconds before the compaction), compare it with the
summary, put back anything missing or warped, and tell the user in one line whether anything is missing from your notes.
The user's last few messages are included inline too, so Claude sees their exact words before it even opens
the note.

If no fresh pillow note exists (the PreCompact hook didn't run), it still warns Claude that the summary
paraphrases, and points it at the transcript, which keeps every word.

Where the memory dir is: the same rules as pillow_note.py (--memory-dir, then MEMORY_HOUSE_DIR, then
<project folder>/memory). Options:
  --memory-dir PATH
  --max-age-hours N   a pillow note older than this belongs to an earlier compaction (default 2)

More than one AI on this computer? Two optional checks keep the hook to its own AI's windows. A flag wins over
its environment variable, and when a check can't tell (a new window with no reply yet, a missing transcript,
any error), the hook works as usual:
  --model TEXT or MEMORY_HOUSE_MODEL           act only when the window's newest reply came from a model whose
                                               id contains TEXT, in any case (for example opus)
  --skip-dirs LIST or MEMORY_HOUSE_SKIP_DIRS   do nothing when the folder the window started in is one of these
                                               folders or inside one; separate them with ;. The start folder is
                                               $CLAUDE_PROJECT_DIR, or the hook's "cwd" when that isn't set

It never fails loudly. On any error it writes one line to <temp dir>/memory-house/errors.log and exits 0.
Standard library only. Python 3.8 or newer, on Windows, macOS or Linux.

Try it by hand:
  echo '{"hook_event_name": "SessionStart", "source": "compact", "session_id": "test"}' \\
      | python3 after_compact.py --memory-dir /tmp/test-house
"""
import argparse
import datetime as dt
import glob
import json
import os
import posixpath
import re
import sys
import tempfile

MAX_OUTPUT = 9000          # characters; keeps the wake-up message well under Claude Code's limits
LINE_LIMIT = 500           # characters per inline message
MODEL_READ_LIMIT = 2 * 1024 * 1024   # whose window is this: the newest reply is near the end; 2 MB is plenty


def log_error(where):
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


def memory_dir(hook, flag=None):
    """Where the house is. A relative path starts at the project folder. Same rules as pillow_note.py."""
    root = os.environ.get("CLAUDE_PROJECT_DIR") or hook.get("cwd") or os.getcwd()
    chosen = flag or os.environ.get("MEMORY_HOUSE_DIR")
    if chosen:
        return os.path.abspath(os.path.join(root, os.path.expanduser(chosen)))
    return os.path.abspath(os.path.join(root, "memory"))


def safe_name(session_id):
    return re.sub(r"[^A-Za-z0-9_-]", "", str(session_id or ""))[:80] or "unknown"


def native(path):
    """On Windows, let a path longer than the old 260-character limit be opened too."""
    if os.name != "nt" or not path:
        return path
    full = os.path.abspath(path)
    if len(full) < 240 or full.startswith("\\\\?\\"):
        return path
    return "\\\\?\\UNC\\" + full[2:] if full.startswith("\\\\") else "\\\\?\\" + full


# ---------- whose window is this? ----------

def folder_key(path):
    """A folder in a form that compares well: forward slashes, no trailing slash, lower case on Windows."""
    text = os.path.expanduser(str(path or "").strip())
    if not text:
        return ""
    text = posixpath.normpath(text.replace("\\", "/"))
    return text.lower() if os.name == "nt" else text


def in_skipped_folder(path, skip_dirs):
    """True when path is one of the folders in skip_dirs (separated by ;) or inside one. A folder covers its
    subfolders, never a neighbor whose name only starts the same ("Other" doesn't cover "Others").
    No path, or any error: False."""
    if not skip_dirs:
        return False
    try:
        here = folder_key(path)
        for folder in str(skip_dirs).split(";"):
            folder = folder_key(folder)
            if here and folder and (here == folder or here.startswith(folder.rstrip("/") + "/")):
                return True
    except Exception:
        log_error("whose-window check")
    return False


def window_model(path):
    """The model id behind the newest reply in the main conversation, read from the end of the transcript,
    or "" when there's no reply yet. Subagents' lines (they can run another model) and stand-ins such as
    "<synthetic>" are passed over."""
    with open(native(path), "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - MODEL_READ_LIMIT))
        lines = f.read().split(b"\n")
    if size > MODEL_READ_LIMIT:
        lines = lines[1:]                     # the seek cut the first line in half
    for raw in reversed(lines):
        if b'"assistant"' not in raw or b'"model"' not in raw:
            continue
        try:
            entry = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        message = entry.get("message")
        model = str(message.get("model") or "").strip() if isinstance(message, dict) else ""
        if model and not model.startswith("<"):
            return model
    return ""


def runs_another_model(transcript, wanted):
    """True when the newest reply came from a model whose id doesn't contain `wanted` (like "opus"), in any
    case. Nothing wanted, no transcript, no reply yet, or any error: False."""
    wanted = str(wanted or "").strip().lower()
    if not wanted or not transcript:
        return False
    try:
        model = window_model(transcript) if os.path.isfile(native(transcript)) else ""
    except Exception:
        log_error("whose-window check")
        return False
    return bool(model) and wanted not in model.lower()


def someone_elses_window(hook, model=None, skip_dirs=None):
    """True when this window belongs to another AI that shares the computer, so the hook must do nothing:
    the folder the window started in is in --skip-dirs (or MEMORY_HOUSE_SKIP_DIRS), or --model (or
    MEMORY_HOUSE_MODEL) is set and the newest reply came from a different model. The start folder is
    $CLAUDE_PROJECT_DIR, which Claude Code sets for hooks and which stays put when the window cds somewhere
    else; only without it, the hook's "cwd". Whenever it can't tell, it says False and the hook works as
    usual: a broken check must never cost the owner a note."""
    started_in = (os.environ.get("CLAUDE_PROJECT_DIR") or "").strip() or hook.get("cwd")
    return (in_skipped_folder(started_in, skip_dirs or os.environ.get("MEMORY_HOUSE_SKIP_DIRS"))
            or runs_another_model(hook.get("transcript_path"), model or os.environ.get("MEMORY_HOUSE_MODEL")))


# ---------- the wake-up message ----------

def find_note(house, session_id, max_age_hours):
    """The pillow note saved for this session, as the dict pillow_note.py wrote, or None.
    The dict gets "stale": True when the note is older than max_age_hours (an earlier compaction's)."""
    folder = os.path.join(house, "pillow", ".sessions")
    if session_id:
        candidates = [os.path.join(folder, safe_name(session_id) + ".json")]
    else:
        candidates = sorted(glob.glob(os.path.join(folder, "*.json")), key=os.path.getmtime, reverse=True)[:1]
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(info, dict) or not info.get("note"):
            continue
        try:
            saved = dt.datetime.fromisoformat(str(info.get("saved_at")))
            age = dt.datetime.now(saved.tzinfo) - saved if saved.tzinfo else dt.datetime.now() - saved
        except (ValueError, TypeError):
            age = dt.timedelta(days=365)
        info["stale"] = age > dt.timedelta(hours=max_age_hours) or not os.path.isfile(info["note"])
        info["age_minutes"] = int(age.total_seconds() // 60)
        return info
    return None


def one_line(text, limit=LINE_LIMIT):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + " [...]"


def wake_text(info, hook):
    transcript = hook.get("transcript_path") or (info or {}).get("transcript_path") or ""
    head = ["A COMPACTION JUST HAPPENED. Everything before this point is now a summary, written in the model's "
            "own words, and summaries paraphrase."]
    tail = []
    if transcript:
        tail = ["", "The full transcript is still on disk, word for word: %s" % transcript]
    if not info or info.get("stale"):
        body = ["", "No pillow note was saved for this compaction (the PreCompact hook may not have run)."]
        if info:
            body.append("The newest pillow note for this session is from an earlier compaction, about %d minutes "
                        "ago: %s" % (info.get("age_minutes", 0), info.get("note")))
        body += ["Be careful with the summary. Before you rely on any rule, ban or preference in it, find the "
                 "user's exact words in the transcript. Tell the user in one line that the pillow note was "
                 "missing, so they can check the setup."]
        return "\n".join(head + body + tail)
    body = ["",
            "Before you do anything else:",
            "1. Read the pillow note: %s" % info["note"],
            "   It was saved seconds before the compaction. It holds the user's last messages word for word, "
            "what you said just before each one, the task list and the files you touched.",
            "2. Compare it with the summary. Check every rule, ban and preference in the summary against the "
            "user's exact words. If the summary states a rule the user never said, makes something temporary "
            "sound permanent (\"for a minute\" turned into \"never\"), or misses a later message that lifted it, "
            "the user's words win. Fix your understanding, and fix any memory note that got it wrong by adding "
            "a new dated line.",
            "3. Put back anything the summary lost: open tasks, promises, decisions, file paths.",
            "4. Tell the user in one line whether anything is missing from your notes or got corrected (say \"nothing is missing from my notes\", never \"nothing was lost\"). Then carry on.",
            "Your front door (CLAUDE.md) and memory index were reloaded from disk; for older things, they are "
            "the record."]
    tasks = info.get("tasks") or []
    if tasks:
        body += ["", "Task list then (it may be out of date): " + "; ".join(
            "[%s] %s" % (t.get("status"), one_line(t.get("content"), 160)) for t in tasks[:20])]
    def label(m):
        if m.get("mid_task"):
            return " (sent while you were working)"
        return " (%s)" % m["how"] if m.get("how") else ""

    lines = ["%s%s: \"%s\"" % (m.get("when") or "time unknown", label(m), one_line(m.get("text")))
             for m in info.get("messages") or [] if isinstance(m, dict)]
    while lines:
        quoted = ["", "The user's last messages before the compaction (oldest first; shortened here, "
                      "complete in the note):"] + ["- " + line for line in lines]
        text = "\n".join(head + body + quoted + tail)
        if len(text) <= MAX_OUTPUT:
            return text
        lines = lines[1:]                     # too long: drop the oldest first
    return "\n".join(head + body + tail)[:MAX_OUTPUT]


def options(argv=None):
    parser = argparse.ArgumentParser(description="Wake-up check (Claude Code SessionStart hook, matcher compact)")
    parser.add_argument("--memory-dir", help="the memory folder (default: <project>/memory)")
    parser.add_argument("--max-age-hours", type=float, default=2.0, help="older notes are an earlier compaction's")
    parser.add_argument("--model", help="act only in windows whose newest reply came from a model with this in "
                                        "its id, e.g. opus (or set MEMORY_HOUSE_MODEL)")
    parser.add_argument("--skip-dirs", help="other AIs' folders, separated by ;: do nothing in windows started "
                                            "in them (or set MEMORY_HOUSE_SKIP_DIRS)")
    return parser.parse_known_args(argv)[0]   # unknown options are ignored, never fatal


def main():
    opts = options()
    if sys.stdin.isatty():
        print(__doc__)
        return
    hook = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    if not isinstance(hook, dict):
        return
    if hook.get("hook_event_name") not in (None, "SessionStart") or hook.get("agent_id"):
        return
    if hook.get("source") not in (None, "compact"):
        return                                # a fresh start, a resume or a /clear: nothing was compacted
    if someone_elses_window(hook, opts.model, opts.skip_dirs):
        return                                # another AI's window on this computer: its own hooks look after it
    info = find_note(memory_dir(hook, opts.memory_dir), hook.get("session_id"), opts.max_age_hours)
    out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": wake_text(info, hook)}}
    if info and not info.get("stale"):
        out["systemMessage"] = "Memory house: Claude will read its pillow note before carrying on."
    else:
        out["systemMessage"] = "Memory house: no fresh pillow note was found for this compaction."
    sys.stdout.write(json.dumps(out, ensure_ascii=True))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass
    except BaseException:
        log_error("after_compact")
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(0)
