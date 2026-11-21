// Speaker administration and discovery for Plex Cast Bridge

import { api, isAuthFailed } from "./api.js";
import { $, announce, deviceKind, iconForKind, roomLabel } from "./util.js";

const SPEAKER_KEY = "plex_cast_selected_speaker";

let speakers = [];
let changeListeners = [];

export function getSpeakers() {
  return speakers;
}

export function getSelectedSpeaker() {
  return localStorage.getItem(SPEAKER_KEY) || "";
}

export function setSelectedSpeaker(name) {
  if (name) localStorage.setItem(SPEAKER_KEY, name);
  else localStorage.removeItem(SPEAKER_KEY);
  notifyChange();
}

export function onSpeakersChange(fn) {
  changeListeners.push(fn);
}

function notifyChange() {
  changeListeners.forEach((fn) => {
    try {
      fn(speakers);
    } catch (_) {}
  });
}

export function enabledSpeakers() {
  return speakers.filter((s) => s.enabled);
}

export function findSpeaker(name) {
  return speakers.find((s) => s.name === name || s.speaker_name === name) || null;
}

function normalizeSpeakersPayload(data) {
  if (Array.isArray(data)) return { speakers: data, summary: null };
  if (data && Array.isArray(data.speakers)) {
    return { speakers: data.speakers, summary: data.summary || null };
  }
  return { speakers: [], summary: null };
}

export async function loadSpeakers(forceScan = false) {
  const label = $("scanButtonLabel");
  if (label && forceScan) label.textContent = "Scanning…";
  try {
    const data = forceScan
      ? await api("/api/speakers/refresh", { method: "POST" })
      : await api("/api/speakers?scan=false");
    const norm = normalizeSpeakersPayload(data);
    speakers = norm.speakers;
    if (forceScan) {
      const msg =
        norm.summary?.message ||
        `Scan complete. Found ${speakers.length} device${speakers.length === 1 ? "" : "s"}.`;
      announce(msg);
    }
  } catch (err) {
    if (forceScan || !speakers.length) announce(err.message || "Could not load speakers");
  } finally {
    if (label) label.textContent = "Refresh speakers";
  }
  renderSpeakers();
  notifyChange();
}

