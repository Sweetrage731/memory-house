#!/usr/bin/env python3
"""Self-test for the memory house. Standard library only.

  python3 tests/test_hooks.py          (add -v to see each test's name)

It builds fake Claude Code transcripts in a temporary folder, pipes sample hook input into each script the
same way Claude Code will (through the guard line from tools/print_settings.py), and checks what comes out.
Everything happens inside the temporary folder, which is deleted afterwards. All the content is invented.
"""
import ast
import datetime as dt
import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
HOOKS = os.path.join(ROOT, "hooks")
TOOLS = os.path.join(ROOT, "tools")
SID = "3f2a9c1e-77aa-4c5e-9d1b-2b7c0d9e4f10"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


settings_tool = load(os.path.join(TOOLS, "print_settings.py"), "print_settings")
pillow = load(os.path.join(HOOKS, "pillow_note.py"), "pillow_note_under_test")
tap = load(os.path.join(HOOKS, "shoulder_tap.py"), "shoulder_tap_under_test")


# ---------- a fake transcript, shaped like the real ones ----------

class Transcript:
    def __init__(self, path, compact_json=True):
        self.path, self.compact, self.n = path, compact_json, 0
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()

    def ts(self, minute):
        return "2026-03-15T%02d:%02d:00.000Z" % (2 + minute // 60, minute % 60)

    def add(self, entry):
        self.n += 1
        entry.setdefault("uuid", "u-%d" % self.n)
        entry.setdefault("sessionId", SID)
        sep = (",", ":") if self.compact else (", ", ": ")
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False, separators=sep) + "\n")

    def user(self, content, minute, **extra):
        e = {"parentUuid": None, "isSidechain": False, "type": "user",
             "message": {"role": "user", "content": content}, "timestamp": self.ts(minute), "cwd": "/fake"}
        e.update(extra)
        self.add(e)

    def human(self, content, minute, **extra):       # the shape the desktop app writes
        self.user(content, minute, origin={"kind": "human"}, promptSource="sdk", **extra)

    def assistant(self, blocks, minute, usage=None, model="claude-opus-5-5", **extra):
        u = usage or {"input_tokens": 3, "cache_creation_input_tokens": 500, "cache_read_input_tokens": 40000,
                      "output_tokens": 120}
        e = {"parentUuid": None, "isSidechain": False, "type": "assistant", "timestamp": self.ts(minute),
             "message": {"model": model, "id": "msg_%d" % self.n, "type": "message", "role": "assistant",
                         "content": blocks, "usage": u}}
        e.update(extra)
        self.add(e)

    def say(self, text, minute, **kw):
        self.assistant([{"type": "text", "text": text}], minute, **kw)

    def tool(self, name, inp, minute, use_id=None):
        self.assistant([{"type": "tool_use", "id": use_id or "toolu_%d" % self.n, "name": name, "input": inp}],
                       minute)

    def result(self, use_id, text, minute, extra=None):
        e = {"parentUuid": None, "isSidechain": False, "type": "user", "timestamp": self.ts(minute),
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use_id,
                                                      "content": text}]},
             "toolUseResult": extra if extra is not None else {"type": "text"}}
        self.add(e)

    def queued(self, prompt, minute, kind="human", mode="prompt", meta=False):
        a = {"type": "queued_command", "prompt": prompt, "commandMode": mode, "timestamp": self.ts(minute)}
        if kind:
            a["origin"] = {"kind": kind}
        if meta:
            a["isMeta"] = True
        self.add({"parentUuid": None, "isSidechain": False, "type": "attachment", "attachment": a,
                  "timestamp": self.ts(minute)})

    def boundary(self, minute, pre=967000):
        self.add({"parentUuid": None, "isSidechain": False, "type": "system", "subtype": "compact_boundary",
                  "content": "Conversation compacted", "compactMetadata": {"trigger": "auto", "preTokens": pre},
                  "timestamp": self.ts(minute)})


VERBATIM = ('Line one with "quotes" and \\backslashes\\\n\n# not a heading\n* not a bullet\n'
            '    indented line   \ncafé ☕ 日本語')


