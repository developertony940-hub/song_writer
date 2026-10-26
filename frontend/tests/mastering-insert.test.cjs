const {test}=require('node:test');
const assert=require('node:assert/strict');
const ready=import('../static/mastering-core.mjs');
const sha='a'.repeat(64);
function clip(clip_id,label,start_s,end_s){return {clip_id,label,source_id:sha,start_s,end_s,gain_db:-2,fade_in_s:.1,fade_out_s:.2,confirmation:{confirmed:true,source_sha256:sha,start_s,end_s}};}
function fixture(){return [clip('intro','Intro',0,7.5),clip('verse','Verse 1',7.5,52.6),clip('chorus','Final Chorus',224,285),clip('outro','Outro',285,300)];}
test('ripple insert preserves complete Final Chorus and Outro, shifts both by full new verse duration',async()=>{
 const c=await ready,before=fixture(),snapshot=structuredClone(before),added=c.invalidate(clip('verse3','Verse 3',91.9,136.9));
 const result=c.insertClip(before,added,'before','chorus');
 assert.equal(c.duration(result),c.duration(before)+45);
 assert.deepEqual(before,snapshot);
 assert.deepEqual(result.filter(x=>x.clip_id!=='verse3'),snapshot);
 for(const id of ['chorus','outro']){
   const old=c.bound(before).find(x=>x.clip_id===id),now=c.bound(result).find(x=>x.clip_id===id);
   assert.equal(now.master_start_s,old.master_start_s+45);assert.equal(now.master_end_s,old.master_end_s+45);
   assert.equal(now.end_s-now.start_s,old.end_s-old.start_s);assert.ok(c.confirmation(now));
 }
 assert.ok(!c.confirmation(result.find(x=>x.clip_id==='verse3')));
});
test('insert after and append preserve all existing clip contents',async()=>{
 const c=await ready,before=fixture(),v=clip('new','Verse 3',10,20);
 assert.deepEqual(c.insertClip(before,v,'after','verse').map(x=>x.clip_id),['intro','verse','new','chorus','outro']);
 assert.deepEqual(c.insertClip(before,v).map(x=>x.clip_id),['intro','verse','chorus','outro','new']);
 assert.deepEqual(c.insertClip([],v),[v]);
});
test('stale anchors, duplicate ids, invalid modes and nonpositive ranges refuse without mutation',async()=>{
 const c=await ready,before=fixture(),snapshot=structuredClone(before),v=clip('new','new',0,5);
 for(const fn of [()=>c.insertClip(before,v,'before','missing'),()=>c.insertClip(before,before[0]),()=>c.insertClip(before,v,'overwrite','verse'),()=>c.insertClip(before,{...v,end_s:0}),()=>c.insertClip(before,{...v,end_s:Infinity})])assert.throws(fn);
 assert.deepEqual(before,snapshot);
});
test('recipe serialization and undo/redo retain full following sections',async()=>{
 const c=await ready,before=fixture(),next=c.insertClip(before,clip('new','Verse 3',10,55),'before','chorus'),h=new c.EditHistory();
 h.push(before);assert.deepEqual(h.undo(next),before);assert.deepEqual(h.redo(before),next);
 const saved=JSON.parse(JSON.stringify(c.recipeSections(next)));
 assert.ok(c.validate(saved,[{source_id:sha,duration_s:300}]));
 assert.deepEqual(saved.slice(-2),before.slice(-2));
 saved[0].label='changed';assert.equal(before[0].label,'Intro');
});
test('UI uses explicit ripple insertion and warns before shortening an existing section',async()=>{
 const fs=require('node:fs'),path=require('node:path'),root=path.join(__dirname,'..');
 const js=fs.readFileSync(path.join(root,'static/mastering.js'),'utf8'),html=fs.readFileSync(path.join(root,'templates/mastering.html'),'utf8');
 assert.match(js,/next=insertClip\(state.sections,c,placement,anchorId\)/);
 assert.match(js,/This trims/);assert.match(js,/Apply this trim\?/);
 assert.match(html,/id="insert-destination"/);assert.match(html,/Ripple insert/);
 assert.match(js,/mastering-core.mjs\?v=draftsave1/);
});
