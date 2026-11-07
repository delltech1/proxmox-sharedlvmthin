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
    // A clipboard write can complete after its source form has changed.
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:value=>new Promise(resolve=>{window.__finishHelperCopy=()=>{window.__copied=value;resolve()}})},configurable:true}));
    await page.getByRole('button',{name:'Copied',exact:true}).click();
    await page.locator('#guide-iscsi-portal').fill('new.example');
    await page.getByRole('button',{name:'Build command'}).click();
    await page.evaluate(()=>window.__finishHelperCopy());
    await page.waitForTimeout(0);
    assert.strictEqual(await page.evaluate(()=>window.__copied),'iscsiadm -m discovery -t sendtargets -p san.example:3260');
    assert((await page.locator('#guide-clipboard-warning').innerText()).includes('may contain an old command'));
    assert(await page.getByRole('button',{name:'Copy discovery command'}).isVisible());
    // The warning remains visible across dashboard navigation, including when
    // the outstanding write completes while the guide is hidden.
    await page.getByRole('button',{name:'Copy discovery command'}).click();
    await page.locator('[data-page="overview"]').click();
    await page.evaluate(()=>window.__finishHelperCopy());
    await page.waitForTimeout(0);
    assert(await page.locator('#guide-clipboard-warning').isVisible());
    await page.selectOption('#pageLanguage','de');
    assert((await page.locator('#guide-clipboard-warning').innerText()).includes('alten Befehl'));
    await page.selectOption('#pageLanguage','en');
    await page.locator('[data-page="sanGuide"]').click();
    assert(await page.locator('#guide-clipboard-warning').isVisible());
    await page.locator('#guide-iscsi-portal').fill('new.example');
    await page.getByRole('button',{name:'Build command'}).click();
    // Two writes can finish in either order, including a static step command.
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:value=>new Promise(resolve=>{(window.__helperWrites ||= []).push(()=>{window.__copied=value;resolve()})})},configurable:true}));
    await page.getByRole('button',{name:'Copy command',exact:true}).click();
    await page.getByRole('button',{name:'Copy discovery command'}).click();
    await page.evaluate(()=>window.__helperWrites[1]());
    await page.evaluate(()=>window.__helperWrites[0]());
    await page.waitForTimeout(0);
    assert(await page.getByRole('button',{name:'Copy command',exact:true}).isVisible());
    assert(await page.getByRole('button',{name:'Copy discovery command'}).isVisible());
    assert((await page.locator('#guide-clipboard-warning').innerText()).includes('may contain an old command'));
    // Navigation and a language switch also invalidate a pending static copy.
    await page.getByRole('button',{name:'Copy command',exact:true}).click();
    await page.getByRole('button',{name:'Back',exact:true}).click();
    await page.selectOption('#pageLanguage','de');
    await page.evaluate(()=>window.__helperWrites[2]());
    await page.waitForTimeout(0);
    assert((await page.locator('#guide-clipboard-warning').innerText()).includes('alten Befehl'));
    await page.selectOption('#pageLanguage','en');
    await page.getByRole('button',{name:'Yes, continue',exact:true}).click();
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>{window.__copied=value}},configurable:true}));
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
    for(const field of ['port_name','roles','port_state'])assert((await page.evaluate(()=>window.__copied)).includes(`/sys/class/fc_remote_ports/rport-*/${field}`));
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
          if((name==='FCoE' && i===4) || (name.includes('Channel') && i===3)){
            const fcCommand=await page.locator('#sanGuideContent .guide-command').innerText();
            for(const field of ['port_name','roles','port_state'])assert(fcCommand.includes(`/sys/class/fc_remote_ports/rport-*/${field}`));
          }
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
    assert(sshCheck.includes('ControlMaster=no'));
    assert(sshCheck.includes('ControlPath=none'));
    assert(sshCheck.includes('/etc/pve/nodes/node-b/ssh_known_hosts'));
    assert(sshCheck.includes('root@192.0.2.11 /usr/bin/true'));
    // Editing the SSH destination while an earlier clipboard write is pending
    // must hide the command and warn even when that old write succeeds later.
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:value=>new Promise(resolve=>{window.__finishAddressCopy=()=>{window.__copied=value;resolve()}})},configurable:true}));
    await page.getByRole('button',{name:'Copied',exact:true}).click();
    await page.locator('#guide-peer-address').fill('192.0.2.12');
    assert(!(await page.getByRole('button',{name:'Copy SSH check'}).isVisible()));
    assert((await page.locator('#guide-clipboard-warning').innerText()).includes('may contain an old command'));
    await page.evaluate(()=>window.__finishAddressCopy());
    await page.waitForTimeout(0);
    assert.strictEqual(await page.evaluate(()=>window.__copied),sshCheck);
    assert(!(await page.getByRole('button',{name:'Copy SSH check'}).isVisible()));
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>{window.__copied=value}},configurable:true}));
    await page.locator('#guide-peer-address').fill('192.0.2.11');
    await page.getByRole('button',{name:'Build SSH check'}).click();
    await page.getByRole('button',{name:'Copy SSH check'}).click();
    assert.strictEqual(await page.evaluate(()=>window.__copied),sshCheck);
    assert(!(await page.locator('#guide-clipboard-warning').isVisible()));
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:value=>new Promise(resolve=>{window.__finishHelperCopy=()=>{window.__copied=value;resolve()}})},configurable:true}));
    await page.getByRole('button',{name:'Copied',exact:true}).click();
    await page.selectOption('#pageLanguage','de');
    assert((await page.locator('#guide-clipboard-warning').innerText()).includes('alten Befehl'));
    await page.getByRole('button',{name:'Zurück',exact:true}).click();
    await page.evaluate(()=>window.__finishHelperCopy());
    await page.waitForTimeout(0);
    assert.strictEqual(await page.evaluate(()=>window.__copied),sshCheck);
    assert((await page.locator('#guide-clipboard-warning').innerText()).includes('alten Befehl'));
    await page.getByRole('button',{name:'Ja, weiter',exact:true}).click();
    assert(!(await page.getByRole('button',{name:'Kopiert',exact:true}).count()));
    await page.selectOption('#pageLanguage','en');
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>{window.__copied=value}},configurable:true}));
    await page.locator('#guide-peer-node').fill('node-b');
    await page.locator('#guide-peer-address').fill('192.0.2.11');
    await page.locator('#guide-peer-node').fill('node-b;bad');
    await page.getByRole('button',{name:'Build SSH check'}).click();
    assert(!(await page.getByRole('button',{name:'Copy SSH check'}).isVisible()));
    for(let i=0;i<2;i++)await page.getByRole('button',{name:'Yes, continue'}).click();
    const observation={result:'OBSERVED',observed_at:new Date().toISOString(),nodes:['node-a','node-b'],storage_definitions:[{id:'local',type:'dir'}],candidates:[{
      name:'example_vg',vg_uuid:'ABCDEF-abcd-1234-ABCD-5678-aaaa-123456',pv_uuid:'123456-abcd-1234-ABCD-5678-aaaa-ABCDEF',
      wwid:'3600000000000001',size_bytes:107374182400,free_bytes:96636764160,
      eligible_for_new_vg_registration:true,empty_on_all_nodes:true,canonical_mapper_on_all_nodes:true,existing_aliases:[],
      paths_by_node:{'node-a':1,'node-b':1},transport_by_node:{'node-a':'iscsi','node-b':'iscsi'}
    }]};
    for (const [lang, phrases] of Object.entries({
      en: ['Observation and command generation do not guarantee VG identity or emptiness at execution time.', 'Before each command, recheck the VG identity and state on every node', 'registrations are not atomic', 'do not retry blindly'],
      de: ['Beobachtung und Befehlserzeugung garantieren weder VG-Identität noch Leere zum Ausführungszeitpunkt.', 'Vor jedem Befehl VG-Identität und Zustand auf allen Knoten erneut prüfen', 'Registrierung sind nicht atomar', 'nicht blind wiederholen']
    })) {
      const text=await page.evaluate(lang=>window.BastrixRegistration.render(lang,'iscsi').textContent,lang);
      for (const phrase of phrases) assert(text.includes(phrase),`${lang} registration warning missing: ${phrase}`);
    }
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
    assert.strictEqual(await page.locator('.registration-zero-warning').isVisible(),true,'default zero reserve must warn without input');
    if(process.env.BASTRIX_REAL_INVENTORY){
      const live=fs.readFileSync(process.env.BASTRIX_REAL_INVENTORY,'utf8');
      await page.locator('#registration-inventory').fill(live);
      await page.getByRole('button',{name:'Read report',exact:true}).click();
      assert.strictEqual(await page.locator('.registration-fields').isVisible(),false,'configured live VGs must not generate registration commands');
      assert((await page.locator('.registration-summary').innerText()).includes('Existing definition found'));
      await page.locator('#registration-inventory').fill(JSON.stringify(observation));
      await page.getByRole('button',{name:'Read report',exact:true}).click();
    }
    const correlated=spawnSync(process.env.BASTRIX_TEST_PYTHON,[path.join(__dirname,'make-registration-fixture.py')],{encoding:'utf8'});
    assert.strictEqual(correlated.status,0,correlated.stderr);
    await page.locator('#registration-inventory').fill(correlated.stdout);
    await page.getByRole('button',{name:'Read report',exact:true}).click();
    assert.strictEqual(await page.locator('.registration-fields').isVisible(),true,'actual correlator output must be accepted');
    await page.locator('#registration-inventory').fill(JSON.stringify(observation));
    await page.getByRole('button',{name:'Read report',exact:true}).click();
    if(process.env.BASTRIX_REGISTRATION_SCREENSHOT)await page.screenshot({path:process.env.BASTRIX_REGISTRATION_SCREENSHOT,fullPage:true});
    await page.getByRole('button',{name:'Generate commands',exact:true}).click();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth <= window.innerWidth), 'generated commands overflow narrow layout');
    await page.getByRole('button',{name:'Copy CLI Command — Eager',exact:true}).click();
    const copiedEager=await page.evaluate(()=>window.__copied);
    await page.getByRole('button',{name:'Copy CLI Command — Lazy',exact:true}).click();
    const copied=await page.evaluate(()=>window.__copied);
    assert(copied.includes('--slt-allocation-mode thick-generations-lazy'));
    assert(copied.includes('--slt-vg-reserve-percent 0'));
    assert(!copied.includes('slt-burst-headroom'));
    // Register and guide commands share one clipboard coordinator: reverse
    // completion order cannot make either button claim the final clipboard.
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:value=>new Promise(resolve=>{(window.__crossWrites ||= []).push(()=>{window.__copied=value;resolve()})})},configurable:true}));
    await page.getByRole('button',{name:'Copy CLI Command — Eager',exact:true}).click();
    await page.getByRole('button',{name:'Copy command',exact:true}).click();
    await page.evaluate(()=>window.__crossWrites[1]());
    await page.evaluate(()=>window.__crossWrites[0]());
    await page.waitForFunction(()=>!document.querySelector('#guide-clipboard-warning').hidden);
    assert.strictEqual(await page.evaluate(()=>window.__copied),copiedEager);
    assert(await page.getByRole('button',{name:'Copy command',exact:true}).isVisible());
    assert.strictEqual(await page.getByRole('button',{name:/^Copied/}).count(),0);
    assert((await page.locator('#guide-clipboard-warning').innerText()).includes('may contain an old command'));
    await page.getByRole('button',{name:'Generate commands',exact:true}).click();
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:value=>new Promise(resolve=>{window.__finishCopy=()=>{window.__copied=value;resolve()}})},configurable:true}));
    await page.getByRole('button',{name:'Copy CLI Command — Eager',exact:true}).click();
    await page.locator('#registration-reserve').fill('6');
    await page.locator('#registration-inventory').fill(JSON.stringify(observation));
    await page.getByRole('button',{name:'Read report',exact:true}).click();
    await page.getByRole('button',{name:'Generate commands',exact:true}).click();
    await page.evaluate(()=>window.__finishCopy());
    await page.waitForFunction(()=>!document.querySelector('#guide-clipboard-warning').hidden);
    assert(await page.getByRole('button',{name:'Copy CLI Command — Eager',exact:true}).isVisible(),'stale callback must preserve newer commands');
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:async()=>{throw Error('denied')}},configurable:true}));
    await page.getByRole('button',{name:'Copy CLI Command — Eager',exact:true}).click();
    assert((await page.locator('.registration-box').innerText()).includes('Copy failed.'));
    assert(await page.getByRole('button',{name:'Copy CLI Command — Eager',exact:true}).isVisible());
    assert(await page.locator('#guide-clipboard-warning').isVisible(),'failed write must not clear warning');
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>{window.__copied=value}},configurable:true}));
    await page.getByRole('button',{name:'Copy CLI Command — Eager',exact:true}).click();
    assert(!(await page.locator('#guide-clipboard-warning').isVisible()),'fresh successful copy clears warning');
    await page.locator('#registration-reserve').fill('5');
    await page.getByRole('button',{name:'Generate commands',exact:true}).click();
    const identity=observation.candidates[0];
    const peer={vg_uuid:identity.vg_uuid,pv_uuid:identity.pv_uuid,wwid:identity.wwid,size_bytes:identity.size_bytes,complete:true,peer_ssh_verified:true,mapper_verified:true,healthy_paths:1};
    const plannerInput={request:{nodes:observation.nodes,vg:identity.name,aliases:{eager:'bastrix-eager',lazy:'bastrix-lazy'},reserve_percent:0},evidence:{quorate:true,inventory_complete:true,members:{'node-a':true,'node-b':true},vg:{...identity,pv_count:1,dedicated:true},node_evidence:{'node-a':peer,'node-b':peer},storage_definitions:[]}};
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
    for (const [lang, phrases] of Object.entries({
      en: ['Observation and command generation do not guarantee VG identity or emptiness at execution time.', 'Before each command, recheck the VG identity and state on every node', 'registrations are not atomic', 'do not retry blindly'],
      de: ['Beobachtung und Befehlserzeugung garantieren weder VG-Identität noch Leere zum Ausführungszeitpunkt.', 'Vor jedem Befehl VG-Identität und Zustand auf allen Knoten erneut prüfen', 'Registrierung sind nicht atomar', 'nicht blind wiederholen']
    })) {
      const text=await page.evaluate(lang=>window.BastrixRegistration.render(lang,'iscsi').textContent,lang);
      for (const phrase of phrases) assert(text.includes(phrase),`${lang} registration warning missing: ${phrase}`);
    }
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
    await page.locator('#registration-reserve').fill('00');
    assert(await page.locator('.registration-zero-warning').isVisible(),'zero with leading zeros must retain the reserve warning');
    await page.locator('#registration-reserve').fill('0');
    await page.getByRole('button',{name:'Generate commands',exact:true}).click();
    assert((await page.locator('.registration-fields pre').first().innerText()).includes('--slt-vg-reserve-percent 0'));
    await page.locator('#registration-reserve').fill('6');
    assert.strictEqual(await page.locator('.registration-commands button').count(),0);
    await page.getByRole('button',{name:'Generate commands',exact:true}).click();
    assert((await page.locator('.registration-fields pre').first().innerText()).includes('--slt-vg-reserve-percent 6'));
    await page.locator('#registration-eager').fill('bad;command');
    await page.getByRole('button',{name:'Generate commands',exact:true}).click();
    assert(!(await page.getByRole('button',{name:'Copied',exact:true}).isVisible()));
    // A trailing line terminator must never become part of generated shell.
    const newlineIdentity=JSON.parse(JSON.stringify(observation));
    newlineIdentity.candidates[0].wwid += '\n';
    await page.locator('#registration-inventory').fill(JSON.stringify(newlineIdentity));
    await page.getByRole('button',{name:'Read report',exact:true}).click();
    assert.strictEqual(await page.locator('.registration-commands button').count(),0);
    const partialInvalid=JSON.parse(JSON.stringify(observation));
    partialInvalid.candidates.push({...partialInvalid.candidates[0],name:'bad\n'});
    await page.locator('#registration-inventory').fill(JSON.stringify(partialInvalid));
    await page.getByRole('button',{name:'Read report',exact:true}).click();
    assert((await page.locator('.registration-box').innerText()).includes('Invalid, incomplete or conflicting report'));
    assert.strictEqual(await page.locator('.registration-summary').innerText(),'','invalid report must not leave partial controls');
    const unrelatedPlus=JSON.parse(JSON.stringify(observation));
    unrelatedPlus.storage_definitions.push({id:'other',type:'lvm',vg_name:'vg+data'},{id:'archive',type:'lvm',vg_name:'.archive'});
    unrelatedPlus.candidates.push({...unrelatedPlus.candidates[0],name:'vg+data',vg_uuid:'ABCDEF-1234-1234-1234-1234-1234-ABCDEF',pv_uuid:'FEDCBA-4321-4321-4321-4321-4321-FEDCBA',wwid:'abcdef0123456789',eligible_for_new_vg_registration:false});
    await page.locator('#registration-inventory').fill(JSON.stringify(unrelatedPlus));await page.getByRole('button',{name:'Read report',exact:true}).click();
    assert.strictEqual(await page.locator('.registration-fields').isVisible(),true,'unrelated valid VG names must not reject eligible VG');
    assert(!(await page.locator('.registration-box').innerText()).includes('Invalid, incomplete or conflicting report'));
    for(const bad of [1234567890123456,['1234567890abcdef']]){
      const fixture=JSON.parse(JSON.stringify(observation));fixture.candidates[0].wwid=bad;
      await page.locator('#registration-inventory').fill(JSON.stringify(fixture));await page.getByRole('button',{name:'Read report',exact:true}).click();
      assert.strictEqual(await page.locator('.registration-commands button').count(),0,'non-string WWID must be refused');
    }
    const duplicate=JSON.parse(JSON.stringify(observation));
    duplicate.candidates[0].wwid='abcdef0123456789';
    duplicate.candidates.push({...duplicate.candidates[0],vg_uuid:'ABCDEF-1234-1234-1234-1234-1234-ABCDEF'});
    duplicate.candidates[1].eligible_for_new_vg_registration=false;
    duplicate.candidates[1].name='other_vg';
    duplicate.candidates[1].pv_uuid='FEDCBA-4321-4321-4321-4321-4321-FEDCBA';
    duplicate.candidates[1].wwid=duplicate.candidates[1].wwid.toUpperCase();
    await page.locator('#registration-inventory').fill(JSON.stringify(duplicate));await page.getByRole('button',{name:'Read report',exact:true}).click();
    assert((await page.locator('.registration-box').innerText()).includes('Invalid, incomplete or conflicting report'));
    const falseMapper=JSON.parse(JSON.stringify(observation));falseMapper.candidates[0].canonical_mapper_on_all_nodes=false;
    await page.locator('#registration-inventory').fill(JSON.stringify(falseMapper));await page.getByRole('button',{name:'Read report',exact:true}).click();
    assert.strictEqual(await page.locator('.registration-fields').isVisible(),false,'unconfirmed canonical mapper must not generate commands');
    const pvConflict=JSON.parse(JSON.stringify(observation));
    pvConflict.storage_definitions.push({id:'other',type:'sharedlvmthin',vg_name:'other_vg',pv_uuid:pvConflict.candidates[0].pv_uuid});
    await page.locator('#registration-inventory').fill(JSON.stringify(pvConflict));await page.getByRole('button',{name:'Read report',exact:true}).click();
    assert.strictEqual(await page.locator('.registration-fields').isVisible(),false,'matching PV UUID must prevent command generation');
    const almostExpired=JSON.parse(JSON.stringify(observation));almostExpired.observed_at=new Date(Date.now()-295000).toISOString();
    await page.locator('#registration-inventory').fill(JSON.stringify(almostExpired));await page.getByRole('button',{name:'Read report',exact:true}).click();
    await page.locator('#registration-eager').fill('bastrix-eager');
    await page.getByRole('button',{name:'Generate commands',exact:true}).click();
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:value=>new Promise(resolve=>{window.__finishExpiringCopy=()=>{window.__copied=value;resolve()}})},configurable:true}));
    await page.getByRole('button',{name:'Copy CLI Command — Eager',exact:true}).click();
    await page.waitForTimeout(5500);
    await page.evaluate(()=>window.__finishExpiringCopy());
    await page.waitForTimeout(0);
    assert.strictEqual(await page.locator('.registration-fields').isVisible(),false,'expired report must remove displayed command controls');
    assert((await page.locator('#guide-clipboard-warning').innerText()).includes('may contain an old command'));
    assert.strictEqual(await page.getByRole('button',{name:'Copied — Eager',exact:true}).count(),0);
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>{window.__copied=value}},configurable:true}));
    const badDefinition=JSON.parse(JSON.stringify(observation));
    badDefinition.storage_definitions.push({id:'other',type:'lvm',vg_name:'example_vg\n'});
    await page.locator('#registration-inventory').fill(JSON.stringify(badDefinition));await page.getByRole('button',{name:'Read report',exact:true}).click();
    assert((await page.locator('.registration-box').innerText()).includes('Invalid, incomplete or conflicting report'));
    for(const count of [64,65]){
      const fixture=JSON.parse(JSON.stringify(observation));for(const node of fixture.nodes)fixture.candidates[0].paths_by_node[node]=count;
      await page.locator('#registration-inventory').fill(JSON.stringify(fixture));await page.getByRole('button',{name:'Read report',exact:true}).click();
      assert.strictEqual(await page.locator('.registration-fields').isVisible(),count===64,`path count ${count}`);
    }
    // A successful first Eager registration followed by an unknown or failed
    // Lazy registration must not turn into an unverified Lazy-only command.
    const partialPair=JSON.parse(JSON.stringify(observation));
    partialPair.observed_at=new Date().toISOString();
    partialPair.storage_definitions.push({id:'bastrix-eager',type:'sharedlvmthin',vg_name:'example_vg'});
    partialPair.candidates[0].existing_aliases=[{id:'bastrix-eager',type:'sharedlvmthin',allocation_mode:'thick-generations'}];
    partialPair.candidates[0].eligible_for_new_vg_registration=false;
    await page.locator('#registration-inventory').fill(JSON.stringify(partialPair));
    await page.getByRole('button',{name:'Read report',exact:true}).click();
    assert((await page.locator('.registration-summary').innerText()).includes('bastrix-eager · sharedlvmthin · thick-generations'));
    assert.strictEqual(await page.locator('.registration-fields').isVisible(),false,'existing Eager alias must not allow Lazy-only generation');
    assert.strictEqual(await page.locator('.registration-commands button').count(),0);
    await page.getByRole('button',{name:'Copy inspection command — bastrix-eager',exact:true}).click();
    assert.strictEqual(await page.evaluate(()=>window.__copied),'pvesh get /storage/bastrix-eager --output-format json');
    await page.getByRole('button',{name:'Prepare continuation check',exact:true}).click();
    await page.getByRole('button',{name:'Copy continuation check',exact:true}).click();
    assert.strictEqual(await page.evaluate(()=>window.__copied),
      'sharedlvmthin-register-preview --vg example_vg --resume-existing bastrix-eager --missing-id bastrix-lazy');
    assert(!String(await page.evaluate(()=>window.__copied)).includes('pvesm add'));
    const missingId=page.getByLabel('Missing storage ID');
    await missingId.fill('lazy;bad');
    await page.getByRole('button',{name:'Prepare continuation check',exact:true}).click();
    assert(!(await page.getByRole('button',{name:'Copy continuation check',exact:true}).isVisible()));
    // Only the live collector can provide a server-validated partial preview.
    partialPair.candidates[0].resume_preview={existing_id:'bastrix-eager',missing_mode:'lazy',placeholder:'slt-resume-preview',config_sha256:'a'.repeat(64),existing_config:{storage:'bastrix-eager',type:'sharedlvmthin',shared:1,nodes:'node-a,node-b',content:'images,rootdir','slt-vgname':'example_vg','slt-vg-layout':'isolated','slt-allocation-mode':'thick-generations','slt-expected-vg-uuid':partialPair.candidates[0].vg_uuid,'slt-expected-pv-uuid':partialPair.candidates[0].pv_uuid,'slt-expected-wwid':partialPair.candidates[0].wwid,'slt-expected-min-paths':1,'slt-vg-reserve-percent':0},argv:[
      'pvesm','add','sharedlvmthin','slt-resume-preview','--slt-vgname','example_vg',
      '--slt-allocation-mode','thick-generations-lazy','--slt-vg-layout','isolated',
      '--slt-expected-vg-uuid',partialPair.candidates[0].vg_uuid,'--slt-expected-pv-uuid',partialPair.candidates[0].pv_uuid,
      '--slt-expected-wwid',partialPair.candidates[0].wwid,'--slt-expected-min-paths','1',
      '--slt-vg-reserve-percent','0','--nodes','node-a,node-b','--shared','1','--content','images,rootdir']};
    inventoryFixture=partialPair;
    await page.getByRole('button',{name:'Discover cluster',exact:true}).click();
    await page.getByRole('button',{name:'Prepare missing alias',exact:true}).click();
    await page.getByRole('button',{name:'Copy CLI Command — missing alias',exact:true}).click();
    const missingCommand=await page.evaluate(()=>window.__copied);
    assert(missingCommand.startsWith('pvesm add sharedlvmthin bastrix-lazy '));
    assert(missingCommand.includes('--slt-allocation-mode thick-generations-lazy'));
    await page.getByLabel('Missing storage ID').fill('lazy;bad');
    await page.getByRole('button',{name:'Prepare missing alias',exact:true}).click();
    assert(!(await page.getByRole('button',{name:'Copy CLI Command — missing alias',exact:true}).isVisible()));
    // Tampered server argv is not a command template, even on the live path.
    const unsafePreview=JSON.parse(JSON.stringify(partialPair));
    unsafePreview.candidates[0].resume_preview.argv.push('--unknown-option','value');
    inventoryFixture=unsafePreview;
    await page.getByRole('button',{name:'Discover cluster',exact:true}).click();
    assert.strictEqual(await page.getByRole('button',{name:'Prepare missing alias',exact:true}).count(),0);
    for(const [key,value] of [['--slt-lock-timeout','9'],['--slt-tg-region-size-kib','4097'],['--slt-vg-reserve-percent','51'],['--slt-expected-min-paths','65']]){
      const bad=JSON.parse(JSON.stringify(partialPair));const argv=bad.candidates[0].resume_preview.argv;
      const index=argv.indexOf(key);if(index<0)argv.push(key,value);else argv[index+1]=value;
      bad.candidates[0].resume_preview.existing_config[key.slice(2)]=Number(value);
      inventoryFixture=bad;await page.getByRole('button',{name:'Discover cluster',exact:true}).click();
      assert.strictEqual(await page.getByRole('button',{name:'Prepare missing alias',exact:true}).count(),0,`unsafe ${key}=${value}`);
    }
    const wrongConfig=JSON.parse(JSON.stringify(partialPair));
    wrongConfig.candidates[0].resume_preview.existing_config['slt-vg-reserve-percent']=5;
    inventoryFixture=wrongConfig;await page.getByRole('button',{name:'Discover cluster',exact:true}).click();
    assert.strictEqual(await page.getByRole('button',{name:'Prepare missing alias',exact:true}).count(),0,'argv/config parity mismatch');
    const newlinePreview=JSON.parse(JSON.stringify(partialPair));
    newlinePreview.candidates[0].resume_preview.argv.push('--slt-lock-timeout','30\n');
    inventoryFixture=newlinePreview;
    await page.getByRole('button',{name:'Discover cluster',exact:true}).click();
    assert.strictEqual(await page.getByRole('button',{name:'Prepare missing alias',exact:true}).count(),0);
    inventoryFixture=null;
    partialPair.candidates[0].empty_on_all_nodes=false;
    await page.locator('#registration-inventory').fill(JSON.stringify(partialPair));
    await page.getByRole('button',{name:'Read report',exact:true}).click();
    assert.strictEqual(await page.getByRole('button',{name:'Prepare continuation check',exact:true}).count(),0);
    observation.storage_definitions.push({id:'other',type:'lvm',vg_name:'example_vg'});
    observation.candidates[0].existing_aliases=[{id:'other',type:'lvm',allocation_mode:null}];
    observation.candidates[0].eligible_for_new_vg_registration=false;
    await page.locator('#registration-inventory').fill(JSON.stringify(observation));
    await page.getByRole('button',{name:'Read report',exact:true}).click();
    assert((await page.locator('.registration-box').innerText()).includes('no empty, unregistered VG'));
    assert((await page.locator('.registration-summary').innerText()).includes('other · lvm · unknown'));
    await page.getByRole('button',{name:'Copy inspection command — other',exact:true}).click();
    assert.strictEqual(await page.evaluate(()=>window.__copied),'pvesh get /storage/other --output-format json');
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:value=>new Promise(resolve=>{window.__finishHelperCopy=()=>{window.__copied=value;resolve()}})},configurable:true}));
    await page.getByRole('button',{name:'Copied — other',exact:true}).click();
    await page.locator('#registration-inventory').fill('{}');
    await page.evaluate(()=>window.__finishHelperCopy());
    await page.waitForTimeout(0);
    assert((await page.locator('#guide-clipboard-warning').innerText()).includes('may contain an old command'));
    assert.strictEqual(await page.getByRole('button',{name:'Copied — other',exact:true}).count(),0);
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>{window.__copied=value}},configurable:true}));
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
