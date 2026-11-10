const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const root = path.resolve(__dirname, '../../src/plex_cast_bridge');
(async () => {
  const browser = await chromium.launch({headless: true, ...(process.env.BROWSER_EXECUTABLE ? {executablePath: process.env.BROWSER_EXECUTABLE} : {})});
  for (const width of [1440, 390]) {
    const page = await browser.newPage({viewport: {width, height: 900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const p = new URL(route.request().url()).pathname;
      if (p === '/') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(root, 'templates/dashboard.html'))});
      if (p.startsWith('/static/')) return route.fulfill({contentType: p.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(path.join(root, p.replace('/static/', 'static/')))});
      if (p === '/api/backup/restore/preview' && !route.request().headers()['content-type']?.startsWith('multipart/form-data;')) throw Error('Backup preview must send multipart form data');
      const body = p === '/api/speakers' ? [] : {};
      return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(body)});
    });
    await page.goto('http://bridge.test/#settings');
    const names = ['connections', 'speakers', 'playback', 'diagnostics', 'backup'];
    const panels = page.locator('[data-settings-panel]');
    if (await panels.count() !== names.length) throw Error('Unexpected settings panel count');
    const nested = await page.locator('[data-settings-panel] [data-settings-panel]').count();
    if (nested) throw Error('Settings panels must not be nested');
    const controls = {connections: '#plexConnectBtn', speakers: '#scanButton', playback: '#saveVolumeSettingsBtn', diagnostics: '#diagRefreshBtn', backup: '#backupPreviewBtn'};
    for (const name of names) {
      const button = page.locator(`[data-settings-tab="${name}"]`);
      await button.focus();
      await page.keyboard.press('Enter');
      for (const other of names) {
        const visible = await page.locator(`[data-settings-panel="${other}"]`).isVisible();
        if (visible !== (other === name)) throw Error(`${name}: incorrect visibility for ${other}`);
      }
      if (!await page.locator(controls[name]).isVisible()) throw Error(`${name}: control not visible`);
      if (!await page.locator(`[data-settings-panel="${name}"] h2`).first().isVisible()) throw Error(`${name}: heading not visible`);
    }
    await page.locator('#backupRestoreFile').setInputFiles({name: 'backup.zip', mimeType: 'application/zip', buffer: Buffer.from('fixture')});
    await page.locator('#backupPreviewBtn').click();
    await page.waitForFunction(() => !document.getElementById('backupRestoreBtn').disabled);
    if (errors.length) throw Error(`JavaScript errors: ${errors.join('; ')}`);
    console.log(`Settings navigation passed at ${width}px`);
    await page.close();
  }
  await browser.close();
})().catch(error => {console.error(error); process.exit(1)});
