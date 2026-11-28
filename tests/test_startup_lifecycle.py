"""Unit tests for startup_lifecycle helpers (deterministic, no external I/O)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plex_cast_bridge.startup_lifecycle import (
    LifespanBackgroundTasks,
    abort_startup_after_background_tasks,
    build_pubsub_dispatch,
    cancel_background_tasks,
    schedule_google_home_reconcile,
    schedule_music_index,
    schedule_speaker_discovery,
    shutdown_application,
)


def test_cancel_background_tasks_awaits_all():
    async def run():
        started = asyncio.Event()
        hold = asyncio.Event()

        async def blocker():
            started.set()
            await hold.wait()

        tasks = LifespanBackgroundTasks(
            discovery=asyncio.create_task(blocker()),
            music_index=asyncio.create_task(blocker()),
            google_home_reconcile=asyncio.create_task(blocker()),
        )
        await started.wait()
        await cancel_background_tasks(tasks)
        assert all(t.done() for t in (tasks.discovery, tasks.music_index, tasks.google_home_reconcile))

    asyncio.run(run())


def test_speaker_discovery_skips_when_interval_zero():
    assert (
        schedule_speaker_discovery(
            interval_sec=0,
            skip=False,
            lock=asyncio.Lock(),
            discover=lambda: [],
            persist=lambda *_a: None,
            cast=object(),
        )
        is None
    )


def test_google_home_reconcile_runs_at_least_once():
    calls = {"n": 0}

    def ok():
        calls["n"] += 1
        return {"state": "ok"}

    async def run():
        task = schedule_google_home_reconcile(interval_sec=1, reconcile=ok)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert calls["n"] >= 1


def test_music_index_scheduler_catches_exceptions():
    app = SimpleNamespace(state=SimpleNamespace(plex=object()))
    path = MagicMock()
    path.return_value.exists.return_value = True

    def boom():
        raise RuntimeError("boom")

    async def run():
        task = schedule_music_index(
            app=app,
            database_path=path,
            get_settings=boom,
            status=lambda: {"scan": {}},
            schedule_due=lambda *_a: False,
            start_scan=lambda _plex: None,
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_shutdown_application_closes_worker_once():
    async def run():
        app = SimpleNamespace(state=SimpleNamespace())
        worker = MagicMock()
        app.state.worker = worker
        cast = MagicMock()
        path = Path("/tmp/never-exists-xyz.sqlite3")
        tasks = LifespanBackgroundTasks()
        await shutdown_application(
            app,
            tasks,
            database_path=lambda: path,
            cancel_index_scan=lambda: None,
            stop_pending_push=lambda: None,
            close_cast=cast.close,
        )
        worker.close.assert_called_once()
        cast.close.assert_called_once()

    asyncio.run(run())


def test_shutdown_order_matches_legacy_sequence():
    """Music-index cancel → scan cancel → stop push → reconcile → discovery → worker → cast."""

    async def run():
        steps: list[str] = []

        ready = asyncio.Event()

        async def slow(name: str):
            steps.append(f"armed:{name}")
            ready.set()
            await asyncio.sleep(3600)

        tasks = LifespanBackgroundTasks(
            music_index=asyncio.create_task(slow("music")),
            google_home_reconcile=asyncio.create_task(slow("reconcile")),
            discovery=asyncio.create_task(slow("discovery")),
        )
        await asyncio.wait_for(ready.wait(), timeout=1)
        path = MagicMock()
        path.return_value.exists.return_value = True
        worker = MagicMock()
        worker.close.side_effect = lambda: steps.append("worker.close")
        cast = MagicMock()
        cast.close.side_effect = lambda: steps.append("cast.close")
        app = SimpleNamespace(state=SimpleNamespace(worker=worker))

        await shutdown_application(
            app,
            tasks,
            database_path=path,
            cancel_index_scan=lambda: steps.append("scan.cancel"),
            stop_pending_push=lambda: steps.append("catalog.stop_push"),
            close_cast=cast.close,
        )

        assert tasks.music_index.done()
        assert steps.index("scan.cancel") < steps.index("catalog.stop_push")
        assert steps.index("catalog.stop_push") < steps.index("worker.close")
        assert steps.index("worker.close") < steps.index("cast.close")
        assert tasks.google_home_reconcile.done()
        assert tasks.discovery.done()

    asyncio.run(run())


def test_shutdown_best_effort_after_scan_cancel_failure():
    async def run():
        cast = MagicMock()
        cast.close = MagicMock()
        app = SimpleNamespace(state=SimpleNamespace(worker=None))
        path = MagicMock()
        path.return_value.exists.return_value = True
        await shutdown_application(
            app,
            LifespanBackgroundTasks(),
            database_path=path,
            cancel_index_scan=lambda: (_ for _ in ()).throw(RuntimeError("scan cancel failed")),
            stop_pending_push=lambda: None,
            close_cast=cast.close,
        )
        cast.close.assert_called_once()

    asyncio.run(run())


def test_abort_startup_closes_cast_after_pubsub_style_failure():
    async def run():
        closed = {"n": 0}
        tasks = LifespanBackgroundTasks(music_index=asyncio.create_task(asyncio.sleep(60)))
        await abort_startup_after_background_tasks(
            tasks,
            stop_pending_push=lambda: None,
            close_cast=lambda: closed.__setitem__("n", closed["n"] + 1),
        )
        assert closed["n"] == 1
        assert tasks.music_index.done()

    asyncio.run(run())


def test_pubsub_dispatch_routes_play_and_transport():
    async def run():
        app = SimpleNamespace(
            state=SimpleNamespace(
                dj="dj",
                plex="plex",
                cast="cast",
                settings="settings",
            )
        )
        seen: list[str] = []

        async def fake_play(**_kwargs):
            seen.append("play")
            return {"ok": True}

        async def fake_transport(**_kwargs):
            seen.append("transport")
            return {"ok": True}

        dispatch = build_pubsub_dispatch(
            app,
            play=fake_play,
            transport=fake_transport,
            command_cls=lambda **kw: SimpleNamespace(**kw),
            transport_cls=lambda **kw: SimpleNamespace(**kw),
        )
        await dispatch({"action": "play", "query": "jazz"})
        await dispatch({"action": "pause"})
        assert seen == ["play", "transport"]

    asyncio.run(run())


def test_pubsub_dispatch_gates_only_targeted_resume_to_cold_start(monkeypatch):
    calls = []

    class ColdStart:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def resume(self, device_id, message_id=None):
            calls.append(("resume", device_id, message_id))
            return {"voice_cold_start": "test"}

    monkeypatch.setattr("plex_cast_bridge.voice_cold_start.VoiceColdStart", ColdStart)
    app = SimpleNamespace(state=SimpleNamespace(cast="cast", settings="settings", plex="plex"))

    async def unexpected_transport(**_kwargs):
        raise AssertionError("cold-start result should handle targeted resume")

    async def run():
        dispatch = build_pubsub_dispatch(
            app,
            play=None,
            transport=unexpected_transport,
            command_cls=None,
            transport_cls=None,
        )
        result = await dispatch({
            "action": "resume",
            "device_id": "plex-jukebox:room",
            "_bridge_pubsub_message_id": "pubsub-id",
        })
        assert result == {"voice_cold_start": "test"}
        assert calls[-1] == ("resume", "plex-jukebox:room", "pubsub-id")

    asyncio.run(run())
