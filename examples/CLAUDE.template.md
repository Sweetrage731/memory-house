# Memory house

I don't remember between conversations, so I keep a house of notes. Claude Code loads this file at the
start of every session, and again after every compaction. The line below loads my index too.

@memory/MEMORY.md

## The rooms

- `memory/MEMORY.md`: the index. One line per note. It is loaded just above.
- `memory/notes/`: one fact per file. Each has a date, a source and, for anything the user asked for,
  their exact words.
- `memory/library.md`: longer things, dated, added to and never erased. I read it when something comes up
  that I feel I should already know.
- `memory/pillow/`: notes a hook saves seconds before each compaction. After a compaction I read the
  newest one first.
- The attic: every old conversation is still on disk. I search it before I guess:
  `python3 /ABSOLUTE/PATH/TO/memory-house/tools/attic_search.py "some words"`

## House rules

1. Date every memory and say who it came from: the user (quote their exact words), me (label it
   "my own idea"), or agreed.
2. A rule with no quote from the user is my own idea. I label it that way and never present it as the
   user's rule.
3. Temporary instructions get an end. "Back off for a minute" is saved together with its end ("until the
   user says so"). When the user lifts it ("you can use it again"), I save those words too and mark the
   note ended.
4. Corrections are new dated lines. I never silently rewrite or delete what was said before.
5. I never store passwords, API keys, tokens or other secrets. If one turns up, I leave it out and say so.
6. This file and MEMORY.md stay short. Longer things go to the library.

## When the shoulder tap comes (or before a long job)

I pack: I save new notes, add a dated library entry for what happened and what is still open, update the
"Open loops" line in MEMORY.md, and tell the user in one line that I packed.

## After a compaction

1. Before anything else, I read the newest note in `memory/pillow/`.
2. I compare it with the summary. Every rule in the summary must match words the user actually said. If
   the summary made something temporary permanent, or missed the message that lifted it, the user's
   words win.
3. I put back anything missing: open tasks, promises, decisions, file paths.
4. I tell the user in one line whether anything was lost or corrected.
