"""Background task scheduling and shutdown for application lifespan."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from starlette.concurrency import run_in_threadpool

log = logging.getLogger(__name__)


@dataclass
class LifespanBackgroundTasks:
    discovery: asyncio.Task | None = None
    music_index: asyncio.Task | None = None
    google_home_reconcile: asyncio.Task | None = None


async def cancel_background_tasks(tasks: LifespanBackgroundTasks) -> None:
    """Cancel and await lifespan background loops (idempotent)."""
    for task in (tasks.discovery, tasks.music_index, tasks.google_home_reconcile):
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def schedule_speaker_discovery(
    *,
    interval_sec: int,
    skip: bool,
    lock: asyncio.Lock,
    discover: Callable[[], Any],
    persist: Callable[[Any, Any], Any],
    cast: Any,
) -> asyncio.Task | None:
    if interval_sec <= 0 or skip:
        return None

    async def _loop() -> None:
        while True:
            await asyncio.sleep(max(interval_sec, 60))
            if lock.locked():
                log.debug("Skipping overlapping speaker discovery job")
                continue
            async with lock:
                try:
                    discovered = await run_in_threadpool(discover)
                    result = await run_in_threadpool(persist, discovered, cast)
                    summary = (result or {}).get("summary") or {}
                    if (result or {}).get("changed"):
                        log.info(
                            "Background speaker discovery: %s",
                            summary.get("message") or "catalog updated",
                        )
                    else:
                        log.debug("Background speaker discovery: unchanged")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("Background speaker discovery failed: %s", exc)

    return asyncio.create_task(_loop())


def schedule_google_home_reconcile(
    *,
    interval_sec: int,
    reconcile: Callable[[], dict[str, Any]],
) -> asyncio.Task:
    async def _loop() -> None:
        backoff = max(10, interval_sec)
        while True:
            try:
                result = await run_in_threadpool(reconcile)
                backoff = min(300, backoff * 2) if result.get("state") == "failed" else max(10, interval_sec)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Google Home reconciliation failed")
                backoff = min(300, backoff * 2)
            await asyncio.sleep(backoff)

    return asyncio.create_task(_loop())


def schedule_music_index(
    *,
    app: Any,
    database_path: Callable[[], Any],
    get_settings: Callable[[], Any],
    status: Callable[[], Any],
    schedule_due: Callable[[Any, Any], bool],
    start_scan: Callable[[Any], Any],
) -> asyncio.Task:
    async def _loop() -> None:
        while True:
            try:
                if database_path().exists():
                    settings_data = await run_in_threadpool(get_settings)
                    plex = getattr(app.state, "plex", None)
                    if plex and settings_data["indexing_enabled"] and settings_data["schedule"] != "manual":
                        state = await run_in_threadpool(status)
                        if schedule_due(settings_data, state["scan"]):
                            await run_in_threadpool(start_scan, plex)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Music indexing scheduler failed")
            await asyncio.sleep(60)

    return asyncio.create_task(_loop())


def build_pubsub_dispatch(
    app: Any,
    *,
    play: Callable[..., Awaitable[Any]],
    transport: Callable[..., Awaitable[Any]],
    command_cls: Any,
    transport_cls: Any,
) -> Callable[[dict[str, Any]], Awaitable[Any]]:
    async def dispatch(data: dict[str, Any]):
        if data["action"] == "play":
            return await play(
                dj=app.state.dj,
                plex=app.state.plex,
                cast=app.state.cast,
                settings=app.state.settings,
                command=command_cls(
                    query=data["query"],
                    speaker=data.get("speaker"),
                    device_id=data.get("device_id"),
                ),
            )
        return await transport(
            cast=app.state.cast,
            settings=app.state.settings,
            command=transport_cls(
                action=data["action"],
                device_id=data.get("device_id"),
                speaker=data.get("speaker"),
                volume=data.get("volume"),
                relative_volume=data.get("relative_volume"),
                mute=data.get("mute"),
            ),
        )

    return dispatch


async def shutdown_application(
    app: Any,
    tasks: LifespanBackgroundTasks,
    *,
    database_path: Callable[[], Any],
    cancel_index_scan: Callable[[], Any],
    stop_pending_push: Callable[[], None],
    close_cast: Callable[[], Any],
) -> None:
    """Shutdown ordering: music index → catalog push → reconcile → discovery → Pub/Sub → Cast."""
    await cancel_background_tasks(tasks)
    if database_path().exists():
        await run_in_threadpool(cancel_index_scan)
    stop_pending_push()
    worker = getattr(app.state, "worker", None)
    if worker:
        worker.close()
    await run_in_threadpool(close_cast)
