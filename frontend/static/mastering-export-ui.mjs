import {sectionExportRequest,sectionExportLinks} from './mastering-export-core.mjs';

export function createSectionExports(host){
 const $=id=>document.getElementById(id);
 let sid=null,records=[];
 const el=(tag,text)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;return e;};
 function reset(){sid=null;records=[];render();}
 function render(){
  const src=host.source(),off=host.off();
  $('export-selection-wav').disabled=off||!src;
  $('export-clip-wav').disabled=off||!host.selected()||host.selected().source_id!==src?.source_id;
  const area=$('section-export-history');area.replaceChildren();
  $('section-export-source').textContent=src?`Exports for this source · SHA-256 ${src.source_id}`:'Select a source to view its section exports.';
  if(!src||sid!==src.source_id)return;
  for(const row of [...records].reverse()){
   const card=el('article');card.className='section-export-card';
   card.append(el('b',row.label||'Section export'),el('p',`${row.status} · ${row.export_id}`));
   const links=sectionExportLinks(row,host.base());
   if(links){
    card.append(el('p',`Source ${host.time(row.start_s)}–${host.time(row.end_s)} · ${row.frames} PCM frames · ${row.sample_rate} Hz · ${row.channels} ch`));
    const actions=el('div');actions.className='actions';
    for(const [kind,url] of Object.entries(links)){const a=el('a',kind==='wav'?'Download section WAV':'Download provenance JSON');a.href=url;a.download='';actions.append(a);}
    card.append(actions,el('small',`WAV SHA-256 ${row.files?.wav?.sha256||'not recorded'}`));
   }else card.append(el('p','Incomplete or failed export retained. Do not repeat an uncertain request; inspect the saved history first.'));
   area.append(card);
  }
  if(!records.length)area.append(el('p','No section WAV exports for this source yet.'));
 }
 async function load(src){
  sid=src.source_id;records=[];render();
  const data=await host.request(host.base()+`/sources/${src.source_id}/exports`);
  if(host.source()?.source_id!==src.source_id)return;
  records=data.exports;render();
 }
 async function exportRange(range,name,src=host.source()){
  const body=sectionExportRequest(src,range,name);
  if(!confirm(`Export “${body.label}” from ${host.time(body.start_s)} to ${host.time(body.end_s)} as WAV? This includes the original vocals AND instruments. Gain/fades are not applied. No voice extraction or AI generation is performed.`))return;
  host.stop();host.notice('Saving selected source frames as a separate WAV. Keep this page open; do not repeat the request…');
  const saved=await host.mutate(`/sources/${src.source_id}/exports`,body);
  if(host.source()?.source_id===src.source_id){sid=src.source_id;records.push(saved);render();}
  host.notice('Section WAV saved. Use Download section WAV below. The original, source map and master revisions are unchanged. This is a full-mix clip—not an isolated voice.');
 }
 $('export-selection-wav').onclick=()=>host.action(()=>exportRange(host.range(),$('section-name').value));
 $('export-clip-wav').onclick=()=>host.action(async()=>{
  const clip=host.selected();if(!clip)throw Error('Select an arrangement section first.');
  const src=host.sources().find(s=>s.source_id===clip.source_id);if(!src||src.source_id!==host.source()?.source_id)throw Error('Select this section and wait for its source to load first.');
  await exportRange({start:clip.start_s,end:clip.end_s},clip.label,src);
 });
 return {reset,load,render};
}
