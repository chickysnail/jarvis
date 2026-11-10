"""Backfill events from a Tasker phone_log.jsonl. Idempotent: re-running stores nothing new.

    uv run python -m jarvis.importer path/to/phone_log.jsonl
"""

import json
import sys
from pathlib import Path

from . import db


def import_file(conn, path: Path) -> dict:
    stats = {"stored": 0, "duplicate": 0, "bad": 0}
    with conn, open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                stored = db.insert_event(conn, json.loads(line), source="import")
            except (json.JSONDecodeError, db.BadEvent) as e:
                stats["bad"] += 1
                print(f"{path}:{lineno}: skipped ({e})", file=sys.stderr)
                continue
            stats["stored" if stored else "duplicate"] += 1
    return stats


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    conn = db.connect()
    for p in argv:
        s = import_file(conn, Path(p))
        print(f"{p}: {s['stored']} stored, {s['duplicate']} duplicates, {s['bad']} bad lines")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
