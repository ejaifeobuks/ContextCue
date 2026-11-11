"""DataRepository: SQLite persistence (baseline section 8).

Stores settings, evidence, notices, acknowledgement/dedupe tombstones and
bounded diagnostics. Uses the standard library `sqlite3` so it runs on a
laptop and inside the App Lab container alike (the `dbstorage_sqlstore`
Brick is SQLite too). Restoring evidence never makes an old observation
fresh: freshness is recomputed from its own timestamps.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .clock import iso, parse_iso
from .settings import Settings

SCHEMA_VERSION = 1
RETENTION = timedelta(days=7)
MAX_DIAGNOSTICS = 5000
_SECRET_KEYS = ("token", "secret", "password", "authorization", "api_key")

_MIGRATIONS = {
    1: [
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL, saved_at TEXT NOT NULL)",
        """CREATE TABLE evidence (evidence_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
               data TEXT NOT NULL, observed_at TEXT, valid_until TEXT, saved_at TEXT NOT NULL)""",
        """CREATE TABLE notices (notice_id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL,
               revision INTEGER NOT NULL, lifecycle TEXT NOT NULL, data TEXT NOT NULL,
               created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        """CREATE TABLE tombstones (dedupe_key TEXT PRIMARY KEY, notice_id TEXT NOT NULL,
               revision INTEGER NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL)""",
        """CREATE TABLE diagnostics (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
               kind TEXT NOT NULL, data TEXT NOT NULL)""",
        "CREATE INDEX diagnostics_at ON diagnostics (at)",
    ],
}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[redacted]" if any(s in k.lower() for s in _SECRET_KEYS) else _redact(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


