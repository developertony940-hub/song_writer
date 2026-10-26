const {test}=require('node:test');
const assert=require('node:assert/strict');
class Element{
 constructor(tag='div'){this.tagName=tag;this.children=[];this.value='';this.textContent='';this.disabled=false;}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.children=[...items];}
}
async function fixture(){
 const elements=new Map();global.document={getElementById:id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement:tag=>new Element(tag)};
 let source={source_id:'a'.repeat(64),duration_s:100},clip=null,off=false,accepted=true,fail=false;
 const calls=[],notices=[],errors=[];
 const record={export_id:'b'.repeat(32),status:'completed',label:'Verse 1',start_s:2,end_s:10,frames:384000,sample_rate:48000,channels:2,files:{wav:{sha256:'c'.repeat(64)}}};
 global.confirm=()=>accepted;
 const host={source:()=>source,sources:()=>[source],selected:()=>clip,off:()=>off,base:()=>'/api/projects/project/master-audio',range:()=>({start:2,end:10}),time:s=>String(s),stop:()=>calls.push(['stop']),notice:x=>notices.push(x),request:async url=>{calls.push(['get',url]);return {exports:[record]};},mutate:async(path,body)=>{calls.push(['post',path,body]);if(fail)throw Error('uncertain');return {...record,label:body.label};},action:async fn=>{try{await fn();}catch(e){errors.push(e.message);}}};
 const {createSectionExports}=await import('../static/mastering-export-ui.mjs');const ui=createSectionExports(host);
 document.getElementById('section-name').value='Verse 3 reference';
 return {ui,e:id=>elements.get(id),record,calls,notices,errors,src:()=>source,setSource:v=>source=v,setClip:v=>clip=v,setOff:v=>off=v,setAccepted:v=>accepted=v,setFail:v=>fail=v};
}
test('source export history renders WAV and manifest downloads',async()=>{
 const f=await fixture();await f.ui.load(f.src());const card=f.e('section-export-history').children[0];
 assert.equal(card.children[0].textContent,'Verse 1');
 const links=card.children.find(e=>e.className==='actions').children;
 assert.equal(links.length,2);assert.match(links[0].href,/\/section-exports\/[a-f0-9]{32}\/wav$/);assert.match(links[1].href,/\/manifest$/);
});
test('export selection sends one source-only request and preserves name',async()=>{
 const f=await fixture();await f.ui.load(f.src());await f.e('export-selection-wav').onclick();
 const posts=f.calls.filter(x=>x[0]==='post');assert.equal(posts.length,1);
 assert.deepEqual(posts[0][2],{source_sha256:f.src().source_id,start_s:2,end_s:10,label:'Verse 3 reference'});
 assert.equal(f.e('section-export-history').children.length,2);assert.match(f.notices.at(-1),/not an isolated voice/);
});
test('cancel does not dispatch or alter saved history',async()=>{
 const f=await fixture();await f.ui.load(f.src());f.setAccepted(false);await f.e('export-selection-wav').onclick();assert.equal(f.calls.filter(x=>x[0]==='post').length,0);assert.equal(f.e('section-export-history').children.length,1);
});
test('uncertain mutation is never retried or added as success',async()=>{
 const f=await fixture();await f.ui.load(f.src());f.setFail(true);await f.e('export-selection-wav').onclick();assert.equal(f.calls.filter(x=>x[0]==='post').length,1);assert.deepEqual(f.errors,['uncertain']);assert.equal(f.e('section-export-history').children.length,1);
});
test('project/source reset removes prior export links and busy disables actions',async()=>{
 const f=await fixture();await f.ui.load(f.src());f.ui.reset();assert.equal(f.e('section-export-history').children.length,0);f.setOff(true);f.ui.render();assert.equal(f.e('export-selection-wav').disabled,true);
});
test('arrangement export uses source bounds and omits gain/fades',async()=>{
 const f=await fixture();await f.ui.load(f.src());f.setClip({source_id:f.src().source_id,start_s:5,end_s:9,label:'Chorus',gain_db:6,fade_in_s:1});f.ui.render();assert.equal(f.e('export-clip-wav').disabled,false);await f.e('export-clip-wav').onclick();
 const body=f.calls.find(x=>x[0]==='post')[2];assert.deepEqual(body,{source_sha256:f.src().source_id,start_s:5,end_s:9,label:'Chorus'});
});
test('cross-source arrangement export refuses to dispatch',async()=>{
 const f=await fixture();f.setClip({source_id:'d'.repeat(64),start_s:0,end_s:1,label:'Other'});f.ui.render();assert.equal(f.e('export-clip-wav').disabled,true);await f.e('export-clip-wav').onclick();assert.equal(f.calls.filter(x=>x[0]==='post').length,0);assert.match(f.errors[0],/wait for its source/);
});
test('late source-history response is ignored after source changes',async()=>{
 const f=await fixture();const load=f.ui.load(f.src());f.setSource({source_id:'d'.repeat(64),duration_s:100});await load;f.ui.render();assert.equal(f.e('section-export-history').children.length,0);
});
