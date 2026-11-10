"""SQLite index safety, persistence and failed-scan behavior."""
from types import SimpleNamespace

import pytest

from plex_cast_bridge import catalog_store, music_index, music_index_scan


@pytest.fixture
def index_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog_store, 'DATA_DIR', tmp_path)
    return tmp_path


def _track(key, title, view_count=None):
    return SimpleNamespace(ratingKey=key, title=title, grandparentTitle='Artist', grandparentRatingKey='a1',
                           parentTitle='Album', parentRatingKey='b1', year=2020, duration=1000,
                           genres=[SimpleNamespace(tag='Jazz')], moods=[], updatedAt=None, viewCount=view_count)


def _plex(tracks, revision=None, calls=None):
    def search(**kwargs):
        start = kwargs['container_start']
        if calls is not None:
            calls.append(start)
        return tracks[start:start + kwargs['container_size']]
    return SimpleNamespace(server=SimpleNamespace(machineIdentifier='server-1', query=lambda *args, **kwargs: {'totalSize': str(len(tracks))}),
                           library=SimpleNamespace(key='library-1', search=search, updatedAt=revision))


def _join():
    music_index_scan._thread.join(timeout=5)
    assert not music_index_scan._thread.is_alive()


def test_default_disabled_and_scanned_index_persists(index_dir):
    assert music_index.get_settings() == {'indexing_enabled': False, 'schedule': 'manual', 'ai_enabled': False}
    with pytest.raises(ValueError, match='Enable'):
        music_index_scan.start(_plex([_track('1', 'First')]))
    music_index.update_settings({'indexing_enabled': True})
    music_index_scan.start(_plex([_track('1', 'First'), _track('2', 'Second')]))
    _join()
    status = music_index.status()
    assert status['scan']['status'] == 'idle'
    assert status['counts'] == {'tracks': 2, 'artists': 1, 'albums': 1}
    assert status['quality']['tracks_with_genres'] == 2
    assert status['quality']['tracks_with_moods'] == 0
    assert (index_dir / 'music_index.sqlite3').stat().st_mode & 0o077 == 0
    music_index_scan.start(_plex([_track('2', 'Updated')]))
    _join()
    with music_index.connect() as db:
        assert [(r['rating_key'], r['title']) for r in db.execute('SELECT * FROM tracks')] == [('2', 'Updated')]


def test_failure_keeps_previous_snapshot(index_dir):
    music_index.update_settings({'indexing_enabled': True})
    music_index_scan.start(_plex([_track('1', 'First')]))
    _join()
    music_index_scan.start(_plex([_track(None, 'Invalid')]))
    _join()
    assert music_index.status()['scan']['status'] == 'failed'
    with music_index.connect() as db:
        assert [r['title'] for r in db.execute('SELECT * FROM tracks')] == ['First']


def test_settings_reject_invalid_values(index_dir):
    with pytest.raises(ValueError):
        music_index.update_settings({'indexing_enabled': 'true'})
    with pytest.raises(ValueError):
        music_index.update_settings({'schedule': 'hourly'})
    assert music_index.get_settings()['indexing_enabled'] is False


def test_cancel_and_overlap_keep_old_snapshot(index_dir):
    import threading

    music_index.update_settings({'indexing_enabled': True})
    music_index_scan.start(_plex([_track('1', 'First')]))
    _join()
    entered, proceed = threading.Event(), threading.Event()
    def slow(**kwargs):
        entered.set()
        assert proceed.wait(5)
        return [_track('2', 'Second')]
    plex = SimpleNamespace(server=SimpleNamespace(machineIdentifier='server-1', query=lambda *args, **kwargs: {'totalSize': '1'}),
                           library=SimpleNamespace(key='library-1', search=slow))
    music_index_scan.start(plex)
    assert entered.wait(5)
    with pytest.raises(RuntimeError, match='already running'):
        music_index_scan.start(plex)
    music_index_scan.recover_interrupted()
    assert music_index.status()['scan']['status'] == 'running'
    music_index_scan.cancel()
    proceed.set()
    _join()
    assert music_index.status()['scan']['status'] == 'cancelled'
    with music_index.connect() as db:
        assert [r['title'] for r in db.execute('SELECT * FROM tracks')] == ['First']