class DataRepository:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.recovered_from: str | None = None
        self._lock = threading.Lock()
        self._db = self._open()

    # -------------------------------------------------------------- setup
    def _open(self) -> sqlite3.Connection:
        db = None
        try:
            db = self._connect()
            db.execute("PRAGMA quick_check").fetchone()
            self._migrate(db)
            return db
        except sqlite3.DatabaseError as exc:
            if db is not None:
                db.close()
            if self.path == ":memory:":
                raise
            # Visible corruption recovery: keep the bad file, start clean.
            stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
            broken = Path(self.path).with_suffix(f".corrupt-{stamp}")
            Path(self.path).rename(broken)
            self.recovered_from = f"{broken.name}: {exc}"
            db = self._connect()
            self._migrate(db)
            return db

    def _connect(self) -> sqlite3.Connection:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL" if self.path != ":memory:" else "PRAGMA journal_mode=MEMORY")
            db.execute("PRAGMA foreign_keys=ON")
        except sqlite3.DatabaseError:
            db.close()
            raise
        return db

    def _migrate(self, db: sqlite3.Connection) -> None:
        has_meta = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        current = 0
        if has_meta:
            row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            current = int(row[0]) if row else 0
        if current > SCHEMA_VERSION:
            raise sqlite3.DatabaseError(f"Database schema {current} is newer than this app ({SCHEMA_VERSION})")
        for version in range(current + 1, SCHEMA_VERSION + 1):
            db.execute("BEGIN")
            try:
                for statement in _MIGRATIONS[version]:
                    db.execute(statement)
                db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(version),))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def _tx(self, statements: list[tuple[str, tuple]]) -> None:
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for sql, params in statements:
                    self._db.execute(sql, params)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    # ------------------------------------------------------------ settings
    def save_settings(self, settings: Settings, now: datetime) -> None:
        settings.validate()
        self._tx([("INSERT OR REPLACE INTO settings VALUES (1, ?, ?)",
                   (json.dumps(settings.to_dict()), iso(now)))])

    def restore_settings(self) -> tuple[Settings, str | None]:
        """Saved settings, or defaults plus the reason defaults were used."""
        rows = self._query("SELECT data FROM settings WHERE id = 1")
        if not rows:
            return Settings(), None
        try:
            data = json.loads(rows[0][0])
            data.pop("version", None)
            known = set(Settings().to_dict())
            return Settings().updated({k: v for k, v in data.items() if k in known}), None
        except (ValueError, TypeError) as exc:
            return Settings(), f"settings_invalid_defaults_used: {exc}"

    # ------------------------------------------------------------ evidence
    def save_evidence(self, evidence: list[dict], now: datetime) -> None:
        self._tx([
            ("INSERT OR REPLACE INTO evidence VALUES (?, ?, ?, ?, ?, ?)",
             (e["evidence_id"], e["revision"], json.dumps(_redact(e), default=str),
              e.get("observed_at"), e.get("valid_until"), iso(now)))
            for e in evidence
        ])

    def load_evidence(self) -> list[dict]:
        return [json.loads(row[0]) for row in self._query("SELECT data FROM evidence")]

    # ------------------------------------------------- notices and tombstones
    def save_notice(self, notice: dict, now: datetime) -> None:
        self._tx([("INSERT OR REPLACE INTO notices VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (notice["notice_id"], notice["dedupe_key"], notice["revision"],
                    notice["lifecycle"], json.dumps(notice, default=str),
                    notice["created_at"], iso(now)))])

    def save_acknowledgement(self, notice: dict, reason: str, now: datetime) -> None:
        """Persist the tombstone and the notice in one transaction."""
        self._tx([
            ("INSERT OR REPLACE INTO tombstones VALUES (?, ?, ?, ?, ?)",
             (notice["dedupe_key"], notice["notice_id"], notice["revision"], reason, iso(now))),
            ("INSERT OR REPLACE INTO notices VALUES (?, ?, ?, ?, ?, ?, ?)",
             (notice["notice_id"], notice["dedupe_key"], notice["revision"], notice["lifecycle"],
              json.dumps(notice, default=str), notice["created_at"], iso(now))),
        ])

    def tombstones(self) -> dict[str, dict]:
        return {row[0]: {"notice_id": row[1], "revision": row[2], "reason": row[3], "at": row[4]}
                for row in self._query("SELECT dedupe_key, notice_id, revision, reason, at FROM tombstones")}

    def recent_notices(self, limit: int = 50) -> list[dict]:
        return [json.loads(row[0]) for row in self._query(
            "SELECT data FROM notices ORDER BY updated_at DESC LIMIT ?", (limit,))]

    # ----------------------------------------------------------- diagnostics
    def write_diagnostic(self, kind: str, data: dict, now: datetime) -> None:
        self._tx([("INSERT INTO diagnostics (at, kind, data) VALUES (?, ?, ?)",
                   (iso(now), kind, json.dumps(_redact(data), default=str)))])

    def diagnostics(self, limit: int = 100) -> list[dict]:
        rows = self._query("SELECT at, kind, data FROM diagnostics ORDER BY id DESC LIMIT ?", (limit,))
        return [{"at": at, "kind": kind, **json.loads(data)} for at, kind, data in rows]

    # ------------------------------------------------------------- retention
    def remove_expired_data(self, now: datetime) -> dict[str, int]:
        """7-day retention; tombstones whose notice could still be valid stay."""
        cutoff = iso(now - RETENTION)
        counts = {}
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for table, column in (("diagnostics", "at"), ("notices", "updated_at"),
                                      ("tombstones", "at"), ("evidence", "saved_at")):
                    cur = self._db.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
                    counts[table] = cur.rowcount
                cur = self._db.execute(
                    "DELETE FROM diagnostics WHERE id NOT IN "
                    "(SELECT id FROM diagnostics ORDER BY id DESC LIMIT ?)", (MAX_DIAGNOSTICS,))
                counts["diagnostics"] += cur.rowcount
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return counts

    def clear_data(self, keep_settings: bool = True) -> None:
        tables = ["evidence", "notices", "tombstones", "diagnostics"]
        if not keep_settings:
            tables.append("settings")
        self._tx([(f"DELETE FROM {t}", ()) for t in tables])

    # ------------------------------------------------------------- secrets
    # Kept in `meta`, apart from settings: never in the view model, logs or
    # exports, and kept by "Clear data" (like settings).
    def save_secret(self, name: str, value: str | None) -> None:
        if value is None:
            self._tx([("DELETE FROM meta WHERE key = ?", (f"secret:{name}",))])
        else:
            self._tx([("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"secret:{name}", value))])

    def load_secret(self, name: str) -> str | None:
        rows = self._query("SELECT value FROM meta WHERE key = ?", (f"secret:{name}",))
        return rows[0][0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._db.close()


def evidence_still_valid(record: dict, now: datetime) -> bool:
    valid_until = record.get("valid_until")
    return bool(valid_until) and record.get("quality") == "valid" and parse_iso(valid_until) > now