def build_main(path, compact_json=True):
    t = Transcript(path, compact_json)
    t.human("OLD MESSAGE from before the last compaction", 0)
    t.say("old reply", 1)
    t.tool("TodoWrite", {"todos": [
        {"content": "Fix the header layout", "status": "completed", "activeForm": "Fixing"},
        {"content": "Re-run the screenshot tests", "status": "in_progress", "activeForm": "Re-running"},
        {"content": "Write the release note", "status": "pending", "activeForm": "Writing"}]}, 2)
    t.add({"type": "custom-title", "customTitle": "Render pipeline cleanup"})
    t.boundary(3)
    t.user("This session is being continued from a previous conversation that ran out of context.", 3,
           isCompactSummary=True, isVisibleInTranscriptOnly=True)
    t.human("back off the GPU for a minute, I'm rendering", 4)
    t.say("Okay. I'll stay off the GPU until you say the render is done.", 5)
    t.tool("Edit", {"file_path": "/fake/project/src/header.css", "old_string": "a", "new_string": "b"}, 6,
           use_id="toolu_edit")
    t.result("toolu_edit", "The file has been updated.", 6)
    t.user([{"type": "text", "text": "[Image #1]"}], 7, isMeta=True)
    t.human("<system-reminder>The file header.css changed on disk.</system-reminder>\nAlso keep the header blue.", 8)
    t.queued("render's done, you can use it again", 9)
    t.user("render's done, you can use it again", 9)              # the same message, recorded again
    t.queued("<task-notification>build finished</task-notification>", 10, kind="task-notification",
             mode="task-notification")
    t.queued("<cross-session-message from=\"other\">PEER TEXT</cross-session-message>", 10, kind="peer", meta=True)
    t.queued("PLAIN PEER TEXT with no tags", 10, kind="peer")             # only the sender check stops these
    t.queued("PLAIN TASK TEXT with no tags", 10, kind=None, mode="task-notification")
    t.user("PLAIN SCHEDULER TEXT", 10, origin={"kind": "task-notification"}, turnOrigin="task_notification")
    t.user("<task-notification>TASK NOTE TEXT</task-notification>", 11, origin={"kind": "task-notification"},
           turnOrigin="task_notification")
    t.user("Another Claude session sent a message: PEER TURN TEXT", 11, origin={"kind": "peer"}, turnOrigin="peer",
           isMeta=True)
    t.user("[Request interrupted by user]", 12)
    t.user("<command-name>/model</command-name>\n<command-args></command-args>", 12)
    t.human([{"type": "text", "text": "Here's the screenshot"},
             {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}], 13)
    t.add({"parentUuid": None, "isSidechain": False, "type": "user", "timestamp": t.ts(14),
           "message": {"role": "user", "content": [
               {"type": "tool_result", "tool_use_id": "toolu_x", "content": "TOOL OUTPUT TEXT"},
               {"type": "text", "text": "also, don't push to main"}]}, "toolUseResult": {"type": "text"}})
    t.human(VERBATIM, 15)
    t.user("SIDECHAIN TEXT", 16, isSidechain=True)
    t.add({"type": "api-request-blob", "sessionId": SID, "hash": "h1",
           "body": {"messages": [{"role": "user", "content": "BLOB TEXT"}]}})
    t.tool("TodoWrite", {"todos": [
        {"content": "Fix the header layout", "status": "completed", "activeForm": "Fixing"},
        {"content": "Re-run the screenshot tests", "status": "completed", "activeForm": "Re-running"},
        {"content": "Write the release note", "status": "in_progress", "activeForm": "Writing"}]}, 17)
    t.tool("Write", {"file_path": "/fake/project/notes.md", "content": "x"}, 18)
    t.tool("MultiEdit", {"file_path": "/fake/project/src/app.js", "edits": []}, 19)
    t.tool("NotebookEdit", {"notebook_path": "/fake/project/explore.ipynb", "new_source": "x"}, 20)
    t.tool("Edit", {"file_path": "/fake/project/src/header.css", "old_string": "b", "new_string": "c"}, 21)
    t.say("No response requested.", 22, model="<synthetic>")
    t.say("Done. The header is blue and the tests are queued.", 23)
    return t


# ---------- running the scripts the way Claude Code will ----------

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memory-house-test-")
        self.project = os.path.join(self.tmp, "fake project é")
        os.makedirs(self.project)
        self.temp_dir = os.path.join(self.tmp, "temp")
        os.makedirs(self.temp_dir)
        self.claude_home = os.path.join(self.tmp, "home", ".claude")
        self.transcript = os.path.join(self.claude_home, "projects", "fake", SID + ".jsonl")

    def tearDown(self):
        # On Windows the long-path test leaves a file deeper than 260 characters; the \\?\ prefix on the
        # root lets rmtree reach it.
        shutil.rmtree("\\\\?\\" + os.path.abspath(self.tmp) if os.name == "nt" else self.tmp, ignore_errors=True)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("MEMORY_HOUSE_", "CLAUDE_"))}
        env.update({"TMP": self.temp_dir, "TEMP": self.temp_dir, "TMPDIR": self.temp_dir,
                    "CLAUDE_PROJECT_DIR": self.project, "CLAUDE_CONFIG_DIR": self.claude_home,
                    "PYTHONIOENCODING": "utf-8"})
        env.update({k: v for k, v in extra.items() if v is not None})
        for k, v in extra.items():
            if v is None:
                env.pop(k, None)
        return env

    def run_hook(self, script, payload, flags=(), env=None, raw=None):
        """Run a hook through the same guard line that print_settings.py writes into settings.json."""
        cmd = [sys.executable, "-c", settings_tool.GUARD, os.path.join(HOOKS, script)] + list(flags)
        data = raw if raw is not None else json.dumps(payload).encode("utf-8")
        p = subprocess.run(cmd, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                           env=env or self.env())
        self.assertEqual(p.returncode, 0, "exit code %s, stderr: %s" % (p.returncode, p.stderr[-800:]))
        return p.stdout.decode("utf-8")

    def run_tool(self, script, args, env=None):
        p = subprocess.run([sys.executable, os.path.join(TOOLS, script)] + list(args), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=120, env=env or self.env(), cwd=self.project)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

    def precompact(self, **extra):
        payload = {"session_id": SID, "transcript_path": self.transcript, "cwd": self.project,
                   "hook_event_name": "PreCompact", "trigger": "auto", "custom_instructions": None}
        payload.update(extra)
        return payload

    def notes(self, house=None):
        return sorted(glob.glob(os.path.join(house or os.path.join(self.project, "memory"), "pillow", "pillow-*.md")))