def test_recover_abandoned_scan(index_dir):
    music_index.initialize()
    with music_index.connect() as db:
        db.execute("UPDATE scan_state SET status='running' WHERE id=1")
        db.commit()
    music_index_scan.recover_interrupted()
    assert music_index.status()['scan']['error'] == 'Scan interrupted by restart'


def test_api_respects_opt_in_and_plex_requirement(index_dir):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from plex_cast_bridge.music_index_routes import router

    app = FastAPI()
    app.include_router(router)
    app.state.plex = None
    client = TestClient(app)
    assert client.get('/api/music-index').json()['settings']['indexing_enabled'] is False
    assert client.post('/api/music-index/scan').status_code == 400
    assert client.patch('/api/music-index/settings', json={'ai_enabled': True}).status_code == 400
    assert client.patch('/api/music-index/settings', json={'indexing_enabled': True, 'schedule': 'weekly'}).status_code == 200
    app.state.plex = _plex([_track('1', 'First')])
    assert client.post('/api/music-index/scan').status_code == 200
    _join()
    assert client.get('/api/music-index').json()['counts']['tracks'] == 1


def test_refuses_unknown_future_schema(index_dir):
    with music_index.connect() as db:
        db.execute('PRAGMA user_version=999')
    with pytest.raises(RuntimeError, match='newer'):
        music_index.initialize()


