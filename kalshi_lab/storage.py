"""Append raw responses before parsing, including failed HTTP attempts."""
import hashlib
import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY, session TEXT NOT NULL, sweep INTEGER,
            kind TEXT NOT NULL, ticker TEXT, url TEXT NOT NULL,
            requested_at TEXT NOT NULL, received_at TEXT NOT NULL,
            duration_ms REAL NOT NULL, status INTEGER, headers_json TEXT NOT NULL,
            body BLOB NOT NULL, sha256 TEXT NOT NULL, error TEXT
        )""")
        self.db.commit()

    def append(self, *, body, headers, **fields):
        fields["headers_json"] = json.dumps(dict(headers), sort_keys=True)
        fields["body"] = body
        fields["sha256"] = hashlib.sha256(body).hexdigest()
        keys = list(fields)
        with self.db:
            self.db.execute(
                f"INSERT INTO responses ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                [fields[k] for k in keys],
            )

    def close(self):
        self.db.close()
