"""Count the compactions in a Claude Code transcript, so the number never has to be remembered.

Claude Code marks each compaction in the transcript with a system line whose subtype is
"compact_boundary". This counts those lines (each one once, by its uuid) and prints the number.

Usage:
    python3 count_compactions.py /path/to/transcript.jsonl
    python3 count_compactions.py /path/to/transcript.jsonl --list     # also print when each one happened

The transcript for a session is in ~/.claude/projects/<project>/<session-id>.jsonl.
Uses nothing beyond Python itself.
"""
import argparse
import json
import sys


def compactions(path):
    """Return [(timestamp, uuid), ...] for each compaction in the transcript, oldest first, each once."""
    seen, found = set(), []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "compact_boundary" not in line:          # cheap test first; most lines are skipped here
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("type") != "system" or o.get("subtype") != "compact_boundary":
                continue                                # the words alone (in a message or a tool call) don't count
            key = o.get("uuid") or o.get("timestamp") or line
            if key in seen:
                continue
            seen.add(key)
            found.append((o.get("timestamp", ""), o.get("uuid", "")))
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description="Count the compactions in a Claude Code transcript.")
    ap.add_argument("transcript", help="path to a session's .jsonl transcript")
    ap.add_argument("--list", action="store_true", help="also list when each compaction happened")
    a = ap.parse_args(argv)
    try:
        found = compactions(a.transcript)
    except OSError as e:
        print(f"Couldn't read the transcript: {e}", file=sys.stderr)
        return 1
    print(f"{len(found)} compaction{'s' if len(found) != 1 else ''} in this transcript.")
    if a.list:
        for n, (when, _uuid) in enumerate(found, 1):
            print(f"  {n}. {when or '(no time recorded)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
