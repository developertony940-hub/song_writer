const {test}=require('node:test');
const assert=require('node:assert/strict');
const api=import('../static/mastering-export-core.mjs');
const source={source_id:'a'.repeat(64),duration_s:300};
test('WAV request contains exact source interval and no edit or approval fields',async()=>{
 const {sectionExportRequest}=await api;
 assert.deepEqual(sectionExportRequest(source,{start:7.5,end:52.6},' Verse 1 '),{source_sha256:source.source_id,start_s:7.5,end_s:52.6,label:'Verse 1'});
});
test('rejects missing or invalid source identity',async()=>{
 const {sectionExportRequest:f}=await api;
 for(const src of [null,{...source,source_id:'../other'},{...source,source_id:'a'.repeat(63)}])assert.throws(()=>f(src,{start:0,end:1},'Verse'));
});
test('rejects nonfinite, reversed, coerced and out-of-source intervals',async()=>{
 const {sectionExportRequest:f}=await api;
 for(const range of [{start:NaN,end:1},{start:0,end:Infinity},{start:-1,end:1},{start:1,end:1},{start:2,end:1},{start:0,end:301},{start:'0',end:1}])assert.throws(()=>f(source,range,'Verse'));
});
test('enforces bounded duration and safe nonempty label',async()=>{
 const {sectionExportRequest:f}=await api;
 assert.throws(()=>f({...source,duration_s:2000},{start:0,end:1801},'Verse'));
 for(const label of ['', ' ', 'a'.repeat(121),'Verse\n3','x\u0000'])assert.throws(()=>f(source,{start:0,end:1},label));
});
test('download URLs are derived locally, not from untrusted stored links',async()=>{
 const {sectionExportLinks:f}=await api;
 const id='b'.repeat(32),base='/api/projects/p/master-audio';
 assert.deepEqual(f({status:'completed',export_id:id,manifest_url:'https://evil.invalid'},base),{wav:`${base}/section-exports/${id}/wav`,manifest:`${base}/section-exports/${id}/manifest`});
});
test('no downloads for failed uncertain or invalid-identity records',async()=>{
 const {sectionExportLinks:f}=await api;
 for(const row of [null,{status:'failed',export_id:'b'.repeat(32)},{status:'uncertain',export_id:'b'.repeat(32)},{status:'completed',export_id:'../x'}])assert.equal(f(row,'/api'),null);
});
