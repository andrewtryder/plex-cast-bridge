/* Start a local test server with LOCAL_API_KEY=browser-test-key, then run this file. */
const { chromium } = require('playwright');
const base = process.env.TEST_BASE_URL || 'http://127.0.0.1:18953';
(async () => {
  const browser = await chromium.launch({headless:true, ...(process.env.BROWSER_EXECUTABLE ? {executablePath:process.env.BROWSER_EXECUTABLE} : {})});
  for (const width of [1440, 390]) {
    const existing = await browser.newContext({viewport:{width,height:900}});
    await existing.addInitScript(() => localStorage.setItem('plex_cast_api_key', 'browser-test-key'));
    const oldPage = await existing.newPage();
    oldPage.on('dialog', d => d.accept());
    await oldPage.goto(base + '/#settings');
    await oldPage.waitForFunction(() => !localStorage.getItem('plex_cast_api_key'));
    const cookies = await existing.cookies();
    if (!cookies.some(c => c.name === 'pcb_session' && c.httpOnly)) throw Error('migration did not create HttpOnly session');
    const newcomer = await browser.newContext({viewport:{width,height:900}});
    const newPage = await newcomer.newPage();
    await newPage.goto(base);
    await newPage.locator('#dashboardAccess').waitFor({state:'visible'});
    await newPage.locator('#dashboardRequestAccess').click();
    await newPage.waitForFunction(() => /\d{8}/.test(document.getElementById('dashboardAccessStatus').textContent));
    const code = (await newPage.locator('#dashboardAccessStatus').innerText()).match(/\d{8}/)[0];
    await oldPage.locator('#dashboardRefreshRequests').click();
    const row = oldPage.locator('#dashboardPendingRequests > div').filter({hasText:code});
    await row.getByRole('button', {name:'Approve'}).click();
    await newPage.waitForFunction(() => document.getElementById('dashboardAccess').hidden, null, {timeout:10000});
    if (!(await newcomer.cookies()).some(c => c.name === 'pcb_session' && c.httpOnly)) throw Error('approval did not create session');
    await existing.close(); await newcomer.close();
    console.log(`Dashboard migration and browser approval passed at ${width}px`);
  }
  await browser.close();
})().catch(err => {console.error(err); process.exit(1)});
