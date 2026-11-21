// Utility functions for Plex Cast Bridge dashboard

export const $ = (id) => document.getElementById(id);
export const $$ = (sel) => document.querySelectorAll(sel);

export async function readJson(res) {
  try {
    return await res.json();
  } catch (_) {
    return null;
  }
}

export function errorDetail(data, fallback) {
  if (!data) return fallback;
  if (typeof data === "string") return data;
  if (data.detail) {
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail)) {
      return data.detail.map((e) => e.msg || e.message || JSON.stringify(e)).join("; ");
    }
    return JSON.stringify(data.detail);
  }
  if (data.message) return String(data.message);
  return fallback;
}

export function announce(message) {
  const box = $("feedback");
  if (!box) return;
  box.textContent = message;
  box.classList.add("visible");
  clearTimeout(box._timer);
  box._timer = setTimeout(() => {
    box.classList.remove("visible");
  }, 4000);
}

export async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) {}
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand("copy");
    document.body.removeChild(ta);
    return true;
  } catch (_) {
    document.body.removeChild(ta);
    return false;
  }
}

export function setConnection(ok, label) {
  const el = $("connectionStatus");
  const lbl = $("connectionLabel");
  if (!el || !lbl) return;
  el.classList.remove("ok", "err");
  if (ok === true) el.classList.add("ok");
  else if (ok === false) el.classList.add("err");
  lbl.textContent = label;
}

export function deviceKind(spk) {
  if (!spk) return "speaker";
  if (spk.is_group || (spk.kind || "").toLowerCase() === "group") return "group";
  const model = (spk.model || "").toLowerCase();
  if (model.includes("hub") || model.includes("display") || model.includes("screen")) return "display";
  return "speaker";
}

export function iconForKind(kind) {
  if (kind === "display") return "#display-icon";
  if (kind === "group") return "#group-icon";
  return "#speaker-icon";
}

export function roomLabel(spk) {
  if (!spk) return "Unknown";
  return spk.room || spk.name || spk.speaker_name || "Unknown";
}

export function isPlayingState(state) {
  return ["PLAYING", "BUFFERING"].includes(String(state || "").toUpperCase());
}

export function pctFromUnit(v) {
  return Math.round(Math.max(0, Math.min(1, Number(v) || 0)) * 100);
}

export function unitFromPct(raw) {
  return Math.max(0, Math.min(1, (Number(raw) || 0) / 100));
}
