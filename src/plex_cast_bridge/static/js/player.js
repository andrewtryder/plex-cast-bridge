// Player transport, queue, artwork, and volume management

import { api } from "./api.js";
import { enabledSpeakers, findSpeaker, getSelectedSpeaker, onSpeakersChange, setSelectedSpeaker } from "./speakers.js";
import { $, announce, isPlayingState, roomLabel } from "./util.js";

let lastCoverThumb = null;
let currentPlayback = null;

export function getCurrentPlayback() {
  return currentPlayback;
}

export function setTransportEnabled(enabled) {
  ["pauseResume", "previousTrack", "nextTrack", "stopTrack", "volumeRange"].forEach((id) => {
    const el = $(id);
    if (el) el.disabled = !enabled;
  });
}

export function paintVolume(pct) {
  const range = $("volumeRange");
  const value = $("volumeValue");
  if (range) range.value = pct;
  if (value) value.textContent = `${pct}%`;
}

export function updateOutputSelect() {
  const sel = $("outputSelect");
  if (!sel) return;
  const current = getSelectedSpeaker();
  const enabled = enabledSpeakers();
  sel.replaceChildren();

  if (!enabled.length) {
    sel.add(new Option("No speakers enabled", ""));
    sel.disabled = true;
    $("djTargetName") && ($("djTargetName").textContent = "No output");
    setTransportEnabled(false);
    return;
  }

  sel.disabled = false;
  let hasMatch = false;
  enabled.forEach((spk) => {
    const name = spk.name || spk.speaker_name;
    const label = `${roomLabel(spk)} (${spk.model || "Google Cast"})`;
    const opt = new Option(label, name);
    if (name === current) {
      opt.selected = true;
      hasMatch = true;
    }
    sel.add(opt);
  });

  if (!hasMatch && enabled.length) {
    sel.selectedIndex = 0;
    setSelectedSpeaker(sel.value);
  }

  const chosen = findSpeaker(getSelectedSpeaker());
  const label = chosen ? roomLabel(chosen) : "No output";
  $("djTargetName") && ($("djTargetName").textContent = label);
  $("playingRoom") && ($("playingRoom").textContent = label);
}

export async function setCoverArt(thumb) {
  const art = $("coverArt");
  const sub = $("coverSub");
  if (!art) return;
  if (!thumb) {
    art.hidden = true;
    art.src = "";
    lastCoverThumb = null;
    if (sub) sub.textContent = "The listening room";
    return;
  }
  if (thumb === lastCoverThumb && !art.hidden) return;
  lastCoverThumb = thumb;
  try {
    const encoded = encodeURIComponent(thumb);
    art.src = `/api/artwork?thumb=${encoded}`;
    art.hidden = false;
    if (sub) sub.textContent = "Artwork from Plex";
  } catch (_) {
    art.hidden = true;
  }
}

export function renderUpNext(items) {
  const list = $("upNextList");
  if (!list) return;
  list.replaceChildren();
  if (!items || !items.length) {
    const p = document.createElement("p");
    p.className = "conn-meta";
    p.textContent = "Queue is empty.";
    list.appendChild(p);
    return;
  }
  items.forEach((item, idx) => {
    const row = document.createElement("div");
    row.className = "queue-item";
    row.style.display = "flex";
    row.style.alignItems = "center";
    row.style.justifyContent = "space-between";
    row.style.padding = "6px 0";
    row.style.borderBottom = "1px solid var(--rule)";

    const info = document.createElement("div");
    const title = document.createElement("span");
    title.style.display = "block";
    title.style.fontSize = "13px";
    title.textContent = `${idx + 1}. ${item.title || "Unknown track"}`;
    const artist = document.createElement("span");
    artist.style.display = "block";
    artist.style.fontSize = "11px";
    artist.style.color = "var(--muted)";
    artist.textContent = `${item.artist || "Unknown artist"}${item.album ? " · " + item.album : ""}`;
    info.append(title, artist);

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "ghost-button";
    removeBtn.textContent = "Remove";
    removeBtn.style.padding = "2px 6px";
    removeBtn.style.fontSize = "11px";
    removeBtn.addEventListener("click", async () => {
      try {
        const selected = getSelectedSpeaker();
        await api("/api/queue", {
          method: "POST",
          body: JSON.stringify({ action: "remove", index: idx, speaker: selected }),
        });
        await refreshStatus();
      } catch (err) {
        announce(err.message || "Could not remove track");
      }
    });

    row.append(info, removeBtn);
    list.appendChild(row);
  });
}

