from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .locks import sqlite_write_lock

LOG = logging.getLogger(__name__)


class QuantDeskDB:
    def __init__(self, db_path: str | Path = "data/quantdesk.db", read_only: bool = False, timeout: float = 30.0, wal: bool = True):
        self.db_path = Path(db_path)
        self.read_only = read_only
        self.timeout = timeout
        self.wal = wal
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.read_only:
            uri = f"file:{self.db_path.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=self.timeout)
        else:
            conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        if self.wal and not self.read_only:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.DatabaseError as exc:
                LOG.warning("sqlite_wal_enable_failed: db_path=%s error=%s", self.db_path, exc)
        self._conn = conn
        return conn

    def initialize(self) -> dict:
        from .schema import initialize_schema
        return initialize_schema(self)

    def execute(self, sql: str, params: Any = None) -> list[dict]:
        cur = self.connect().execute(sql, params or [])
        if cur.description is None:
            self.connect().commit()
            return []
        return [dict(row) for row in cur.fetchall()]

    def execute_one(self, sql: str, params: Any = None) -> dict | None:
        cur = self.connect().execute(sql, params or [])
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def executemany(self, sql: str, rows: list[dict] | list[tuple]) -> int:
        if self.read_only:
            raise RuntimeError("database is read-only")
        with sqlite_write_lock():
            conn = self.connect()
            try:
                with conn:
                    conn.executemany(sql, rows)
                return len(rows)
            except Exception:
                conn.rollback()
                raise

    @contextmanager
    def transaction(self):
        if self.read_only:
            raise RuntimeError("database is read-only")
        lock = sqlite_write_lock()
        lock.acquire()
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            lock.release()

    def upsert_many(self, table: str, rows: list[dict], key_columns: list[str]) -> dict:
        if not rows:
            return {"ok": True, "status": "passed", "table": table, "row_count": 0}
        columns = list(rows[0].keys())
        placeholders = ", ".join(":" + c for c in columns)
        update_cols = [c for c in columns if c not in key_columns]
        updates = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
        if updates:
            sql += f" ON CONFLICT({', '.join(key_columns)}) DO UPDATE SET {updates}"
        else:
            sql += f" ON CONFLICT({', '.join(key_columns)}) DO NOTHING"
        with self.transaction() as conn:
            conn.executemany(sql, rows)
        return {"ok": True, "status": "passed", "table": table, "row_count": len(rows)}

    def table_stats(self) -> dict:
        from .schema import get_required_tables
        stats: dict[str, int | None] = {}
        for table in get_required_tables():
            try:
                row = self.execute_one(f"SELECT COUNT(*) AS n FROM {table}")
                stats[table] = int(row["n"]) if row else 0
            except sqlite3.DatabaseError as exc:
                LOG.warning("sqlite_table_stats_failed: table=%s error=%s", table, exc)
                stats[table] = None
        return stats

    def reset(self, confirm: bool = False) -> dict:
        if not confirm:
            return {"ok": False, "status": "failed", "error": "confirm_required", "db_path": str(self.db_path)}
        self.close()
        removed = False
        for suffix in ["", "-wal", "-shm"]:
            path = Path(str(self.db_path) + suffix)
            if path.exists():
                path.unlink()
                removed = True
        return {"ok": True, "status": "passed", "removed": removed, "db_path": str(self.db_path), "csv_preserved": True}

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception as exc:  # noqa: BLE001 - destructors must not raise
            LOG.warning("sqlite_close_in_del_failed: db_path=%s error=%s", self.db_path, exc)
