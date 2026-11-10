export async function readJson(res) {
    const text = await res.text();
    if (!text) {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return null;
    }
    try {
      return JSON.parse(text);
    } catch {
      const snippet = text.trim().replace(/\s+/g, ' ').slice(0, 160);
      throw new Error(snippet || `HTTP ${res.status}`);
    }
  }

export function errorDetail(data, fallback) {
    if (!data) return fallback;
    const detail = data.detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      return detail.map((d) => d.msg || JSON.stringify(d)).join('; ') || fallback;
    }
    return fallback;
  }

export function deviceKind(spk) {
    const model = String(spk.model || '').toLowerCase();
    if (model.includes('group')) return 'group';
    if (model.includes('hub') || model.includes('display') || String(spk.name || '').toLowerCase().includes('display')) {
      return 'display';
    }
    return 'speaker';
  }

export function iconForKind(kind) {
    if (kind === 'display') return '#display-icon';
    if (kind === 'group') return '#group-icon';
    return '#speaker-icon';
  }

export function roomLabel(spk) {
    if (!spk) return 'No output';
    return spk.room || spk.name || 'Speaker';
  }

export function isPlayingState(state) {
    const s = String(state || '').toUpperCase();
    return s === 'PLAYING' || s === 'BUFFERING';
  }

export function pctFromUnit(v) {
    if (v == null || Number.isNaN(Number(v))) return '';
    return String(Math.round(Number(v) * 100));
  }

export function unitFromPct(raw) {
    const n = Number(raw);
    if (Number.isNaN(n)) return null;
    return Math.max(0, Math.min(100, n)) / 100;
  }
