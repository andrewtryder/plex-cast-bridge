// Smart DJ form, query resolution, and recents management

import { api } from "./api.js";
import { refreshStatus } from "./player.js";
import { getSelectedSpeaker } from "./speakers.js";
import { $, announce } from "./util.js";

const RECENTS_KEY = "plex_cast_dj_recents";

export function buildDjQuery(raw, mode) {
  const q = raw.trim();
  if (mode === "artist") return `${q} artist only`;
  if (mode === "radio") return `${q} radio`;
  return q;
}

export function loadRecents() {
  try {
    const list = JSON.parse(localStorage.getItem(RECENTS_KEY) || "[]");
    return Array.isArray(list) ? list : [];
  } catch (_) {
    return [];
  }
}

export function saveRecent(query) {
  const trimmed = query.trim();
  if (!trimmed) return;
  const current = loadRecents().filter((item) => item.toLowerCase() !== trimmed.toLowerCase());
  current.unshift(trimmed);
  try {
    localStorage.setItem(RECENTS_KEY, JSON.stringify(current.slice(0, 10)));
  } catch (_) {}
  renderRecents();
}

export function renderRecents() {
  const list = $("recentList");
  if (!list) return;
  list.replaceChildren();
  const recents = loadRecents();
  if (!recents.length) {
    const p = document.createElement("p");
    p.className = "conn-meta";
    p.textContent = "No recent requests yet.";
    list.appendChild(p);
    return;
  }
  recents.forEach((query) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ghost-button";
    btn.style.margin = "0 6px 6px 0";
    btn.textContent = query;
    btn.addEventListener("click", () => {
      const prompt = $("promptText");
      if (prompt) {
        prompt.value = query;
        prompt.focus();
      }
    });
    list.appendChild(btn);
  });
}

export async function sendDjCommand(query) {
  const selected = getSelectedSpeaker();
  if (!selected) {
    announce("Select a speaker before sending DJ requests");
    return;
  }
  const submitBtn = $("djSubmit");
  const label = $("djSubmitLabel");
  if (submitBtn) submitBtn.disabled = true;
  if (label) label.textContent = "Asking Smart DJ…";
  try {
    const res = await api("/api/command", {
      method: "POST",
      body: JSON.stringify({ query, speaker: selected }),
    });
    announce(res.message || "Smart DJ loaded tracks");
    saveRecent(query);
    location.hash = "#player";
    await refreshStatus();
  } catch (err) {
    announce(err.message || "Smart DJ request failed");
  } finally {
    if (submitBtn) submitBtn.disabled = false;
    if (label) label.textContent = "Play music";
  }
}

export function initDjUi() {
  const form = $("djForm");
  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const prompt = $("promptText");
      if (!prompt || !prompt.value.trim()) return;
      const mode = form.querySelector("input[name=djMode]:checked")?.value || "artist";
      const q = buildDjQuery(prompt.value, mode);
      await sendDjCommand(q);
    });
  }

  const modeRadios = document.querySelectorAll("input[name=djMode]");
  modeRadios.forEach((radio) => {
    radio.addEventListener("change", (e) => {
      const explainer = $("modeExplainer");
      if (explainer) {
        explainer.textContent =
          e.target.value === "radio" ? "Artist + similar tracks" : "100% that artist";
      }
    });
  });

  renderRecents();
}
