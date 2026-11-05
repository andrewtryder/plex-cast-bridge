import asyncio
import json
import logging
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .ai_router import SmartDJ
from .cast_manager import CastManager, extract_room_name
from .config import Settings
from .plex_service import PlexService
from .pubsub_worker import PubSubWorker

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CANONICAL_CATALOG = PROJECT_ROOT / "catalog.json"
CLOUD_RUN_CATALOG = PROJECT_ROOT / "gcp-cloud-run" / "catalog.json"
ICON_PATH = PROJECT_ROOT / "assets" / "icon.png"


def _load_disabled_speakers_from_catalog() -> set[str]:
    disabled: set[str] = set()
    if CANONICAL_CATALOG.exists():
        try:
            with open(CANONICAL_CATALOG, "r", encoding="utf-8") as f:
                data = json.load(f)
                for spk in data.get("speakers", []):
                    if not spk.get("enabled", True):
                        if spk.get("speaker_name"):
                            disabled.add(spk["speaker_name"].casefold())
                        if spk.get("roomHint"):
                            disabled.add(spk["roomHint"].casefold())
        except Exception as exc:
            log.warning("Could not read disabled speakers from %s: %s", CANONICAL_CATALOG, exc)
    return disabled


def _persist_discovered_speakers_to_catalog(discovered: list[dict], cast_mgr: CastManager):
    """Persist discovered LAN speakers and their enabled/disabled flags into catalog.json."""
    try:
        data = {}
        if CANONICAL_CATALOG.exists():
            with open(CANONICAL_CATALOG, "r", encoding="utf-8") as f:
                data = json.load(f)

        existing_by_room = {
            (s.get("roomHint") or "").casefold(): s
            for s in data.get("speakers", [])
            if s.get("roomHint")
        }

        speakers_list = []
        seen_rooms = set()
        for dev in discovered:
            name = dev["name"]
            if name.casefold() == "test speaker":
                continue
            room = dev.get("room") or extract_room_name(name)
            r_fold = room.casefold()
            if r_fold in seen_rooms:
                continue
            seen_rooms.add(r_fold)
            enabled = not cast_mgr.is_disabled(name) and not cast_mgr.is_disabled(room)
            speakers_list.append({
                "id": f"plex-jukebox:{room}",
                "name": "Plex",
                "nicknames": [f"{room} Plex", "Plex", "Plex Jukebox", "Plex Radio"],
                "roomHint": room,
                "speaker_name": name,
                "model": dev.get("model", "Google Cast"),
                "host": dev.get("host", ""),
                "port": dev.get("port", 8009),
                "uuid": dev.get("uuid", ""),
                "enabled": enabled,
            })

        # Keep any previously known speakers that might be temporarily asleep on LAN
        for r_fold, old_spk in existing_by_room.items():
            if r_fold not in seen_rooms:
                old_name = old_spk.get("speaker_name", "")
                old_room = old_spk.get("roomHint", "")
                old_spk["enabled"] = not cast_mgr.is_disabled(old_name) and not cast_mgr.is_disabled(old_room)
                speakers_list.append(old_spk)

        if speakers_list:
            data["speakers"] = speakers_list
            data["inputs"] = [s["roomHint"] for s in speakers_list if s.get("enabled", True)]
            with open(CANONICAL_CATALOG, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
            if CLOUD_RUN_CATALOG.parent.exists():
                shutil.copyfile(CANONICAL_CATALOG, CLOUD_RUN_CATALOG)
    except Exception as exc:
        log.warning("Could not persist speaker states to catalog.json: %s", exc)


class Command(BaseModel):
    query: str = Field(min_length=1)
    speaker: str | None = None


class SpeakerTest(BaseModel):
    speaker: str | None = None
    query: str | None = None


class SpeakerToggle(BaseModel):
    speaker: str = Field(min_length=1)
    enabled: bool


class Transport(BaseModel):
    action: Literal["pause", "resume", "next", "stop", "volume"]
    speaker: str | None = None
    volume: float | None = Field(default=None, ge=0, le=1)
    relative_volume: float | None = Field(default=None, ge=-1, le=1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    app.state.settings = settings
    app.state.plex = await run_in_threadpool(
        PlexService,
        settings.plex_url,
        settings.plex_token.get_secret_value(),
        settings.plex_music_section,
    )
    await run_in_threadpool(app.state.plex.refresh)
    app.state.dj = SmartDJ(settings.gemini_api_key.get_secret_value())
    disabled_speakers = _load_disabled_speakers_from_catalog()
    app.state.cast = CastManager(
        default_volume=settings.default_cast_volume,
        default_speaker_host=settings.default_speaker_host,
        disabled_speakers=disabled_speakers,
    )
    # Run initial background speaker discovery on daemon startup
    try:
        discovered = await run_in_threadpool(app.state.cast.discover)
        await run_in_threadpool(_persist_discovered_speakers_to_catalog, discovered, app.state.cast)
        log.info("Startup LAN scan discovered %d Cast targets", len(discovered))
    except Exception as exc:
        log.warning("Initial speaker discovery warning: %s", exc)

    app.state.worker = None
    if settings.enable_pubsub_listener:
        async def dispatch(data):
            if data["action"] == "play":
                return await play(app, Command(query=data["query"], speaker=data.get("speaker")))
            return await transport(
                app,
                Transport(
                    action=data["action"],
                    speaker=data.get("speaker"),
                    volume=data.get("volume"),
                    relative_volume=data.get("relative_volume"),
                ),
            )
        app.state.worker = PubSubWorker(settings, asyncio.get_running_loop(), dispatch)
    try:
        yield
    finally:
        if app.state.worker:
            app.state.worker.close()
        await run_in_threadpool(app.state.cast.close)


app = FastAPI(title="Plex Cast Bridge", lifespan=lifespan)


async def play(app, command: Command):
    def execute():
        intent = app.state.dj.parse(command.query, app.state.plex.catalog)
        tracks = app.state.plex.resolve(intent)
        if not tracks:
            raise ValueError("No matching tracks found in the Plex library")
        name = command.speaker or intent.target_speaker or app.state.settings.default_speaker_name
        speaker = app.state.cast.get(name)
        speaker.replace(tracks, intent.mode in ("artist", "artist_radio", "theme_mix"))
        return {"intent": intent.model_dump(), "playback": speaker.status()}
    return await run_in_threadpool(execute)


async def transport(app, command: Transport):
    def execute():
        speaker = app.state.cast.get(command.speaker or app.state.settings.default_speaker_name)
        if command.action == "next":
            speaker.skip()
        elif command.action == "stop":
            speaker.stop()
        elif command.action in ("pause", "resume"):
            getattr(speaker, command.action)()
        if command.volume is not None:
            speaker.set_volume(command.volume)
        elif command.relative_volume is not None and hasattr(speaker, "adjust_volume"):
            speaker.adjust_volume(command.relative_volume)
        return speaker.status()
    return await run_in_threadpool(execute)


@app.get("/assets/icon.png", include_in_schema=False)
async def serve_icon():
    if ICON_PATH.exists():
        return FileResponse(ICON_PATH, media_type="image/png")
    raise HTTPException(status_code=404, detail="Icon not found")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_ui():
    return HTMLResponse(DASHBOARD_HTML)


@app.post("/api/command")
async def command_endpoint(command: Command):
    try:
        return await play(app, command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/library/tracks")
async def library_tracks(query: str = "", limit: int = 25):
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")

    def search():
        kwargs = {"libtype": "track", "maxresults": limit}
        if query:
            kwargs["title"] = query
        tracks = app.state.plex.library.search(**kwargs)
        return [
            {
                "title": t.title,
                "artist": getattr(t, "grandparentTitle", ""),
                "album": getattr(t, "parentTitle", ""),
            }
            for t in tracks
        ]
    return await run_in_threadpool(search)


@app.post("/api/speakers/test")
async def test_speaker_endpoint(command: SpeakerTest):
    def execute():
        tracks = (
            app.state.plex.library.search(title=command.query, libtype="track")
            if command.query
            else app.state.plex.library.search(libtype="track", maxresults=1)
        )
        if not tracks:
            raise ValueError("No matching tracks found in the Plex library")
        speaker = app.state.cast.get(command.speaker or app.state.settings.default_speaker_name)
        speaker.replace([tracks[0]], repeat=False)
        return speaker.status()
    try:
        return await run_in_threadpool(execute)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/transport")
async def transport_endpoint(command: Transport):
    try:
        return await transport(app, command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/speakers")
async def speakers_endpoint(scan: bool = False):
    def get_list():
        if scan or not getattr(app.state.cast, "last_discovered", None):
            discovered = app.state.cast.discover()
            _persist_discovered_speakers_to_catalog(discovered, app.state.cast)
            return discovered
        for dev in app.state.cast.last_discovered:
            dev["enabled"] = not app.state.cast.is_disabled(dev["name"])
        return app.state.cast.last_discovered
    return await run_in_threadpool(get_list)


@app.post("/api/speakers/toggle")
async def toggle_speaker_endpoint(toggle: SpeakerToggle):
    def update():
        app.state.cast.set_speaker_enabled(toggle.speaker, toggle.enabled)
        discovered = getattr(app.state.cast, "last_discovered", None) or app.state.cast.discover()
        for dev in discovered:
            dev["enabled"] = not app.state.cast.is_disabled(dev["name"])
        _persist_discovered_speakers_to_catalog(discovered, app.state.cast)
        return {
            "speaker": toggle.speaker,
            "enabled": toggle.enabled,
            "speakers": discovered,
        }
    return await run_in_threadpool(update)


@app.get("/api/status")
async def status_endpoint():
    return {name: speaker.status() for name, speaker in list(app.state.cast.speakers.items())}


@app.post("/api/library/refresh")
async def refresh_endpoint():
    return await run_in_threadpool(app.state.plex.refresh)


@app.get("/api/sync-catalog")
async def catalog_endpoint():
    return {"genres": app.state.plex.catalog["genres"], "artists": app.state.plex.catalog["artists"][:100]}


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Plex Cast Bridge — Local Control & Speaker Manager</title>
  <style>
    :root {
      --bg: #0f1117;
      --panel: #181b23;
      --panel-border: #272b38;
      --text: #f3f4f6;
      --muted: #9ca3af;
      --accent: #e5a00d;
      --accent-hover: #f5b01d;
      --blue: #3b82f6;
      --green: #10b981;
      --red: #ef4444;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }
    .container {
      max-width: 1024px;
      margin: 0 auto;
      padding: 28px 20px 60px;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 28px;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--panel-border);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .brand img {
      width: 48px;
      height: 48px;
      border-radius: 11px;
      box-shadow: 0 4px 14px rgba(0,0,0,0.4);
    }
    .brand h1 {
      margin: 0;
      font-size: 1.35rem;
      font-weight: 700;
    }
    .brand p {
      margin: 2px 0 0;
      font-size: 0.85rem;
      color: var(--muted);
    }
    .grid {
      display: grid;
      grid-template-columns: 1.15fr 0.85fr;
      gap: 20px;
    }
    @media (max-width: 820px) {
      .grid { grid-template-columns: 1fr; }
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--panel-border);
      border-radius: 14px;
      padding: 20px;
      margin-bottom: 20px;
    }
    .card-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 14px;
    }
    .card-header h2 {
      margin: 0;
      font-size: 1.05rem;
      font-weight: 600;
    }
    .btn {
      background: #252936;
      color: var(--text);
      border: 1px solid #363b4d;
      border-radius: 8px;
      padding: 8px 14px;
      font-size: 0.85rem;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .btn:hover { background: #313748; }
    .btn-primary {
      background: var(--accent);
      color: #111;
      border-color: var(--accent);
      font-weight: 600;
    }
    .btn-primary:hover { background: var(--accent-hover); }
    .speaker-list {
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .speaker-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 14px;
      background: #13161d;
      border: 1px solid var(--panel-border);
      border-radius: 10px;
      transition: opacity 0.2s;
    }
    .speaker-item.disabled {
      opacity: 0.55;
    }
    .speaker-meta {
      display: flex;
      flex-direction: column;
    }
    .speaker-name {
      font-weight: 600;
      font-size: 0.95rem;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .badge {
      font-size: 0.72rem;
      padding: 2px 7px;
      border-radius: 999px;
      background: #262b38;
      color: var(--muted);
      font-weight: 500;
    }
    .badge.room {
      background: rgba(229, 160, 13, 0.16);
      color: var(--accent);
    }
    .speaker-sub {
      font-size: 0.78rem;
      color: var(--muted);
      margin-top: 2px;
    }
    /* Toggle Switch */
    .switch {
      position: relative;
      display: inline-block;
      width: 44px;
      height: 24px;
      flex-shrink: 0;
    }
    .switch input { opacity: 0; width: 0; height: 0; }
    .slider {
      position: absolute;
      cursor: pointer;
      inset: 0;
      background-color: #374151;
      transition: .2s;
      border-radius: 24px;
    }
    .slider:before {
      position: absolute;
      content: "";
      height: 18px;
      width: 18px;
      left: 3px;
      bottom: 3px;
      background-color: white;
      transition: .2s;
      border-radius: 50%;
    }
    input:checked + .slider { background-color: var(--green); }
    input:checked + .slider:before { transform: translateX(20px); }
    /* Prompt Box */
    .form-row {
      display: flex;
      gap: 10px;
      margin-bottom: 12px;
    }
    input[type="text"], select {
      background: #12151c;
      border: 1px solid var(--panel-border);
      color: var(--text);
      border-radius: 8px;
      padding: 10px 12px;
      font-size: 0.9rem;
      width: 100%;
    }
    input[type="text"]:focus, select:focus {
      outline: none;
      border-color: var(--accent);
    }
    .transport-bar {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 12px;
    }
    .now-playing {
      padding: 14px;
      background: #13161d;
      border: 1px solid var(--panel-border);
      border-radius: 10px;
      margin-bottom: 10px;
    }
    .now-title { font-weight: 600; font-size: 1rem; color: var(--accent); }
    .now-sub { font-size: 0.85rem; color: var(--muted); }
    .toast {
      font-size: 0.82rem;
      color: var(--muted);
      margin-top: 8px;
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="brand">
        <img src="/assets/icon.png" alt="Plex Cast Bridge" onerror="this.style.display='none'" />
        <div>
          <h1>Plex Cast Bridge</h1>
          <p>Local Cast Discovery, Speaker Enable/Disable Manager & Smart DJ Console</p>
        </div>
      </div>
      <div>
        <button class="btn" onclick="scanSpeakers(true)">Rescan LAN Speakers</button>
      </div>
    </header>

    <div class="grid">
      <div>
        <div class="card">
          <div class="card-header">
            <h2>Discovered Cast Speakers & Rooms</h2>
            <span id="speakerCount" class="badge">Scanning...</span>
          </div>
          <p style="font-size:0.83rem; color:var(--muted); margin-top:0; margin-bottom:14px;">
            Toggle speakers on or off. Disabled speakers cannot be cast to and are excluded when syncing rooms to Google Home.
          </p>
          <div id="speakerList" class="speaker-list"></div>
        </div>
      </div>

      <div>
        <div class="card">
          <div class="card-header">
            <h2>Smart DJ & Playback Control</h2>
          </div>
          <div class="form-row">
            <select id="targetSpeakerSelect"></select>
          </div>
          <div class="form-row">
            <input type="text" id="queryInput" placeholder="e.g. DJ Shadow, or DJ Shadow radio, or 90s trip hop" onkeydown="if(event.key==='Enter') sendPlay()" />
            <button class="btn btn-primary" onclick="sendPlay()">Play</button>
          </div>
          <div class="transport-bar">
            <button class="btn" onclick="sendTransport('pause')">Pause</button>
            <button class="btn" onclick="sendTransport('resume')">Resume</button>
            <button class="btn" onclick="sendTransport('next')">Next Track</button>
            <button class="btn" onclick="sendTransport('stop')">Stop</button>
            <button class="btn" onclick="sendVolume(-0.05)">Vol -5%</button>
            <button class="btn" onclick="sendVolume(0.05)">Vol +5%</button>
          </div>
          <div id="actionStatus" class="toast">Ready. Saying an artist name plays 100% that artist; add "radio" for a 50/50 station mix.</div>
        </div>

        <div class="card">
          <div class="card-header">
            <h2>Active Speaker Sessions</h2>
            <button class="btn" onclick="refreshStatus()">Refresh</button>
          </div>
          <div id="activeSessions">
            <div class="now-sub">No active streams yet.</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <script>
    let speakers = [];

    async function scanSpeakers(forceScan = false) {
      document.getElementById('speakerCount').textContent = forceScan ? 'Scanning LAN...' : 'Loading...';
      try {
        const res = await fetch(`/api/speakers?scan=${forceScan}`);
        speakers = await res.json();
        renderSpeakers();
      } catch (err) {
        document.getElementById('speakerCount').textContent = 'Error';
      }
    }

    function renderSpeakers() {
      const list = document.getElementById('speakerList');
      const select = document.getElementById('targetSpeakerSelect');
      const prevSelected = select.value;
      list.innerHTML = '';
      select.innerHTML = '';

      document.getElementById('speakerCount').textContent = `${speakers.length} Devices`;

      speakers.forEach(spk => {
        const item = document.createElement('div');
        item.className = 'speaker-item' + (spk.enabled ? '' : ' disabled');
        item.innerHTML = `
          <div class="speaker-meta">
            <div class="speaker-name">
              <span>${spk.name}</span>
              <span class="badge room">${spk.room || spk.name}</span>
            </div>
            <div class="speaker-sub">${spk.model || 'Google Cast'} &bull; ${spk.host}${spk.port ? ':' + spk.port : ''}</div>
          </div>
          <label class="switch" title="${spk.enabled ? 'Enabled' : 'Disabled'}">
            <input type="checkbox" ${spk.enabled ? 'checked' : ''} onchange="toggleSpeaker('${spk.name.replace(/'/g, "\\'")}', this.checked)" />
            <span class="slider"></span>
          </label>
        `;
        list.appendChild(item);

        if (spk.enabled) {
          const opt = document.createElement('option');
          opt.value = spk.name;
          opt.textContent = `${spk.room || spk.name} (${spk.name})`;
          if (spk.name.toLowerCase().includes('master bedroom')) opt.selected = true;
          select.appendChild(opt);
        }
      });

      if (prevSelected && Array.from(select.options).some(o => o.value === prevSelected)) {
        select.value = prevSelected;
      }
    }

    async function toggleSpeaker(name, enabled) {
      const statusEl = document.getElementById('actionStatus');
      statusEl.textContent = `${enabled ? 'Enabling' : 'Disabling'} '${name}'...`;
      try {
        const res = await fetch('/api/speakers/toggle', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({speaker: name, enabled})
        });
        const data = await res.json();
        speakers = data.speakers || speakers;
        renderSpeakers();
        statusEl.textContent = `'${name}' is now ${enabled ? 'enabled' : 'disabled'} (saved to catalog.json).`;
      } catch (err) {
        statusEl.textContent = `Error toggling speaker: ${err}`;
      }
    }

    async function sendPlay() {
      const query = document.getElementById('queryInput').value.trim();
      const speaker = document.getElementById('targetSpeakerSelect').value;
      if (!query) return;
      const statusEl = document.getElementById('actionStatus');
      statusEl.textContent = `Routing '${query}' to ${speaker}...`;
      try {
        const res = await fetch('/api/command', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({query, speaker})
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Command failed');
        const mode = data.intent?.mode || 'artist';
        const track = data.playback?.current_track;
        statusEl.textContent = `Playing (${mode}): ${track ? track.title + ' — ' + track.artist : 'Started'} on ${speaker}`;
        refreshStatus();
      } catch (err) {
        statusEl.textContent = `Error: ${err.message}`;
      }
    }

    async function sendTransport(action) {
      const speaker = document.getElementById('targetSpeakerSelect').value;
      const statusEl = document.getElementById('actionStatus');
      statusEl.textContent = `Sending '${action}' to ${speaker}...`;
      try {
        const res = await fetch('/api/transport', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({action, speaker})
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Transport failed');
        statusEl.textContent = `Transport '${action}' executed on ${speaker}.`;
        refreshStatus();
      } catch (err) {
        statusEl.textContent = `Error: ${err.message}`;
      }
    }

    async function sendVolume(delta) {
      const speaker = document.getElementById('targetSpeakerSelect').value;
      const statusEl = document.getElementById('actionStatus');
      try {
        const res = await fetch('/api/transport', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({action: 'volume', speaker, relative_volume: delta})
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Volume change failed');
        const volPct = data.volume != null ? Math.round(data.volume * 100) + '%' : 'updated';
        statusEl.textContent = `Volume on ${speaker}: ${volPct}`;
        refreshStatus();
      } catch (err) {
        statusEl.textContent = `Error: ${err.message}`;
      }
    }

    async function refreshStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        const container = document.getElementById('activeSessions');
        const keys = Object.keys(data);
        if (keys.length === 0) {
          container.innerHTML = '<div class="now-sub">No active streams yet.</div>';
          return;
        }
        container.innerHTML = keys.map(k => {
          const s = data[k];
          const t = s.current_track;
          const vol = s.volume != null ? Math.round(s.volume * 100) + '%' : '—';
          return `
            <div class="now-playing">
              <div style="display:flex; justify-content:space-between; align-items:center;">
                <strong>${s.speaker}</strong>
                <span class="badge">${s.state} &bull; Vol ${vol}</span>
              </div>
              <div class="now-title" style="margin-top:6px;">${t ? t.title : 'Idle'}</div>
              <div class="now-sub">${t ? t.artist + (t.album ? ' — ' + t.album : '') : 'No track loaded'} &bull; ${s.queue_length} in queue</div>
            </div>
          `;
        }).join('');
      } catch (err) {}
    }

    scanSpeakers(false);
    refreshStatus();
    setInterval(refreshStatus, 5000);
  </script>
</body>
</html>
"""
