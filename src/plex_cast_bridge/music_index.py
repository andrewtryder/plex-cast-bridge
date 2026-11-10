"""Optional local music index and bridge-managed mix storage.

The database is created only when requested.  It never stores Plex credentials.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from . import catalog_store

SCHEMA_VERSION = 1


def database_path() -> Path:
    return catalog_store.DATA_DIR / "music_index.sqlite3"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=10000")
    try:
        yield db
    finally:
        db.close()


def initialize() -> None:
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("Music index was created by a newer application version")
        if version == 0:
            schema = """
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE scan_state (
                id INTEGER PRIMARY KEY CHECK (id=1), status TEXT NOT NULL DEFAULT 'idle',
                server_id TEXT, library_id TEXT, started_at TEXT, completed_at TEXT,
                scanned INTEGER NOT NULL DEFAULT 0, error TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO scan_state (id) VALUES (1);
            CREATE TABLE tracks (
                server_id TEXT NOT NULL, library_id TEXT NOT NULL, rating_key TEXT NOT NULL,
                title TEXT NOT NULL, artist TEXT, artist_key TEXT, album TEXT, album_key TEXT,
                year INTEGER, duration_ms INTEGER, genres TEXT NOT NULL DEFAULT '[]',
                moods TEXT NOT NULL DEFAULT '[]', updated_at TEXT,
                PRIMARY KEY(server_id, library_id, rating_key)
            );
            CREATE TABLE artists (
                server_id TEXT NOT NULL, library_id TEXT NOT NULL, artist_key TEXT NOT NULL, name TEXT,
                PRIMARY KEY(server_id, library_id, artist_key)
            );
            CREATE TABLE albums (
                server_id TEXT NOT NULL, library_id TEXT NOT NULL, album_key TEXT NOT NULL,
                artist_key TEXT, title TEXT, year INTEGER,
                PRIMARY KEY(server_id, library_id, album_key)
            );
            CREATE TABLE track_tags (
                server_id TEXT NOT NULL, library_id TEXT NOT NULL, rating_key TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('genre', 'mood')), tag TEXT NOT NULL,
                PRIMARY KEY(server_id, library_id, rating_key, kind, tag),
                FOREIGN KEY(server_id, library_id, rating_key)
                    REFERENCES tracks(server_id, library_id, rating_key) ON DELETE CASCADE
            );
            CREATE INDEX track_tags_search ON track_tags(server_id, library_id, kind, tag COLLATE NOCASE);
            CREATE INDEX tracks_artist ON tracks(server_id, library_id, artist COLLATE NOCASE);
            CREATE INDEX tracks_album ON tracks(server_id, library_id, album COLLATE NOCASE);
            CREATE INDEX tracks_year ON tracks(server_id, library_id, year);
            CREATE TABLE scan_tracks AS SELECT * FROM tracks WHERE 0;
            CREATE UNIQUE INDEX scan_tracks_identity ON scan_tracks(server_id, library_id, rating_key);
            CREATE TABLE mixes (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, server_id TEXT NOT NULL,
                library_id TEXT NOT NULL, created_at TEXT NOT NULL, provenance TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE mix_tracks (
                mix_id INTEGER NOT NULL REFERENCES mixes(id) ON DELETE CASCADE,
                position INTEGER NOT NULL, rating_key TEXT NOT NULL,
                PRIMARY KEY(mix_id, position)
            );
            PRAGMA user_version=1;
            """
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
        db.commit()


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def get_settings() -> dict:
    initialize()
    with connect() as db:
        settings = {row['key']: json.loads(row['value']) for row in db.execute('SELECT key, value FROM settings')}
    return {'indexing_enabled': bool(settings.get('indexing_enabled', False)),
            'schedule': settings.get('schedule', 'manual'),
            'ai_enabled': bool(settings.get('ai_enabled', False))}


def update_settings(patch: dict) -> dict:
    current = get_settings()
    for key in ('indexing_enabled', 'ai_enabled'):
        if key in patch:
            if not isinstance(patch[key], bool):
                raise ValueError(f'{key} must be a boolean')
            current[key] = patch[key]
    if 'schedule' in patch:
        if patch['schedule'] not in ('manual', 'daily', 'weekly'):
            raise ValueError('schedule must be manual, daily, or weekly')
        current['schedule'] = patch['schedule']
    with connect() as db:
        db.executemany('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                       [(key, json.dumps(value)) for key, value in current.items()])
        db.commit()
    return current


def status() -> dict:
    initialize()
    with connect() as db:
        scan = dict(db.execute('SELECT * FROM scan_state WHERE id=1').fetchone())
        scope = (scan['server_id'], scan['library_id'])
        row = db.execute('''SELECT COUNT(*) tracks,
            COUNT(DISTINCT NULLIF(artist_key, '')) artists,
            COUNT(DISTINCT NULLIF(album_key, '')) albums,
            COALESCE(SUM(CASE WHEN COALESCE(artist, '') != '' AND COALESCE(album, '') != '' THEN 1 ELSE 0 END), 0) artist_album,
            COALESCE(SUM(CASE WHEN genres != '[]' THEN 1 ELSE 0 END), 0) genre,
            COALESCE(SUM(CASE WHEN moods != '[]' THEN 1 ELSE 0 END), 0) mood
            FROM tracks WHERE server_id=? AND library_id=?''', scope).fetchone()
        years = db.execute('''SELECT COUNT(*) FROM (SELECT album_key FROM tracks WHERE server_id=? AND library_id=?
            AND COALESCE(album_key,'') != '' GROUP BY album_key HAVING MAX(COALESCE(year,0)) > 0)''', scope).fetchone()[0]
        return {'scan': scan, 'counts': {'tracks': row['tracks'], 'artists': row['artists'], 'albums': row['albums']},
                'quality': {'tracks_with_artist_and_album': row['artist_album'], 'tracks_with_genres': row['genre'],
                            'tracks_with_moods': row['mood'], 'albums_with_year': years}}
