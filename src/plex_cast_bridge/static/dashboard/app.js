import { createDjHelpers } from './dj.js';
import { initMaintenance } from './maintenance.js';
import { initGoogleHome } from './google-home.js';
import { readJson, errorDetail, deviceKind, iconForKind, roomLabel, isPlayingState, pctFromUnit, unitFromPct } from './util.js';


  const $ = (id) => document.getElementById(id);
  const SPEAKER_KEY = 'plex_cast_selected_speaker';
  const POLL_MS = 5000;


  let speakers = [];
  let selected = localStorage.getItem(SPEAKER_KEY) || '';
  let lastPlayback = null;
  let toastTimer = null;
  let volumeTimer = null;
  let statusInFlight = false;
  let commandPending = false;
  let bridgeOk = null;
  let volumeDragging = false;
  let plexStatus = { linked: false, plex_ready: false };
  let plexPollTimer = null;
  let plexConnecting = false;

  localStorage.removeItem('plex_cast_api_key');

  function jsonHeaders(extra = {}) {
    return { 'Content-Type': 'application/json', ...extra };
  }


  function announce(message) {
    const el = $('feedback');
    el.textContent = message;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 3200);
  }

  async function copyText(text) {
    const value = String(text ?? '');
    if (!value) throw new Error('Nothing to copy');
    // navigator.clipboard requires a secure context (HTTPS/localhost). LAN HTTP needs a fallback.
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return;
    }
    const ta = document.createElement('textarea');
    ta.value = value;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    ta.setSelectionRange(0, value.length);
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } finally {
      ta.remove();
    }
    if (!ok) throw new Error('Copy command failed');
  }

  function setConnection(ok, label) {
    bridgeOk = ok;
    const wrap = $('connectionStatus');
    wrap.classList.toggle('is-ok', ok === true);
    wrap.classList.toggle('is-bad', ok === false);
    $('connectionLabel').textContent = label;
  }





  function enabledSpeakers() {
    return speakers.filter((s) => s.enabled);
  }

  function currentDevice() {
    return speakers.find((d) => d.name === selected) || null;
  }





  const { buildDjQuery, saveRecent, renderRecents } = createDjHelpers($);

  function updateView() {
    const hash = location.hash || '#player';
    const settings = hash === '#settings';
    const dj = hash === '#dj';
    const speakersView = hash === '#speakers';
    const player = !settings && !dj && !speakersView;
    $('playerPage').hidden = !player;
    $('djPage').hidden = !dj;
    $('settingsPage').hidden = !settings;
    $('speakersPage').hidden = !speakersView;
    $('navPlayer').removeAttribute('aria-current');
    $('navDj').removeAttribute('aria-current');
    $('navSpeakers').removeAttribute('aria-current');
    $('navSettings').removeAttribute('aria-current');
    if (settings) {
      $('navSettings').setAttribute('aria-current', 'page');
      document.title = 'Settings — Plex Cast Bridge';
      refreshPlexStatus();
    } else if (speakersView) {
      $('navSpeakers').setAttribute('aria-current', 'page');
      document.title = 'Speakers — Plex Cast Bridge';
      loadSpeakers();
      refreshGoogleHome();
    } else if (dj) {
      $('navDj').setAttribute('aria-current', 'page');
      document.title = 'Smart DJ — Plex Cast Bridge';
    } else {
      $('navPlayer').setAttribute('aria-current', 'page');
      document.title = 'Player — Plex Cast Bridge';
    }
    window.scrollTo(0, 0);
  }

  function renderPlexStatus() {
    const linked = Boolean(plexStatus.linked || plexStatus.plex_ready);
    const stateEl = $('plexLinkState');
    stateEl.classList.toggle('is-linked', linked);
    let label = 'Not linked';
    if (linked) {
      const bits = [];
      if (plexStatus.friendly_name || plexStatus.username) bits.push(plexStatus.friendly_name || plexStatus.username);
      if (plexStatus.server_name) bits.push(plexStatus.server_name);
      if (plexStatus.music_section) bits.push(plexStatus.music_section);
      label = bits.length ? `Linked · ${bits.join(' · ')}` : 'Linked';
    } else if (plexConnecting) {
      label = 'Waiting for Plex authorization…';
    }
    $('plexLinkLabel').textContent = label;
    $('plexConnectBtn').hidden = linked;
    $('plexDisconnectBtn').hidden = !linked;
    $('plexConnectLabel').textContent = plexConnecting ? 'Waiting…' : 'Connect with Plex';
    $('plexConnectBtn').disabled = plexConnecting;
    if (!linked) {
      $('djSubmit').title = 'Connect Plex in Settings before playing';
    } else {
      $('djSubmit').removeAttribute('title');
    }
  }

  async function refreshPlexStatus() {
    try {
      plexStatus = await api('/api/plex/status');
      renderPlexStatus();
      if (bridgeOk !== false) {
        setConnection(true, plexStatus.linked ? 'Local bridge connected' : 'Bridge online · Plex not linked');
      }
    } catch (err) {
      setConnection(false, 'Bridge unreachable');
      $('plexLinkLabel').textContent = err.message || 'Could not load Plex status';
    }
  }

  function stopPlexPoll() {
    if (plexPollTimer) {
      clearInterval(plexPollTimer);
      plexPollTimer = null;
    }
  }

  async function loadPlexServersAndSections() {
    const servers = await api('/api/plex/servers');
    const serverSel = $('plexServerSelect');
    serverSel.replaceChildren();
    (servers || []).forEach((s) => {
      serverSel.add(new Option(s.name || s.id, s.id));
    });
    if (!servers || !servers.length) {
      announce('No Plex servers found on this account');
      return;
    }
    $('plexPicker').hidden = false;
    await loadPlexSections();
  }

  async function loadPlexSections() {
    const serverId = $('plexServerSelect').value;
    if (!serverId) return;
    const sections = await api(`/api/plex/sections?server_id=${encodeURIComponent(serverId)}`);
    const sectionSel = $('plexSectionSelect');
    sectionSel.replaceChildren();
    const musicFirst = [...(sections || [])].sort((a, b) => {
      const am = /artist|music/i.test(a.type || '') ? 0 : 1;
      const bm = /artist|music/i.test(b.type || '') ? 0 : 1;
      return am - bm;
    });
    musicFirst.forEach((s) => {
      sectionSel.add(new Option(`${s.title} (${s.type})`, s.title));
    });
  }

  async function startPlexConnect() {
    if (plexConnecting) return;
    plexConnecting = true;
    renderPlexStatus();
    try {
      const data = await api('/api/plex/connect/start', { method: 'POST', body: '{}' });
      if (data.auth_url) {
        window.open(data.auth_url, '_blank', 'noopener');
      }
      announce('Complete sign-in in the Plex tab, then return here.');
      stopPlexPoll();
      plexPollTimer = setInterval(async () => {
        try {
          const st = await api('/api/plex/connect/status');
          if (st.state === 'authorized') {
            stopPlexPoll();
            plexConnecting = false;
            announce('Plex authorized. Choose your server and music library.');
            await loadPlexServersAndSections();
            await refreshPlexStatus();
    if (typeof refreshGoogleHome === 'function') await refreshGoogleHome();
          } else if (st.state === 'expired' || st.state === 'error') {
            stopPlexPoll();
            plexConnecting = false;
            renderPlexStatus();
            announce(st.detail || 'Plex login failed');
          }
        } catch (err) {
          stopPlexPoll();
          plexConnecting = false;
          renderPlexStatus();
          announce(err.message || 'Plex login poll failed');
        }
      }, 2000);
    } catch (err) {
      plexConnecting = false;
      renderPlexStatus();
      announce(err.message || 'Could not start Plex login');
    }
  }

  async function confirmPlexSelection() {
    const server_id = $('plexServerSelect').value;
    const section = $('plexSectionSelect').value;
    if (!server_id || !section) {
      announce('Select a server and music library');
      return;
    }
    try {
      plexStatus = await api('/api/plex/select', {
        method: 'POST',
        body: JSON.stringify({ server_id, section }),
      });
      $('plexPicker').hidden = true;
      renderPlexStatus();
      announce(`Connected to ${section} on ${plexStatus.server_name || 'Plex'}`);
    } catch (err) {
      announce(err.message || 'Could not save Plex selection');
    }
  }

  async function disconnectPlex() {
    try {
      plexStatus = await api('/api/plex/disconnect', { method: 'POST', body: '{}' });
      $('plexPicker').hidden = true;
      renderPlexStatus();
      announce('Plex disconnected');
    } catch (err) {
      announce(err.message || 'Could not disconnect Plex');
    }
  }

  function paintVolume(pct) {
    const v = Math.max(0, Math.min(100, Math.round(Number(pct) || 0)));
    $('volume').value = String(v);
    $('volume').style.setProperty('--pct', `${v}%`);
    $('volume').setAttribute('aria-valuenow', String(v));
    $('volumeValue').textContent = `${v}%`;
  }

  function setTransportEnabled(enabled) {
    ['previousTrack', 'pauseResume', 'nextTrack', 'stopTrack', 'volume', 'outputSelect'].forEach((id) => {
      const el = $(id);
      if (id === 'outputSelect') {
        el.disabled = !enabledSpeakers().length;
      } else if (id === 'volume') {
        el.disabled = !enabled;
      } else {
        el.disabled = !enabled || commandPending;
      }
    });
    $('djSubmit').disabled = !enabled || commandPending;
  }

  function updateOutputSelect() {
    const opts = enabledSpeakers();
    if (selected && !opts.some((d) => d.name === selected)) {
      selected = opts[0]?.name || '';
    }
    if (!selected && opts.length) {
      const preferred = opts.find((d) => String(d.name).toLowerCase().includes('master bedroom'));
      selected = (preferred || opts[0]).name;
    }
    if (selected) localStorage.setItem(SPEAKER_KEY, selected);

    const sel = $('outputSelect');
    const prev = sel.value;
    sel.replaceChildren();
    if (!opts.length) {
      sel.add(new Option(speakers.length ? 'No speakers enabled' : 'No speakers found', ''));
      sel.disabled = true;
      selected = '';
    } else {
      sel.disabled = false;
      opts.forEach((d) => {
        sel.add(new Option(`${roomLabel(d)} — ${d.name}`, d.name));
      });
      sel.value = selected;
      if (sel.value !== selected && opts[0]) {
        selected = opts[0].name;
        sel.value = selected;
      }
    }

    $('playingRoom').textContent = roomLabel(currentDevice());
    $('djTargetName').textContent = roomLabel(currentDevice());
    setTransportEnabled(Boolean(selected));
    if (prev && prev !== sel.value && !volumeDragging) {
      // keep UI quiet on first load
    }
  }

  function applyPlayback(playback) {
    lastPlayback = playback || null;
    const track = playback?.current_track;
    const state = playback?.state || 'IDLE';
    const playing = isPlayingState(state);

    $('trackTitle').textContent = track?.title || (playback ? 'Idle' : 'Nothing playing');
    $('trackArtist').textContent = track?.artist || (playback ? 'No track loaded' : 'Choose a speaker and ask the DJ');
    $('trackAlbum').textContent = track?.album || '';

    $('playbackState').textContent = playing ? 'Playing' : (state === 'PAUSED' ? 'Paused' : (state === 'IDLE' || state === 'UNKNOWN' ? (track ? 'Idle' : 'Idle') : state));
    $('statusLine').classList.toggle('is-playing', playing);
    $('pauseResume').setAttribute('aria-label', playing ? 'Pause playback' : 'Resume playback');
    $('pauseResumeIcon').querySelector('use').setAttribute('href', playing ? '#pause-icon' : '#play-icon');

    if (playback?.speaker) {
      const match = speakers.find((s) => s.name === playback.speaker);
      if (match?.enabled) {
        selected = match.name;
        localStorage.setItem(SPEAKER_KEY, selected);
        if ($('outputSelect').value !== selected && enabledSpeakers().some((s) => s.name === selected)) {
          $('outputSelect').value = selected;
        }
        $('playingRoom').textContent = roomLabel(match);
        $('djTargetName').textContent = roomLabel(match);
      } else {
        $('playingRoom').textContent = playback.speaker;
      }
    } else {
      $('playingRoom').textContent = roomLabel(currentDevice());
    }

    const art = track?.art;
    const cover = $('cover');
    const img = $('coverArt');
    if (art) {
      cover.classList.add('has-art');
      cover.setAttribute('aria-label', `Album artwork for ${track.title || 'current track'}`);
      img.hidden = false;
      setCoverArt(art);
    } else {
      cover.classList.remove('has-art');
      cover.setAttribute('aria-label', 'Album artwork placeholder');
      img.hidden = true;
      img.removeAttribute('src');
      $('coverSub').textContent = roomLabel(currentDevice()) === 'No output' ? 'The listening room' : roomLabel(currentDevice());
    }

    if (!volumeDragging && playback?.volume != null && Number.isFinite(Number(playback.volume))) {
      const maxV = playback?.max_volume != null ? Number(playback.max_volume) : 1;
      const pct = Math.min(Number(playback.volume), maxV) * 100;
      paintVolume(pct);
      $('volume').max = String(Math.round(maxV * 100));
    }

    renderUpNext(playback?.up_next || []);
  }

  let coverObjectUrl = null;
  async function setCoverArt(art) {
    const img = $('coverArt');
    if (!art) return;
    if (art.startsWith('/api/')) {
      try {
        const res = await fetch(art, { headers: jsonHeaders() });
        if (!res.ok) throw new Error('art');
        const blob = await res.blob();
        if (coverObjectUrl) URL.revokeObjectURL(coverObjectUrl);
        coverObjectUrl = URL.createObjectURL(blob);
        img.src = coverObjectUrl;
      } catch {
        img.hidden = true;
      }
      return;
    }
    // Legacy absolute URLs (should not include tokens after upgrade).
    if (String(art).includes('X-Plex-Token=')) {
      img.hidden = true;
      return;
    }
    if (img.src !== art) img.src = art;
  }

  function renderUpNext(items) {
    const list = $('upNextList');
    if (!list) return;
    list.replaceChildren();
    (items || []).slice(0, 12).forEach((t, idx) => {
      const li = document.createElement('li');
      const description = document.createElement('span');
      description.textContent = `${t.title || 'Track'} — ${t.artist || ''}`;
      li.appendChild(description);
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'ghost-button';
      btn.textContent = 'Remove';
      btn.addEventListener('click', async () => {
        try {
          await api('/api/queue', {
            method: 'POST',
            body: JSON.stringify({ action: 'remove', index: idx, speaker: selected }),
          });
          await refreshStatus();
        } catch (err) {
          announce(err.message || 'Could not update queue');
        }
      });
      li.appendChild(btn);
      list.appendChild(li);
    });
  }

  function renderDevices() {
    const list = $('deviceList');
    list.replaceChildren();

    if (!speakers.length) {
      const row = document.createElement('div');
      row.className = 'empty-row';
      row.textContent = bridgeOk === false
        ? 'Bridge unreachable. Check that the local service is running.'
        : 'No speakers discovered yet. Try Refresh speakers.';
      list.appendChild(row);
      return;
    }

    speakers.forEach((d) => {
      const kind = deviceKind(d);
      const row = document.createElement('div');
      row.className = 'device' + (d.enabled ? '' : ' is-disabled');

      const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      icon.setAttribute('class', 'icon device-icon');
      icon.setAttribute('aria-hidden', 'true');
      const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
      use.setAttribute('href', iconForKind(kind));
      icon.appendChild(use);

      const text = document.createElement('div');
      const name = document.createElement('span');
      name.className = 'device-name';
      name.textContent = roomLabel(d);
      const sub = document.createElement('span');
      sub.className = 'device-desc';
      const avail = d.availability && d.availability !== 'online' ? ` · ${d.availability}` : '';
      const ghLabel = d.google_home_name || `${roomLabel(d)} Plex`;
      sub.textContent = `Physical: ${d.name} · ${d.model || 'Google Cast'}${avail} · Virtual: ${ghLabel}`;
      text.append(name, sub);
      if (d.needs_review) {
        const warning = document.createElement('span');
        warning.className = 'device-desc';
        warning.textContent = 'Identity conflict: check Details, then choose which virtual devices to show in Google Home.';
        text.appendChild(warning);
      }

      const meta = document.createElement('div');
      meta.className = 'device-meta';
      meta.hidden = true;
      const bits = [];
      if (d.host) bits.push(`${d.host}${d.port ? ':' + d.port : ''}`);
      if (d.model) bits.push(d.model);
      if (d.uuid) bits.push(`Cast UUID: ${d.uuid}`);
      if (d.google_home_device_id) bits.push(`gh ${String(d.google_home_device_id)}`);
      if (d.last_seen) bits.push(`seen ${d.last_seen}`);
      meta.textContent = bits.length ? bits.join(' · ') : 'No technical details available.';

      const nameWrap = document.createElement('div');
      nameWrap.className = 'device-meta';
      nameWrap.style.marginTop = '0.35rem';
      const nameInput = document.createElement('input');
      nameInput.className = 'field-select';
      nameInput.value = ghLabel;
      nameInput.setAttribute('aria-label', `Google Home name for ${d.name}`);
      nameInput.placeholder = 'Kitchen Plex';
      const saveName = document.createElement('button');
      saveName.type = 'button';
      saveName.className = 'ghost-button';
      saveName.textContent = 'Save';
      saveName.setAttribute('aria-label', `Save Google Home name for ${d.name}`);
      saveName.addEventListener('click', () => patchSpeaker(d, { google_home_name: nameInput.value.trim() }));
      const cancelName = document.createElement('button');
      cancelName.type = 'button';
      cancelName.className = 'ghost-button';
      cancelName.textContent = 'Cancel';
      cancelName.setAttribute('aria-label', `Cancel Google Home name for ${d.name}`);
      cancelName.addEventListener('click', () => { nameInput.value = ghLabel; });
      nameWrap.append(nameInput, saveName, cancelName);

      const toggleMeta = document.createElement('button');
      toggleMeta.type = 'button';
      toggleMeta.className = 'device-toggle-meta';
      toggleMeta.textContent = 'Details';
      toggleMeta.setAttribute('aria-expanded', 'false');
      toggleMeta.addEventListener('click', () => {
        const open = meta.hidden;
        meta.hidden = !open;
        toggleMeta.textContent = open ? 'Hide details' : 'Details';
        toggleMeta.setAttribute('aria-expanded', open ? 'true' : 'false');
      });
      text.append(nameWrap, toggleMeta, meta);

      const actions = document.createElement('div');
      actions.className = 'device-actions';
      actions.style.display = 'flex';
      actions.style.flexDirection = 'column';
      actions.style.gap = '0.35rem';
      actions.style.alignItems = 'flex-end';

      const playLabel = document.createElement('label');
      playLabel.className = 'switch';
      playLabel.title = 'Allow playback';
      const playInput = document.createElement('input');
      playInput.type = 'checkbox';
      playInput.setAttribute('role', 'switch');
      playInput.checked = Boolean(d.enabled);
      playInput.setAttribute('aria-label', `Allow playback on ${d.name}`);
      playInput.addEventListener('change', () => patchSpeaker(d, { enabled: playInput.checked }, playInput));
      playLabel.appendChild(playInput);
      const playHint = document.createElement('span');
      playHint.className = 'device-desc';
      playHint.textContent = 'Allow playback';
      const playRow = document.createElement('div');
      playRow.style.display = 'flex';
      playRow.style.alignItems = 'center';
      playRow.style.gap = '0.4rem';
      playRow.append(playHint, playLabel);

      const ghLabelEl = document.createElement('label');
      ghLabelEl.className = 'switch';
      ghLabelEl.title = 'Show in Google Home';
      const ghInput = document.createElement('input');
      ghInput.type = 'checkbox';
      ghInput.setAttribute('role', 'switch');
      ghInput.checked = Boolean(d.google_home_visible);
      ghInput.setAttribute('aria-label', `Show ${ghLabel} in Google Home`);
      ghInput.addEventListener('change', () => patchSpeaker(d, { google_home_visible: ghInput.checked }, ghInput));
      ghLabelEl.appendChild(ghInput);
      const ghHint = document.createElement('span');
      ghHint.className = 'device-desc';
      ghHint.textContent = 'Google Home';
      const ghRow = document.createElement('div');
      ghRow.style.display = 'flex';
      ghRow.style.alignItems = 'center';
      ghRow.style.gap = '0.4rem';
      ghRow.append(ghHint, ghLabelEl);

      const forgetBtn = document.createElement('button');
      forgetBtn.type = 'button';
      forgetBtn.className = 'ghost-button';
      forgetBtn.textContent = 'Forget';
      forgetBtn.title = 'Remove virtual Plex device from Google Home on next sync';
      forgetBtn.addEventListener('click', () => forgetSpeaker(d));

      actions.append(playRow, ghRow, forgetBtn);
      row.append(icon, text, actions);
      list.appendChild(row);
    });
  }

  async function api(path, options = {}) {
    const res = await fetch(path, {
      ...options,
      headers: { ...jsonHeaders(), ...(options.headers || {}) },
    });
    const data = await readJson(res);
    if (!res.ok) throw new Error(errorDetail(data, `HTTP ${res.status}`));
    setConnection(true, 'Local bridge connected');
    return data;
  }

  function normalizeSpeakersPayload(data) {
    if (Array.isArray(data)) return { speakers: data, summary: null };
    if (data && Array.isArray(data.speakers)) return { speakers: data.speakers, summary: data.summary || null };
    return { speakers: [], summary: null };
  }

  async function loadSpeakers(forceScan = false) {
    const label = $('scanButtonLabel');
    const btn = $('scanButton');
    if (forceScan) {
      btn.disabled = true;
      label.textContent = 'Scanning…';
    }
    try {
      const data = forceScan
        ? await api('/api/speakers/refresh', { method: 'POST' })
        : await api('/api/speakers?scan=false');
      const norm = normalizeSpeakersPayload(data);
      speakers = norm.speakers;
      renderDevices();
      updateOutputSelect();
      if (forceScan) {
        const msg = norm.summary?.message || `Scan complete. Found ${speakers.length} device${speakers.length === 1 ? '' : 's'}.`;
        announce(msg);
      }
    } catch (err) {
      if (bridgeOk !== false) setConnection(false, 'Bridge unreachable');
      renderDevices();
      updateOutputSelect();
      if (forceScan || !speakers.length) announce(err.message || 'Could not load speakers');
    } finally {
      btn.disabled = false;
      label.textContent = 'Refresh speakers';
    }
  }

  async function forgetSpeaker(device) {
    const id = device.google_home_device_id || device.id;
    if (!id) {
      announce('Speaker is missing a stable id');
      return;
    }
    const ok = window.confirm(
      'This removes the virtual Plex device from Google Home during the next successful sync. It does not remove the actual Nest speaker from Google Home.\n\nContinue?'
    );
    if (!ok) return;
    try {
      const res = await api(`/api/speakers/${encodeURIComponent(id)}`, { method: 'DELETE' });
      speakers = speakers.filter((s) => (s.google_home_device_id || s.id) !== id);
      renderDevices();
      updateOutputSelect();
      announce(res.note || 'Speaker forgotten. Sync Google Home to update the Home app.');
      await refreshGoogleHome();
    } catch (err) {
      announce(err.message || 'Could not forget speaker');
    }
  }

  async function toggleSpeaker(name, enabled, inputEl) {
    const previous = speakers.map((s) => ({ ...s }));
    speakers = speakers.map((s) => (s.name === name ? { ...s, enabled } : s));
    renderDevices();
    updateOutputSelect();
    try {
      const data = await api('/api/speakers/toggle', {
        method: 'POST',
        body: JSON.stringify({ speaker: name, enabled }),
      });
      const norm = normalizeSpeakersPayload(data);
      if (norm.speakers.length) speakers = norm.speakers;
      renderDevices();
      updateOutputSelect();
      announce(`${name} ${enabled ? 'enabled' : 'disabled'}.`);
    } catch (err) {
      speakers = previous;
      renderDevices();
      updateOutputSelect();
      if (inputEl) inputEl.checked = !enabled;
      announce(err.message || 'Could not update speaker');
    }
  }

  async function patchSpeaker(device, patch, inputEl) {
    const id = device.google_home_device_id || device.id;
    if (!id) {
      announce('Speaker is missing a stable Google Home id');
      return;
    }
    const previous = speakers.map((s) => ({ ...s }));
    speakers = speakers.map((s) => ((s.google_home_device_id || s.id) === id ? { ...s, ...patch } : s));
    renderDevices();
    try {
      const updated = await api(`/api/speakers/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        body: JSON.stringify(patch),
      });
      speakers = speakers.map((s) => ((s.google_home_device_id || s.id) === id ? { ...s, ...updated } : s));
      renderDevices();
      updateOutputSelect();
      if (patch.google_home_visible === true) {
        announce(`Showing ${updated.google_home_name || 'Plex'} in Google Home. Assign its room in the Google Home app after sync.`);
      } else {
        announce('Speaker settings saved.');
      }
    } catch (err) {
      speakers = previous;
      renderDevices();
      updateOutputSelect();
      if (inputEl && typeof patch.google_home_visible === 'boolean') inputEl.checked = !patch.google_home_visible;
      announce(err.message || 'Could not update speaker');
    }
  }

  async function refreshStatus() {
    if (statusInFlight) return;
    statusInFlight = true;
    try {
      const data = await api('/api/status');
      const sessions = data && typeof data === 'object' ? data : {};
      const keys = Object.keys(sessions);
      let playback = null;
      if (selected && sessions[selected]) {
        playback = sessions[selected];
      } else if (keys.length) {
        playback = sessions[keys[0]];
      }
      applyPlayback(playback);
    } catch (err) {
      setConnection(false, 'Bridge unreachable');
    } finally {
      statusInFlight = false;
    }
  }

  async function sendTransport(action, extra = {}) {
    if (!selected || commandPending) return;
    commandPending = true;
    setTransportEnabled(true);
    try {
      const data = await api('/api/transport', {
        method: 'POST',
        body: JSON.stringify({ action, speaker: selected, ...extra }),
      });
      applyPlayback(data);
      return data;
    } catch (err) {
      announce(err.message || `Transport ${action} failed`);
      throw err;
    } finally {
      commandPending = false;
      setTransportEnabled(Boolean(selected));
    }
  }

  async function sendCommand(query) {
    if (!selected || commandPending) return;
    const q = query.trim();
    if (!q) return;
    commandPending = true;
    $('djSubmitLabel').textContent = 'Playing…';
    setTransportEnabled(true);
    try {
      const data = await api('/api/command', {
        method: 'POST',
        body: JSON.stringify({ query: q, speaker: selected }),
      });
      if (data.playback) applyPlayback(data.playback);
      saveRecent(q);
      location.hash = '#player';
      const track = data.playback?.current_track;
      announce(track ? `Playing ${track.title} — ${track.artist}` : `Playing on ${roomLabel(currentDevice())}`);
      refreshStatus();
    } catch (err) {
      announce(err.message || 'Playback failed');
    } finally {
      commandPending = false;
      $('djSubmitLabel').textContent = 'Play music';
      setTransportEnabled(Boolean(selected));
    }
  }

  // Events
  $('outputSelect').addEventListener('change', (e) => {
    selected = e.target.value;
    if (selected) localStorage.setItem(SPEAKER_KEY, selected);
    $('playingRoom').textContent = roomLabel(currentDevice());
    $('djTargetName').textContent = roomLabel(currentDevice());
    $('coverSub').textContent = roomLabel(currentDevice());
    announce(`Output set to ${roomLabel(currentDevice())}`);
    refreshStatus();
  });

  $('volume').addEventListener('pointerdown', () => { volumeDragging = true; });
  $('volume').addEventListener('pointerup', () => { volumeDragging = false; });
  $('volume').addEventListener('input', (e) => {
    paintVolume(e.target.value);
  });
  $('volume').addEventListener('change', (e) => {
    volumeDragging = false;
    const pct = Number(e.target.value);
    paintVolume(pct);
    clearTimeout(volumeTimer);
    volumeTimer = setTimeout(async () => {
      const previous = lastPlayback?.volume;
      try {
        await sendTransport('volume', { volume: pct / 100 });
      } catch {
        if (previous != null) paintVolume(previous * 100);
      }
    }, 80);
  });

  $('pauseResume').addEventListener('click', async () => {
    if (!selected) return;
    const playing = isPlayingState(lastPlayback?.state);
    await sendTransport(playing ? 'pause' : 'resume');
  });
  $('nextTrack').addEventListener('click', () => sendTransport('next'));
  $('previousTrack').addEventListener('click', () => sendTransport('previous'));
  $('stopTrack').addEventListener('click', async () => {
    await sendTransport('stop');
    announce('Playback stopped');
  });
  $('scanButton').addEventListener('click', () => loadSpeakers(true));

  document.querySelectorAll('[name="djMode"]').forEach((r) => {
    r.addEventListener('change', () => {
      const mode = document.querySelector('[name="djMode"]:checked').value;
      $('modeExplainer').textContent = mode === 'radio' ? '50/50 with similar' : '100% that artist';
    });
  });

  $('djForm').addEventListener('submit', (e) => {
    e.preventDefault();
    const raw = $('promptText').value.trim();
    if (!raw) return;
    if (!plexStatus.linked && !plexStatus.plex_ready) {
      announce('Connect Plex in Settings before playing');
      location.hash = '#settings';
      return;
    }
    if (!selected) {
      announce('Enable a speaker before playing');
      return;
    }
    const mode = document.querySelector('[name="djMode"]:checked').value;
    sendCommand(buildDjQuery(raw, mode));
  });

  $('plexConnectBtn').addEventListener('click', startPlexConnect);
  $('plexDisconnectBtn').addEventListener('click', disconnectPlex);
  $('plexSelectBtn').addEventListener('click', confirmPlexSelection);
  $('plexServerSelect').addEventListener('change', () => {
    loadPlexSections().catch((err) => announce(err.message || 'Could not load libraries'));
  });

  document.querySelectorAll('[data-settings-tab]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const tab = btn.getAttribute('data-settings-tab');
      document.querySelectorAll('[data-settings-tab]').forEach((b) => b.setAttribute('aria-current', b === btn ? 'true' : 'false'));
      document.querySelectorAll('[data-settings-panel]').forEach((p) => {
        p.hidden = p.getAttribute('data-settings-panel') !== tab;
      });
      if (tab === 'connections') refreshGoogleHome();
      if (tab === 'playback') refreshPlaybackSettings();
      if (tab === 'diagnostics') refreshDiagnostics();
    });
  });



  function renderLibrarySummary(library) {
    const box = $('librarySummary');
    const meta = $('librarySummaryMeta');
    if (!library || !library.linked) {
      meta.textContent = 'No music library linked yet.';
      box.textContent = 'Connect with Plex under Settings → Connections.';
      return;
    }
    meta.textContent = library.section ? `Section: ${library.section}` : 'Linked music library.';
    const parts = [];
    if (library.artists != null) parts.push(`${library.artists.toLocaleString()} artists`);
    if (library.albums != null) parts.push(`${library.albums.toLocaleString()} albums`);
    if (library.tracks != null) parts.push(`${library.tracks.toLocaleString()} tracks`);
    box.textContent = parts.length ? parts.join(' · ') : 'Library linked (counts unavailable).';
  }
  async function refreshPlaybackSettings() {
    try {
      const data = await api('/api/settings');
      const vols = data.volumes || {};
      $('defaultVolumeInput').value = pctFromUnit(vols.default_cast_volume);
      $('maxVolumeInput').value = pctFromUnit(vols.max_cast_volume);
      const stored = data.stored?.configured;
      $('volumeSettingsHint').textContent = stored
        ? 'Your volume settings are saved.'
        : 'Using the default volume settings.';
      renderLibrarySummary(data.library);
    } catch (err) {
      $('volumeSettingsHint').textContent = err.message || 'Could not load settings';
      $('librarySummary').textContent = 'Unavailable';
    }
  }
  $('saveVolumeSettingsBtn').addEventListener('click', async () => {
    const default_cast_volume = unitFromPct($('defaultVolumeInput').value);
    const max_cast_volume = unitFromPct($('maxVolumeInput').value);
    if (default_cast_volume == null || max_cast_volume == null) {
      announce('Enter default and max volume as percents (0–100)');
      return;
    }
    if (default_cast_volume > max_cast_volume) {
      announce('Default volume cannot exceed maximum');
      return;
    }
    try {
      await api('/api/settings', {
        method: 'PATCH',
        body: JSON.stringify({ default_cast_volume, max_cast_volume }),
      });
      announce('Volume settings saved');
      await refreshPlaybackSettings();
    } catch (err) {
      announce(err.message || 'Could not save volume settings');
    }
  });

  $('clearQueueBtn')?.addEventListener('click', async () => {
    try {
      await api('/api/queue', { method: 'POST', body: JSON.stringify({ action: 'clear', speaker: selected }) });
      await refreshStatus();
    } catch (err) {
      announce(err.message || 'Could not clear queue');
    }
  });


  const { refreshGoogleHome, refreshGoogleHomePairing } = initGoogleHome({ $, api, announce, copyText, loadSpeakers });

  const { refreshDiagnostics } = initMaintenance({ $, api, announce, jsonHeaders });

  window.addEventListener('hashchange', updateView);
  if (!location.hash) location.hash = '#player';
  updateView();
  renderRecents();
  paintVolume(20);
  setTransportEnabled(false);
  setConnection(null, 'Connecting…');
  renderPlexStatus();

  (async () => {
    await loadSpeakers(false);
    await refreshStatus();
    await refreshPlexStatus();
    await refreshGoogleHome();
    setInterval(() => {
      if (document.visibilityState === 'visible') {
        refreshStatus();
        if (!$('settingsPage').hidden) refreshGoogleHomePairing();
      }
    }, POLL_MS);
  })();
