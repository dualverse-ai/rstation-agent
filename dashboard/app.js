'use strict';
const $=id=>document.getElementById(id);
let snapshot=null,selected=null;
const names={working:'Working',transient_error:'Transient error',needs_attention:'Needs attention',intentional:'Retired'};
const tones={working:'working',transient_error:'degraded',needs_attention:'down',intentional:'intentional'};
function countsFor(rows){const c={working:0,transient_error:0,needs_attention:0,intentional:0};for(const w of rows)c[w.health]++;return c;}
function matchingRows(){const term=$('filter').value.toLowerCase();return snapshot.workers.filter(w=>[w.problem,w.worker,w.run,w.model,w.host,w.handle].join(' ').toLowerCase().includes(term));}
const states={running:'Running',continuing:'Continuing',retrying:'Retrying',blocked:'Blocked',paused:'Paused',stopped:'Stopped',starting:'Starting',sleeping:'Waiting',not_started:'Not started',unknown:'Unknown'};
function setTheme(theme){document.documentElement.dataset.theme=theme;$('theme-toggle').textContent=theme==='dark'?'Light':'Dark';$('theme-toggle').setAttribute('aria-label','Switch to '+(theme==='dark'?'light':'dark')+' theme');try{localStorage.setItem('agent-pool-theme',theme);}catch{}}
try{setTheme(localStorage.getItem('agent-pool-theme')||'dark');}catch{setTheme('dark');}
$('theme-toggle').addEventListener('click',()=>setTheme(document.documentElement.dataset.theme==='dark'?'light':'dark'));
function node(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
function age(ts){if(!ts)return 'Unknown';const n=Math.max(0,snapshot.now-ts);return n<60?'<1 m':n<3600?Math.floor(n/60)+' m':n<86400?(n/3600).toFixed(1)+' h':Math.floor(n/86400)+' d';}
function timeCell(ts){const td=node('td',age(ts));if(ts)td.title=new Date(ts*1000).toLocaleString();return td;}
function cell(tr,text,sub){const td=node('td',text??'Unknown');if(sub)td.append(node('small',sub));tr.append(td);return td;}
function errorText(error){return error?.category?[error.http_status,error.category.replaceAll('_',' ')].filter(Boolean).join(' · '):null;}
function why(w){return [w.health_reason,['blocked','retrying'].includes(w.state)?errorText(w.error):null].filter(Boolean).join(' · ');}
function openDetail(id){selected=id;detail();if(!$('detail').open)$('detail').showModal();}
function renderSummary(rows){
 const labels={responding:'Responding',no_recent_response:'No recent response',unknown:'Unknown',inactive:'No active workers'};
 $('families').replaceChildren(...(snapshot.families||[]).map(f=>{
  const c=countsFor(rows.filter(w=>w.family===f.family));const active=c.working+c.transient_error+c.needs_attention;
  const tone=c.needs_attention?'down':c.transient_error?'degraded':active?'working':'intentional';
  const tile=node('article',undefined,'family-tile '+tone);tile.dataset.family=f.family;
  const top=node('div',undefined,'family-top');top.append(node('h3',f.family[0].toUpperCase()+f.family.slice(1)),node('span',labels[f.status],'badge '+(f.status==='responding'?'working':'unknown')));
  const nums=node('div',undefined,'family-numbers');for(const status of ['working','transient_error','needs_attention']){const v=node('div',undefined,tones[status]);v.dataset.status=status;v.append(node('strong',c[status]),node('span',names[status].toLowerCase()));nums.append(v);}
  tile.append(top,nums,node('p',active+' active · '+c.intentional+' retired in this view','muted'),node('p','Provider last response '+(f.last_response?age(f.last_response)+' ago':'unknown'),'muted'));return tile;
 }));
}
function render(){if(!snapshot)return;
 $('updated').textContent='Updated '+new Date(snapshot.now*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
 const stale=snapshot.hosts.filter(h=>h.stale||h.error);$('banner').textContent=!snapshot.hosts.length?'Waiting for the first host collection.':stale.length?'Collection needs attention: '+stale.map(h=>h.host+(h.stale?' · stale':' · incomplete')).join(', '):'';
 const matched=matchingRows(),counts=countsFor(matched),active=matched.filter(w=>w.health!=='intentional'),retired=matched.filter(w=>w.health==='intentional');
 $('counts').replaceChildren(...['working','transient_error','needs_attention'].map(status=>{const d=node('div',undefined,'count '+tones[status]);d.dataset.status=status;d.append(node('strong',counts[status]),node('span',names[status].toLowerCase()));return d;}),node('p',active.length+' active = '+counts.working+' green + '+counts.transient_error+' yellow + '+counts.needs_attention+' red','muted'));
 renderSummary(matched);
 $('retired-label').textContent='Show retired ('+retired.length+')';
 $('workers-title').textContent='Active workers ('+active.length+')';
 $('retired-title').textContent='Retired workers ('+retired.length+')';
 $('retired-panel').hidden=!$('show-paused').checked;
const keyLabels={working:'Responding',errors:'Errors observed',mixed:'Mixed results',unknown:'Unknown'};
const keyTones={working:'working',errors:'down',mixed:'degraded',unknown:'unknown'};
$('key-count').textContent=(snapshot.provider_keys||[]).length+' keys'+(snapshot.key_identity_unknown?' · '+snapshot.key_identity_unknown+' worker connections unidentified':'');
const keyRows=[];
for(const family of ['gemini','claude','codex']){
 const priority=k=>Math.min(...k.names.filter(n=>n.toLowerCase().startsWith(family+' ')).map(n=>/ primary$/.test(n)?0:Number(n.match(/ backup (\d+)$/)?.[1]??999)),999);
 const keys=(snapshot.provider_keys||[]).filter(k=>k.families.includes(family)).sort((a,b)=>priority(a)-priority(b)||a.endpoint.localeCompare(b.endpoint)||a.id.localeCompare(b.id));if(!keys.length)continue;
 const heading=node('tr',undefined,'key-group');heading.dataset.family=family;const title=node('th',family[0].toUpperCase()+family.slice(1)+' · '+keys.length+' keys');title.colSpan=6;title.scope='colgroup';heading.append(title);keyRows.push(heading);
 keyRows.push(...keys.map(k=>{const tr=node('tr');const canonical=typeof k.display_name==='string'&&[...k.display_name].length<=100&&k.display_name===k.display_name.toLowerCase()&&k.display_name===k.display_name.normalize('NFC')&&!/[^\p{L}\p{Nd}_]/u.test(k.display_name)&&/^[a-z0-9]+(?:_[\p{L}\p{Nd}]+){2,}$/u.test(k.display_name)?k.display_name:null;const name=cell(tr,canonical||(k.name_status==='conflict'?'Name conflict':'Name unavailable'),(k.models.length?k.models.join(', '):k.families.join(', '))+' · '+k.hosts.join(', '));name.title=canonical||'';cell(tr,k.endpoint);const status=cell(tr,'');status.append(node('span',keyLabels[k.status],'badge '+keyTones[k.status]));if(k.errors.length)status.append(node('small',k.errors.map(e=>/^\d+$/.test(e)?'HTTP '+e:e).join(', ')));tr.append(timeCell(k.last_response));cell(tr,k.workers);const trend=cell(tr,'');const strip=node('div',undefined,'key-history');k.history.forEach((value,i)=>{const bar=node('span',undefined,'cell '+(value?keyTones[value]:''));const at=(Math.floor(snapshot.now/300)-17+i)*300;bar.title=new Date(at*1000).toLocaleString()+' · '+(value?keyLabels[value]:'No observation');strip.append(bar);});trend.append(strip);return tr;}));
}
$('keys').replaceChildren(...keyRows);
$('keys-empty').hidden=!!snapshot.provider_keys?.length;
function workerRow(w){const tr=node('tr');tr.dataset.id=w.id;tr.addEventListener('click',()=>openDetail(w.id));const h=cell(tr,'');const badge=node('span',names[w.health],'badge '+tones[w.health]);badge.title=w.health_reason;h.append(badge,node('small',states[w.state]||'Unknown'));cell(tr,(w.problem||'Unknown').replaceAll('_',' '));const id=cell(tr,'');const b=node('button',w.handle||w.worker,'worker-link');b.title=w.run+'/'+w.worker;id.append(b,node('small',w.worker));cell(tr,w.model,w.host+' · route '+(w.route??'?'));tr.append(timeCell(w.last_response));cell(tr,why(w));return tr;}
$('workers').replaceChildren(...active.map(workerRow));$('retired-workers').replaceChildren(...retired.map(workerRow));$('empty').hidden=active.length!==0;if(selected&&$('detail').open)detail();}
function detail(){const w=snapshot.workers.find(x=>x.id===selected);if(!w)return;const d=$('detail');const top=node('div',undefined,'detail-top'),title=node('div');const h=node('h2',w.handle||w.worker);h.id='detail-title';title.append(h,node('p',w.run+'/'+w.worker,'muted'));const close=node('button','Close','button');close.type='button';close.addEventListener('click',()=>d.close());top.append(title,close);d.replaceChildren(top,node('p',why(w),'muted'));
const dl=node('dl',undefined,'detail-grid');const pairs=[['Health',names[w.health]],['Needs attention',w.attention_known?(w.needs_attention?'Yes · six hours without a model response':'No · six-hour threshold'):'Unknown evidence'],['Attention clock',age(w.attention_since)],['Process state',states[w.state]||'Unknown'],['Model / host',w.model+' · '+w.host],['Last real response',age(w.last_response)],['Last completed tool',age(w.last_tool)],['Last observed GPU progress',age(w.last_job_progress)],['Last public activity',w.last_public_activity?age(w.last_public_activity.at)+' · '+w.last_public_activity.kind:'Unknown'],['Registered',age(w.registered_at)],['This launch started',age(w.started_at)],['Complete interaction time','Unknown'],['Local platform receipt',w.last_platform_receipt?age(w.last_platform_receipt.at):'Unknown'],['Observed stalled time · 24 h',w.stalled_hours.toFixed(2)+' h / '+w.observed_hours.toFixed(2)+' h covered'],['GPUs held / observed hours · 24 h',(w.gpu.length?w.gpu.join(', '):'None')+' / '+w.gpu_hours.toFixed(2)+' h'],['Disk used / allowance',(w.disk_bytes===null?'Unknown':(w.disk_bytes/1e9).toFixed(1)+' GB')+' / '+(w.disk_allowance_gb??'?')+' GB'],['Published papers · 7 d / total',(w.papers_7d??'?')+' / '+(w.papers_total??'?')],['Owner messages',w.pending_messages+' pending · last receipt '+age(w.last_owner_received)],['Context',w.context_tokens?w.context_tokens.toLocaleString():'Not recorded'],['Host / account evidence',age(w.sampled_at)+' / '+age(w.profile_sampled_at)],['Controller update',w.upgrade_pending?'Waiting for a safe point':'None pending']];for(const [k,v]of pairs){const div=node('div');div.append(node('dt',k),node('dd',v));dl.append(div);}d.append(dl,node('h2','Seven-day work history'));
const sc=node('div',undefined,'scroll'),strip=node('div',undefined,'strip');w.history.forEach((v,i)=>{const c=node('span',undefined,'cell '+(v||''));const at=(Math.floor(snapshot.now/3600)-167+i)*3600;c.title=new Date(at*1000).toLocaleString()+' · '+(v==='working'?'real work observed':v==='intentional'?'retired':v==='unknown'?'unknown evidence':v?'sampled without work this hour':'no coverage');strip.append(c);});sc.append(strip);d.append(sc);const legend=node('div',undefined,'legend');legend.append(node('span','7 days ago · blank = no coverage'),node('span','Now'));d.append(legend);}
$('detail').addEventListener('close',()=>{selected=null;});
function ageUnavailableSnapshot(){
 snapshot.now=Date.now()/1000;
 for(const w of snapshot.workers){
  if(w.health==='intentional')continue;
  if(w.attention_due_at!==null&&w.attention_due_at!==undefined&&snapshot.now>=w.attention_due_at){w.health='needs_attention';w.health_reason='No real model response for at least 6 hours';}
  else {w.health='transient_error';w.health_reason='Dashboard connection unavailable';}
  w.needs_attention=w.health==='needs_attention';
 }
 for(const k of snapshot.provider_keys||[])k.status='unknown';
 for(const f of snapshot.families||[])f.status='unknown';
}
async function load(){try{const r=await fetch('/api/snapshot',{cache:'no-store'});if(!r.ok)throw Error();snapshot=await r.json();render();}catch{if(snapshot){ageUnavailableSnapshot();render();}$('banner').textContent='Dashboard connection unavailable. Response clocks continue; last recorded process states are shown.';$('updated').textContent='Refresh failed';}}
$('filter').addEventListener('input',render);$('show-paused').addEventListener('change',render);load();setInterval(load,30000);
