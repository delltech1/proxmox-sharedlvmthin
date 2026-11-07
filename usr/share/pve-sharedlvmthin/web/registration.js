/* Read-only command composition. Imported inventory is not authenticated by
   this page and must never become authority to mutate storage. */
(() => {
  'use strict';
  // JavaScript's $ also matches before a trailing newline; require true EOF.
  const safeName = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}(?![\s\S])/;
  // Unrelated LVM VGs may contain '+'; they are displayed and checked for
  // conflicts, while generated command names keep the narrower grammar.
  const observedVgName = /^(?!\.{1,2}(?![\s\S]))[A-Za-z0-9_.+][A-Za-z0-9_.+-]{0,127}(?![\s\S])/;
  const lvmUuid = /^[A-Za-z0-9]{6}(?:-[A-Za-z0-9]{4}){5}-[A-Za-z0-9]{6}(?![\s\S])/;
  const wwidPattern = /^[0-9a-fA-F]{16,128}(?![\s\S])/;
  const resumeOptions=new Set(['--slt-vgname','--slt-allocation-mode','--slt-vg-layout','--slt-expected-vg-uuid','--slt-expected-pv-uuid','--slt-expected-wwid','--slt-expected-min-paths','--slt-vg-reserve-percent','--slt-vg-reserve-gib','--slt-lock-timeout','--slt-lock-yield-ms','--slt-bridge-admission-timeout','--slt-tg-close-timeout','--slt-tg-command-deadline-sec','--slt-tg-max-active-materializations','--slt-tg-region-size-kib','--slt-tg-hydration-timeout','--slt-tg-hydration-threshold','--slt-tg-hydration-batch-size','--slt-mutation-admission-timeout','--slt-tg-online-materialization','--nodes','--shared','--content']);
  const resumeNumeric={'slt-expected-min-paths':[1,64],'slt-vg-reserve-percent':[0,50],'slt-vg-reserve-gib':[0,1048576],'slt-lock-timeout':[10,86400],'slt-lock-yield-ms':[0,5000],'slt-bridge-admission-timeout':[60,604800],'slt-tg-close-timeout':[1,300],'slt-tg-command-deadline-sec':[5,600],'slt-tg-max-active-materializations':[1,64],'slt-tg-region-size-kib':[64,4096],'slt-tg-hydration-timeout':[60,86400],'slt-tg-hydration-threshold':[1,256],'slt-tg-hydration-batch-size':[1,256],'slt-mutation-admission-timeout':[10,86400]};
  function verifiedResumeArgv(candidate,alias,nodes){
    const p=candidate.resume_preview;
    if(!p||p.existing_id!==alias.id||typeof p.placeholder!=='string'||!safeName.test(p.placeholder)||!Array.isArray(p.argv)||p.argv.length<6||(p.argv.length-4)%2!==0||
       p.argv[0]!=='pvesm'||p.argv[1]!=='add'||p.argv[2]!=='sharedlvmthin'||p.argv[3]!==p.placeholder)return null;
    const flags=new Map();
    for(let i=4;i<p.argv.length;i+=2){const key=p.argv[i],value=p.argv[i+1];
      if(!resumeOptions.has(key)||flags.has(key)||typeof value!=='string'||!/^[A-Za-z0-9][A-Za-z0-9_.:,+-]*(?![\s\S])/.test(value))return null;
      flags.set(key,value);
    }
    const cfg=p.existing_config;
    if(!cfg||typeof cfg!=='object'||Array.isArray(cfg)||!(/^[0-9a-f]{64}(?![\s\S])/.test(p.config_sha256))||
       cfg.storage!==alias.id||cfg.type!=='sharedlvmthin'||![1,'1'].includes(cfg.shared)||
       (cfg.disable!==undefined&&![0,'0'].includes(cfg.disable))||cfg['slt-vgname']!==candidate.name||
       cfg['slt-vg-layout']!=='isolated'||cfg['slt-allocation-mode']!==alias.allocation_mode||
       cfg['slt-expected-vg-uuid']!==candidate.vg_uuid||cfg['slt-expected-pv-uuid']!==candidate.pv_uuid||
       cfg['slt-expected-wwid']!==candidate.wwid||typeof cfg.nodes!=='string'||
       cfg.nodes.split(',').sort().join(',')!==[...nodes].sort().join(',')||
       typeof cfg.content!=='string'||cfg.content.split(',').sort().join(',')!=='images,rootdir')return null;
    const cfgAllowed=new Set(['storage','type','shared','disable','nodes','content','slt-vgname','slt-vg-layout','slt-allocation-mode','slt-expected-vg-uuid','slt-expected-pv-uuid','slt-expected-wwid','slt-tg-online-materialization',...Object.keys(resumeNumeric)]);
    if(Object.keys(cfg).some(k=>!cfgAllowed.has(k)))return null;
    for(const [key,[min,max]] of Object.entries(resumeNumeric)){
      const present=Object.prototype.hasOwnProperty.call(cfg,key), flag=flags.get('--'+key);
      if(!present){if(flag!==undefined)return null;continue}
      const raw=String(cfg[key]);
      if(!/^(?:0|[1-9][0-9]*)(?![\s\S])/.test(raw)||!Number.isSafeInteger(Number(raw))||Number(raw)<min||Number(raw)>max||flag!==raw)return null;
    }
    const online=cfg['slt-tg-online-materialization'];
    if(online===undefined?flags.has('--slt-tg-online-materialization'):
       (!['asynchronous','synchronous'].includes(online)||flags.get('--slt-tg-online-materialization')!==online))return null;
    const mode=alias.allocation_mode==='thick-generations'?'lazy':'eager';
    const expectedMode=mode==='lazy'?'thick-generations-lazy':'thick-generations';
    if(p.missing_mode!==mode||flags.get('--slt-allocation-mode')!==expectedMode||flags.get('--slt-vg-layout')!=='isolated'||
       flags.get('--slt-vgname')!==candidate.name||flags.get('--slt-expected-vg-uuid')!==candidate.vg_uuid||
       flags.get('--slt-expected-pv-uuid')!==candidate.pv_uuid||flags.get('--slt-expected-wwid')!==candidate.wwid||
       flags.get('--nodes')!==[...nodes].sort().join(',')||flags.get('--shared')!=='1'||
       flags.get('--content')!=='images,rootdir'||(!flags.has('--slt-vg-reserve-percent')&&!flags.has('--slt-vg-reserve-gib')))return null;
    return [...p.argv];
  }
  const messages = {
    en: { title:'Build Thick registration commands', intro:'Discover the cluster read-only, or run the helper on a trusted node and paste its JSON report below. The dashboard does not register storage.', paste:'Paste discovery report', load:'Read report', vg:'Dedicated VG', mode:'Allocation mode', both:'Eager and Lazy', eager:'Eager only', lazy:'Lazy only', eagerId:'Eager storage ID', lazyId:'Lazy storage ID', reserve:'VG reserve (%)', generate:'Generate commands', copy:'Copy CLI Command', copied:'Copied', invalid:'Invalid, incomplete or conflicting report. Run discovery again and review the result.', noCandidates:'No empty, unregistered VG passed all checks. Existing or unverified LVs require review.', warning:'Imported reports are not authenticated by this page and can become stale. Verify the source and identities, re-run discovery before use, and review every command. Never use the reserve as a guarantee that a snapshot will fit.', once:'Observation and command generation do not guarantee VG identity or emptiness at execution time. Before each command, recheck the VG identity and state on every node, including whether it is empty and existing storage definitions. Eager and Lazy registrations are not atomic. If the result is partial or uncertain, inspect it; do not retry blindly.', helper:'sharedlvmthin-register-preview --discover-only', detected:'Detected in imported report', configured:'Already configured in imported report', unverified:'Unverified — review required', transport:'Observed transport', paths:'Healthy paths' },
    de: { title:'Thick-Registrierungsbefehle erstellen', intro:'Untersuchen Sie den Cluster lesend oder führen Sie den Helfer auf einem vertrauenswürdigen Knoten aus und fügen Sie den JSON-Bericht unten ein. Das Dashboard registriert keinen Storage.', paste:'Discovery-Bericht einfügen', load:'Bericht lesen', vg:'Dedizierte VG', mode:'Allokationsmodus', both:'Eager und Lazy', eager:'Nur Eager', lazy:'Nur Lazy', eagerId:'Eager-Storage-ID', lazyId:'Lazy-Storage-ID', reserve:'VG-Reserve (%)', generate:'Befehle erstellen', copy:'CLI-Befehl kopieren', copied:'Kopiert', invalid:'Ungültiger, unvollständiger oder widersprüchlicher Bericht. Discovery erneut ausführen und Ergebnis prüfen.', noCandidates:'Keine leere, unregistrierte VG hat alle Prüfungen bestanden. Vorhandene oder ungeprüfte LVs müssen geprüft werden.', warning:'Importierte Berichte werden von dieser Seite nicht authentifiziert und können veralten. Quelle und Identitäten prüfen, Discovery vor der Verwendung erneut ausführen und jeden Befehl kontrollieren. Eine Reserve garantiert keinen erfolgreichen Snapshot.', once:'Beobachtung und Befehlserzeugung garantieren weder VG-Identität noch Leere zum Ausführungszeitpunkt. Vor jedem Befehl VG-Identität und Zustand auf allen Knoten erneut prüfen, einschließlich Leere und vorhandener Storage-Definitionen. Eager- und Lazy-Registrierung sind nicht atomar. Bei teilweisem oder unklarem Ergebnis prüfen; nicht blind wiederholen.', helper:'sharedlvmthin-register-preview --discover-only', detected:'Im importierten Bericht erkannt', configured:'Im importierten Bericht bereits konfiguriert', unverified:'Nicht verifiziert — Prüfung erforderlich', transport:'Beobachteter Transport', paths:'Gesunde Pfade' }
  };
  function render(language, selectedTransport) {
    const t = messages[language === 'de' ? 'de' : 'en'];
    const el = (tag, text) => { const x=document.createElement(tag); if(text!==undefined)x.textContent=text; return x; };
    const box=el('div'); box.className='registration-box';
    box.append(el('h3',t.title),el('p',t.intro));
    const discover=el('button',language==='de'?'Cluster untersuchen':'Discover cluster'); discover.type='button'; discover.className='guide-next'; box.append(discover);
    const fallback=el('details');fallback.className='registration-fallback';
    fallback.append(el('summary',language==='de'?'Manuelle Discovery (optional)':'Manual discovery fallback (optional)'));
    const helper=el('pre',t.helper); helper.className='guide-command'; fallback.append(helper);
    const label=el('label',t.paste); label.htmlFor='registration-inventory'; fallback.append(label);
    const input=el('textarea'); input.id='registration-inventory'; input.rows=5; input.spellcheck=false; fallback.append(input);
    const load=el('button',t.load); load.type='button'; fallback.append(load);box.append(fallback);
    const state=el('p'); state.className='small warn'; box.append(state);
    const summary=el('div'); summary.className='registration-summary'; box.append(summary);
    const fields=el('div'); fields.className='registration-fields'; fields.hidden=true; box.append(fields);
    const addField=(title,id,element)=>{const l=el('label',title); l.htmlFor=id;element.id=id;const wrap=el('div');wrap.append(l,element);fields.append(wrap);return element;};
    const vgs=addField(t.vg,'registration-vg',el('select'));
    const mode=addField(t.mode,'registration-mode',el('select'));
    for(const [v,n] of [['both',t.both],['eager',t.eager],['lazy',t.lazy]]){const o=el('option',n);o.value=v;mode.append(o)}
    const eager=addField(t.eagerId,'registration-eager',el('input')); eager.value='bastrix-eager';
    const lazy=addField(t.lazyId,'registration-lazy',el('input')); lazy.value='bastrix-lazy';
    const reserve=addField(t.reserve,'registration-reserve',el('input')); reserve.type='number';reserve.min='0';reserve.max='50';reserve.value='0';
    const zeroWarning=el('div');zeroWarning.className='registration-zero-warning';zeroWarning.hidden=false;
    const zeroText=el('p',language==='de'?'Ohne geschützte Reserve können die freien VG-Extents vollständig verbraucht werden. Snapshots, Rollback oder Vergrößerungen können dann scheitern. Eine Reserve, zum Beispiel 5 %, ist optional.':'Without a protected reserve, free VG extents can be exhausted. Snapshots, rollback or disk growth may then be refused. A reserve, for example 5%, is optional.');zeroText.className='small warn';
    zeroWarning.append(zeroText);fields.append(zeroWarning);
    const generate=el('button',t.generate);generate.type='button';fields.append(generate);
    const warning=el('p',t.warning);warning.className='notice';fields.append(warning);
    const output=el('div');output.className='registration-commands';output.hidden=true;fields.append(output);
    const once=el('p',t.once);once.className='small muted';fields.append(once);
    const partial=el('p',language==='de'?'Eager und Lazy sind getrennte Änderungen: Die erste kann erfolgreich sein, obwohl die zweite fehlschlägt. Bei Unterbrechung oder unklarem Ergebnis bestehende Definitionen prüfen und Discovery erneut ausführen; nicht blind wiederholen.':'Eager and Lazy are separate changes: the first may succeed even if the second fails. After interruption or an uncertain result, inspect existing definitions and run discovery again; do not retry blindly.');partial.className='small warn';fields.append(partial);
    let report=null, source='imported', discoveryGeneration=0, expiryTimer=null, commandGeneration=0, copySequence=0;
    const invalidate=()=>{commandGeneration++;window.BastrixGuide.invalidateClipboard();output.hidden=true;output.replaceChildren();};
    for(const element of [vgs,mode,eager,lazy,reserve]){
      element.addEventListener('input',invalidate);
      element.addEventListener('change',invalidate);
    }
    reserve.addEventListener('input',()=>{zeroWarning.hidden=reserve.valueAsNumber!==0;});
    input.addEventListener('input',()=>{discoveryGeneration++;source='imported';report=null;fields.hidden=true;state.textContent='';summary.replaceChildren();invalidate();});
    discover.onclick=async()=>{
      const generation=++discoveryGeneration;
      report=null; fields.hidden=true; summary.replaceChildren(); invalidate();
      discover.disabled=true; state.textContent=language==='de'?'Lese Cluster-Daten …':'Reading cluster inventory …';
      try{
        const response=await fetch('/api/registration/inventory',{cache:'no-store'});
        if(!response.ok){
          let error={};try{error=await response.json()}catch{}
          if(generation!==discoveryGeneration)return;
          const hints=language==='de'?{peer_ssh_unverified:'Peer-SSH nicht bestätigt. Prüfen Sie Erreichbarkeit, root-Anmeldung und unabhängig verifizierte PVE-Hostschlüssel in jeder Richtung. Keine Hostschlüsselprüfung deaktivieren.',quorum_unverified:'Cluster-Quorum nicht bestätigt. Prüfen Sie pvecm status, bevor Sie fortfahren.',nodes_unverified:'Cluster-Mitgliedschaft unvollständig oder ein Knoten nicht verfügbar. Prüfen Sie den Knotenstatus.'}:{peer_ssh_unverified:'Peer SSH not confirmed. Check reachability, root authentication and independently verified PVE host keys in every direction. Do not disable host-key checking.',quorum_unverified:'Cluster quorum not confirmed. Check pvecm status before continuing.',nodes_unverified:'Cluster membership incomplete or a node is unavailable. Check node status.'};
          state.textContent=hints[error.code]||(language==='de'?'Automatische Discovery nicht verfügbar. Führen Sie den Helfer manuell aus.':'Automatic discovery unavailable. Run the helper manually.');return;
        }
        const data=await response.json();
        if(generation!==discoveryGeneration)return;
        input.value=JSON.stringify(data,null,2); source='collector'; load.click();
      }catch{if(generation===discoveryGeneration)state.textContent=language==='de'?'Automatische Discovery nicht verfügbar. Führen Sie den Helfer manuell aus.':'Automatic discovery unavailable. Run the helper manually.'}
      finally{discover.disabled=false}
    };
    load.onclick=()=>{
      discoveryGeneration++;
      if(expiryTimer!==null){clearTimeout(expiryTimer);expiryTimer=null}
      report=null; fields.hidden=true; invalidate(); state.textContent='';vgs.replaceChildren();summary.replaceChildren();
      try{
        if(input.value.length>1000000)throw Error();
        const data=JSON.parse(input.value);
        if(!data||data.result!=='OBSERVED'||!Array.isArray(data.nodes)||!data.nodes.length||!data.nodes.every(n=>typeof n==='string'&&safeName.test(n))||new Set(data.nodes).size!==data.nodes.length||!Array.isArray(data.candidates)||!Array.isArray(data.storage_definitions))throw Error();
        if(typeof data.observed_at!=='string')throw Error();
        const observed=Date.parse(data.observed_at); if(!Number.isFinite(observed)||observed>Date.now()+120000||Date.now()-observed>300000)throw Error();
        if(!data.storage_definitions.every(d=>d&&typeof d.id==='string'&&safeName.test(d.id)&&typeof d.type==='string'&&safeName.test(d.type)&&!( ['sharedlvmthin','lvm','lvmthin'].includes(d.type) && (typeof d.vg_name!=='string'||!observedVgName.test(d.vg_name)) )&&(d.vg_uuid===undefined||d.vg_uuid===null||(typeof d.vg_uuid==='string'&&lvmUuid.test(d.vg_uuid)))&&(d.pv_uuid===undefined||d.pv_uuid===null||(typeof d.pv_uuid==='string'&&lvmUuid.test(d.pv_uuid)))&&(d.wwid===undefined||d.wwid===null||(typeof d.wwid==='string'&&wwidPattern.test(d.wwid)))))throw Error();
        if(new Set(data.storage_definitions.map(d=>d.id)).size!==data.storage_definitions.length)throw Error();
        // Contradictory candidates must not disappear merely because one fails
        // the later eligibility filter. Compare WWIDs case-insensitively.
        for(const key of ['name','vg_uuid','pv_uuid','wwid']){
          const values=data.candidates.filter(c=>c&&typeof c[key]==='string').map(c=>key==='wwid'||key==='vg_uuid'?c[key].toLowerCase():c[key]);
          if(new Set(values).size!==values.length)throw Error();
        }
        const expectedTransport={iscsi:'iscsi',sas:'sas',fc:'fc-or-fcoe',fcoe:'fc-or-fcoe'}[selectedTransport];
        const sameFold=(a,b)=>typeof a==='string'&&typeof b==='string'&&a.toLowerCase()===b.toLowerCase();
        const candidateEligible=c=>!!(c&&expectedTransport&&c.transport_by_node&&data.nodes.every(n=>c.transport_by_node[n]===expectedTransport)&&c.eligible_for_new_vg_registration===true&&c.empty_on_all_nodes===true&&c.canonical_mapper_on_all_nodes===true&&Array.isArray(c.existing_aliases)&&c.existing_aliases.length===0&&!data.storage_definitions.some(d=>d.vg_name===c.name||sameFold(d.vg_uuid,c.vg_uuid)||(typeof d.pv_uuid==='string'&&d.pv_uuid===c.pv_uuid)||sameFold(d.wwid,c.wwid))&&typeof c.name==='string'&&safeName.test(c.name)&&typeof c.vg_uuid==='string'&&lvmUuid.test(c.vg_uuid)&&typeof c.pv_uuid==='string'&&lvmUuid.test(c.pv_uuid)&&typeof c.wwid==='string'&&wwidPattern.test(c.wwid)&&Number.isSafeInteger(c.size_bytes)&&Number.isSafeInteger(c.free_bytes)&&c.size_bytes>0&&c.free_bytes>=0&&c.free_bytes<=c.size_bytes&&c.paths_by_node&&data.nodes.every(n=>Number.isSafeInteger(c.paths_by_node[n])&&c.paths_by_node[n]>0)&&Math.min(...data.nodes.map(n=>c.paths_by_node[n]))<=64);
        const reasonLabels=language==='de'?{vg_not_visible_on_every_node:'Eine VG ist nicht auf allen Knoten sichtbar.',identity_mismatch:'Die Storage-Identität stimmt zwischen Knoten nicht überein.',healthy_paths_unverified:'Gesunde Pfade konnten nicht auf allen Knoten bestätigt werden.'}:{vg_not_visible_on_every_node:'A VG is not visible on every node.',identity_mismatch:'Storage identity differs between nodes.',healthy_paths_unverified:'Healthy paths could not be confirmed on every node.'};
        if(data.unverified_reasons!==undefined){if(!Array.isArray(data.unverified_reasons)||data.unverified_reasons.some(reason=>!Object.prototype.hasOwnProperty.call(reasonLabels,reason)))throw Error();for(const reason of data.unverified_reasons){const row=el('div');row.className='registration-observation';row.append(el('strong',t.unverified),el('span',reasonLabels[reason]));summary.append(row)}}
        for(const c of data.candidates){
          if(!c||typeof c.name!=='string'||!observedVgName.test(c.name))throw Error();
          const row=el('div'); row.className='registration-observation';
          const status=Array.isArray(c.existing_aliases)&&c.existing_aliases.length
            ? (language==='de'?'Vorhandene Definition gefunden — Kompatibilität prüfen':'Existing definition found — verify compatibility')
            : candidateEligible(c)
              ? (source==='collector'?(language==='de'?'Erkannt':'Detected'):t.detected)
              : t.unverified;
          row.append(el('strong',c.name),el('span',status));
          const identityLabel=language==='de'?'Beobachtete Identität':'Observed identity';
          const display=(value,pattern)=>typeof value==='string'&&pattern.test(value)?value:'unknown';
          row.append(el('span',`${identityLabel}: VG UUID ${display(c.vg_uuid,lvmUuid)} · PV UUID ${display(c.pv_uuid,lvmUuid)} · WWID ${display(c.wwid,wwidPattern)}`));
          if(Number.isSafeInteger(c.size_bytes)&&Number.isSafeInteger(c.free_bytes)&&c.size_bytes>0&&c.free_bytes>=0&&c.free_bytes<=c.size_bytes)row.append(el('span',`${language==='de'?'Freie / gesamte Kapazität':'Free / total capacity'}: ${(c.free_bytes/1073741824).toFixed(2)} / ${(c.size_bytes/1073741824).toFixed(2)} GiB`));
          if(Array.isArray(c.existing_aliases)&&c.existing_aliases.length){
            for(const alias of c.existing_aliases){
              const id=alias&&typeof alias==='object'?alias.id:alias;
              const kind=alias&&typeof alias==='object'&&typeof alias.type==='string'?alias.type:'unknown';
              const allocation=alias&&typeof alias==='object'&&typeof alias.allocation_mode==='string'?alias.allocation_mode:'unknown';
              row.append(el('span',`${language==='de'?'Definition':'Definition'}: ${typeof id==='string'?id:'?'} · ${kind} · ${allocation}`));
              if(typeof id==='string'&&safeName.test(id)){
                const command=`pvesh get /storage/${id} --output-format json`;
                const inspect=el('button',`${language==='de'?'Prüfbefehl kopieren':'Copy inspection command'} — ${id}`);inspect.type='button';
                inspect.onclick=()=>window.BastrixGuide.copyCommand(command,inspect,state,`${t.copied} — ${id}`);
                row.append(inspect);
              }
            }
            row.append(el('span',language==='de'?'Prüfen Sie die vollständige Konfiguration und den Zustand. Ein gefundener Eintrag bestätigt keine erfolgreiche Registrierung oder Kompatibilität.':'Inspect the full configuration and health. A found entry does not prove successful registration or compatibility.'));
            // This command only requests a fresh read-only CLI preview. It
            // does not reuse the browser report as proof or create storage.
            const alias=c.existing_aliases.length===1?c.existing_aliases[0]:null;
            if(c.empty_on_all_nodes===true&&c.canonical_mapper_on_all_nodes===true&&safeName.test(c.name)&&
               alias&&typeof alias==='object'&&typeof alias.id==='string'&&safeName.test(alias.id)&&alias.type==='sharedlvmthin'&&
               ['thick-generations','thick-generations-lazy'].includes(alias.allocation_mode)){
              const label=el('label',language==='de'?'Name des fehlenden Storage':'Missing storage ID');
              const field=el('input');field.type='text';field.id=`registration-resume-${alias.id}`;label.htmlFor=field.id;field.value=alias.allocation_mode==='thick-generations'?'bastrix-lazy':'bastrix-eager';
              const template=source==='collector'?verifiedResumeArgv(c,alias,data.nodes):null;
              const make=el('button',template?(language==='de'?'Fehlenden Alias vorbereiten':'Prepare missing alias'):(language==='de'?'Fortsetzungsprüfung vorbereiten':'Prepare continuation check'));make.type='button';
              const out=el('pre');out.className='guide-command';out.hidden=true;
              const copy=el('button',template?(language==='de'?'CLI-Befehl kopieren — fehlender Alias':'Copy CLI Command — missing alias'):(language==='de'?'Fortsetzungsprüfung kopieren':'Copy continuation check'));copy.type='button';copy.hidden=true;
              const resumeGeneration=discoveryGeneration;
              let command='';const clear=()=>{window.BastrixGuide.invalidateClipboard();command='';out.textContent='';out.hidden=true;copy.hidden=true;};
              field.addEventListener('input',clear);
              const fresh=()=>resumeGeneration===discoveryGeneration&&Date.now()-observed<=300000&&observed<=Date.now()+120000&&row.isConnected;
              make.onclick=()=>{clear();if(!fresh()||!safeName.test(field.value)||data.storage_definitions.some(d=>d.id===field.value)){state.textContent=t.invalid;return}
                if(template){const argv=[...template];argv[3]=field.value;command=argv.join(' ')}
                else command=`sharedlvmthin-register-preview --vg ${c.name} --resume-existing ${alias.id} --missing-id ${field.value}`;
                out.textContent=command;out.hidden=false;copy.hidden=false;
                state.textContent=template?(language==='de'?'Nur Vorschau – vor der Ausführung erneut prüfen.':'Preview only — recheck before execution.'):(language==='de'?'Nur lesende Prüfung: Der Helfer prüft erneut und erstellt höchstens eine Vorschau.':'Read-only check: the helper rechecks and produces a preview only.');};
              copy.onclick=()=>window.BastrixGuide.copyCommand(command,copy,state,undefined,fresh);
              row.append(el('span',language==='de'?'Bei einer unterbrochenen Registrierung kann der Helfer die vollständige Konfiguration erneut prüfen. Das gilt nur für leere VGs.':'After partial registration, the helper can recheck the full configuration. This is limited to empty VGs.'),label,field,make,out,copy);
            }
          }
          if(c.paths_by_node&&typeof c.paths_by_node==='object')row.append(el('span',`${t.paths}: ${Object.entries(c.paths_by_node).map(([n,p])=>`${n} ${p}`).join(', ')}`));
          if(c.transport_by_node&&typeof c.transport_by_node==='object')row.append(el('span',`${t.transport}: ${Object.entries(c.transport_by_node).map(([n,p])=>`${n} ${p}`).join(', ')}`));
          summary.append(row);
        }
        const loadedGeneration=discoveryGeneration;
        expiryTimer=setTimeout(()=>{if(discoveryGeneration!==loadedGeneration)return;report=null;fields.hidden=true;invalidate();summary.replaceChildren();state.textContent=t.invalid},Math.max(1,observed+300001-Date.now()));
        const candidates=data.candidates.filter(candidateEligible);
        if(!candidates.length){state.textContent=language==='de'?'Nicht verifiziert: Keine leere, unregistrierte VG für den ausgewählten Transport wurde auf allen Knoten bestätigt. Prüfen Sie Identitäten, vorhandene Definitionen und Transport.':'Unverified: no empty, unregistered VG for the selected transport was confirmed on every node. Review identities, existing definitions and transport.';return}
        for(const key of ['name','vg_uuid','pv_uuid','wwid'])if(new Set(candidates.map(c=>c[key])).size!==candidates.length)throw Error();
        report={nodes:[...data.nodes].sort(),candidates,storage:data.storage_definitions,observed};
        warning.textContent=source==='collector'
          ? (language==='de'?'Dieser lesende Bericht ist eine Momentaufnahme und kann veralten. Vor Ausführung erneut prüfen; eine Reserve garantiert keinen Snapshot.':'This read-only report is a point-in-time observation and can become stale. Recheck before executing; a reserve does not guarantee a snapshot.')
          : t.warning;
        if(selectedTransport==='fc'||selectedTransport==='fcoe')warning.textContent+=language==='de'?' Die FC-SCSI-Kennung unterscheidet FC nicht von FCoE; Transport und Fabric unabhängig verifizieren.':' The FC SCSI label does not distinguish FC from FCoE; independently verify the actual transport and fabric.';
        for(const c of candidates){const o=el('option',`${c.name} · ${(c.free_bytes/1073741824).toFixed(2)} GiB free`);o.value=c.vg_uuid;vgs.append(o)}
        fields.hidden=false;
      }catch{if(expiryTimer!==null){clearTimeout(expiryTimer);expiryTimer=null}report=null;fields.hidden=true;invalidate();vgs.replaceChildren();summary.replaceChildren();state.textContent=t.invalid}
    };
    generate.onclick=()=>{
      invalidate();state.textContent='';
      try{
        if(!report||Date.now()-report.observed>300000||report.observed>Date.now()+120000)throw Error();
        const c=report.candidates.find(x=>x.vg_uuid===vgs.value); const n=Number(reserve.value);
        const ids=mode.value==='both'?[eager.value,lazy.value]:[mode.value==='eager'?eager.value:lazy.value];
        if(!c||!/^\d+$/.test(reserve.value)||!Number.isInteger(n)||n<0||n>50||ids.some(id=>!safeName.test(id))||new Set(ids).size!==ids.length||report.storage.some(d=>ids.includes(d.id)))throw Error();
        const count=Math.min(...report.nodes.map(node=>c.paths_by_node[node]));
        const make=(id,m)=>['pvesm','add','sharedlvmthin',id,'--slt-vgname',c.name,'--slt-allocation-mode',m,'--slt-vg-layout','isolated','--slt-expected-vg-uuid',c.vg_uuid,'--slt-expected-pv-uuid',c.pv_uuid,'--slt-expected-wwid',c.wwid,'--slt-expected-min-paths',String(count),'--slt-vg-reserve-percent',String(n),'--nodes',report.nodes.join(','),'--shared','1','--content','images,rootdir'].join(' ');
        const commands=[];if(mode.value!=='lazy')commands.push(['Eager',make(eager.value,'thick-generations')]);if(mode.value!=='eager')commands.push(['Lazy',make(lazy.value,'thick-generations-lazy')]);
        for(const [name,command] of commands){
          const section=el('section');section.className='registration-command';
          const heading=el('h4',`${name} — ${language==='de'?'separate Registrierung':'separate registration'}`);
          const out=el('pre',command);out.className='guide-command';
          const copy=el('button',`${t.copy} — ${name}`);copy.type='button';
          copy.onclick=async()=>{try{const captured=report, generation=commandGeneration, sequence=++copySequence;const current=()=>report===captured&&generation===commandGeneration&&sequence===copySequence;const valid=()=>!!captured&&current()&&Date.now()-captured.observed<=300000&&captured.observed<=Date.now()+120000;if(!valid())throw Error();const result=await window.BastrixGuide.copyCommand(command,copy,state,`${t.copied} — ${name}`,valid);if(result==='stale'&&current()&&box.isConnected){invalidate();state.textContent=language==='de'?'Zwischenablage kann veraltet sein. Discovery erneut ausführen und Befehl neu erstellen.':'Clipboard may be stale. Run discovery again and regenerate the command.'}}catch{invalidate();state.textContent=t.invalid}};
          section.append(heading,out,copy);output.append(section);
        }
        output.hidden=false;
      }catch{state.textContent=t.invalid}
    };
    return box;
  }
  window.BastrixRegistration={render};
})();

