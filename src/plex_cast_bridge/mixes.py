"""Bridge-managed mixes backed by the local index; never writes to Plex."""
from __future__ import annotations

import json
import re
from typing import Any

from . import music_index

MAX_TRACKS = 100


def _scope(db) -> tuple[str, str]:
    row = db.execute('SELECT server_id, library_id FROM scan_state WHERE id=1').fetchone()
    if not row or not row['server_id'] or not row['library_id']:
        raise ValueError('Complete a library scan first')
    return row['server_id'], row['library_id']


def _track(row) -> dict:
    return {key: row[key] for key in ('rating_key', 'title', 'artist', 'album', 'year', 'duration_ms', 'play_count')}


def preview(prompt: str, mode: str, limit: int = 25) -> dict:
    music_index.initialize()
    prompt = prompt.strip()
    if not prompt or len(prompt) > 300:
        raise ValueError('Enter a request of up to 300 characters')
    if mode not in {'artist', 'genre', 'mood', 'decade', 'deep_cuts'}:
        raise ValueError('Unknown mix type')
    limit = max(1, min(50, limit))
    with music_index.connect() as db:
        server, library = _scope(db)
        args: list[Any] = [server, library]
        base = 'SELECT t.* FROM tracks t WHERE t.server_id=? AND t.library_id=?'
        warning = None
        if mode == 'artist':
            base += " AND COALESCE(t.artist, '') != '' AND instr(lower(?), lower(t.artist)) > 0"
            args.append(prompt)
            warning = 'Artist mix uses tracks by the matching artist; similar artists require additional metadata.'
        elif mode in {'genre', 'mood'}:
            base += ' AND EXISTS (SELECT 1 FROM track_tags g WHERE g.server_id=t.server_id AND g.library_id=t.library_id AND g.rating_key=t.rating_key AND g.kind=? AND instr(lower(?), lower(g.tag))>0)'
            args.extend([mode, prompt])
        elif mode == 'decade':
            match = re.search(r'\b(?:(19|20)(\d0)|(\d0))s\b', prompt.casefold())
            if not match:
                raise ValueError('Include a decade such as 1990s or 90s')
            year = int(match.group(1) + match.group(2)) if match.group(1) else (2000 if int(match.group(3)) < 30 else 1900) + int(match.group(3))
            base += ' AND t.year>=? AND t.year<?'
            args.extend([year, year + 10])
        elif mode == 'deep_cuts':
            base += ' AND t.play_count IS NOT NULL'
            rows = db.execute(base + ' ORDER BY t.play_count ASC, t.artist COLLATE NOCASE, t.rating_key LIMIT ?',
                              (*args, limit)).fetchall()
            if rows:
                warning = 'Ranked by Plex play counts. Tracks without play-count metadata are excluded.'
            else:
                warning = 'Plex has not provided play-count metadata for this library yet.'
            tracks = [_track(row) for row in rows]
            return {'tracks': tracks, 'duration_ms': sum(t['duration_ms'] or 0 for t in tracks), 'warning': warning}
        else:
            raise ValueError('Unknown mix type')
        rows = db.execute(base + ' ORDER BY t.artist COLLATE NOCASE, t.album COLLATE NOCASE, t.rating_key LIMIT ?', (*args, limit)).fetchall()
    tracks = [_track(row) for row in rows]
    if not tracks:
        warning = 'No matching indexed tracks. Check the library’s tags and the request.'
    return {'tracks': tracks, 'duration_ms': sum(t['duration_ms'] or 0 for t in tracks), 'warning': warning}


def _valid_keys(db, server: str, library: str, keys: list[str]) -> list[str]:
    keys = [str(key) for key in keys]
    if not keys or len(keys) > MAX_TRACKS or len(set(keys)) != len(keys):
        raise ValueError('Choose 1–100 distinct indexed tracks')
    found = {row[0] for row in db.execute(
        f"SELECT rating_key FROM tracks WHERE server_id=? AND library_id=? AND rating_key IN ({','.join('?' for _ in keys)})",
        (server, library, *keys))}
    if found != set(keys):
        raise ValueError('One or more tracks are no longer in this library index')
    return keys


