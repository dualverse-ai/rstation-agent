const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
class El{constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.value='';this.checked=false;this.open=false;}append(...xs){this.children.push(...xs);}replaceChildren(...xs){this.children=xs;}addEventListener(){}setAttribute(){}get text(){return String(this.textContent??'')+this.children.map(c=>c.text??c).join(' ');}}
const elements={};let now=100000;
const context={document:{getElementById:id=>elements[id]??=new El(id),createElement:tag=>new El(tag),documentElement:new El('html')},localStorage:{getItem:()=>null,setItem(){}},setInterval(){},fetch:()=>new Promise(()=>{}),Date:class extends Date{static now(){return now*1000;}}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[2]||'dashboard/app.js','utf8'),context);
const workers=[];for(const family of ['gemini','claude','codex'])for(const [i,health]of ['working','transient_error','needs_attention','intentional'].entries())workers.push({id:family+i,health,state:i===1?'blocked':i===3?'paused':'running',health_reason:'Reason '+i,needs_attention:i===2,attention_known:true,attention_due_at:i===2?100000:100001,host:'h6',family,model:family,worker:family+i,run:'run',problem:'test',last_response:i===2?78400:78401,error:{category:'infra_transient'}});
context.data={now,hosts:[{host:'h6'}],workers,provider_keys:[],families:['gemini','claude','codex'].map(family=>({family,status:'responding',last_response:99999}))};
function render(){vm.runInContext('snapshot=data;render()',context);}
function verify(expectedActive,expectedRetired){
 const rows=elements.workers.children;assert.equal(rows.length,expectedActive);
 assert.equal(elements['retired-workers'].children.length,expectedRetired);
 const counts={working:0,transient_error:0,needs_attention:0};
 for(const tr of rows){const w=workers.find(w=>w.id===tr.dataset.id);counts[w.health]++;const badge=tr.children[0].children[0];assert.equal(badge.text,{working:'Working',transient_error:'Transient error',needs_attention:'Needs attention'}[w.health]);assert.equal(badge.className,'badge '+{working:'working',transient_error:'degraded',needs_attention:'down'}[w.health]);assert.match(tr.text,new RegExp(w.state==='blocked'?'Blocked':'Running'));}
 for(const card of elements.counts.children.filter(c=>c.dataset.status))assert.equal(Number(card.children[0].text),counts[card.dataset.status]);
 assert.match(elements.counts.text,new RegExp(expectedActive+' active ='));
 for(const tile of elements.families.children)for(const card of tile.children[1].children){const actual=rows.filter(tr=>{const w=workers.find(w=>w.id===tr.dataset.id);return w.family===tile.dataset.family&&w.health===card.dataset.status;}).length;assert.equal(Number(card.children[0].text),actual);}
}
render();verify(9,3);assert.equal(elements['retired-panel'].hidden,true);
elements['show-paused'].checked=true;render();verify(9,3);assert.equal(elements['retired-panel'].hidden,false);
elements.filter.value='claude';render();verify(3,1);
elements.filter.value='missing';render();verify(0,0);assert.equal(elements.empty.hidden,false);
elements.filter.value='';vm.runInContext('ageUnavailableSnapshot();render()',context);verify(9,3);assert.equal(workers.filter(w=>w.health==='transient_error').length,6);assert.equal(workers.filter(w=>w.needs_attention).length,3);now=100001;vm.runInContext('ageUnavailableSnapshot();render()',context);verify(9,3);
assert.equal(workers.filter(w=>w.needs_attention).length,9);
assert.equal(workers.filter(w=>w.health==='intentional').length,3);
assert.equal(workers.find(w=>w.id==='gemini1').state,'blocked');
console.log('Row colors, all-family counts, filtering, retired separation and failed-refresh boundary reconcile.');
const target=workers[0];
Object.assign(target,{gemini_exhausted:true,gemini_enabled:['hapi','ikun','router'],gemini_model_observations:{gemini:Object.fromEntries(['hapi','ikun','router'].map((id,i)=>[id,{status:'errors',at:99990+i,http_status:503}]))},gpu:[],history:[],observed_hours:1,stalled_hours:0,gpu_hours:0,disk_bytes:null});
vm.runInContext('selected="gemini0";detail()',context);
const dialog=elements.detail;
assert.match(dialog.text,/All enabled Gemini routes attempted/);
const routes=dialog.children.find(e=>e.className==='detail-grid'&&e.children.length===3);
assert.equal(routes.children.filter(e=>e.children.some(b=>b.className==='badge down'&&b.text==='Provider failed')).length,3);
assert.match(routes.text,/HTTP 503/);
console.log('All three failing routes are red inside the individual worker detail.');
