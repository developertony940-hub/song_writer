const {test}=require('node:test');
const assert=require('node:assert/strict');
const ready=import('../static/mastering-core.mjs');
const sha='a'.repeat(64),sources=[{source_id:sha,duration_s:300}];
const draft=()=>({clip_id:'verse',source_id:sha,label:'Verse',start_s:0,end_s:45,gain_db:0,fade_in_s:0,fade_out_s:0,confirmation:{confirmed:false,source_sha256:sha,start_s:0,end_s:45}});
test('draft validation allows genuine unconfirmed progress but master validation refuses',async()=>{
 const c=await ready,clip=draft();assert.equal(c.validate([clip],sources,{requireConfirmation:false}),true);
 assert.throws(()=>c.validate([clip],sources),/building a master/);assert.equal(c.confirmation(clip),false);
 const saved=JSON.parse(JSON.stringify(c.recipeSections([clip])));assert.equal(saved[0].confirmation.confirmed,false);assert.deepEqual(saved,[clip]);
});
test('draft saving retains finite bounds gain and fade validation',async()=>{
 const c=await ready;for(const change of [{start_s:-1},{end_s:301},{gain_db:13},{fade_in_s:46},{end_s:NaN}])assert.throws(()=>c.validate([{...draft(),...change}],sources,{requireConfirmation:false}));
 assert.throws(()=>c.validate([draft(),draft()],sources,{requireConfirmation:false}));
});
test('mixed review status roundtrips unchanged and never becomes confirmation by saving',async()=>{
 const c=await ready,a=draft(),b={...draft(),clip_id:'chorus',confirmation:{...draft().confirmation,confirmed:true}};
 const roundtrip=JSON.parse(JSON.stringify(c.recipeSections([a,b])));
 assert.deepEqual(roundtrip.map(x=>c.confirmation(x)),[false,true]);
 const h=new c.EditHistory();h.push(roundtrip);assert.deepEqual(h.undo([]),roundtrip);
});
test('save and export have distinct frontend gates and nearby feedback',()=>{
 const fs=require('node:fs'),path=require('node:path'),root=path.join(__dirname,'..');
 const js=fs.readFileSync(path.join(root,'static/mastering.js'),'utf8'),html=fs.readFileSync(path.join(root,'templates/mastering.html'),'utf8');
 assert.match(js,/validate\(state.sections,state.data.sources,\{requireConfirmation:false\}\)/);
 assert.match(js,/validate\(state.sections,state.data.sources\);if\(!confirm/);
 assert.match(js,/rendered\|\|pending>0/);assert.match(js,/inspectorPending\(\)\)throw Error\('Unapplied/);
 for(const id of ['save-state','save-feedback','inspector-feedback'])assert.match(html,new RegExp(`id="${id}"`));
 assert.match(js,/DRAFT \/ REVIEW PENDING/);assert.match(html,/mastering.js\?v=draftsave1/);
});
