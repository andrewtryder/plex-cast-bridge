"""SQLite index safety, persistence and failed-scan behavior."""
from types import SimpleNamespace

import pytest

from plex_cast_bridge import catalog_store, music_index, music_index_scan


@pytest.fixture
def index_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog_store, 'DATA_DIR', tmp_path)
    return tmp_path


def _track(key, title):
    return SimpleNamespace(ratingKey=key, title=title, grandparentTitle='Artist', grandparentRatingKey='a1',
                           parentTitle='Album', parentRatingKey='b1', year=2020, duration=1000,
                           genres=[SimpleNamespace(tag='Jazz')], moods=[], updatedAt=None)


def _plex(tracks):
    def search(**kwargs):
        start = kwargs['container_start']
        return tracks[start:start + kwargs['container_size']]
    return SimpleNamespace(server=SimpleNamespace(machineIdentifier='server-1'),
                           library=SimpleNamespace(key='library-1', search=search))


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
    plex = SimpleNamespace(server=SimpleNamespace(machineIdentifier='server-1'),
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
