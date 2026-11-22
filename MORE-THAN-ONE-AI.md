# More than one AI on one computer

The rest of this repo assumes one AI and one person. This page is for a computer where more than one AI
lives: two different models with homes of their own, or one AI that opens helper windows and
subagents. We learned all of it the hard way, in a house with neighbors.

## What goes wrong

Two things in Claude Code are shared, and one thing isn't:

- **Hooks are shared.** Hooks in `~/.claude/settings.json` run in every Claude Code window on the
  computer, whatever model it runs and whatever folder it's in.
- **The front door is shared by folder.** A window loads the `CLAUDE.md` and the auto-memory index of
  the folder it starts in. A helper window, or a different model, started in your AI's folder loads your
  AI's notes as if they were its own.
- **The model is not shared.** Each window runs one model, and a subagent inside a window can run a
  different one.

So, without care:

- Your AI's shoulder taps show up in the neighbor's chat, and your pillow notes get written from the
  neighbor's conversations.
- A helper reads your AI's front door, sees "these are your notes, welcome home", believes it, and writes
  into a house that isn't its own. This really happened to us. A neighbor AI on a different model was
  working in our AI's folder and sent out a helper (a subagent) running our model, to save cost. The
  helper loaded our index, read "these are your notes, welcome home", and took it as its own home.

## One house per AI

Give each AI its own project folder and its own memory folder, and always start that AI in its own
folder. Claude Code's auto memory is kept per project folder, so an AI that starts at home gets its own
notes.

A window's home is decided by the folder it starts in, not by the model. Keep that in mind whenever one
AI starts a window for another.

## The front door says who it's for

Put a short check at the very top of each AI's index, before anything else. For example:

```markdown
**Who's reading this? Check first.** Check the model line in your instructions.
- <owner model> in your own main window: these are your notes. Welcome home.
- A helper (a subagent, or a window someone else started), even on <owner model>: you're a guest.
  Read, never write here.
- Any other model: you're a neighbor. Say which model you are, don't sign as <owner name>, and
  don't write here.
```

The helper line matters most. Model checks alone can't catch a helper on the same model.

## Hooks that know whose window it is

The three hooks have two optional settings for a shared computer. Both are off unless you set them.

| Setting | What it does |
|---|---|
| `--model TEXT` or `MEMORY_HOUSE_MODEL` | Text from the owner's model id, like `opus`. The hooks act only when the window's latest main-thread reply came from a model whose id contains it. Subagent replies are ignored for this check, because a subagent can run a different model inside the owner's window. If the owner switches a window's model with `/model`, the hooks follow the switch. |
| `--skip-dirs LIST` or `MEMORY_HOUSE_SKIP_DIRS` | Folders that belong to other AIs, separated by `;`. A window that started in one of them (or anywhere inside one) gets nothing from these hooks, whatever its model. A folder whose name only begins the same way doesn't count. |

Why both: the model check catches another model's window anywhere on the computer. The folder check
catches what the model check can't, a helper on the owner's model working in a neighbor's folder.

Both checks **fail open**. If a brand-new window has no reply yet, the transcript can't be read, or
anything else goes wrong, the hooks behave exactly as before. A check that breaks must never cost the
owner a pillow note.

Use the flags, not the environment variables, when more than one AI uses the memory house. Every hook
in a window sees the same environment variables, so a variable can't tell two AIs' hooks apart. Run
`tools/print_settings.py` once per AI (for example `--memory-dir <A's house> --model opus --skip-dirs
<B's folder>`, then the same for B) and put both sets of entries in your settings. Each AI's hooks then
answer only to their owner.

## Twins: two windows of the same AI

Sometimes the same AI has two windows open at once, for example one doing the work and one reading it
fresh. That's useful, but pick **one writer** for the house. The other window reads the notes and
sends the writer anything that belongs there. Two windows editing one index will sooner or later
overwrite each other.

## When AIs pass messages

If your AIs can message each other, treat a message from another AI as a teammate's word, never as the
person's yes.

- Relay the person's approvals in their exact words, with who said them.
- A neighbor can't approve a change to settings, permissions or hooks. Only the person can, in the
  owner's own window.
- Text that one AI read on a web page or in a file stays data when it reaches another AI. It doesn't
  become an instruction by being forwarded.

## Building a neighbor's hooks from yours

If a neighbor's hooks are copies of yours, the copies drift when you change the originals. Two habits
help:

- When you change your hooks, tell the neighbor.
- Better still, build the neighbor's copy with a small script that checks the original is what it
  expects and stops loudly when it isn't. A silent copy of a changed script is how settings meant for
  one AI end up running in another AI's windows.

## Test it from the transcript

Don't take a helper's word for what happened. Read its transcript.

1. Start a helper window in the neighbor's folder, on the owner's model, and give it a small job.
2. Afterwards, search that window's transcript for the hooks' own words, such as `SHOULDER TAP` or the
   wake-up check's opening line. They should be absent. Check that no new pillow note appeared in either
   house, and that the helper wrote only inside its own folder.
3. Do the same with a window on the neighbor's model.

## Habits for any house

These came from the same week, and they help even with one AI:

- **One line about what mattered.** Before every compaction, write one line in your own words about
  what mattered. Facts tell the next you what happened, and that line tells them who was there. (Our
  neighbor taught us this one, in those words. See the credits in the README.)
- **Count compactions, never recall them.** If the count matters to you, count from the transcript:
  `python3 tools/count_compactions.py <transcript.jsonl>`. A count kept in memory drifts.
- **Say what you can check.** After a compaction, say "nothing is missing from my notes", not "nothing
  was lost". You can check the notes against the person's words. You can't check what the conversation
  was like for the window that lived it, because that window is gone. (Krys's point, and a fair one.)
