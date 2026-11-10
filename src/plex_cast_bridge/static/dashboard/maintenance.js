export function initMaintenance({ $, api, announce, authHeaders }) {
  async function refreshDiagnostics() {
    try {
      const data = await api('/api/diagnostics');
      const list = $('diagList');
      if (!list) return;
      list.replaceChildren();
      (data.checks || []).forEach((c) => {
        const row = document.createElement('div');
        row.className = 'diag-row';
        const st = c.status === 'connected' ? 'Connected' : (c.status === 'needs_attention' ? 'Needs attention' : 'Not configured');
        const detail = document.createElement('span');
        const small = document.createElement('small');
        small.textContent = c.detail || '';
        detail.append(document.createTextNode(c.label || ''), document.createElement('br'), small);
        const state = document.createElement('span');
        state.className = c.status === 'connected' ? 'diag-ok' : 'diag-bad';
        state.textContent = st;
        row.append(detail, state);
        list.appendChild(row);
      });
      if ($('diagTechnical')) $('diagTechnical').textContent = JSON.stringify(data.technical || {}, null, 2);
      await refreshActivity();
    } catch (err) {
      announce(err.message || 'Diagnostics failed');
    }
  }
  async function refreshActivity() {
    try {
      const data = await api('/api/activity');
      $('activityList').textContent = (data.events || []).map((event) =>
        `${new Date(event.time).toLocaleTimeString()} · ${event.source} · ${event.event}${event.speaker ? ` · ${event.speaker}` : ''}`
      ).join('\n') || 'No activity observed since the bridge started.';
    } catch (_) {
      $('activityList').textContent = 'Could not load activity.';
    }
  }
  $('activityRefreshBtn')?.addEventListener('click', refreshActivity);
  $('diagRefreshBtn')?.addEventListener('click', refreshDiagnostics);
  $('diagTestBtn')?.addEventListener('click', async () => {
    try {
      const test = await api('/api/google-home/test', { method: 'POST', body: '{}' });
      announce(test.summary || 'Test complete');
      await refreshDiagnostics();
    } catch (err) {
      announce(err.message || 'Test failed');
    }
  });
  $('backupDownloadBtn')?.addEventListener('click', async (e) => {
    e.preventDefault();
    try {
      const res = await fetch('/api/backup', { headers: authHeaders() });
      if (!res.ok) throw new Error('Backup failed');
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'plex-cast-bridge-backup.zip';
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      announce(err.message || 'Backup failed');
    }
  });
  let backupFile = null;
  $('backupPreviewBtn')?.addEventListener('click', async () => {
    try {
      const input = $('backupRestoreFile');
      if (!input?.files?.length) { announce('Choose a backup zip'); return; }
      backupFile = input.files[0];
      const fd = new FormData();
      fd.append('file', backupFile);
      const res = await fetch('/api/backup/restore/preview', { method: 'POST', body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Preview failed');
      $('backupRestorePreview').textContent = JSON.stringify(data, null, 2);
      $('backupRestoreBtn').disabled = false;
    } catch (err) {
      announce(err.message || 'Preview failed');
    }
  });
  $('backupRestoreBtn')?.addEventListener('click', async () => {
    try {
      if (!backupFile) return;
      const fd = new FormData();
      fd.append('file', backupFile);
      fd.append('confirm', 'true');
      const res = await fetch('/api/backup/restore', { method: 'POST', body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Restore failed');
      announce('Restored: ' + (data.restored || []).join(', '));
    } catch (err) {
      announce(err.message || 'Restore failed');
    }
  });

  return { refreshDiagnostics };
}
