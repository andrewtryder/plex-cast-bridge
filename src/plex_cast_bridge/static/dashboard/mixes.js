export function initMixes({ $, api, announce, getSpeakers, getSelected }) {
  let tracks = [], editingId = null, lastRequest = null;
  function speakerOptions() {
    const select = $('mixSpeaker'), previous = select.value || getSelected();
    select.replaceChildren();
    const enabled = getSpeakers().filter(s => s.enabled);
    for (const speaker of enabled) select.add(new Option(speaker.name, speaker.name));
    if (enabled.some(s => s.name === previous)) select.value = previous;
    if (!enabled.length) select.add(new Option('No speakers enabled', ''));
  }
  function renderPreview() {
    $('mixPreview').hidden = false;
    const total = tracks.reduce((sum, track) => sum + (track.duration_ms || 0), 0);
    $('mixPreviewSummary').textContent = `${tracks.length} tracks · ${Math.round(total / 60000)} min`;
    const list = $('mixPreviewTracks');
    list.replaceChildren();
    tracks.forEach((track, index) => {
      const li = document.createElement('li');
      const label = document.createElement('span');
      label.textContent = track.missing ? `Unavailable track ${track.rating_key}` : `${track.title} — ${track.artist || 'Unknown artist'}${track.album ? ' · ' + track.album : ''}`;
      li.appendChild(label);
      for (const [title, offset] of [['Move up', -1], ['Move down', 1], ['Remove', 0]]) {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'ghost-button'; button.textContent = title;
        button.setAttribute('aria-label', `${title}: ${track.title || track.rating_key}`);
        button.disabled = offset === -1 ? index === 0 : offset === 1 ? index === tracks.length - 1 : false;
        button.addEventListener('click', () => {
          if (!offset) tracks.splice(index, 1);
          else [tracks[index], tracks[index + offset]] = [tracks[index + offset], tracks[index]];
          renderPreview();
        });
        li.appendChild(button);
      }
      list.appendChild(li);
    });
    $('mixSave').textContent = editingId ? 'Save changes' : 'Save mix';
    speakerOptions();
  }
  async function refreshSavedMixes() {
    const list = $('savedMixes');
    try {
      const data = await api('/api/mixes');
      list.replaceChildren();
      if (!data.mixes.length) { list.textContent = 'No saved mixes yet.'; return; }
      for (const mix of data.mixes) {
        const row = document.createElement('div'), label = document.createElement('span');
        label.textContent = `${mix.name} · ${mix.track_count} tracks`;
        row.appendChild(label);
        const edit = document.createElement('button');
        edit.type = 'button'; edit.className = 'ghost-button'; edit.textContent = 'Open';
        edit.setAttribute('aria-label', `Open ${mix.name}`);
        edit.addEventListener('click', async () => {
          try {
            const data = await api(`/api/mixes/${mix.id}`);
            editingId = mix.id; tracks = data.tracks; $('mixName').value = data.name;
            lastRequest = null; $('mixFeedback').textContent = ''; renderPreview();
            $('mixPreview').scrollIntoView({behavior: 'smooth'});
          } catch (err) { announce(err.message || 'Could not open mix'); }
        });
        const remove = document.createElement('button');
        remove.type = 'button'; remove.className = 'ghost-button'; remove.textContent = 'Delete';
        remove.setAttribute('aria-label', `Delete ${mix.name}`);
        remove.addEventListener('click', async () => {
          if (!window.confirm(`Delete the saved mix “${mix.name}”?`)) return;
          try { await api(`/api/mixes/${mix.id}`, {method: 'DELETE'}); if (editingId === mix.id) { editingId = null; $('mixPreview').hidden = true; } await refreshSavedMixes(); }
          catch (err) { announce(err.message || 'Could not delete mix'); }
        });
        row.append(edit, remove); list.appendChild(row);
      }
    } catch (err) { list.textContent = err.message || 'Saved mixes unavailable'; }
  }
  $('mixGenerateForm').addEventListener('submit', async event => {
    event.preventDefault();
    const body = {mode: $('mixMode').value, prompt: $('mixPrompt').value.trim(), use_ai: $('mixUseAi').checked};
    try {
      const result = await api('/api/mixes/preview', {method: 'POST', body: JSON.stringify(body)});
      tracks = result.tracks; editingId = null; lastRequest = body; $('mixName').value = '';
      $('mixFeedback').textContent = result.warning || ''; renderPreview();
    } catch (err) { $('mixFeedback').textContent = err.message || 'Could not generate mix'; }
  });
  $('mixSave').addEventListener('click', async () => {
    try {
      const name = $('mixName').value.trim(), ids = tracks.map(t => t.rating_key);
      if (!name || !ids.length) throw new Error('Add a name and at least one track');
      if (editingId) await api(`/api/mixes/${editingId}`, {method: 'PATCH', body: JSON.stringify({name, track_ids: ids})});
      else {
        const data = await api('/api/mixes', {method: 'POST', body: JSON.stringify({name, track_ids: ids, ...lastRequest})});
        editingId = data.id;
      }
      announce('Mix saved'); await refreshSavedMixes(); renderPreview();
    } catch (err) { announce(err.message || 'Could not save mix'); }
  });
  $('mixPlay').addEventListener('click', async () => {
    try {
      const speaker = $('mixSpeaker').value;
      if (!speaker) throw new Error('Choose an enabled speaker');
      const body = editingId ? {speaker} : {speaker, track_ids: tracks.map(t => t.rating_key)};
      const url = editingId ? `/api/mixes/${editingId}/play` : '/api/mixes/play-preview';
      const data = await api(url, {method: 'POST', body: JSON.stringify(body)});
      announce(data.missing_track_ids?.length ? `Playing; ${data.missing_track_ids.length} unavailable tracks skipped` : 'Playing mix');
    } catch (err) { announce(err.message || 'Could not play mix'); }
  });
  return {refreshSavedMixes, speakerOptions};
}
