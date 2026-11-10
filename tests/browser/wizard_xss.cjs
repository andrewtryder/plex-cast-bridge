/* Run with Playwright installed: node tests/browser/wizard_xss.cjs */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const root = path.resolve(__dirname, '../..');

(async () => {
  const browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_EXECUTABLE ? { executablePath: process.env.BROWSER_EXECUTABLE } : {}) });
  for (const width of [1440, 390]) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    let step = 'cloud_auth';
    const payload = '\"><img src=x onerror="window.injected=true">';
    await page.route('**/*', async route => {
      const p = new URL(route.request().url()).pathname;
      if (p === '/') return route.fulfill({ contentType: 'text/html', body: fs.readFileSync(path.join(root, 'src/plex_cast_bridge/templates/dashboard.html')) });
      if (p.startsWith('/static/dashboard/')) return route.fulfill({contentType:'text/javascript', body:fs.readFileSync(path.join(root,'src/plex_cast_bridge/static/dashboard',p.split('/').pop()))});
      if (p === '/static/css/dashboard.css') return route.fulfill({ contentType: 'text/css', body: fs.readFileSync(path.join(root, 'src/plex_cast_bridge/static/css/dashboard.css')) });
      const body = p === '/api/google-home/status' ? { setup: { current_step: step, project_id: payload, billing_url: 'javascript:alert(1)', provision_progress: { test: payload } } } : p === '/api/speakers' ? [] : {};
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    });
    await page.goto('http://bridge.test/#settings');
    await page.locator('#ghSetupBtn').click();
    await page.locator('#ghProjectId').waitFor();
    if (await page.locator('#ghProjectId').inputValue() !== payload) throw Error('project ID was not preserved as data');
    if (await page.evaluate(() => !!window.injected) || await page.locator('#ghWizardFields img').count()) throw Error('project ID became HTML');
    step = 'prepare_services';
    await page.locator('#ghSetupBtn').click();
    await page.locator('#ghProjectSummary').waitFor();
    if (await page.locator('#ghProjectSummary').textContent() !== payload) throw Error('project summary changed');
    const href = await page.locator('#ghWizardFields a').first().getAttribute('href');
    if (!href.startsWith('https://console.cloud.google.com/')) throw Error('unsafe billing URL');
    if (await page.evaluate(() => !!window.injected) || await page.locator('#ghWizardFields img').count()) throw Error('setup data became HTML');
    await page.close();
    console.log(`Wizard handles untrusted values safely at ${width}px`);
  }
  await browser.close();
})().catch(err => { console.error(err); process.exit(1); });
