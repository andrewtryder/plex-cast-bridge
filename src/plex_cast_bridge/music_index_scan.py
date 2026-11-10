"""Bounded, cancellable Plex indexing without mutating the active snapshot mid-scan."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import catalog_store, music_index

log = logging.getLogger(__name__)
_BATCH_SIZE = 100
_lock = threading.Lock()
_cancel = threading.Event()
_thread: threading.Thread | None = None


def schedule_due(settings: dict, scan: dict, now: datetime | None = None) -> bool:
    if not settings.get('indexing_enabled') or settings.get('schedule') not in {'daily', 'weekly'}:
        return False
    if scan.get('status') == 'running':
        return False
    last = scan.get('started_at')
    if not last:
        return True
    try:
        started = datetime.fromisoformat(last)
        if started.tzinfo is None:
            return False
        interval = timedelta(days=1 if settings['schedule'] == 'daily' else 7)
        return (now or datetime.now(UTC)) - started >= interval
    except (TypeError, ValueError):
        return False


def _text(value: Any) -> str | None:
    return str(value) if value is not None and str(value).strip() else None


def _tags(item: Any, name: str) -> str:
    values = sorted({str(getattr(tag, 'tag', tag)).strip() for tag in (getattr(item, name, None) or []) if str(getattr(tag, 'tag', tag)).strip()})
    return json.dumps(values, ensure_ascii=False, separators=(',', ':'))


def _record(track: Any, server_id: str, library_id: str) -> tuple:
    key = _text(getattr(track, 'ratingKey', None))
    title = _text(getattr(track, 'title', None))
    if not key or not title:
        raise ValueError('Plex returned a track without a stable ID or title')
    year = getattr(track, 'year', None)
    duration = getattr(track, 'duration', None)
    updated = getattr(track, 'updatedAt', None)
    return (server_id, library_id, key, title, _text(getattr(track, 'grandparentTitle', None)),
            _text(getattr(track, 'grandparentRatingKey', None)), _text(getattr(track, 'parentTitle', None)),
            _text(getattr(track, 'parentRatingKey', None)), int(year) if year else None,
            int(duration) if duration is not None else None, _tags(track, 'genres'), _tags(track, 'moods'),
            updated.isoformat() if hasattr(updated, 'isoformat') else _text(updated))


def _is_cancelled() -> bool:
    if _cancel.is_set():
        return True
    with music_index.connect() as db:
        return bool(db.execute('SELECT cancel_requested FROM scan_state WHERE id=1').fetchone()[0])


def _total_tracks(plex: Any, library_id: str) -> int:
    result = plex.server.query(
        f'/library/sections/{library_id}/all',
        params={'type': 10, 'X-Plex-Container-Start': 0, 'X-Plex-Container-Size': 0},
    )
    raw = result.attrib.get('totalSize') if hasattr(result, 'attrib') else result.get('totalSize')
    if raw is None:
        raise ValueError('Plex did not report library size; preserving the previous index')
    total = int(raw)
    if total < 0:
        raise ValueError('Plex reported an invalid library size')
    return total


def _run(plex: Any, lock_file) -> None:
    try:
        server_id = _text(getattr(plex.server, 'machineIdentifier', None))
        library_id = _text(getattr(plex.library, 'key', None))
        if not server_id or not library_id:
            raise ValueError('Plex server or library identity is unavailable')
        initial_total = _total_tracks(plex, library_id)
        with music_index.connect() as db:
            db.execute('DELETE FROM scan_tracks')
            db.commit()
        offset = 0
        while True:
            if _is_cancelled():
                raise InterruptedError('Scan cancelled')
            page = plex.library.search(libtype='track', container_start=offset,
                                       container_size=_BATCH_SIZE, maxresults=_BATCH_SIZE)
            records = [_record(track, server_id, library_id) for track in page]
            if _is_cancelled():
                raise InterruptedError('Scan cancelled')
            with music_index.connect() as db:
                db.executemany('INSERT INTO scan_tracks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', records)
                db.execute('UPDATE scan_state SET scanned=? WHERE id=1', (offset + len(records),))
                db.commit()
            offset += len(records)
            if len(records) < _BATCH_SIZE:
                break
        if _is_cancelled():
            raise InterruptedError('Scan cancelled')
        if offset != initial_total or _total_tracks(plex, library_id) != initial_total:
            raise ValueError('Plex library changed during scan; preserving the previous index')
        with music_index.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT cancel_requested FROM scan_state WHERE id=1').fetchone()[0]:
                raise InterruptedError('Scan cancelled')
            db.execute('''INSERT INTO tracks SELECT * FROM scan_tracks WHERE 1
                ON CONFLICT(server_id, library_id, rating_key) DO UPDATE SET
                  title=excluded.title, artist=excluded.artist, artist_key=excluded.artist_key,
                  album=excluded.album, album_key=excluded.album_key, year=excluded.year,
                  duration_ms=excluded.duration_ms, genres=excluded.genres, moods=excluded.moods,
                  updated_at=excluded.updated_at
                WHERE tracks.title IS NOT excluded.title OR tracks.artist IS NOT excluded.artist
                   OR tracks.artist_key IS NOT excluded.artist_key OR tracks.album IS NOT excluded.album
                   OR tracks.album_key IS NOT excluded.album_key OR tracks.year IS NOT excluded.year
                   OR tracks.duration_ms IS NOT excluded.duration_ms OR tracks.genres IS NOT excluded.genres
                   OR tracks.moods IS NOT excluded.moods OR tracks.updated_at IS NOT excluded.updated_at''')
            db.execute('''DELETE FROM tracks WHERE server_id=? AND library_id=? AND NOT EXISTS
                (SELECT 1 FROM scan_tracks s WHERE s.server_id=tracks.server_id AND s.library_id=tracks.library_id
                 AND s.rating_key=tracks.rating_key)''', (server_id, library_id))
            for table in ('artists', 'albums', 'track_tags'):
                db.execute(f'DELETE FROM {table} WHERE server_id=? AND library_id=?', (server_id, library_id))
            db.execute("""INSERT INTO artists SELECT server_id, library_id, artist_key, MAX(artist)
                FROM tracks WHERE server_id=? AND library_id=? AND COALESCE(artist_key, '') != ''
                GROUP BY server_id, library_id, artist_key""", (server_id, library_id))
            db.execute("""INSERT INTO albums SELECT server_id, library_id, album_key, MAX(artist_key), MAX(album), MAX(year)
                FROM tracks WHERE server_id=? AND library_id=? AND COALESCE(album_key, '') != ''
                GROUP BY server_id, library_id, album_key""", (server_id, library_id))
            for kind, column in (('genre', 'genres'), ('mood', 'moods')):
                db.execute(f"""INSERT OR IGNORE INTO track_tags
                    SELECT t.server_id, t.library_id, t.rating_key, ?, tag.value
                    FROM tracks t, json_each(t.{column}) tag WHERE t.server_id=? AND t.library_id=?""",
                    (kind, server_id, library_id))
            db.execute('DELETE FROM scan_tracks')
            db.execute("UPDATE scan_state SET status='idle', server_id=?, library_id=?, completed_at=?, error=NULL WHERE id=1",
                       (server_id, library_id, music_index.utcnow()))
            db.commit()
    except Exception as exc:
        log.warning('Music library scan stopped: %s', exc)
        with music_index.connect() as db:
            db.execute('DELETE FROM scan_tracks')
            db.execute("UPDATE scan_state SET status=?, error=? WHERE id=1",
                       ('cancelled' if isinstance(exc, InterruptedError) else 'failed',
                        'Scan cancelled' if isinstance(exc, InterruptedError) else 'Library scan failed; check bridge logs'))
            db.commit()
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def start(plex: Any) -> None:
    global _thread
    if plex is None:
        raise ValueError('Connect Plex before indexing')
    if not music_index.get_settings()['indexing_enabled']:
        raise ValueError('Enable background library indexing first')
    with _lock:
        if _thread and _thread.is_alive():
            raise RuntimeError('A library scan is already running')
        path = Path(catalog_store.DATA_DIR) / 'music_index.lock'
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        lock_file = os.fdopen(fd, 'r+')
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            raise RuntimeError('A library scan is already running') from None
        try:
            _cancel.clear()
            with music_index.connect() as db:
                db.execute("UPDATE scan_state SET status='running', started_at=?, scanned=0, error=NULL, cancel_requested=0 WHERE id=1", (music_index.utcnow(),))
                db.commit()
            _thread = threading.Thread(target=_run, args=(plex, lock_file), name='music-index-scan', daemon=True)
            _thread.start()
        except Exception:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
            raise


def recover_interrupted() -> None:
    """Mark an abandoned scan without interfering with a scan in another process."""
    if not music_index.database_path().exists():
        return
    music_index.initialize()
    path = Path(catalog_store.DATA_DIR) / 'music_index.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'r+') as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            with music_index.connect() as db:
                if db.execute('SELECT status FROM scan_state WHERE id=1').fetchone()[0] == 'running':
                    db.execute('DELETE FROM scan_tracks')
                    db.execute("UPDATE scan_state SET status='failed', error='Scan interrupted by restart' WHERE id=1")
                    db.commit()
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)



def cancel() -> None:
    _cancel.set()
    music_index.initialize()
    with music_index.connect() as db:
        db.execute("UPDATE scan_state SET cancel_requested=1 WHERE id=1 AND status='running'")
        db.commit()
