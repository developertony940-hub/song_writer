const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const ready=import('../static/mastering-media.mjs');
const wav=Buffer.concat([Buffer.from('RIFF'),Buffer.alloc(4),Buffer.from('WAVEfmt ')]);
test('playback fetch returns browser typed WAV bytes once',async()=>{
 const {fetchPlaybackBlob}=await ready;let calls=0;
 const b=await fetchPlaybackBlob('/pcm',async()=>{calls++;return new Response(wav);});
 assert.equal(calls,1);assert.equal(b.type,'audio/wav');assert.deepEqual(Buffer.from(await b.arrayBuffer()),wav);
});
test('busy source returns actionable HTTP error without retry',async()=>{
 const {fetchPlaybackBlob}=await ready;let calls=0;
 await assert.rejects(fetchPlaybackBlob('/pcm',async()=>{calls++;return new Response(JSON.stringify({detail:{message:'another master-audio operation is active'}}),{status:409});}),/HTTP 409.*another master-audio operation/);
 assert.equal(calls,1);
});
test('missing route and malformed errors retain HTTP status',async()=>{
 const {fetchPlaybackBlob}=await ready;
 await assert.rejects(fetchPlaybackBlob('/pcm',async()=>new Response('missing',{status:404})),/HTTP 404/);
});
test('HTML or JSON returned with 200 is not assigned as audio',async()=>{
 const {fetchPlaybackBlob}=await ready;
 await assert.rejects(fetchPlaybackBlob('/pcm',async()=>new Response('<html>not audio</html>')),/did not return a PCM WAV/);
});
test('network failure is not retried',async()=>{
 const {fetchPlaybackBlob}=await ready;let calls=0;
 await assert.rejects(fetchPlaybackBlob('/pcm',async()=>{calls++;throw Error('offline');}),/offline/);assert.equal(calls,1);
});
test('source selection finishes waveform request before media fetch, never assigns server URL directly',()=>{
 const js=fs.readFileSync(require('node:path').join(__dirname,'../static/mastering.js'),'utf8');
 const selection=js.slice(js.indexOf('async function selectSource('),js.indexOf('function currentRange('));
 assert.ok(selection.indexOf('/peaks?bins=1600')<selection.indexOf('fetchPlaybackBlob(src.pcm_url)'));
 assert.ok(selection.includes('URL.createObjectURL(blob)'));assert.ok(!selection.includes('audio.src=src.pcm_url'));
 assert.ok(js.includes('URL.revokeObjectURL(sourceObjectUrl)'));
});
