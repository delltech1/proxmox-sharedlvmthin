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
let inventoryFixture=null;
let inventoryDelayMs=0;
let inventoryFailureCode=null;
const server=http.createServer((req,res)=>{
  requests.push([req.method,req.url]);
  if(req.url==='/api/version'){res.setHeader('content-type','application/json');return res.end(JSON.stringify({user:'test@pve'}));}
  if(req.url==='/api/health'){res.setHeader('content-type','application/json');return res.end(JSON.stringify(incompleteHealth?{result:'UNKNOWN',error:'health snapshot not ready'}:invalidHealthRows?{...health,storages:[null]}:health));}
  if(req.url==='/api/registration/inventory'){res.setHeader('content-type','application/json');if(!inventoryFixture){res.statusCode=503;return res.end(JSON.stringify({code:inventoryFailureCode}))}const body=JSON.stringify(inventoryFixture);return setTimeout(()=>res.end(body),inventoryDelayMs);}
  if(req.method==='GET'&&(req.url==='/login'||req.url==='/tfa-fixture')){res.setHeader('content-type','text/html');return res.end(req.url==='/login'?authPages.login:authPages.tfa);}
  const files={'/':'index.html','/guide.js':'guide.js','/registration.js':'registration.js','/i18n.js':'i18n.js','/login-i18n.js':'login-i18n.js'};
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
    const commandGuide=await page.locator('.guide-operation-reference').innerText();
    for(const term of ['iSCSI discovery','local discovery or node records','iSCSI login','pvcreate / vgcreate','pvesm add','Unverified','does not prove execution'])assert(commandGuide.includes(term),`missing operation guidance: ${term}`);
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
    await page.locator('#guide-iscsi-portal').fill('san.example');
    await page.getByRole('button',{name:'Build command'}).click();
    await page.getByRole('button',{name:'Copy discovery command'}).click();
    assert.strictEqual(await page.evaluate(()=>window.__copied),'iscsiadm -m discovery -t sendtargets -p san.example:3260');
    await page.locator('#guide-iscsi-portal').fill('san.example;touch bad');
    await page.getByRole('button',{name:'Build command'}).click();
    assert(!(await page.getByRole('button',{name:'Copy discovery command'}).isVisible()));
    for(const bad of ['999.1.1.1','1.2.3.04','-bad.example','::1']){
      await page.locator('#guide-iscsi-portal').fill(bad);
      await page.getByRole('button',{name:'Build command'}).click();
      assert(!(await page.getByRole('button',{name:'Copy discovery command'}).isVisible()),`invalid portal accepted: ${bad}`);
    }
    await page.locator('#guide-iscsi-portal').fill('192.0.2.8');
    await page.locator('#guide-iscsi-port').fill('3260');
    await page.getByRole('button',{name:'Build command'}).click();
    assert((await page.getByRole('button',{name:'Copy discovery command'}).isVisible()));
    await page.locator('#guide-iscsi-port').fill('65536');
    assert(!(await page.getByRole('button',{name:'Copy discovery command'}).isVisible()),'editing port must invalidate command');
    await page.getByRole('button',{name:'Build command'}).click();
    assert(!(await page.getByRole('button',{name:'Copy discovery command'}).isVisible()),'out-of-range port accepted');
    await page.selectOption('#pageLanguage','de');
    assert((await page.locator('#sanGuideContent').innerText()).includes('content none'));
    await page.selectOption('#pageLanguage','en');
    await page.getByRole('button',{name:'Back'}).click();
    await page.getByRole('button',{name:'Back'}).click();
    await page.selectOption('#pageLanguage','de');
    assert.strictEqual(await page.locator('#sanGuideTitle').innerText(),'Anleitung für gemeinsam genutzten Speicher');
    assert((await page.locator('#sanGuideContent').innerText()).includes('dedizierte LUN'));
    if(process.env.BASTRIX_SCREENSHOT)await page.screenshot({path:process.env.BASTRIX_SCREENSHOT,fullPage:true});
    for(let i=0;i<8;i++)await page.getByRole('button',{name:'Ja, weiter'}).click();
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
        ? [['iSCSI',10],['Fibre Channel (FC)',9],['FCoE',10],['Shared SAS — not lab qualified',8]]
        : [['iSCSI',10],['Fibre Channel (FC)',9],['FCoE',10],['Gemeinsam genutzter SAS-Speicher — im Labor nicht qualifiziert',8]];
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
    await page.selectOption('#pageLanguage','en');
    await page.getByRole('button',{name:'iSCSI',exact:true}).click();
    for(let i=0;i<7;i++)await page.getByRole('button',{name:'Yes, continue'}).click();
    await page.locator('#guide-peer-node').fill('node-b');
    await page.locator('#guide-peer-address').fill('192.0.2.11');
    await page.getByRole('button',{name:'Build SSH check'}).click();
    await page.getByRole('button',{name:'Copy SSH check'}).click();
    const sshCheck=await page.evaluate(()=>window.__copied);
    assert(sshCheck.includes('StrictHostKeyChecking=yes'));
    assert(sshCheck.includes('/etc/pve/nodes/node-b/ssh_known_hosts'));
    assert(sshCheck.includes('root@192.0.2.11 /usr/bin/true'));
    await page.locator('#guide-peer-node').fill('node-b;bad');
    await page.getByRole('button',{name:'Build SSH check'}).click();
    assert(!(await page.getByRole('button',{name:'Copy SSH check'}).isVisible()));
    for(let i=0;i<2;i++)await page.getByRole('button',{name:'Yes, continue'}).click();
    const observation={result:'OBSERVED',observed_at:new Date().toISOString(),nodes:['node-a','node-b'],storage_definitions:[{id:'local',type:'dir'}],candidates:[{
      name:'example_vg',vg_uuid:'ABCDEF-abcd-1234-ABCD-5678-aaaa-123456',pv_uuid:'123456-abcd-1234-ABCD-5678-aaaa-ABCDEF',
      wwid:'3600000000000001',size_bytes:107374182400,free_bytes:96636764160,
      eligible_for_new_vg_registration:true,empty_on_all_nodes:true,existing_aliases:[],
      paths_by_node:{'node-a':1,'node-b':1},transport_by_node:{'node-a':'iscsi','node-b':'iscsi'}
    }]};
    inventoryFixture=observation;
    await page.getByRole('button',{name:'Discover cluster',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.registration-summary').textContent.includes('Detected'));
    assert((await page.locator('.registration-box').innerText()).includes('point-in-time observation'));
    await page.locator('.registration-fallback summary').click();
    await page.locator('#registration-inventory').fill(JSON.stringify(observation));
    await page.getByRole('button',{name:'Read report',exact:true}).click();
    assert((await page.locator('.registration-summary').innerText()).includes('Detected in imported report'));
    assert((await page.locator('.registration-summary').innerText()).includes('iscsi'));
    assert((await page.locator('.registration-summary').innerText()).includes(observation.candidates[0].vg_uuid));
    assert((await page.locator('.registration-summary').innerText()).includes(observation.candidates[0].pv_uuid));
    assert((await page.locator('.registration-summary').innerText()).includes(observation.candidates[0].wwid));
    assert((await page.locator('.registration-summary').innerText()).includes('90.00 / 100.00 GiB'));
    if(process.env.BASTRIX_REGISTRATION_SCREENSHOT)await page.screenshot({path:process.env.BASTRIX_REGISTRATION_SCREENSHOT,fullPage:true});
    await page.getByRole('button',{name:'Generate commands',exact:true}).click();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth <= window.innerWidth), 'generated commands overflow narrow layout');
    await page.getByRole('button',{name:'Copy CLI Command — Eager',exact:true}).click();
    const copiedEager=await page.evaluate(()=>window.__copied);
    await page.getByRole('button',{name:'Copy CLI Command — Lazy',exact:true}).click();
    const copied=await page.evaluate(()=>window.__copied);
    assert(copied.includes('--slt-allocation-mode thick-generations-lazy'));
    assert(copied.includes('--slt-vg-reserve-percent 5'));
    assert(!copied.includes('slt-burst-headroom'));
    const identity=observation.candidates[0];
    const peer={vg_uuid:identity.vg_uuid,pv_uuid:identity.pv_uuid,wwid:identity.wwid,size_bytes:identity.size_bytes,complete:true,peer_ssh_verified:true,mapper_verified:true,healthy_paths:1};
    const plannerInput={request:{nodes:observation.nodes,vg:identity.name,aliases:{eager:'bastrix-eager',lazy:'bastrix-lazy'},reserve_percent:5},evidence:{quorate:true,inventory_complete:true,members:{'node-a':true,'node-b':true},vg:{...identity,pv_count:1,dedicated:true},node_evidence:{'node-a':peer,'node-b':peer},storage_definitions:[]}};
    const plannerPath=path.resolve(__dirname,'../../usr/libexec/pve-sharedlvmthin/sharedlvmthin-registration-plan.py');
    const planned=spawnSync(process.env.BASTRIX_TEST_PYTHON,[plannerPath],{input:JSON.stringify(plannerInput),encoding:'utf8'});
    assert.strictEqual(planned.status,0,planned.stderr);
    assert.deepStrictEqual([copiedEager,copied],JSON.parse(planned.stdout).definitions.map(x=>x.cli));
    inventoryFixture=null;
    await page.getByRole('button',{name:'Discover cluster',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.registration-box').textContent.includes('Automatic discovery unavailable'));
    assert.strictEqual(await page.locator('.registration-commands button').count(),0,'failed discovery must invalidate old command');
    inventoryFailureCode='peer_ssh_unverified';
    await page.getByRole('button',{name:'Discover cluster',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.registration-box').textContent.includes('Peer SSH not confirmed'));
    assert((await page.locator('.registration-box').innerText()).includes('Do not disable host-key checking'));
    inventoryFailureCode=null;
    await page.locator('#registration-inventory').fill(JSON.stringify(observation));
    await page.getByRole('button',{name:'Read report',exact:true}).click();
    inventoryFixture=observation;inventoryDelayMs=400;
    await page.getByRole('button',{name:'Discover cluster',exact:true}).click();
    const altered={...observation,result:'UNKNOWN'};
    await page.locator('#registration-inventory').fill(JSON.stringify(altered));
    await page.getByRole('button',{name:'Read report',exact:true}).click();
    await page.waitForTimeout(600);
    assert((await page.locator('.registration-box').innerText()).includes('Invalid, incomplete or conflicting report'),'late discovery must not overwrite later manual input');
    assert.strictEqual(await page.locator('.registration-commands button').count(),0);
    inventoryDelayMs=0;
    await page.locator('#registration-inventory').fill(JSON.stringify(observation));
    await page.getByRole('button',{name:'Read report',exact:true}).click();
    await page.locator('#registration-reserve').fill('0');
    assert((await page.locator('.registration-zero-warning').innerText()).includes('free VG extents can be exhausted'));
    await page.getByRole('button',{name:'Generate commands',exact:true}).click();
    assert((await page.locator('.registration-fields pre').first().innerText()).includes('--slt-vg-reserve-percent 0'));
    await page.locator('#registration-reserve').fill('6');
    assert.strictEqual(await page.locator('.registration-commands button').count(),0);
    await page.getByRole('button',{name:'Generate commands',exact:true}).click();
    assert((await page.locator('.registration-fields pre').first().innerText()).includes('--slt-vg-reserve-percent 6'));
    await page.locator('#registration-eager').fill('bad;command');
    await page.getByRole('button',{name:'Generate commands',exact:true}).click();
    assert(!(await page.getByRole('button',{name:'Copied',exact:true}).isVisible()));
    observation.storage_definitions.push({id:'other',type:'lvm',vg_name:'example_vg'});
    observation.candidates[0].existing_aliases=[{id:'other',type:'lvm',allocation_mode:null}];
    observation.candidates[0].eligible_for_new_vg_registration=false;
    await page.locator('#registration-inventory').fill(JSON.stringify(observation));
    await page.getByRole('button',{name:'Read report',exact:true}).click();
    assert((await page.locator('.registration-box').innerText()).includes('no empty, unregistered VG'));
    assert((await page.locator('.registration-summary').innerText()).includes('other · lvm · unknown'));
    await page.getByRole('button',{name:'Copy inspection command — other',exact:true}).click();
    assert.strictEqual(await page.evaluate(()=>window.__copied),'pvesh get /storage/other --output-format json');
    assert.strictEqual(await page.locator('.registration-commands button').count(),0,'existing alias must not provide registration commands');
    observation.candidates[0].existing_aliases=[];
    observation.candidates[0].eligible_for_new_vg_registration=true;
    const mismatch={...observation,candidates:[],unverified_reasons:['identity_mismatch']};
    await page.locator('#registration-inventory').fill(JSON.stringify(mismatch));
    await page.getByRole('button',{name:'Read report',exact:true}).click();
    assert((await page.locator('.registration-summary').innerText()).includes('Storage identity differs between nodes'));
    assert((await page.locator('.registration-box').innerText()).includes('Unverified: no empty, unregistered VG'));
    assert.strictEqual(await page.locator('.registration-commands button').count(),0);
    const wrongTransport=JSON.parse(JSON.stringify(observation));
    wrongTransport.storage_definitions=wrongTransport.storage_definitions.filter(d=>d.id!=='other');
    wrongTransport.candidates[0].transport_by_node['node-b']='fc-or-fcoe';
    await page.locator('#registration-inventory').fill(JSON.stringify(wrongTransport));
    await page.getByRole('button',{name:'Read report',exact:true}).click();
    assert((await page.locator('.registration-box').innerText()).includes('selected transport'));
    assert.strictEqual(await page.locator('.registration-commands button').count(),0,'mixed transport must not create a registration preview');
    observation.storage_definitions.pop(); observation.observed_at=new Date(Date.now()-600000).toISOString();
    await page.locator('#registration-inventory').fill(JSON.stringify(observation));
    await page.getByRole('button',{name:'Read report',exact:true}).click();
    assert((await page.locator('.registration-box').innerText()).includes('Invalid, incomplete or conflicting report'));
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