# ---------- pillow_note.py ----------

class PillowNote(Base):
    def test_note_holds_the_users_words_and_only_theirs(self):
        build_main(self.transcript)
        out = self.run_hook("pillow_note.py", self.precompact())
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        with open(notes[0], encoding="utf-8") as f:
            note = f.read()
        self.assertIn("Instructions for this summary", out)
        self.assertIn(notes[0], out)
        self.assertLess(len(out), 10000)
        for words in ("> back off the GPU for a minute, I'm rendering", "> Also keep the header blue.",
                      "> render's done, you can use it again", "> Here's the screenshot\n> [image]",
                      "> also, don't push to main", "Okay. I'll stay off the GPU until you say the render is done.",
                      "Done. The header is blue and the tests are queued.", "Conversation: Render pipeline cleanup",
                      "automatic, because the conversation was full", SID):
            self.assertIn(words, note)
        for leak in ("OLD MESSAGE", "This session is being continued", "build finished", "PEER TEXT",
                     "PLAIN PEER TEXT", "PLAIN TASK TEXT", "PLAIN SCHEDULER TEXT",
                     "TASK NOTE TEXT", "PEER TURN TEXT", "Request interrupted", "/model", "SIDECHAIN TEXT",
                     "BLOB TEXT", "TOOL OUTPUT TEXT", "No response requested", "changed on disk", "[Image #1]",
                     "old reply"):
            self.assertNotIn(leak, note)
        self.assertEqual(note.count("render's done, you can use it again"), 1, "the double record was kept twice")
        self.assertRegex(note, r"\*\*\d+\. [^*]+\*\* \(sent while Claude was working\)\n\n> render's done")
        self.assertRegex(note, r"\*\*\d+\. [^*]+\*\* \(sent while Claude was working\)\n\n> also, don't push")
        order = [note.index(s) for s in ("> back off the GPU", "> Also keep the header blue", "> render's done",
                                         "> Here's the screenshot", "> also, don't push", "> Line one")]
        self.assertEqual(order, sorted(order), "messages should be oldest first")
        self.assertIn("(the latest; it may have been mid-task)\n\n> Done. The header is blue", note)

    def test_word_for_word(self):
        build_main(self.transcript)
        self.run_hook("pillow_note.py", self.precompact())
        with open(self.notes()[0], encoding="utf-8") as f:
            note = f.read()
        quoted = "\n".join("> " + line if line.strip() else ">" for line in VERBATIM.split("\n"))
        self.assertIn(quoted, note)

    def test_task_list_files_and_companion(self):
        build_main(self.transcript)
        self.run_hook("pillow_note.py", self.precompact())
        with open(self.notes()[0], encoding="utf-8") as f:
            note = f.read()
        self.assertIn("## Task list (from TodoWrite", note)
        self.assertIn("- [completed] Re-run the screenshot tests", note)
        self.assertIn("- [in_progress] Write the release note", note)
        files = re.findall(r"^- `(.+)`$", note.split("## Files Claude wrote or edited")[1], re.M)
        self.assertEqual(files[:4], ["/fake/project/src/header.css", "/fake/project/explore.ipynb",
                                     "/fake/project/src/app.js", "/fake/project/notes.md"])
        pillow_dir = os.path.join(self.project, "memory", "pillow")
        with open(os.path.join(pillow_dir, ".gitignore"), encoding="utf-8") as f:
            self.assertIn("\n*\n", f.read())
        with open(os.path.join(pillow_dir, ".sessions", SID + ".json"), encoding="utf-8") as f:
            companion = json.load(f)
        self.assertEqual(companion["note"], self.notes()[0])
        self.assertEqual(companion["messages"][-1]["text"], VERBATIM)
        self.assertTrue(any(m["mid_task"] for m in companion["messages"]))

    def test_older_messages_are_counted_not_lost(self):
        build_main(self.transcript)
        self.run_hook("pillow_note.py", self.precompact(), flags=["--messages", "2"])
        with open(self.notes()[0], encoding="utf-8") as f:
            note = f.read()
        self.assertIn("(4 earlier messages since the last compaction are in the transcript.)", note)
        self.assertNotIn("> back off the GPU", note)
        self.assertIn("**5. ", note)

    def test_task_tools_and_desktop_plan(self):
        t = Transcript(self.transcript)
        t.human("please track this", 1)
        t.tool("TaskCreate", {"subject": "Re-run screenshot tests", "description": "x"}, 2, use_id="toolu_c1")
        t.result("toolu_c1", "Task #4 created successfully: Re-run screenshot tests", 2)
        t.tool("TaskCreate", {"subject": "Write the release note", "description": "y"}, 3, use_id="toolu_c2")
        t.result("toolu_c2", "created", 3, extra={"task": {"id": "5", "subject": "Write the release note"}})
        t.tool("TaskUpdate", {"taskId": "4", "status": "in_progress"}, 4)
        t.tool("TaskUpdate", {"taskId": "5", "status": "deleted"}, 5)
        self.run_hook("pillow_note.py", self.precompact())
        with open(self.notes()[0], encoding="utf-8") as f:
            note = f.read()
        self.assertIn("- [in_progress] Re-run screenshot tests", note)
        self.assertNotIn("Write the release note", note)
        os.remove(self.transcript)
        t = Transcript(self.transcript)
        t.human("plan it", 1)
        t.tool("mcp__desktop__update_plan", {"items": [{"content": "Look", "status": "in_progress"},
                                                       {"content": "Fix", "status": "pending"}]}, 2)
        t.tool("mcp__desktop__update_plan", {"advance": True}, 3)
        self.run_hook("pillow_note.py", self.precompact(), flags=["--keep", "0"])
        with open(self.notes()[-1], encoding="utf-8") as f:
            note = f.read()
        self.assertIn("- [completed] Look", note)
        self.assertIn("- [in_progress] Fix", note)

    def test_where_the_house_is(self):
        build_main(self.transcript)
        self.run_hook("pillow_note.py", self.precompact(), env=self.env(MEMORY_HOUSE_DIR="house"))
        self.assertEqual(len(self.notes(os.path.join(self.project, "house"))), 1)
        elsewhere = os.path.join(self.tmp, "elsewhere")
        self.run_hook("pillow_note.py", self.precompact(), flags=["--memory-dir", elsewhere],
                      env=self.env(MEMORY_HOUSE_DIR="house"))
        self.assertEqual(len(self.notes(elsewhere)), 1)
        self.run_hook("pillow_note.py", self.precompact(), env=self.env(CLAUDE_PROJECT_DIR=None))
        self.assertEqual(len(self.notes()), 1, "with no CLAUDE_PROJECT_DIR, the hook's cwd is the project")

    def test_keeps_only_the_newest(self):
        build_main(self.transcript)
        pillow_dir = os.path.join(self.project, "memory", "pillow")
        os.makedirs(pillow_dir)
        for day in range(1, 6):
            open(os.path.join(pillow_dir, "pillow-2020-01-0%d-120000-aaaaaaaa.md" % day), "w").close()
        self.run_hook("pillow_note.py", self.precompact(), flags=["--keep", "3"])
        names = [os.path.basename(n) for n in self.notes()]
        self.assertEqual(len(names), 3)
        self.assertTrue(names[-1].startswith("pillow-2026") or names[-1] > "pillow-2025", names)
        self.assertNotIn("pillow-2020-01-01-120000-aaaaaaaa.md", names)
        self.assertFalse(os.path.exists(os.path.join(pillow_dir, ".gitignore")),
                         "the .gitignore is only written when the folder is new")

    def test_quiet_when_it_should_be(self):
        build_main(self.transcript)
        self.assertEqual(self.run_hook("pillow_note.py", self.precompact(hook_event_name="UserPromptSubmit")), "")
        self.assertEqual(self.run_hook("pillow_note.py", self.precompact(agent_id="agent-1")), "")
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.run_hook("pillow_note.py", self.precompact(), flags=["--no-summary-hints"]), "")
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(self.run_hook("pillow_note.py", None, raw=b"not json {"), "")
        missing = self.run_hook("pillow_note.py", self.precompact(transcript_path=os.path.join(self.tmp, "nope")))
        self.assertIn("Instructions for this summary", missing)
        self.assertNotIn("pillow note at", missing)
        self.assertIn("Instructions for this summary",
                      self.run_hook("pillow_note.py", self.precompact(), flags=["--bogus-flag"]))

    def test_scheduled_task_prompt_and_picture_only_messages(self):
        t = Transcript(self.transcript)
        t.user('<scheduled-task name="nightly-check">Check the build and list what failed.</scheduled-task>', 1,
               origin={"kind": "task-notification"}, turnOrigin="scheduled", promptSource="system")
        t.say("Checking now.", 2)
        t.user("<task-notification>build done</task-notification>", 3, origin={"kind": "task-notification"},
               turnOrigin="task_notification")
        t.human([{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}], 4)
        t.human([{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "BBBB"}}], 5)
        self.run_hook("pillow_note.py", self.precompact())
        with open(self.notes()[0], encoding="utf-8") as f:
            note = f.read()
        self.assertIn("(a scheduled task's prompt, written ahead of time)\n\n> <scheduled-task", note)
        self.assertIn("Check the build and list what failed.", note)
        self.assertNotIn("build done", note)
        self.assertEqual(note.count("> [image]"), 2, "two pictures sent minutes apart are two messages")
        self.assertNotIn("AAAA", note)

    def test_plain_json_spacing_and_manual_trigger(self):
        build_main(self.transcript, compact_json=False)
        self.run_hook("pillow_note.py", self.precompact(trigger="manual", custom_instructions="focus on the CSS"))
        with open(self.notes()[0], encoding="utf-8") as f:
            note = f.read()
        self.assertIn("manual, because someone ran /compact", note)
        self.assertIn("Focus asked for with /compact: focus on the CSS", note)
        self.assertNotIn("BLOB TEXT", note)
        self.assertIn("> back off the GPU for a minute", note)

    @unittest.skipUnless(os.name == "nt", "the 260-character path limit is a Windows thing")
    def test_windows_long_transcript_path(self):
        deep = os.path.join(self.claude_home, "projects", "x" * 120, "y" * 60)
        self.transcript = os.path.join(deep, SID + ".jsonl")
        self.assertGreater(len(os.path.abspath(self.transcript)), 260)
        os.makedirs(pillow.native(deep))
        lines = [json.dumps({"type": "user", "message": {"role": "user", "content": "a long way down"},
                             "timestamp": "2026-03-15T02:00:00.000Z", "origin": {"kind": "human"}}),
                 json.dumps({"type": "assistant", "timestamp": "2026-03-15T02:01:00.000Z",
                             "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "ok"}],
                                         "usage": {"input_tokens": 720000}}})]
        with open(pillow.native(self.transcript), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.run_hook("pillow_note.py", self.precompact())
        with open(self.notes()[0], encoding="utf-8") as f:
            self.assertIn("> a long way down", f.read())
        tapped = self.run_hook("shoulder_tap.py", {"session_id": SID, "transcript_path": self.transcript,
                                                   "hook_event_name": "PostToolUse"})
        self.assertIn("about 72% full", tapped)

    def test_reads_only_the_tail_of_a_huge_transcript(self):
        t = Transcript(self.transcript)
        t.human("first message", 1)
        t.human("second message", 2)
        old = pillow.READ_LIMIT
        try:
            size = os.path.getsize(self.transcript)
            pillow.READ_LIMIT = size - 20          # lands inside the first line
            entries = list(pillow.read_entries(self.transcript))
        finally:
            pillow.READ_LIMIT = old
        self.assertEqual([pillow.typed_message(e) for e in entries], ["second message"])


# ---------- after_compact.py ----------

class WakeUp(Base):
    def wake(self, **extra):
        payload = {"session_id": SID, "transcript_path": self.transcript, "cwd": self.project,
                   "hook_event_name": "SessionStart", "source": "compact"}
        payload.update(extra)
        return payload

    def test_points_claude_at_the_note(self):
        build_main(self.transcript)
        self.run_hook("pillow_note.py", self.precompact())
        out = json.loads(self.run_hook("after_compact.py", self.wake()))
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("pillow note", out["systemMessage"])
        self.assertIn(self.notes()[0], text)
        self.assertIn("\"render's done, you can use it again\"", text)
        self.assertIn("(sent while you were working)", text)
        self.assertIn("Tell the user in one line", text)
        self.assertIn("[in_progress] Write the release note", text)
        self.assertIn(self.transcript, text)

    def test_no_note_or_someone_elses(self):
        build_main(self.transcript)
        self.run_hook("pillow_note.py", self.precompact())
        out = json.loads(self.run_hook("after_compact.py", self.wake(session_id="another-session")))
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("No pillow note was saved for this compaction", text)
        self.assertNotIn("render's done", text)
        self.assertEqual(self.run_hook("after_compact.py", self.wake(source="startup")), "")
        self.assertEqual(self.run_hook("after_compact.py", self.wake(agent_id="a1")), "")

    def test_old_note_is_an_earlier_compaction(self):
        build_main(self.transcript)
        self.run_hook("pillow_note.py", self.precompact())
        companion = os.path.join(self.project, "memory", "pillow", ".sessions", SID + ".json")
        with open(companion, encoding="utf-8") as f:
            info = json.load(f)
        info["saved_at"] = (dt.datetime.now().astimezone() - dt.timedelta(hours=5)).isoformat()
        with open(companion, "w", encoding="utf-8") as f:
            json.dump(info, f)
        text = json.loads(self.run_hook("after_compact.py", self.wake()))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("from an earlier compaction", text)

    def test_stays_short_and_works_without_a_session_id(self):
        t = Transcript(self.transcript)
        for i in range(12):
            t.human(("long message %d " % i) + "x" * 2000, i)
        self.run_hook("pillow_note.py", self.precompact())
        out = json.loads(self.run_hook("after_compact.py", self.wake(session_id=None)))
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(text), 9000)
        self.assertIn("long message 11", text)
        self.assertIn(self.notes()[0], text)


