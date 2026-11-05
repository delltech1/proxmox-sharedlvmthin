// Local-only browser smoke test; starts no PVE service and never touches storage.
'use strict';
const {chromium}=require('playwright');
const http=require('http');
const fs=require('fs');
const path=require('path');
const assert=require('assert');
const {spawnSync}=require('child_process');
const root=path.resolve(__dirname,'../../usr/share/pve-sharedlvmthin/web');
const fixtureProcess=spawnSync(process.env.BASTRIX_TEST_PYTHON,[path.join(__dirname,'make-login-fixture.py')],{encoding:'utf8'});
assert.strictEqual(fixtureProcess.status,0,fixtureProcess.stderr);
const authPages=JSON.parse(fixtureProcess.stdout);
const health={timestamp:'2026-01-01T00:00:00Z',collection_duration_ms:1,cache:{age_seconds:1},cluster:{name:'test-cluster',quorate:true,nodes:2},host:{hostname:'test-node',kernel:'test',uptime_seconds:10,load:{'1m':0,'5m':0,'15m':0},memory:{total_bytes:1024,available_bytes:512},root_fs:{total_bytes:1024,free_bytes:512}},platform:{package_flavor:'dual',storage_api:16,plugin_api:16,pve_manager:'test',plugin_version:'test'},lvm:{version:'test',dmeventd_active:false},dm_clone:{},storages:[{id:'Overview',vg:'Storage',allocation_mode:'thick-generations',pve:{status:'active'},vg_info:{size_bytes:1024,free_bytes:512},transport:{state:'HEALTHY',paths_healthy:1,paths_total:1,type:'iscsi'},thick_anchors:[]},{id:'test-thin',vg:'test-vg',allocation_mode:'thin',pve:{status:'active'},vg_info:{size_bytes:1024,free_bytes:512},transport:{state:'HEALTHY',paths_healthy:1,paths_total:1,type:'fc'},pools:[{name:'test-pool',owned:false}]}],checks:[{status:'PASS',message:'Storage'}]};
const requests=[];
let incompleteHealth=false;
let invalidHealthRows=false;
const server=http.createServer((req,res)=>{
  requests.push([req.method,req.url]);
  if(req.url==='/api/version'){res.setHeader('content-type','application/json');return res.end(JSON.stringify({user:'test@pve'}));}
  if(req.url==='/api/health'){res.setHeader('content-type','application/json');return res.end(JSON.stringify(incompleteHealth?{result:'UNKNOWN',error:'health snapshot not ready'}:invalidHealthRows?{...health,storages:[null]}:health));}
  if(req.method==='GET'&&(req.url==='/login'||req.url==='/tfa-fixture')){res.setHeader('content-type','text/html');return res.end(req.url==='/login'?authPages.login:authPages.tfa);}
  const files={'/':'index.html','/guide.js':'guide.js','/i18n.js':'i18n.js','/login-i18n.js':'login-i18n.js'};
  if(req.method!=='GET'||!files[req.url]){res.statusCode=404;return res.end('not found');}
  res.setHeader('content-type',req.url.endsWith('.js')?'application/javascript':'text/html');
  return res.end(fs.readFileSync(path.join(root,files[req.url])));
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true,executablePath:process.env.BASTRIX_TEST_BROWSER});
  try{
    const page=await browser.newPage({viewport:{width:1280,height:800}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.addInitScript(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>{window.__copied=value}},configurable:true}));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForSelector('#clusterHealth span');
    assert(await page.evaluate(()=>document.documentElement.scrollWidth <= window.innerWidth), 'desktop layout overflows');
    assert.strictEqual(await page.locator('#clusterHealth').innerText(),'HEALTHY');
    await page.locator('#guideShortcut').click();
    assert(await page.locator('#sanGuide').isVisible());
    await page.getByRole('button',{name:'iSCSI',exact:true}).click();
    assert((await page.locator('#sanGuideContent').innerText()).includes('Open-iSCSI'));
    assert((await page.locator('#sanGuideContent').innerText()).includes('Each package should show installed'));
    assert((await page.locator('#sanGuideContent .guide-command').innerText()).includes('${db:Status-Status}'));
    await page.getByRole('button',{name:'Yes, continue'}).click();
    assert((await page.locator('#sanGuideContent').innerText()).includes('initiator IQN'));
    await page.getByRole('button',{name:'Yes, continue'}).click();
    assert((await page.locator('#sanGuideContent').innerText()).includes('dedicated LUN'));
    assert((await page.locator('#sanGuideContent').innerText()).includes('identify the dedicated LUN'));
    await page.getByRole('button',{name:'No / I am not sure'}).click();
    assert((await page.locator('#sanGuideContent').innerText()).includes('vendor documentation'));
    await page.getByRole('button',{name:'Yes, continue'}).click();
    await page.getByRole('button',{name:'Copy command'}).click();
    assert.strictEqual(await page.evaluate(()=>window.__copied),'ip -brief address\nip route\nping -c 3 TARGET_IP');
    await page.getByRole('button',{name:'Yes, continue'}).click();
    assert((await page.locator('#sanGuideContent').innerText()).includes('content none'));
    await page.selectOption('#pageLanguage','de');
    assert((await page.locator('#sanGuideContent').innerText()).includes('content none'));
    await page.selectOption('#pageLanguage','en');
    await page.getByRole('button',{name:'Back'}).click();
    await page.getByRole('button',{name:'Back'}).click();
    await page.selectOption('#pageLanguage','de');
    assert.strictEqual(await page.locator('#sanGuideTitle').innerText(),'Anleitung für gemeinsam genutzten Speicher');
    assert((await page.locator('#sanGuideContent').innerText()).includes('dedizierte LUN'));
    if(process.env.BASTRIX_SCREENSHOT)await page.screenshot({path:process.env.BASTRIX_SCREENSHOT,fullPage:true});
    for(let i=0;i<6;i++)await page.getByRole('button',{name:'Ja, weiter'}).click();
    assert((await page.locator('#sanGuideContent').innerText()).includes('Anleitung abgeschlossen'));
    await page.locator('[data-page="overview"]').click();
    assert.strictEqual(await page.locator('#clusterHealth').innerText(),'GESUND');
    assert.strictEqual(await page.locator('#storageOverview .sn').first().innerText(),'Overview');
    assert.strictEqual(await page.locator('#checksOverview .check div').last().innerText(),'Storage');
    assert((await page.locator('#storageOverview').innerText()).includes('Kapazität'));
    assert((await page.locator('#storageOverview').innerText()).includes('Thin-Pools'));
    await page.locator('[data-page="storage"]').click();
    assert((await page.locator('#storagePage').innerText()).includes('PVE-Referenzen'));
    assert((await page.locator('#storagePage').innerText()).includes('Nicht zugeordnet'));
    if(process.env.BASTRIX_STORAGE_SCREENSHOT)await page.screenshot({path:process.env.BASTRIX_STORAGE_SCREENSHOT,fullPage:true});
    await page.selectOption('#pageLanguage','en');
    assert.strictEqual(await page.locator('#clusterHealth').innerText(),'HEALTHY');
    assert((await page.locator('#storageOverview').innerText()).includes('Capacity'));
    assert.strictEqual(await page.locator('#storageOverview .sn').first().innerText(),'Overview');
    await page.selectOption('#pageLanguage','de');
    await page.reload();await page.waitForSelector('#clusterHealth span');
    assert.strictEqual(await page.locator('#pageLanguage').inputValue(),'de');
    await page.setViewportSize({width:650,height:900});await page.locator('#guideShortcut').click();
    assert(await page.locator('#sanGuide').isVisible());
    assert(await page.evaluate(()=>document.documentElement.scrollWidth <= window.innerWidth), 'narrow layout overflows');
    await page.selectOption('#pageLanguage','en');
    await page.getByRole('button',{name:'Fibre Channel (FC)',exact:true}).click();
    for(let i=0;i<3;i++)await page.getByRole('button',{name:'Yes, continue'}).click();
    assert((await page.locator('#sanGuideContent').innerText()).includes('expected FC target ports'));
    await page.getByRole('button',{name:'Copy command'}).click();
    assert((await page.evaluate(()=>window.__copied)).includes('/sys/class/fc_remote_ports/rport-*/port_state'));
    for(let i=0;i<4;i++)await page.getByRole('button',{name:'Back'}).click();
    for(const name of ['Fibre Channel (FC)','FCoE','Shared SAS — not lab qualified']){
      await page.getByRole('button',{name,exact:true}).click();
      assert((await page.locator('#sanGuideContent').innerText()).includes('Check the result yourself'));
      await page.getByRole('button',{name:'Back'}).click();
    }
    // Exercise every step in both languages, including the help branch. The
    // synthetic server only allows GET; this does not qualify real storage.
    for(const lang of ['en','de']){
      await page.selectOption('#pageLanguage',lang);
      const choices=lang==='en'
        ? [['iSCSI',8],['Fibre Channel (FC)',7],['FCoE',8],['Shared SAS — not lab qualified',6]]
        : [['iSCSI',8],['Fibre Channel (FC)',7],['FCoE',8],['Gemeinsam genutzter SAS-Speicher — im Labor nicht qualifiziert',6]];
      const no=lang==='en'?'No / I am not sure':'Nein / nicht sicher';
      const next=lang==='en'?'Yes, continue':'Ja, weiter';
      const done=lang==='en'?'Guide complete':'Anleitung abgeschlossen';
      const restart=lang==='en'?'Start over':'Neu beginnen';
      for(const [name,count] of choices){
        await page.getByRole('button',{name,exact:true}).click();
        for(let i=0;i<count;i++){
          assert((await page.locator('#sanGuideContent').innerText()).includes(`${i+1} / ${count}`));
          if(name==='FCoE' && i===1){
            assert((await page.locator('#sanGuideContent .guide-command').innerText()).includes('/sys/bus/fcoe/devices/'));
          }
          await page.getByRole('button',{name:no,exact:true}).click();
          await page.getByRole('button',{name:next,exact:true}).click();
        }
        assert((await page.locator('#sanGuideContent').innerText()).includes(done));
        await page.getByRole('button',{name:lang==='en'?'Back':'Zurück',exact:true}).click();
        assert((await page.locator('#sanGuideContent').innerText()).includes(`${count} / ${count}`));
        await page.getByRole('button',{name:next,exact:true}).click();
        assert((await page.locator('#sanGuideContent').innerText()).includes(done));
        await page.getByRole('button',{name:restart,exact:true}).click();
      }
    }
    incompleteHealth=true;
    const pendingPage=await browser.newPage();const pendingErrors=[];
    pendingPage.on('pageerror',e=>pendingErrors.push(e.message));
    await pendingPage.goto(`http://127.0.0.1:${server.address().port}/`);
    await pendingPage.waitForFunction(()=>document.querySelector('#api').textContent.includes('API error'));
    await pendingPage.selectOption('#pageLanguage','de');
    await pendingPage.locator('#guideShortcut').click();
    assert(await pendingPage.locator('#sanGuide').isVisible());
    incompleteHealth=false;
    await pendingPage.evaluate(()=>loadHealth());
    await pendingPage.waitForFunction(()=>document.querySelector('#clusterHealth').textContent.includes('GESUND'));
    invalidHealthRows=true;
    await pendingPage.evaluate(()=>loadHealth());
    await pendingPage.waitForFunction(()=>document.querySelector('#api').textContent.includes('API-Fehler'));
    await pendingPage.selectOption('#pageLanguage','en');
    assert.strictEqual(await pendingPage.locator('#clusterHealth').innerText(),'HEALTHY');
    assert((await pendingPage.locator('#api').innerText()).includes('API error'));
    invalidHealthRows=false;
    assert.deepStrictEqual(pendingErrors,[]);
    await pendingPage.close();
    await page.selectOption('#pageLanguage','de');
    await page.goto(`http://127.0.0.1:${server.address().port}/login`);
    await page.waitForFunction(()=>document.documentElement.lang==='de');
    assert.strictEqual(await page.locator('.card h1').innerText(),'Proxmox VE Authentifizierung');
    assert.strictEqual(await page.locator('.card .err').innerText(),'Authentifizierung fehlgeschlagen.');
    assert.strictEqual(await page.locator('input[name="username"]').inputValue(),'example@pve');
    await page.locator('input[name="password"]').fill('temporary-test-only');
    await page.selectOption('#login-language','en');
    assert.strictEqual(await page.locator('input[name="username"]').inputValue(),'example@pve');
    assert.strictEqual(await page.locator('input[name="password"]').inputValue(),'temporary-test-only');
    await page.selectOption('#login-language','de');
    await page.locator('input[name="password"]').clear();
    await page.goto(`http://127.0.0.1:${server.address().port}/tfa-fixture`);
    await page.waitForFunction(()=>document.documentElement.lang==='de');
    assert((await page.locator('.card .sub').innerText()).includes('example@pve'));
    await page.selectOption('select[name="factor"]','recovery');
    await page.locator('input[name="response"]').fill('temporary-test-only');
    await page.selectOption('#login-language','en');
    assert.strictEqual(await page.locator('.card h1').innerText(),'Two-Factor Authentication');
    assert.strictEqual(await page.locator('select[name="factor"]').inputValue(),'recovery');
    assert.strictEqual(await page.locator('input[name="response"]').inputValue(),'temporary-test-only');
    assert.deepStrictEqual(errors,[]);
    assert(requests.every(([method])=>method==='GET'));
    console.log('GUIDE_BROWSER_SMOKE_PASS');
  }finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1});
