export function initMusicIndex({ $, api, announce }) {
  let timer = null;
  async function refreshMusicIndex() {
    try {
      const data = await api('/api/music-index');
      $('indexEnabled').checked = data.settings.indexing_enabled;
      $('indexSchedule').value = data.settings.schedule;
      const scan = data.scan;
      $('indexStatus').textContent = `Status: ${scan.status} · scanned ${scan.scanned} · last successful scan: ${scan.completed_at || 'never'}${scan.error ? ' · ' + scan.error : ''}`;
      const counts = data.counts, quality = data.quality;
      $('indexQuality').textContent = `${counts.tracks} tracks · ${counts.artists} artists · ${counts.albums} albums. Artist and album: ${quality.tracks_with_artist_and_album}/${counts.tracks} tracks; genres: ${quality.tracks_with_genres}/${counts.tracks}; moods: ${quality.tracks_with_moods}/${counts.tracks}; albums with year: ${quality.albums_with_year}/${counts.albums}.`;
      clearTimeout(timer);
      if (scan.status === 'running') timer = setTimeout(refreshMusicIndex, 2000);
    } catch (err) { $('indexStatus').textContent = err.message || 'Index status unavailable'; }
  }
  $('indexSave').addEventListener('click', async () => {
    try {
      await api('/api/music-index/settings', {method: 'PATCH', body: JSON.stringify({indexing_enabled: $('indexEnabled').checked, schedule: $('indexSchedule').value})});
      announce('Index settings saved');
      await refreshMusicIndex();
    } catch (err) { announce(err.message || 'Could not save settings'); }
  });
  $('indexStart').addEventListener('click', async () => {
    try { await api('/api/music-index/scan', {method: 'POST'}); await refreshMusicIndex(); }
    catch (err) { announce(err.message || 'Could not start scan'); }
  });
  $('indexCancel').addEventListener('click', async () => {
    try { await api('/api/music-index/cancel', {method: 'POST'}); await refreshMusicIndex(); }
    catch (err) { announce(err.message || 'Could not cancel scan'); }
  });
  return { refreshMusicIndex };
}
