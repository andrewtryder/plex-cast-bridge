const {chromium}=require('playwright');
const fs=require('fs');
const root=require('path').resolve(__dirname, '../../src/plex_cast_bridge');
(async()=>{let browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE ? {executablePath:process.env.BROWSER_EXECUTABLE} : {})});
for (const width of [1440,390]) {
 let ctx=await browser.newContext({viewport:{width,height:900}}), page=await ctx.newPage();
 let dev={id:'plex-jukebox:a',google_home_device_id:'plex-jukebox:a',name:'Kitchen',google_home_name:'Kitchen Plex',model:'Nest',enabled:true,google_home_visible:false,availability:'online'};let sync='pending', actions=[]; let fail=false;
 await page.route('**/*',async route=>{let req=route.request(),url=new URL(req.url()),p=url.pathname,method=req.method();let out={};
 if(p==='/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(root+'/templates/dashboard.html')});
 if(p==='/static/css/dashboard.css')return route.fulfill({contentType:'text/css',body:fs.readFileSync(root+'/static/css/dashboard.css')});
 if(p==='/api/speakers/refresh'){actions.push('refresh');if(fail)return route.fulfill({status:500,contentType:'application/json',body:JSON.stringify({detail:'Scan failed'})});out={speakers:[dev],summary:{message:'Scan complete'}}}
 else if(p==='/api/speakers')out=[dev];
 else if(p.startsWith('/api/speakers/')&&method==='PATCH'){actions.push('patch');Object.assign(dev,req.postDataJSON());out=dev}
 else if(p.startsWith('/api/speakers/')&&method==='DELETE'){actions.push('forget');out={note:'Speaker forgotten'}}
 else if(p==='/api/google-home/sync'&&method==='POST'){actions.push('sync');out={label:sync}}
 else if(p==='/api/google-home/sync/status')out={status:sync,label:sync};
 else if(p==='/api/status')out={};
 return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(out)});
 });
 page.on('dialog',d=>d.dismiss());await page.goto('http://bridge.test/');await page.locator('#navSettings').click();await page.locator('[data-settings-tab=speakers]').click();await page.waitForSelector('#deviceList .device');
 await page.locator('#scanButton').click();await page.getByRole('switch',{name:'Allow playback on Kitchen'}).uncheck();await page.getByRole('switch',{name:'Show Kitchen Plex in Google Home'}).check();
 let name=page.getByRole('textbox',{name:'Google Home name for Kitchen'});await name.fill('Discarded');await page.getByRole('button',{name:'Cancel Google Home name for Kitchen'}).click();if(await name.inputValue()!=='Kitchen Plex')throw Error('cancel did not restore name');await name.fill('Kitchen Music');await page.getByRole('button',{name:'Save Google Home name for Kitchen'}).click();await page.waitForTimeout(100);
 if(dev.enabled!==false||dev.google_home_visible!==true||dev.google_home_name!=='Kitchen Music')throw Error('speaker updates not persisted');await page.getByRole('button',{name:'Forget',exact:true}).click();if(actions.includes('forget'))throw Error('dismissed forget sent request');
 dev.availability='offline';await page.locator('#scanButton').click();await page.getByText(/offline/).first().waitFor();dev.availability='online';await page.locator('#scanButton').click();fail=true;await page.locator('#scanButton').click();await page.getByText('Scan failed').first().waitFor();fail=false;await page.locator('[data-settings-tab=connections]').click();await page.locator('#ghSyncBtn').click();await page.locator('#feedback').getByText('pending').waitFor();sync='succeeded';await page.locator('#ghSyncBtn').click();await page.locator('#feedback').getByText('succeeded').waitFor();sync='failed';await page.locator('#ghSyncBtn').click();await page.locator('#feedback').getByText('failed').waitFor();
 await page.locator('[data-settings-tab=speakers]').click();await page.screenshot({path:`/tmp/plex-browser-${width}.png`,fullPage:true});
 let overflow=await page.evaluate(()=>({scroll:document.documentElement.scrollWidth,width:innerWidth}));
 page.removeAllListeners('dialog');page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'Forget',exact:true}).click();if(!actions.includes('forget'))throw Error('confirmed forget did not send request');let touch=await page.locator('#scanButton').boundingBox(); await page.locator('#scanButton').focus();let focus=await page.evaluate(()=>document.activeElement.id);if(overflow.scroll>overflow.width)throw Error('horizontal overflow');if(focus!=='scanButton')throw Error('scan button did not receive focus');if(touch.height<44)throw Error('scan touch target too small');console.log(JSON.stringify({width,actions,dev,overflow,touch,focus}));await ctx.close();
} await browser.close()})().catch(e=>{console.error(e);process.exit(1)});