def test_migrates_v1_index_without_losing_tracks(index_dir):
    import sqlite3

    path = music_index.database_path()
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE tracks (
            server_id TEXT NOT NULL, library_id TEXT NOT NULL, rating_key TEXT NOT NULL,
            title TEXT NOT NULL, artist TEXT, artist_key TEXT, album TEXT, album_key TEXT,
            year INTEGER, duration_ms INTEGER, genres TEXT NOT NULL DEFAULT '[]',
            moods TEXT NOT NULL DEFAULT '[]', updated_at TEXT,
            PRIMARY KEY(server_id, library_id, rating_key))''')
        db.execute('CREATE TABLE scan_tracks AS SELECT * FROM tracks WHERE 0')
        db.execute('''CREATE TABLE scan_state (
            id INTEGER PRIMARY KEY CHECK (id=1), status TEXT NOT NULL DEFAULT 'idle',
            server_id TEXT, library_id TEXT, started_at TEXT, completed_at TEXT,
            scanned INTEGER NOT NULL DEFAULT 0, error TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0)''')
        db.execute("INSERT INTO scan_state (id, server_id, library_id, scanned) VALUES (1, 'server', 'library', 1)")
        db.execute("INSERT INTO tracks VALUES ('server', 'library', '42', 'Old track', NULL, NULL, NULL, NULL, NULL, NULL, '[]', '[]', NULL)")
        db.execute('PRAGMA user_version=1')
    music_index.initialize()
    with music_index.connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 3
        migrated = db.execute('SELECT title, play_count FROM tracks WHERE rating_key=?', ('42',)).fetchone()
        assert tuple(migrated) == ('Old track', None)
        assert 'play_count' in {row['name'] for row in db.execute('PRAGMA table_info(scan_tracks)')}
        assert {'source_updated_at', 'last_full_scan_at'} <= {row['name'] for row in db.execute('PRAGMA table_info(scan_state)')}


def test_daily_scans_skip_unchanged_plex_library_revision(index_dir):
    music_index.update_settings({'indexing_enabled': True})
    calls = []
    plex = _plex([_track('1', 'First')], revision='rev-1', calls=calls)
    music_index_scan.start(plex)
    _join()
    assert calls == [0]
    calls.clear()
    music_index_scan.start(plex)
    _join()
    assert calls == []
    assert music_index.status()['scan']['status'] == 'idle'
    changed = _plex([_track('1', 'Changed')], revision='rev-2', calls=calls)
    music_index_scan.start(changed)
    _join()
    assert calls == [0]
    with music_index.connect() as db:
        assert db.execute('SELECT title FROM tracks WHERE rating_key="1"').fetchone()[0] == 'Changed'


def test_unchanged_library_still_gets_weekly_full_reconciliation():
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 11, 10, tzinfo=UTC)
    state = {'source_updated_at': 'rev-1', 'last_full_scan_at': (now - timedelta(days=6)).isoformat()}
    assert music_index_scan._can_skip_full_scan('rev-1', state, 10, 10, now)
    state['last_full_scan_at'] = (now - timedelta(days=7)).isoformat()
    assert not music_index_scan._can_skip_full_scan('rev-1', state, 10, 10, now)
    assert not music_index_scan._can_skip_full_scan('rev-2', state, 10, 10, now)
    assert not music_index_scan._can_skip_full_scan('rev-1', state, 10, 9, now)


def test_library_revision_is_refetched_from_plex_sections():
    from xml.etree import ElementTree

    plex = SimpleNamespace(
        server=SimpleNamespace(query=lambda path: ElementTree.fromstring(
            '<MediaContainer><Directory key="1" updatedAt="fresh-revision" /></MediaContainer>')),
        library=SimpleNamespace(updatedAt='stale-cached-revision'),
    )
    assert music_index_scan._library_revision(plex, '1') == 'fresh-revision'


def test_changed_library_during_scan_preserves_snapshot(index_dir):
    music_index.update_settings({'indexing_enabled': True})
    music_index_scan.start(_plex([_track('1', 'First')]))
    _join()
    plex = _plex([_track('2', 'Second')])
    sizes = iter([{'totalSize': '1'}, {'totalSize': '2'}])
    plex.server.query = lambda *args, **kwargs: next(sizes)
    music_index_scan.start(plex)
    _join()
    assert music_index.status()['scan']['status'] == 'failed'
    with music_index.connect() as db:
        assert [r['title'] for r in db.execute('SELECT * FROM tracks')] == ['First']


def test_scheduler_only_runs_enabled_due_scans():
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 1, 8, tzinfo=UTC)
    daily = {'indexing_enabled': True, 'schedule': 'daily'}
    assert not music_index_scan.schedule_due({'indexing_enabled': False, 'schedule': 'daily'}, {}, now)
    assert not music_index_scan.schedule_due({'indexing_enabled': True, 'schedule': 'manual'}, {}, now)
    assert not music_index_scan.schedule_due(daily, {'status': 'running'}, now)
    assert music_index_scan.schedule_due(daily, {}, now)
    assert not music_index_scan.schedule_due(daily, {'started_at': (now - timedelta(hours=23)).isoformat()}, now)
    assert music_index_scan.schedule_due(daily, {'started_at': (now - timedelta(days=1)).isoformat()}, now)
    assert not music_index_scan.schedule_due({'indexing_enabled': True, 'schedule': 'weekly'},
                                              {'started_at': (now - timedelta(days=6)).isoformat()}, now)
    assert not music_index_scan.schedule_due(daily, {'started_at': 'bad'}, now)


def test_concurrent_database_initialization(index_dir):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(lambda _: music_index.status(), range(12)))
    assert all(item['counts']['tracks'] == 0 for item in statuses)


def test_index_survives_a_new_process(index_dir):
    import json
    import os
    import subprocess
    import sys

    music_index.update_settings({'indexing_enabled': True})
    music_index_scan.start(_plex([_track('persisted', 'Survives restart')]))
    _join()
    env = {**os.environ, 'DATA_DIR': str(index_dir)}
    result = subprocess.run(
        [sys.executable, '-c',
         'import json; from plex_cast_bridge import music_index; '
         'print(json.dumps(music_index.status()))'],
        check=True, capture_output=True, text=True, env=env,
    )
    persisted = json.loads(result.stdout)
    assert persisted['counts']['tracks'] == 1
    assert persisted['scan']['status'] == 'idle'
    with music_index.connect() as db:
        assert db.execute('SELECT title FROM tracks WHERE rating_key=?', ('persisted',)).fetchone()[0] == 'Survives restart'
