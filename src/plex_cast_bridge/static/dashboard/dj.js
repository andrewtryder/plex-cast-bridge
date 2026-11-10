const RECENT_KEY = 'plex_cast_recent_queries';
const MAX_RECENTS = 8;

export function createDjHelpers($) {
  function buildDjQuery(raw, mode) {
    const q = raw.trim();
    if (mode !== 'radio') return q;
    if (/\b(radio|station|mix|similar)\b/i.test(q)) return q;
    // Leave decade/genre/mood prompts free-form; only nudge artist-like prompts.
    if (/\d0s\b/i.test(q) || /\b(like|mood|genre|trip-?hop|jazz|rock|soul|funk|ambient|electronic|classical|punk|metal|country|folk|indie|hip-?hop|mellow|chill)\b/i.test(q)) {
      return q;
    }
    return `${q} radio`;
  }

  function loadRecents() {
    try {
      const raw = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]');
      return Array.isArray(raw) ? raw.filter((x) => typeof x === 'string' && x.trim()).slice(0, MAX_RECENTS) : [];
    } catch {
      return [];
    }
  }

  function saveRecent(query) {
    const q = query.trim();
    if (!q) return;
    const next = [q, ...loadRecents().filter((x) => x.toLowerCase() !== q.toLowerCase())].slice(0, MAX_RECENTS);
    localStorage.setItem(RECENT_KEY, JSON.stringify(next));
    renderRecents();
  }

  function renderRecents() {
    const list = $('recentList');
    const items = loadRecents();
    list.replaceChildren();
    $('recentsHint').textContent = items.length ? 'Tap to reuse' : 'Saved after you play something';
    if (!items.length) {
      const empty = document.createElement('p');
      empty.className = 'empty-row';
      empty.style.borderBottom = '0';
      empty.textContent = 'No recent requests yet.';
      list.appendChild(empty);
      return;
    }
    items.forEach((q) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'recent';
      btn.setAttribute('aria-label', `Use recent request: ${q}`);
      const span = document.createElement('span');
      span.textContent = q;
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('class', 'icon sm');
      svg.setAttribute('aria-hidden', 'true');
      const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
      use.setAttribute('href', '#arrow-icon');
      svg.appendChild(use);
      btn.append(span, svg);
      btn.addEventListener('click', () => {
        $('promptText').value = q;
        $('promptText').focus();
      });
      list.appendChild(btn);
    });
  }

  return { buildDjQuery, saveRecent, renderRecents };
}