export function applyPlayback(playback) {
  currentPlayback = playback;
  const isPlaying = isPlayingState(playback?.state);
  const track = playback?.track;

  $("trackTitle") && ($("trackTitle").textContent = track?.title || (playback ? "Nothing playing" : "Bridge idle"));
  $("trackArtist") &&
    ($("trackArtist").textContent =
      track?.artist || (playback ? "No track loaded" : "Choose a speaker and ask the DJ"));
  $("trackAlbum") && ($("trackAlbum").textContent = track?.album || "");

  const dot = $("playingDot");
  if (dot) dot.classList.toggle("active", isPlaying);

  const stateEl = $("playbackState");
  if (stateEl) stateEl.textContent = playback?.state || "Idle";

  if (playback?.speaker) {
    const match = findSpeaker(playback.speaker);
    const room = match ? roomLabel(match) : playback.speaker;
    $("playingRoom") && ($("playingRoom").textContent = room);
  }

  const icon = $("pauseResumeIcon");
  if (icon) {
    const use = icon.querySelector("use");
    if (use) use.setAttribute("href", isPlaying ? "#pause-icon" : "#play-icon");
  }

  setCoverArt(track?.thumb);
  renderUpNext(playback?.queue || []);

  if (typeof playback?.volume === "number") {
    paintVolume(Math.round(playback.volume * 100));
  }

  setTransportEnabled(Boolean(playback && getSelectedSpeaker()));
}

export async function sendTransport(action, extra = {}) {
  const speaker = getSelectedSpeaker();
  if (!speaker) {
    announce("Select a speaker before sending transport commands");
    return;
  }
  try {
    await api("/api/transport", {
      method: "POST",
      body: JSON.stringify({ action, speaker, ...extra }),
    });
    await refreshStatus();
  } catch (err) {
    announce(err.message || `Transport ${action} failed`);
  }
}

export async function refreshStatus() {
  try {
    const speaker = getSelectedSpeaker();
    const query = speaker ? `?speaker=${encodeURIComponent(speaker)}` : "";
    const data = await api(`/api/status${query}`);
    applyPlayback(data);
  } catch (_) {}
}

export function initPlayerUi() {
  onSpeakersChange(() => {
    updateOutputSelect();
  });

  $("outputSelect")?.addEventListener("change", (e) => {
    setSelectedSpeaker(e.target.value);
    updateOutputSelect();
    refreshStatus();
  });

  $("previousTrack")?.addEventListener("click", () => sendTransport("previous"));
  $("nextTrack")?.addEventListener("click", () => sendTransport("next"));
  $("stopTrack")?.addEventListener("click", () => sendTransport("stop"));

  $("pauseResume")?.addEventListener("click", () => {
    const isPlaying = isPlayingState(currentPlayback?.state);
    sendTransport(isPlaying ? "pause" : "resume");
  });

  const range = $("volumeRange");
  if (range) {
    range.addEventListener("input", (e) => {
      const pct = Number(e.target.value) || 0;
      paintVolume(pct);
    });
    range.addEventListener("change", (e) => {
      const pct = Number(e.target.value) || 0;
      sendTransport("volume", { volume: pct / 100 });
    });
  }

  $("queueClear")?.addEventListener("click", async () => {
    const selected = getSelectedSpeaker();
    if (!selected) return;
    try {
      await api("/api/queue", {
        method: "POST",
        body: JSON.stringify({ action: "clear", speaker: selected }),
      });
      await refreshStatus();
    } catch (err) {
      announce(err.message || "Could not clear queue");
    }
  });
}