def save(name: str, keys: list[str], provenance: dict | None = None) -> int:
    name = name.strip()
    if not name or len(name) > 100:
        raise ValueError('Mix name must contain 1–100 characters')
    music_index.initialize()
    with music_index.connect() as db:
        server, library = _scope(db)
        keys = _valid_keys(db, server, library, keys)
        cursor = db.execute('INSERT INTO mixes(name, server_id, library_id, created_at, provenance) VALUES (?,?,?,?,?)',
                            (name, server, library, music_index.utcnow(), json.dumps(provenance or {})))
        db.executemany('INSERT INTO mix_tracks(mix_id, position, rating_key) VALUES (?,?,?)',
                       [(cursor.lastrowid, i, key) for i, key in enumerate(keys)])
        db.commit()
        return cursor.lastrowid


def list_mixes() -> list[dict]:
    music_index.initialize()
    with music_index.connect() as db:
        server, library = _scope(db)
        rows = db.execute('''SELECT m.id,m.name,m.created_at,COUNT(t.rating_key) track_count
            FROM mixes m LEFT JOIN mix_tracks t ON t.mix_id=m.id WHERE m.server_id=? AND m.library_id=?
            GROUP BY m.id ORDER BY m.id DESC''', (server, library)).fetchall()
        return [dict(row) for row in rows]


def get(mix_id: int) -> dict:
    music_index.initialize()
    with music_index.connect() as db:
        server, library = _scope(db)
        mix = db.execute('SELECT * FROM mixes WHERE id=? AND server_id=? AND library_id=?', (mix_id, server, library)).fetchone()
        if not mix:
            raise ValueError('Mix not found for this library')
        rows = db.execute('''SELECT mt.rating_key,t.title,t.artist,t.album,t.year,t.duration_ms,t.play_count
            FROM mix_tracks mt LEFT JOIN tracks t ON t.server_id=? AND t.library_id=? AND t.rating_key=mt.rating_key
            WHERE mt.mix_id=? ORDER BY mt.position''', (server, library, mix_id)).fetchall()
        return {'id': mix['id'], 'name': mix['name'], 'server_id': server, 'library_id': library,
                'created_at': mix['created_at'], 'provenance': json.loads(mix['provenance']),
                'tracks': [{**_track(row), 'missing': row['title'] is None} for row in rows]}


def update(mix_id: int, name: str | None = None, keys: list[str] | None = None) -> None:
    music_index.initialize()
    with music_index.connect() as db:
        server, library = _scope(db)
        if not db.execute('SELECT 1 FROM mixes WHERE id=? AND server_id=? AND library_id=?', (mix_id, server, library)).fetchone():
            raise ValueError('Mix not found for this library')
        if name is not None:
            name = name.strip()
            if not name or len(name) > 100:
                raise ValueError('Mix name must contain 1–100 characters')
            db.execute('UPDATE mixes SET name=? WHERE id=?', (name, mix_id))
        if keys is not None:
            keys = [str(key) for key in keys]
            existing = {row[0] for row in db.execute('SELECT rating_key FROM mix_tracks WHERE mix_id=?', (mix_id,))}
            if not keys or len(keys) > MAX_TRACKS or len(set(keys)) != len(keys) or not set(keys) <= existing:
                raise ValueError('Reorder or remove existing mix tracks; at least one must remain')
            db.execute('DELETE FROM mix_tracks WHERE mix_id=?', (mix_id,))
            db.executemany('INSERT INTO mix_tracks(mix_id, position, rating_key) VALUES (?,?,?)',
                           [(mix_id, i, key) for i, key in enumerate(keys)])
        db.commit()


def delete(mix_id: int) -> None:
    music_index.initialize()
    with music_index.connect() as db:
        server, library = _scope(db)
        if not db.execute('DELETE FROM mixes WHERE id=? AND server_id=? AND library_id=?', (mix_id, server, library)).rowcount:
            raise ValueError('Mix not found for this library')
        db.commit()


def validate_preview(keys: list[str]) -> dict:
    """Resolve an unsaved preview solely against the currently indexed library."""
    music_index.initialize()
    with music_index.connect() as db:
        server, library = _scope(db)
        keys = _valid_keys(db, server, library, keys)
        return {'server_id': server, 'library_id': library,
                'tracks': [{'rating_key': key, 'missing': False} for key in keys]}
