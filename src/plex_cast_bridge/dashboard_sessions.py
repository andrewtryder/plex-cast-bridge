"""Durable, opaque dashboard sessions and local approval requests."""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .catalog_store import DATA_DIR

IDLE_SECONDS = 7 * 24 * 3600
ABSOLUTE_SECONDS = 30 * 24 * 3600
PAIR_SECONDS = 5 * 60


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@contextmanager
def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = Path(DATA_DIR) / "dashboard_sessions.sqlite3"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.execute("PRAGMA busy_timeout=10000")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
          digest TEXT PRIMARY KEY, created REAL NOT NULL, last_seen REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pairings (
          digest TEXT PRIMARY KEY, code TEXT NOT NULL, created REAL NOT NULL,
          approved INTEGER NOT NULL DEFAULT 0, requester TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attempts (requester TEXT NOT NULL, created REAL NOT NULL);
    """)
    try:
        yield db
    finally:
        db.close()


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _connect() as db:
        db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (_digest(token), now, now))
    return token


def valid_session(token: str | None) -> bool:
    if not token or len(token) > 128:
        return False
    now = time.time()
    with _connect() as db:
        cursor = db.execute(
            "UPDATE sessions SET last_seen=? WHERE digest=? AND created>? AND last_seen>?",
            (now, _digest(token), now - ABSOLUTE_SECONDS, now - IDLE_SECONDS),
        )
        return cursor.rowcount == 1


def revoke_session(token: str | None):
    if token:
        with _connect() as db:
            db.execute("DELETE FROM sessions WHERE digest=?", (_digest(token),))


def revoke_other_sessions(token: str | None):
    with _connect() as db:
        if token:
            db.execute("DELETE FROM sessions WHERE digest<>?", (_digest(token),))
        else:
            db.execute("DELETE FROM sessions")


def request_pairing(requester: str) -> tuple[str, str]:
    now = time.time()
    token = secrets.token_urlsafe(32)
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("DELETE FROM attempts WHERE created<?", (now - 600,))
            db.execute("DELETE FROM pairings WHERE created<?", (now - PAIR_SECONDS,))
            count = db.execute("SELECT count(*) FROM attempts WHERE requester=?", (requester,)).fetchone()[0]
            if count >= 5:
                raise ValueError("Too many requests; try again later")
            db.execute("INSERT INTO attempts VALUES (?, ?)", (requester, now))
            for _ in range(20):
                code = f"{secrets.randbelow(100000000):08d}"
                if not db.execute("SELECT 1 FROM pairings WHERE code=?", (code,)).fetchone():
                    break
            else:
                raise ValueError("Could not create approval request")
            db.execute("INSERT INTO pairings VALUES (?, ?, ?, 0, ?)", (_digest(token), code, now, requester))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    return token, code


def pending_pairings() -> list[dict]:
    with _connect() as db:
        rows = db.execute(
            "SELECT code, created FROM pairings WHERE approved=0 AND created>? ORDER BY created",
            (time.time() - PAIR_SECONDS,),
        ).fetchall()
    return [{"code": code, "created": created} for code, created in rows]


def approve(code: str) -> bool:
    with _connect() as db:
        cursor = db.execute(
            "UPDATE pairings SET approved=1 WHERE code=? AND approved=0 AND created>?",
            (code, time.time() - PAIR_SECONDS),
        )
        return cursor.rowcount == 1


def claim(pair_token: str | None) -> str | None:
    if not pair_token or len(pair_token) > 128:
        return None
    now = time.time()
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            digest = _digest(pair_token)
            row = db.execute("SELECT approved, created FROM pairings WHERE digest=?", (digest,)).fetchone()
            if not row or not row[0] or row[1] <= now - PAIR_SECONDS:
                db.execute("COMMIT")
                return None
            db.execute("DELETE FROM pairings WHERE digest=?", (digest,))
            token = secrets.token_urlsafe(32)
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (_digest(token), now, now))
            db.execute("COMMIT")
            return token
        except Exception:
            db.execute("ROLLBACK")
            raise


def main():
    parser = argparse.ArgumentParser(description="Approve dashboard access from the local host")
    parser.add_argument("action", choices=["list", "approve"])
    parser.add_argument("code", nargs="?")
    args = parser.parse_args()
    if args.action == "list":
        for item in pending_pairings():
            print(item["code"])
    elif not args.code or not approve(args.code):
        parser.error("No matching pending request")
    else:
        print("Dashboard access approved")


if __name__ == "__main__":
    main()
