const $ = (id) => document.getElementById(id);

// One-time upgrade of previously enrolled browsers. Never persist a new API key.
export const migrationReady = (async () => {
    const oldKey = localStorage.getItem('plex_cast_api_key');
    if (!oldKey) return;
    try {
      const response = await fetch('/api/session/migrate', {method: 'POST', headers: {'X-API-Key': oldKey}});
      if (response.ok) localStorage.removeItem('plex_cast_api_key');
    } catch (_) { /* Retry on a later page load. */ }
  })();

export function initDashboardSession({ api, announce }) {
  $('dashboardHttpWarning').hidden = window.location.protocol === 'https:';
  $('dashboardSessionWarning').hidden = window.location.protocol === 'https:';
  let dashboardClaimTimer = null;
  let dashboardExpiryTimer = null;
  async function refreshDashboardRequests() {
    try {
      const response = await fetch('/api/session/pending');
      if (!response.ok) return;
      const data = await response.json();
      const target = $('dashboardPendingRequests');
      target.replaceChildren();
      for (const item of data.requests || []) {
        const row = document.createElement('div');
        const label = document.createElement('span');
        label.textContent = `Code ${item.code} `;
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'ghost-button'; button.textContent = 'Approve';
        button.addEventListener('click', async () => {
          if (!window.confirm(`Does ${item.code} match the code on the new browser?`)) return;
          try {
            await api('/api/session/approve', {method:'POST', body:JSON.stringify({code:item.code})});
            announce('Browser approved');
            await refreshDashboardRequests();
          } catch (err) { announce(err.message || 'Approval failed'); }
        });
        row.append(label, button); target.appendChild(row);
      }
      if (!target.childNodes.length) target.textContent = 'No access requests waiting.';
    } catch (_) { /* The dashboard may not be authenticated yet. */ }
  }
  $('dashboardRequestAccess').addEventListener('click', async () => {
    try {
      const response = await fetch('/api/session/request', {method:'POST'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not request access');
      $('dashboardAccessStatus').textContent = `Approval code: ${data.code}. This request expires in five minutes.`;
      clearInterval(dashboardClaimTimer);
      clearTimeout(dashboardExpiryTimer);
      dashboardClaimTimer = setInterval(async () => {
        try {
          const result = await fetch('/api/session/claim', {method:'POST'});
          if (result.ok && (await result.json()).status === 'authenticated') {
            clearInterval(dashboardClaimTimer); window.location.reload();
          }
        } catch (_) { /* Keep polling until approval or expiry. */ }
      }, 2000);
      dashboardExpiryTimer = setTimeout(() => {
        clearInterval(dashboardClaimTimer);
        $('dashboardAccessStatus').textContent = 'Request expired. Request access again if needed.';
      }, data.expires_in * 1000);
    } catch (err) { $('dashboardAccessStatus').textContent = err.message; }
  });
  $('dashboardRefreshRequests').addEventListener('click', refreshDashboardRequests);
  $('dashboardRevokeOthers').addEventListener('click', async () => {
    if (!window.confirm('Sign out all other dashboard browsers?')) return;
    try { await api('/api/session/revoke-others', {method:'POST'}); announce('Other browsers signed out'); }
    catch (err) { announce(err.message); }
  });
  $('dashboardLogout').addEventListener('click', async () => {
    try { await api('/api/session/logout', {method:'POST'}); window.location.reload(); }
    catch (err) { announce(err.message); }
  });
  setInterval(refreshDashboardRequests, 10000);





}