export async function patchSpeaker(device, patch, inputEl) {
  const id = device.google_home_device_id || device.id;
  if (!id) return;
  const previous = speakers.map((s) => ({ ...s }));
  speakers = speakers.map((s) =>
    (s.google_home_device_id || s.id) === id ? { ...s, ...patch } : s
  );
  renderSpeakers();
  notifyChange();
  try {
    const updated = await api(`/api/speakers/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    speakers = speakers.map((s) =>
      (s.google_home_device_id || s.id) === id ? { ...s, ...updated } : s
    );
  } catch (err) {
    speakers = previous;
    if (inputEl && "checked" in inputEl) {
      const match = previous.find((s) => (s.google_home_device_id || s.id) === id);
      if (match) {
        if ("enabled" in patch) inputEl.checked = Boolean(match.enabled);
        if ("google_home_visible" in patch) inputEl.checked = Boolean(match.google_home_visible);
      }
    }
    announce(err.message || "Could not update speaker");
  }
  renderSpeakers();
  notifyChange();
}

export async function forgetSpeaker(device) {
  const id = device.google_home_device_id || device.id;
  const name = device.google_home_name || roomLabel(device);
  const ok = window.confirm(
    `Forget "${name}"?\n\n` +
      "This removes the virtual Plex device from Google Home during the next successful sync. " +
      "It does not remove the actual Nest speaker from Google Home.\n\nContinue?"
  );
  if (!ok) return;
  try {
    await api(`/api/speakers/${encodeURIComponent(id)}`, { method: "DELETE" });
    speakers = speakers.filter((s) => (s.google_home_device_id || s.id) !== id);
    announce(`Forgot ${name}`);
  } catch (err) {
    announce(err.message || "Could not forget speaker");
  }
  renderSpeakers();
  notifyChange();
}

export function renderSpeakers() {
  const list = $("deviceList");
  if (!list) return;
  list.replaceChildren();

  if (isAuthFailed()) {
    const row = document.createElement("div");
    row.className = "error-row";
    row.textContent = "Dashboard access is required. Request approval above.";
    list.appendChild(row);
    return;
  }

  if (!speakers.length) {
    const row = document.createElement("div");
    row.className = "empty-row";
    row.textContent = "No speakers discovered yet. Try Refresh speakers.";
    list.appendChild(row);
    return;
  }

  speakers.forEach((d) => {
    const kind = deviceKind(d);
    const row = document.createElement("div");
    row.className = "device" + (d.enabled ? "" : " is-disabled");

    // Icon
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("class", "icon device-icon");
    icon.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", iconForKind(kind));
    icon.appendChild(use);

    // Info block
    const text = document.createElement("div");
    text.className = "device-info-block";

    // 1. Physical speaker name
    const physicalRow = document.createElement("div");
    physicalRow.className = "device-physical-row";
    const physicalName = document.createElement("span");
    physicalName.className = "device-name";
    physicalName.textContent = roomLabel(d);

    const modelSpan = document.createElement("span");
    modelSpan.className = "device-model";
    modelSpan.textContent = ` (${d.model || "Google Cast"})`;
    physicalRow.append(physicalName, modelSpan);

    // 2. Status badges: offline status & sync status
    const statusRow = document.createElement("div");
    statusRow.className = "device-status-row";
    statusRow.style.display = "flex";
    statusRow.style.alignItems = "center";
    statusRow.style.gap = "0.5rem";
    statusRow.style.margin = "4px 0";

    const isOffline = d.availability === "offline";
    const availBadge = document.createElement("span");
    availBadge.className = `status-badge ${isOffline ? "badge-offline" : "badge-online"}`;
    availBadge.textContent = isOffline ? "Offline" : "Online";

    const syncBadge = document.createElement("span");
    syncBadge.className = `status-badge ${d.google_home_visible ? "badge-synced" : "badge-hidden"}`;
    syncBadge.textContent = d.google_home_visible ? "Synced to Google Home" : "Hidden from Google Home";

    statusRow.append(availBadge, syncBadge);

    // 3. Virtual Google Home device name with explicit Save/Cancel renaming
    const ghLabel = d.google_home_name || `${roomLabel(d)} Plex`;
    const renameBlock = document.createElement("div");
    renameBlock.className = "device-rename-block";
    renameBlock.style.margin = "6px 0";

    const virtualLabel = document.createElement("span");
    virtualLabel.className = "device-desc";
    virtualLabel.textContent = `Virtual device name: `;

    const renameInput = document.createElement("input");
    renameInput.className = "field-select speaker-rename-input";
    renameInput.value = ghLabel;
    renameInput.setAttribute("aria-label", `Google Home name for ${d.name}`);
    renameInput.placeholder = "e.g. Kitchen Plex";
    renameInput.style.maxWidth = "220px";
    renameInput.style.marginRight = "0.4rem";

    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.className = "plain-button save-rename-btn";
    saveBtn.textContent = "Save";
    saveBtn.title = "Save virtual device name";

    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "ghost-button cancel-rename-btn";
    cancelBtn.textContent = "Cancel";
    cancelBtn.title = "Cancel renaming";

    const doSave = () => {
      const val = renameInput.value.trim();
      if (val && val !== d.google_home_name) {
        patchSpeaker(d, { google_home_name: val }, renameInput);
      }
    };
    const doCancel = () => {
      renameInput.value = d.google_home_name || `${roomLabel(d)} Plex`;
    };

    saveBtn.addEventListener("click", doSave);
    cancelBtn.addEventListener("click", doCancel);
    renameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        doSave();
      } else if (e.key === "Escape") {
        e.preventDefault();
        doCancel();
      }
    });
    renameInput.addEventListener("change", doSave);

    const renameActions = document.createElement("span");
    renameActions.style.display = "inline-flex";
    renameActions.style.gap = "0.3rem";
    renameActions.style.verticalAlign = "middle";
    renameActions.append(saveBtn, cancelBtn);

    renameBlock.append(virtualLabel, renameInput, renameActions);

    if (d.needs_review) {
      const warning = document.createElement("div");
      warning.className = "device-desc";
      warning.style.color = "#d97706";
      warning.textContent =
        "Identity conflict: check Details, then choose which virtual devices to show in Google Home.";
      text.appendChild(warning);
    }

    // 4. Details container (UUID, IP/host, Google Home ID, last seen)
    const meta = document.createElement("div");
    meta.className = "device-meta";
    meta.hidden = true;
    const bits = [];
    if (d.host) bits.push(`IP / Host: ${d.host}${d.port ? ":" + d.port : ""}`);
    if (d.uuid) bits.push(`Cast UUID: ${d.uuid}`);
    if (d.google_home_device_id || d.id) bits.push(`Virtual ID: ${d.google_home_device_id || d.id}`);
    if (d.last_seen) bits.push(`Last seen: ${d.last_seen}`);
    meta.textContent = bits.length ? bits.join(" · ") : "No technical details available.";

    const toggleMeta = document.createElement("button");
    toggleMeta.type = "button";
    toggleMeta.className = "device-toggle-meta";
    toggleMeta.textContent = "Details";
    toggleMeta.setAttribute("aria-expanded", "false");
    toggleMeta.addEventListener("click", () => {
      const open = meta.hidden;
      meta.hidden = !open;
      toggleMeta.textContent = open ? "Hide details" : "Details";
      toggleMeta.setAttribute("aria-expanded", open ? "true" : "false");
    });

    text.append(physicalRow, statusRow, renameBlock, toggleMeta, meta);

    // Actions block: Playback toggle, Google Home toggle, Forget button
    const actions = document.createElement("div");
    actions.className = "device-actions";
    actions.style.display = "flex";
    actions.style.flexDirection = "column";
    actions.style.gap = "0.35rem";
    actions.style.alignItems = "flex-end";

    // Playback switch
    const playLabel = document.createElement("label");
    playLabel.className = "switch";
    playLabel.title = "Allow playback";
    const playInput = document.createElement("input");
    playInput.type = "checkbox";
    playInput.setAttribute("role", "switch");
    playInput.checked = Boolean(d.enabled);
    playInput.setAttribute("aria-label", `Allow playback on ${d.name}`);
    playInput.addEventListener("change", () =>
      patchSpeaker(d, { enabled: playInput.checked }, playInput)
    );
    playLabel.appendChild(playInput);
    const playHint = document.createElement("span");
    playHint.className = "device-desc";
    playHint.textContent = "Allow playback";
    const playRow = document.createElement("div");
    playRow.style.display = "flex";
    playRow.style.alignItems = "center";
    playRow.style.gap = "0.4rem";
    playRow.append(playHint, playLabel);

    // Google Home switch
    const ghLabelEl = document.createElement("label");
    ghLabelEl.className = "switch";
    ghLabelEl.title = "Show in Google Home";
    const ghInput = document.createElement("input");
    ghInput.type = "checkbox";
    ghInput.setAttribute("role", "switch");
    ghInput.checked = Boolean(d.google_home_visible);
    ghInput.setAttribute("aria-label", `Show ${ghLabel} in Google Home`);
    ghInput.addEventListener("change", () =>
      patchSpeaker(d, { google_home_visible: ghInput.checked }, ghInput)
    );
    ghLabelEl.appendChild(ghInput);
    const ghHint = document.createElement("span");
    ghHint.className = "device-desc";
    ghHint.textContent = "Google Home";
    const ghRow = document.createElement("div");
    ghRow.style.display = "flex";
    ghRow.style.alignItems = "center";
    ghRow.style.gap = "0.4rem";
    ghRow.append(ghHint, ghLabelEl);

    // Forget button
    const forgetBtn = document.createElement("button");
    forgetBtn.type = "button";
    forgetBtn.className = "ghost-button";
    forgetBtn.textContent = "Forget";
    forgetBtn.title = "Remove virtual Plex device from Google Home on next sync";
    forgetBtn.addEventListener("click", () => forgetSpeaker(d));

    actions.append(playRow, ghRow, forgetBtn);
    row.append(icon, text, actions);
    list.appendChild(row);
  });
}

export function initSpeakersUi() {
  $("scanButton")?.addEventListener("click", () => loadSpeakers(true));
}
