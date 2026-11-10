/* Run with Playwright installed: node tests/browser/password_free_pairing.cjs */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const root = path.resolve(__dirname, '../..');
(async () => {
  const browser = await chromium.launch({headless:true, ...(process.env.BROWSER_EXECUTABLE ? {executablePath:process.env.BROWSER_EXECUTABLE} : {})});
  for (const width of [1440, 390]) {
    const context = await browser.newContext({viewport:{width,height:900}});
    const page = await context.newPage();
    let approved = false;
    const ticket = 't'.repeat(43), code = '123456';
    await page.route('**/*', async route => {
      const req = route.request(), p = new URL(req.url()).pathname;
      if (p === '/') return route.fulfill({contentType:'text/html', body:fs.readFileSync(path.join(root,'src/plex_cast_bridge/templates/dashboard.html'))});
      if (p.startsWith('/static/dashboard/')) return route.fulfill({contentType:'text/javascript', body:fs.readFileSync(path.join(root,'src/plex_cast_bridge/static/dashboard',p.split('/').pop()))});
      if (p === '/static/css/dashboard.css') return route.fulfill({contentType:'text/css', body:fs.readFileSync(path.join(root,'src/plex_cast_bridge/static/css/dashboard.css'))});
      let body = {};
      if (p === '/api/speakers') body = [];
      if (p === '/api/google-home/pairing') body = {pending: approved ? null : {ticket,code}};
      if (p === '/api/google-home/pairing/approve') {
        const sent = req.postDataJSON();
        if (sent.ticket !== ticket || sent.code !== code) throw Error('incorrect approval payload');
        approved = true; body = {status:'approved'};
      }
      return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
    });
    page.on('dialog', dialog => dialog.accept());
    await page.goto('http://bridge.test/#settings');
    await page.locator('#ghPairingCard').waitFor({state:'visible'});
    if ((await page.locator('#ghPairingCode').innerText()) !== code) throw Error('code not shown');
    await page.locator('#ghPairingApprove').click();
    await page.getByText('Approved. Return to Google Home to finish linking.').waitFor();
    if (!approved) throw Error('approval not sent');
    await page.screenshot({path:`/tmp/plex-password-free-${width}.png`,fullPage:true});
    console.log(`Password-free dashboard approval passed at ${width}px`);
    await context.close();
  }
  await browser.close();
})().catch(err => {console.error(err);process.exit(1)});
