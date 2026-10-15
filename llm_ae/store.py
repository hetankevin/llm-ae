"""Transactional editorial state and an append-only application audit trail."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .config import AEError


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def days_after(days, since=None):
    return (datetime.fromisoformat(since or now()) + timedelta(days=days)).isoformat(timespec="seconds")


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


SCHEMA = '''
CREATE TABLE IF NOT EXISTS submissions (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, authors TEXT NOT NULL DEFAULT '[]',
 editor TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'received', round INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, source_message_id TEXT,
 access_link TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS documents (
 id TEXT PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES submissions(id),
 round INTEGER NOT NULL, filename TEXT NOT NULL, path TEXT NOT NULL, text TEXT NOT NULL,
 sha256 TEXT NOT NULL, warnings TEXT NOT NULL DEFAULT '[]',
 UNIQUE(submission_id, round)
);
CREATE TABLE IF NOT EXISTS assessments (
 id TEXT PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES submissions(id),
 round INTEGER NOT NULL, kind TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
 document_sha256 TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
 email TEXT PRIMARY KEY, name TEXT NOT NULL, topics TEXT NOT NULL DEFAULT '',
 source TEXT NOT NULL, interactions INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS candidates (
 id TEXT PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES submissions(id),
 name TEXT NOT NULL, email TEXT NOT NULL DEFAULT '', affiliation TEXT NOT NULL DEFAULT '',
 source TEXT NOT NULL, evidence TEXT NOT NULL, score REAL NOT NULL DEFAULT 0,
 conflict TEXT NOT NULL DEFAULT 'unchecked', conflict_note TEXT NOT NULL DEFAULT '',
 UNIQUE(submission_id, source, name)
);
CREATE TABLE IF NOT EXISTS assignments (
 id TEXT PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES submissions(id),
 candidate_id TEXT NOT NULL REFERENCES candidates(id), round INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'draft', due_at TEXT, reminders INTEGER NOT NULL DEFAULT 0,
 last_reminder_at TEXT, report TEXT, recommendation TEXT,
 UNIQUE(submission_id, candidate_id, round)
);
CREATE TABLE IF NOT EXISTS drafts (
 id TEXT PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES submissions(id),
 round INTEGER NOT NULL, assignment_id TEXT REFERENCES assignments(id),
 kind TEXT NOT NULL, recipient TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'draft', approved_hash TEXT, message_id TEXT NOT NULL UNIQUE,
 reply_to_id TEXT, created_at TEXT NOT NULL, sent_at TEXT, error TEXT,
 effect TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS messages (
 id TEXT PRIMARY KEY, message_id TEXT NOT NULL UNIQUE, sender TEXT NOT NULL,
 subject TEXT NOT NULL, body TEXT NOT NULL, received_at TEXT NOT NULL,
 submission_id TEXT REFERENCES submissions(id), assignment_id TEXT REFERENCES assignments(id),
 status TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, action TEXT NOT NULL,
 entity TEXT NOT NULL, details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
'''


class Store:
    def __init__(self, home):
        path = home / "state.sqlite3"
        self.db = sqlite3.connect(path, isolation_level=None, timeout=15)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA busy_timeout = 15000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise AEError(f"Unsupported database version {version}")
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA user_version = 1")

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def all(self, sql, params=()):
        return [dict(row) for row in self.db.execute(sql, params)]

    def one(self, sql, params=()):
        row = self.db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def get(self, table, identifier):
        if table not in {"submissions", "documents", "assessments", "candidates", "assignments", "drafts", "messages"}:
            raise ValueError("Invalid entity")
        row = self.one(f"SELECT * FROM {table} WHERE id=?", (identifier,))
        if not row:
            raise AEError(f"No {table} record: {identifier}")
        return row

    def audit(self, action, entity, details=None):
        self.db.execute("INSERT INTO audit(at,action,entity,details) VALUES(?,?,?,?)",
                        (now(), action, entity, json.dumps(details or {}, ensure_ascii=False)))

    def status(self, sid, status):
        self.db.execute("UPDATE submissions SET status=?,updated_at=? WHERE id=?", (status, now(), sid))
        self.audit("status", sid, {"status": status})
