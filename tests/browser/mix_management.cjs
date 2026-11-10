const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const root = path.resolve(__dirname, '../../src/plex_cast_bridge');
(async () => {
  const browser = await chromium.launch({headless:true, ...(process.env.BROWSER_EXECUTABLE ? {executablePath:process.env.BROWSER_EXECUTABLE} : {})});
  for (const width of [1440, 390]) {
    const page = await browser.newPage({viewport:{width,height:900}});
    const errors=[], calls=[];
    let saved=null;
    const tracks=[{rating_key:'1',title:'First',artist:'Artist',album:'Album',duration_ms:120000},{rating_key:'2',title:'Second',artist:'Artist',album:'Album',duration_ms:180000}];
    page.on('pageerror', err => errors.push(err.message));
    page.on('dialog', dialog => dialog.accept());
    await page.route('**/*', async route => {
      const req=route.request(), p=new URL(req.url()).pathname, method=req.method();
      if (p === '/') return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(root,'templates/dashboard.html'))});
      if (p.startsWith('/static/')) return route.fulfill({contentType:p.endsWith('.css')?'text/css':'text/javascript',body:fs.readFileSync(path.join(root,p.replace('/static/','static/')))});
      let data={};
      if (p==='/api/speakers') data=[{name:'Kitchen',enabled:true}];
      else if (p==='/api/mixes/preview') {calls.push('preview'); data={tracks,warning:null};}
      else if (p==='/api/mixes' && method==='GET') data={mixes:saved?[{id:1,name:saved.name,track_count:saved.tracks.length}]:[]};
      else if (p==='/api/mixes' && method==='POST') {const body=req.postDataJSON();calls.push('save');saved={id:1,name:body.name,tracks:body.track_ids.map(id=>tracks.find(t=>t.rating_key===id))};data=saved;}
      else if (p==='/api/mixes/play-preview') {calls.push('play-preview');data={missing_track_ids:[]};}
      else if (p==='/api/mixes/1' && method==='GET') data=saved;
      else if (p==='/api/mixes/1' && method==='PATCH') {const body=req.postDataJSON();calls.push('edit');saved={id:1,name:body.name,tracks:body.track_ids.map(id=>tracks.find(t=>t.rating_key===id))};data=saved;}
      else if (p==='/api/mixes/1' && method==='DELETE') {calls.push('delete');saved=null;data={status:'deleted'};}
      else if (p==='/api/mixes/1/play') {calls.push('play-saved');data={missing_track_ids:[]};}
      return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
    });
    await page.goto('http://bridge.test/#dj');
    await page.locator('#mixMode').selectOption('genre');
    await page.locator('#mixPrompt').fill('jazz');
    await page.getByRole('button',{name:'Generate preview'}).focus();
    await page.keyboard.press('Enter');
    await page.getByText('2 tracks · 5 min').waitFor();
    if (calls.some(c=>c.startsWith('play'))) throw Error('generation started playback');
    await page.getByRole('button',{name:'Move up: Second'}).click();
    if (!(await page.locator('#mixPreviewTracks li').first().innerText()).startsWith('Second')) throw Error('reorder failed');
    await page.locator('#mixPlay').click();
    await page.locator('#mixName').fill('Jazz mix');
    await page.locator('#mixSave').click();
    await page.getByRole('button',{name:'Open Jazz mix'}).waitFor();
    await page.getByRole('button',{name:'Open Jazz mix'}).click();
    await page.getByRole('button',{name:'Remove: First'}).click();
    await page.locator('#mixName').fill('Quiet mix');
    await page.getByRole('button',{name:'Save changes'}).click();
    await page.getByRole('button',{name:'Open Quiet mix'}).waitFor();
    if (saved.tracks.length!==1 || saved.tracks[0].rating_key!=='2') throw Error('saved edit failed');
    await page.locator('#mixPlay').click();
    await page.getByRole('button',{name:'Delete Quiet mix'}).click();
    await page.getByText('No saved mixes yet.').waitFor();
    if (!['preview','play-preview','save','edit','play-saved','delete'].every(c=>calls.includes(c))) throw Error(`Missing operation: ${calls}`);
    if (errors.length) throw Error(errors.join('; '));
    const dims=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));
    if (dims.scroll>dims.width) throw Error('horizontal overflow');
    console.log(`Mix management passed at ${width}px`);
    await page.close();
  }
  await browser.close();
})().catch(err=>{console.error(err);process.exit(1)});