# ---------- shoulder_tap.py ----------

class ShoulderTap(Base):
    def usage(self, tokens):
        return {"input_tokens": 2, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": tokens - 1100,
                "output_tokens": 98}

    def ask(self, event="PostToolUse", flags=(), env=None, **extra):
        payload = {"session_id": SID, "transcript_path": self.transcript, "cwd": self.project,
                   "hook_event_name": event}
        payload.update(extra)
        out = self.run_hook("shoulder_tap.py", payload, flags=flags, env=env)
        return json.loads(out) if out else None

    def test_taps_once_per_mark_and_starts_over_after_a_compaction(self):
        t = Transcript(self.transcript)
        t.human("hi", 1)
        t.say("working", 2, usage=self.usage(720000))
        first = self.ask()
        self.assertIn("about 72% full", first["hookSpecificOutput"]["additionalContext"])
        self.assertIn("Pack at the next natural pause", first["hookSpecificOutput"]["additionalContext"])
        self.assertIn("72%", first["systemMessage"])
        self.assertIsNone(self.ask(), "the same mark must not tap twice")
        t.say("more", 3, usage=self.usage(810000))
        self.assertIn("pack now", self.ask()["hookSpecificOutput"]["additionalContext"])
        t.say("much more", 4, usage=self.usage(950000))
        big = self.ask(event="UserPromptSubmit", prompt="ok")
        self.assertEqual(big["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("suggest they run /compact", big["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.ask(), "88 was skipped on the jump, and must not tap afterwards")
        t.boundary(5)
        self.assertIsNone(self.ask())
        t.say("after the compaction", 6, usage=self.usage(50000))
        self.assertIsNone(self.ask())
        t.say("full again", 7, usage=self.usage(705000))
        self.assertIn("about 70% full", self.ask()["hookSpecificOutput"]["additionalContext"])
        beats = glob.glob(os.path.join(self.temp_dir, "memory-house*", "shoulder-tap", "heartbeat-*.txt"))
        self.assertEqual(len(beats), 1)

    def fresh(self, tokens, model):
        """A new one-reply transcript, and no memory of earlier taps."""
        for state in glob.glob(os.path.join(self.temp_dir, "memory-house*", "shoulder-tap", SID + ".json")):
            os.remove(state)
        if os.path.exists(self.transcript):
            os.remove(self.transcript)
        Transcript(self.transcript).say("reply", 1, usage=self.usage(tokens), model=model)

    def test_window_sizes(self):
        self.fresh(150000, "claude-sonnet-4-5-20250929")
        self.assertIn("about 75% full", self.ask()["hookSpecificOutput"]["additionalContext"])
        self.fresh(300000, "claude-sonnet-4-5")
        self.assertIsNone(self.ask(), "300k on a 200k guess means the window is really 1M: 30%, no tap")
        self.assertIn("about 75% full", json.dumps(self.ask(flags=["--window", "400k"])))
        self.fresh(300000, "claude-opus-5-5")
        self.assertIn("about 75% full", json.dumps(self.ask(env=self.env(CLAUDE_CODE_AUTO_COMPACT_WINDOW="400000"))))
        self.fresh(300000, "claude-sonnet-4-5")
        self.assertIsNone(self.ask(env=self.env(CLAUDE_CODE_AUTO_COMPACT_WINDOW="400000")),
                          "Claude Code caps the auto-compact window at the model's, so the 1M switch still applies")
        self.fresh(300000, "claude-opus-5-5")
        out = self.ask(env=self.env(MEMORY_HOUSE_CONTEXT_WINDOW="1M", MEMORY_HOUSE_TAPS="25,30"))
        self.assertIn("about 30% full", out["hookSpecificOutput"]["additionalContext"])
        self.assertIn("suggest they run /compact", out["hookSpecificOutput"]["additionalContext"])

    def test_skips_what_isnt_the_main_conversation(self):
        t = Transcript(self.transcript)
        t.say("real", 1, usage=self.usage(720000))
        t.say("No response requested.", 2, usage={"input_tokens": 0, "output_tokens": 0}, model="<synthetic>")
        t.say("subagent", 3, usage=self.usage(10000), isSidechain=True)
        self.assertIn("about 72% full", json.dumps(self.ask()))
        os.remove(glob.glob(os.path.join(self.temp_dir, "memory-house*", "shoulder-tap", SID + ".json"))[0])
        self.assertIsNone(self.ask(agent_id="agent-7"))
        self.assertIsNone(self.ask(event="Stop"))
        self.assertIsNone(self.ask(transcript_path=os.path.join(self.tmp, "missing.jsonl")))

    def test_failures_are_logged_not_shown(self):
        t = Transcript(self.transcript)
        t.say("reply", 1, usage=self.usage(720000))
        base = "memory-house" + ("-%d" % os.getuid() if hasattr(os, "getuid") else "")
        os.makedirs(os.path.join(self.temp_dir, base), exist_ok=True)
        open(os.path.join(self.temp_dir, base, "shoulder-tap"), "w").close()   # a file where a folder must go
        self.assertIsNone(self.ask())
        broken = os.path.join(self.tmp, "broken-house")
        os.makedirs(broken)
        open(os.path.join(broken, "pillow"), "w").close()
        out = self.run_hook("pillow_note.py", self.precompact(), flags=["--memory-dir", broken])
        self.assertIn("Instructions for this summary", out)
        self.assertNotIn("pillow note at", out)
        with open(os.path.join(self.temp_dir, "memory-house", "errors.log"), encoding="utf-8") as f:
            log = f.read()
        self.assertIn("shoulder_tap:", log)
        self.assertIn("pillow_note:", log)

    def test_model_guesses(self):
        os.environ.pop("CLAUDE_CODE_DISABLE_1M_CONTEXT", None)
        cases = {"claude-opus-5-5": 1000000, "claude-fable-5-2": 1000000, "claude-opus-4-7": 1000000,
                 "claude-opus-4-6": 200000, "claude-opus-4-6[1m]": 1000000, "claude-sonnet-4-6": 200000,
                 "claude-sonnet-5": 1000000, "claude-haiku-4-5-20251001": 200000, "claude-haiku-5-5": 1000000,
                 "claude-sonnet-4-20250514": 200000, "claude-3-5-sonnet-20241022": 200000,
                 "us.anthropic.claude-3-7-sonnet-20250219-v1:0": 200000, "my-gateway-model": 1000000,
                 "anthropic/claude-opus-4-8": 1000000}
        for model, window in cases.items():
            self.assertEqual(tap.model_window(model), window, model)
        self.assertEqual(tap.tokens_from("200k"), 200000)
        self.assertEqual(tap.tokens_from("1M"), 1000000)
        self.assertIsNone(tap.tokens_from("lots"))
        self.assertEqual(tap.taps_from("93, 70,80"), [70, 80, 93])


# ---------- the tools ----------

class Tools(Base):
    def test_attic_search(self):
        slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(self.project))
        self.transcript = os.path.join(self.claude_home, "projects", slug, SID + ".jsonl")
        build_main(self.transcript)
        notes = os.path.join(self.project, "memory", "notes")
        os.makedirs(notes)
        with open(os.path.join(notes, "gpu-break.md"), "w", encoding="utf-8") as f:
            f.write("---\ndate: 2026-03-14\nsource: user\n---\nKeep off the GPU while the user renders.\n")
        code, out, err = self.run_tool("attic_search.py", ["GPU", "minute", "--project", self.project])
        self.assertEqual(code, 0, err)
        self.assertIn("): 1 match,", out)
        self.assertIn("back off the GPU for a minute", out)
        self.assertNotIn("gpu-break.md", out, "the note doesn't say 'minute'")
        code, out, err = self.run_tool("attic_search.py", ["use it again", "--project", self.project])
        self.assertIn("): 1 match,", out, "the message recorded twice should show once")
        self.assertIn("  user  ", out)
        code, out, err = self.run_tool("attic_search.py", ["GPU", "--who", "claude", "--project", self.project])
        self.assertIn("stay off the GPU", out)
        self.assertNotIn("back off the GPU", out)
        self.assertIn("gpu-break.md", out)
        code, out, err = self.run_tool("attic_search.py", ["BLOB", "--project", self.project])
        self.assertIn("): 0 matches,", out)
        code, out, err = self.run_tool("attic_search.py", ["café", "--project", self.project])
        self.assertIn("): 1 match,", out)
        moved = os.path.join(self.claude_home, "projects", "shortened-name")
        os.rename(os.path.dirname(self.transcript), moved)
        with open(os.path.join(moved, SID + ".jsonl"), "r+", encoding="utf-8") as f:
            body = f.read().replace('"cwd":"/fake"', json.dumps({"cwd": self.project})[1:-1].replace(": ", ":"), 1)
            f.seek(0)
            f.write(body)
            f.truncate()
        code, out, err = self.run_tool("attic_search.py", ["header", "blue", "--project", self.project])
        self.assertIn("Also keep the header blue", out, out + err)

    def test_stale_check(self):
        house = os.path.join(self.project, "memory")
        os.makedirs(os.path.join(house, "notes"))
        notes = {"old-plan.md": "---\ndate: 2026-01-01\ncheck_by: 2026-02-01   # look again then\n---\nplan\n",
                 "fresh.md": "---\ndate: 2026-02-20\ncheck_by: 2026-06-01\n---\nfresh\n",
                 "temp-active.md": "---\nstatus: active\nends: 2026-02-10\n---\ntemporary\n",
                 "temp-ended.md": "---\nstatus: ended\nends: 2026-02-10\n---\nover\n"}
        for name, body in notes.items():
            with open(os.path.join(house, "notes", name), "w", encoding="utf-8") as f:
                f.write(body)
        with open(os.path.join(house, "MEMORY.md"), "w", encoding="utf-8") as f:
            f.write("# Memory index\n\n## Open loops (updated 2026-02-01)\n- waiting\n\n"
                    "- [user] Quiet hours until 2026-02-15 -> notes/quiet.md\n"
                    "- ENDED [user] GPU break until 2026-02-15 -> notes/gpu.md\n")
        code, out, err = self.run_tool("stale_check.py", ["--memory-dir", house, "--today", "2026-03-01"])
        self.assertEqual(code, 0, err)
        self.assertIn("notes%sold-plan.md" % os.sep, out)
        self.assertIn("notes%stemp-active.md" % os.sep, out)
        self.assertNotIn("fresh.md", out)
        self.assertNotIn("temp-ended.md", out)
        self.assertIn("open-loops line is 28 days old", out)
        self.assertIn("Quiet hours", out)
        self.assertNotIn("GPU break", out)
        code, out, err = self.run_tool("stale_check.py", ["--memory-dir", house, "--today", "2026-01-15"])
        self.assertIn("nothing looks out of date", out)

    def test_print_settings_and_the_guard(self):
        code, out, err = self.run_tool("print_settings.py", [])
        self.assertEqual(code, 0, err)
        hooks = json.loads(out)["hooks"]
        self.assertEqual(set(hooks), {"PreCompact", "SessionStart", "PostToolUse", "UserPromptSubmit"})
        self.assertEqual(hooks["SessionStart"][0]["matcher"], "compact")
        entry = hooks["PreCompact"][0]["hooks"][0]
        self.assertTrue(os.path.isfile(entry["args"][2]))
        build_main(self.transcript)
        p = subprocess.run([entry["command"]] + entry["args"], input=json.dumps(self.precompact()).encode(),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env(), timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(self.notes()), 1)
        wrong = [entry["command"], "-c", settings_tool.GUARD, os.path.join(self.tmp, "no", "such", "hook.py")]
        p = subprocess.run(wrong, input=b"{}", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual((p.returncode, p.stdout), (0, b""), "a wrong path must do nothing, never exit 2")
        code, out, err = self.run_tool("print_settings.py", ["--memory-dir", self.tmp, "--no-shoulder-tap"])
        hooks = json.loads(out)["hooks"]
        self.assertEqual(set(hooks), {"PreCompact", "SessionStart"})
        self.assertEqual(hooks["PreCompact"][0]["hooks"][0]["args"][-2:], ["--memory-dir", os.path.abspath(self.tmp)])

    def test_settings_example_matches(self):
        with open(os.path.join(ROOT, "settings.example.json"), encoding="utf-8") as f:
            example = json.load(f)["hooks"]
        printed = settings_tool.block()["hooks"]
        self.assertEqual(set(example), set(printed))
        for event in printed:
            want, have = printed[event][0], example[event][0]
            self.assertEqual(want.get("matcher"), have.get("matcher"), event)
            w, h = want["hooks"][0], have["hooks"][0]
            self.assertEqual(h["args"][:2], ["-c", settings_tool.GUARD], event)
            self.assertEqual(os.path.basename(h["args"][2]), os.path.basename(w["args"][2]), event)
            self.assertEqual((h["timeout"], h.get("statusMessage")), (w["timeout"], w.get("statusMessage")), event)


class Hygiene(unittest.TestCase):
    def test_python_38_syntax(self):
        for path in glob.glob(os.path.join(ROOT, "**", "*.py"), recursive=True):
            with open(path, encoding="utf-8") as f:
                ast.parse(f.read(), filename=path, feature_version=(3, 8))


if __name__ == "__main__":
    unittest.main()
