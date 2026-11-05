/* Translate interface-owned labels only; never rewrite storage IDs or diagnostic data. */
(() => {
  'use strict';
  const de={
    'Storage guide':'Storage-Anleitung','Guidance':'Anleitung','Storage setup guide':'Storage-Einrichtung','Shared storage setup guide':'Anleitung für gemeinsam genutzten Speicher',
    'Monitoring':'Monitoring','Overview':'Übersicht','Storage':'Storage','Paths':'Pfade','Node':'Knoten','Health':'Status',
    'Validation':'Prüfung','Deep Test':'Tiefenprüfung','Refresh':'Aktualisieren','CLUSTER':'CLUSTER','STORAGE':'STORAGE',
    'PATHS':'PFADE','COMPATIBILITY':'KOMPATIBILITÄT','SharedLvmThin Storage':'SharedLvmThin Storage','Node Health':'Knotenstatus',
    'Health Checks':'Statusprüfungen','Storage Paths':'Storage-Pfade','Multipath and transport health':'Multipath- und Transportstatus',
    'Local Proxmox host health':'Status des lokalen Proxmox-Hosts','Non-destructive read-only validation':'Nicht destruktive schreibgeschützte Prüfung',
    'Run Health Check':'Statusprüfung ausführen','Deep Validation':'Tiefenprüfung','Privileged write validation':'Schreibprüfung mit erhöhten Rechten',
    'CLI only':'Nur CLI','Deep validation performs temporary write operations, snapshot creation, rollback and cleanup.':'Die Tiefenprüfung führt temporäre Schreibvorgänge sowie Snapshot-Erstellung, Rollback und Bereinigung aus.',
    'For security reasons it cannot be started from this dashboard.':'Aus Sicherheitsgründen kann sie nicht über dieses Dashboard gestartet werden.',
    'READ ONLY':'SCHREIBGESCHÜTZT','Logout':'Abmelden','SharedLvmThin Health Dashboard · read-only':'SharedLvmThin Status-Dashboard · schreibgeschützt',
    'Capacity':'Kapazität','Free':'Frei','Mode':'Modus','Materialization slots':'Materialisierungsplätze','Thick volumes':'Thick-Volumes',
    'Thin pools':'Thin-Pools','Hostname':'Hostname','Uptime':'Betriebszeit','Load':'Last','Memory':'Arbeitsspeicher','Root FS':'Root-Dateisystem',
    'dm-clone throttle':'dm-clone-Drosselung','Volume':'Volume','Phase':'Phase','Operation':'Vorgang','Generation':'Generation','PVE references':'PVE-Referenzen',
    'Status':'Status','Reason':'Grund','Pool':'Pool','Reservation':'Reservierung','Payload':'Nutzdaten','Reserved slack':'Reservierter Freiraum',
    'Metadata %':'Metadaten %','Monitor':'Monitor','Ownership':'Eigentümerschaft','State':'Status','Healthy paths':'Funktionsfähige Pfade',
    'Vendor / Product':'Hersteller / Produkt','Active iSCSI sessions':'Aktive iSCSI-Sitzungen','Materialization admission:':'Materialisierungsfreigabe:',
    'This health check is read-only. It does not create, modify or delete storage objects.':'Diese Statusprüfung ist schreibgeschützt. Sie erstellt, ändert und löscht keine Storage-Objekte.',
    'No SharedLvmThin storage detected.':'Kein SharedLvmThin Storage erkannt.',
    'Detailed system diagnostics are shown in their original language.':'Detaillierte Systemdiagnosen werden in ihrer Originalsprache angezeigt.',
    'Each listed anchor describes one independently allocated Thick Generations volume. Monitoring is read-only; warnings never trigger automatic cleanup or repair.':'Jeder aufgeführte Anker beschreibt ein unabhängig zugewiesenes Thick-Generations-Volume. Die Überwachung ist schreibgeschützt; Warnungen lösen keine automatische Bereinigung oder Reparatur aus.',
    'Pool reservation is physical VG space unavailable to other VM pools. Payload is the approximate allocated data inside that reservation; reserved slack is not returned to the VG automatically.':'Die Pool-Reservierung belegt physischen VG-Speicher, der anderen VM-Pools nicht zur Verfügung steht. Nutzdaten sind die ungefähr zugewiesenen Daten; reservierter Freiraum wird nicht automatisch an die VG zurückgegeben.'
  };
  const original=new WeakMap();
  const selectors=['nav .nt','nav .ni','#guideShortcut','header button[title]','header .ro','main section .pt h1','main section .pt .sub:not(#clusterName):not(#storageModeSub)','main section .pt button','#overview .cl','#overview > .grid .ph','#deep .deep h3','#deep .deep p','footer','#diagnosticLanguage','#storageOverview .k','#nodeOverview .k','#nodePage .k','#storagePage th','#pathsPage .k','#healthPage .notice','#storagePage .notice b','#storagePage .muted','#storageOverview > .muted','#pathsPage .small.muted'];
  function change(el,lang){
    if(el.children.length)return;
    let old=original.get(el);
    if(old===undefined){old=el.textContent;original.set(el,old);}
    const target=lang==='de'?(de[old.trim()]||old):old;
    if(el.textContent!==target)el.textContent=target;
  }
  function translateDynamic(lang){
    const api=document.getElementById('api');
    if(lang!=='de'){
      for(const [id,deText,enText] of [['clusterName','Wird geladen...','Loading...'],['storageModeSub','Paketprofil wird geladen...','Loading package profile...']]){const el=document.getElementById(id);if(el&&el.textContent===deText)el.textContent=enText;}
      if(api)for(const child of api.childNodes){if(child.nodeType===Node.TEXT_NODE){const old=child.nodeValue;const next=old.replace(/(\d+) s alt/,'$1s old').replace(/^Aktualisierung läuft$/,'Refreshing');if(next!==old)child.nodeValue=next;}else if(child.nodeType===Node.ELEMENT_NODE&&child.children.length===0){const map={'● API veraltet':'● API stale','● API funktionsfähig':'● API healthy','● API-Fehler':'● API error'};if(map[child.textContent])child.textContent=map[child.textContent];}}
      return;
    }
    const set=(id,transform)=>{const el=document.getElementById(id);if(!el)return;const old=el.textContent;const next=transform(old);if(next!==old)el.textContent=next;};
    set('clusterName',s=>s==='Loading...'?'Wird geladen...':s);
    set('storageModeSub',s=>s==='Loading package profile...'?'Paketprofil wird geladen...':s==='Thick Generations only'?'Nur Thick Generations':s==='Thin pools and Thick Generations'?'Thin-Pools und Thick Generations':s);
    set('clusterSub',s=>s.replace(/^(\d+) node\(s\) · quorum (yes|no)$/,(_,n,q)=>`${n} Knoten · Quorum ${q==='yes'?'ja':'nein'}`));
    set('storageSub',s=>s.replace(/^(\d+) configured$/,(_,n)=>`${n} konfiguriert`));
    set('pathSub',s=>s.replace(/^(\d+) healthy path\(s\) · (\d+) storage unavailable$/,(_,n,m)=>`${n} funktionsfähige Pfade · ${m} Storage nicht verfügbar`).replace(/^(\d+) healthy path\(s\)$/,(_,n)=>`${n} funktionsfähige Pfade`).replace(/^No paths detected$/,'Keine Pfade erkannt'));
    set('updated',s=>s.replace(/^Updated /,'Aktualisiert ').replace(/ · collected in (\d+) ms$/,' · erfasst in $1 ms'));
    for(const id of ['clusterHealth','storageHealth','compatHealth']){
      const el=document.querySelector(`#${id} span`);if(!el)continue;
      const map={HEALTHY:'GESUND',CRITICAL:'KRITISCH',DEGRADED:'BEEINTRÄCHTIGT',EXACT:'EXAKT',COMPAT:'KOMPATIBEL'};
      if(map[el.textContent])el.textContent=map[el.textContent];
    }
    for(const el of document.querySelectorAll('#storageOverview .storage-block .sub')){
      const old=el.textContent;const next=old.replace(/^VG usage (\d+)% · shared VG aliases report the same physical capacity and must not be summed$/,'VG-Nutzung $1% · gemeinsame VG-Aliase zeigen dieselbe physische Kapazität und dürfen nicht addiert werden');if(next!==old)el.textContent=next;
    }
    for(const label of document.querySelectorAll('#nodePage .k,#nodeOverview .k'))if(label.textContent==='dmeventd'&&label.nextElementSibling){const value=label.nextElementSibling;const map={ACTIVE:'AKTIV',INACTIVE:'INAKTIV'};if(map[value.textContent])value.textContent=map[value.textContent];}
    for(const label of document.querySelectorAll('#pathsPage .k'))if(label.textContent==='Multipath'&&label.nextElementSibling){const value=label.nextElementSibling;const map={Yes:'Ja',No:'Nein'};if(map[value.textContent])value.textContent=map[value.textContent];}
    for(const el of document.querySelectorAll('#pathsPage .path > div:last-child')){const map={READY:'BEREIT',FAILED:'FEHLER'};if(map[el.textContent])el.textContent=map[el.textContent];}
    for(const table of document.querySelectorAll('#storagePage table'))if([...table.querySelectorAll('th')].some(el=>el.textContent==='Eigentümerschaft'))for(const el of table.querySelectorAll('tbody tr td:last-child')){const map={Owned:'Zugeordnet',Unowned:'Nicht zugeordnet'};if(map[el.textContent])el.textContent=map[el.textContent];}
    for(const el of document.querySelectorAll('#storagePage .ph')){const old=el.textContent;const next=old.replace(/ · Thin pools$/,' · Thin-Pools');if(next!==old)el.textContent=next;}
    for(const label of document.querySelectorAll('#storageOverview .k'))if(label.textContent==='Modus'&&label.nextElementSibling&&label.nextElementSibling.textContent==='Thin pools')label.nextElementSibling.textContent='Thin-Pools';
    for(const label of document.querySelectorAll('#storageOverview .k'))if(label.textContent==='Materialisierungsplätze'&&label.nextElementSibling)for(const child of label.nextElementSibling.childNodes){if(child.nodeType!==Node.TEXT_NODE)continue;const old=child.nodeValue;const next=old.replace(/ active · /,' aktiv · ').replace(/ free$/,' frei');if(next!==old)child.nodeValue=next;}
    for(const el of document.querySelectorAll('#storagePage .notice'))for(const child of el.childNodes){if(child.nodeType!==Node.TEXT_NODE)continue;const old=child.nodeValue;const next=old.replace(/ active · /,' aktiv · ').replace(/ free\. UNKNOWN never authorizes a new transition\./,' frei. UNKNOWN erlaubt niemals eine neue Transition.');if(next!==old)child.nodeValue=next;}
    if(api){for(const child of api.childNodes){if(child.nodeType===Node.TEXT_NODE){const old=child.nodeValue;const next=old.replace(/(\d+)s old/,'$1 s alt').replace(/^Refreshing$/,'Aktualisierung läuft');if(next!==old)child.nodeValue=next;}else if(child.nodeType===Node.ELEMENT_NODE&&child.children.length===0){const map={'● API stale':'● API veraltet','● API healthy':'● API funktionsfähig','● API error':'● API-Fehler'};if(map[child.textContent])child.textContent=map[child.textContent];}}}
  }
  function apply(lang){
    for(const el of document.querySelectorAll(selectors.join(',')))change(el,lang);
    const title=document.getElementById('sanGuideTitle');const wanted=lang==='de'?'Anleitung für gemeinsam genutzten Speicher':'Shared storage setup guide';if(title&&title.textContent!==wanted)title.textContent=wanted;
    document.documentElement.lang=lang;
    const logout=document.querySelector('header button[title]');if(logout)logout.title=lang==='de'?'Abmelden':'Sign out';
    translateDynamic(lang);
  }
  let language='en';try{language=localStorage.getItem('bastrix-language')==='de'?'de':'en'}catch{}
  const select=document.getElementById('pageLanguage');select.value=language;
  function choose(lang){language=lang==='de'?'de':'en';try{localStorage.setItem('bastrix-language',language)}catch{};if(typeof D!=='undefined'&&D)render();apply(language);window.BastrixGuide.setLanguage(language);}
  select.addEventListener('change',()=>choose(select.value));
  document.getElementById('guideShortcut').addEventListener('click',()=>document.querySelector('[data-page="sanGuide"]').click());
  let pending=false;
  const observer=new MutationObserver(()=>{if(pending)return;pending=true;queueMicrotask(()=>{pending=false;apply(language)})});
  observer.observe(document.querySelector('main'),{childList:true,subtree:true});
  observer.observe(document.querySelector('header'),{childList:true,subtree:true,characterData:true});
  choose(language);
})();
