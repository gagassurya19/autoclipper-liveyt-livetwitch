"""Tiny SQLite state log for clips (swap for Postgres when moving to the queue architecture)."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_lock = threading.Lock()

SCHEMA = """
create table if not exists clips (
  clip_id text primary key,
  streamer text,
  created real,
  reason text,
  status text,          -- candidate | rendered | sent | skipped | failed
  score integer,
  title text,
  path text,
  tg_message_id integer,
  error text
)"""


class DB:
    def __init__(self, path: Path):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.execute(SCHEMA)
        self.c.commit()

    def upsert(self, clip_id: str, **fields):
        with _lock:
            cur = self.c.execute("select 1 from clips where clip_id=?", (clip_id,)).fetchone()
            if not cur:
                self.c.execute("insert into clips (clip_id, created) values (?, ?)", (clip_id, time.time()))
            if fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                self.c.execute(f"update clips set {sets} where clip_id=?", (*fields.values(), clip_id))
            self.c.commit()
