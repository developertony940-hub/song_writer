import {labelsFromLyrics,validateMap,moveBoundary,splitMap,mergeMap,mapKey,mapToClips} from './mastering-sections.mjs';

export function createSectionAnalysis(host){
 const $=id=>document.getElementById(id),s={sid:null,rows:[],revision:0,analysis:null,maps:[],dirty:false,heard:new Set(),active:0,drag:null};
 const clone=x=>structuredClone(x);
 function el(tag,text){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;return e;}
 function button(text,fn){const b=el('button',text);b.type='button';b.disabled=host.off();b.onclick=()=>host.action(fn);return b;}
 function change(rows){s.rows=rows;s.dirty=true;render();host.draw();}
 function selected(){return s.rows[s.active];}
 function reset(){Object.assign(s,{sid:null,rows:[],revision:0,analysis:null,maps:[],dirty:false,active:0,drag:null});s.heard.clear();render();}
 function discard(){return !s.dirty||confirm('Discard unsaved source-map changes? Saved maps and source audio remain preserved.');}
 async function load(src){
  reset();s.sid=src.source_id;
  const maps=await host.request(host.base()+`/sources/${s.sid}/maps`);
  const analyses=await host.request(host.base()+`/sources/${s.sid}/analysis`);
  s.maps=maps.maps;s.revision=maps.map_revision;
  const row=s.maps.at(-1)||analyses.analyses.at(-1);
  if(row){s.rows=clone(row.sections);s.analysis=row.analysis_id;s.dirty=false;showEvidence(row);}
  else $('section-analysis-status').textContent='No source map yet. Analyze to suggest boundaries; you will review them by listening.';
  render();
 }
 function showEvidence(row){$('section-analysis-status').textContent=[row.kind==='suggestion'?'Unconfirmed audio-analysis suggestions.':'Saved source map; confirmation is your listening attestation.',...(row.warnings||[])].join(' ');}
 async function analyze(){
  const src=host.source();if(!src)throw Error('Select an imported source first.');
  if(s.rows.length&&!confirm('Create a new analysis and replace the displayed map? Saved maps remain preserved.'))return;
  host.stop();host.notice('Analyzing local PCM for silence and energy transitions. No model, network service or generation is used…');
  const section_labels=$('section-label-hints').value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
  if(section_labels.length>64||section_labels.some(x=>x.length>120))throw Error('Provide at most 64 section labels, up to 120 characters each.');
  const row=await host.mutate(`/sources/${src.source_id}/analysis`,{section_labels});
  s.sid=src.source_id;s.rows=clone(row.sections);s.analysis=row.analysis_id;s.active=0;s.dirty=true;showEvidence(row);render();host.draw();host.notice(`Suggested ${s.rows.length} sections. These are not verified verse/chorus labels. Review and save the source map.`);
 }
 function render(){
  const off=host.off(),src=host.source();for(const name of ['analyze-sections','save-source-map','use-source-map','map-split','map-merge','load-lyric-labels'])$(name).disabled=off||!src;
  $('section-label-hints').disabled=off;$('map-history').disabled=off||!s.maps.length;
  $('save-source-map').disabled=off||!s.rows.length||!s.dirty;
  $('use-source-map').disabled=off||!s.rows.length;
  $('source-map-meta').textContent=`${s.rows.length} sections · saved map v${s.revision}${s.dirty?' · unsaved changes':''} · ${s.rows.filter(r=>r.confirmed).length} confirmed`;
  const hist=$('map-history');hist.replaceChildren(el('option','Choose saved map…'));hist.firstChild.value='';
  for(const m of s.maps){const o=el('option',`Source map v${m.map_revision} · ${m.sections.length} sections`);o.value=String(m.map_revision);hist.append(o);}
  const area=$('source-map-rows');area.replaceChildren();
  s.rows.forEach((row,i)=>{
   const div=el('div');div.className='map-row'+(i===s.active?' active':'');
   const name=el('input');name.value=row.label;name.maxLength=120;name.setAttribute('aria-label',`Section ${i+1} name`);name.disabled=off;
   name.onchange=()=>{s.rows[i].label=name.value.trim();s.rows[i].confirmed=false;s.dirty=true;render();};
   const timing=el('div');timing.className='map-timing';
   const input=el('input');input.type='number';input.step='.001';input.value=row.start_s;input.disabled=off||i===0;input.setAttribute('aria-label',`Section ${i+1} source start seconds`);
   input.onchange=()=>{try{change(moveBoundary(s.rows,i,Number(input.value)));}catch(e){host.notice(e.message,true);render();}};
   const end=el('input');end.type='number';end.step='.001';end.value=row.end_s;end.disabled=off||i===s.rows.length-1;end.setAttribute('aria-label',`Section ${i+1} source end seconds`);
   end.onchange=()=>{try{change(moveBoundary(s.rows,i+1,Number(end.value)));}catch(e){host.notice(e.message,true);render();}};
   timing.append(input,el('span','→'),end);
   const review=el('label');review.className='check';const check=el('input');check.type='checkbox';check.checked=row.confirmed;check.disabled=off||(!row.confirmed&&!s.heard.has(mapKey(s.sid,row)));check.onchange=()=>{s.rows[i].confirmed=check.checked;s.dirty=true;render();};review.append(check,document.createTextNode(' Listened / confirmed'));
   const audition=button('▶ Section',async()=>{s.active=i;host.range(row);await host.audition({...mapToClips([row],s.sid,host.id)[0],label:row.label});render();});
   const boundary=button('▶ Join',async()=>{if(!i)throw Error('The first section starts at source zero.');s.active=i;const at=row.start_s;host.range(row);await host.audition({source_id:s.sid,start_s:Math.max(0,at-2),end_s:Math.min(src.duration_s,at+2),gain_db:0,fade_in_s:0,fade_out_s:0,label:'Boundary preview'});render();});
   const select=button(String(i+1).padStart(2,'0'),async()=>{s.active=i;host.range(row);render();host.draw();});
   const confidence=el('small',row.confidence?`${row.confidence} confidence · ${row.boundary_reason||'audio heuristic'}`:'User-reviewed source map');
   div.append(select,name,timing,audition,boundary,review,confidence);area.append(div);
  });
 }
 $('analyze-sections').onclick=()=>host.action(analyze);
 $('load-lyric-labels').onclick=()=>host.action(async()=>{
  const list=await host.request(`/api/music/${host.pid()}/revisions`);
  if(!list.revisions.length)throw Error('This project has no saved lyric revisions. Enter optional labels manually.');
  const choice=prompt(`Choose a saved lyric revision number (${list.revisions.map(r=>'v'+r.version).join(', ')}). Labels are hints only; no lyric or approval is changed.`,String(list.current_version||list.revisions.at(-1).version));
  if(choice===null)return;const version=Number(choice.replace(/^v/i,''));if(!list.revisions.some(r=>r.version===version))throw Error('Choose an existing revision number.');
  const detail=await host.request(`/api/music/${host.pid()}/revisions/${version}`);
  const labels=labelsFromLyrics(detail.settings?.lyrics||'');if(!labels.length)throw Error('No bracketed lyric section tags found. Enter section names manually; no times will be inferred from prose.');
  $('section-label-hints').value=labels.join('\n');$('section-hint-provenance').textContent=`Labels copied from lyric revision v${version}. Not verified against this audio; timestamps and sung wording were not changed.`;
 });
 $('save-source-map').onclick=()=>host.action(async()=>{
  validateMap(s.rows,host.source().duration_s);host.stop();const saved=await host.mutate(`/sources/${s.sid}/maps`,{expected_map_revision:s.revision,analysis_id:s.analysis,sections:s.rows.map(({label,start_s,end_s,confirmed})=>({label,start_s,end_s,confirmed}))});
  s.maps.push(saved);s.revision=saved.map_revision;s.rows=clone(saved.sections);s.dirty=false;render();host.notice(`Saved source map v${s.revision}. Unconfirmed suggestions remain unconfirmed. No master was built.`);
 });
 $('use-source-map').onclick=()=>host.action(async()=>{validateMap(s.rows,host.source().duration_s);if(!confirm('Replace the current unsaved master arrangement with this source map? Saved revisions remain preserved. Unconfirmed sections will still require listening review.'))return;host.apply(mapToClips(s.rows,s.sid,host.id));host.notice('Source map copied into the master arrangement as a draft. No source audio or saved master was changed.');});
 $('map-history').onchange=()=>host.action(async()=>{const m=s.maps.find(m=>String(m.map_revision)===$('map-history').value);if(!m||!discard())return;s.rows=clone(m.sections);s.analysis=m.analysis_id;s.active=0;s.dirty=true;showEvidence(m);render();host.draw();});
 $('map-split').onclick=()=>host.action(async()=>{const row=selected();if(!row)throw Error('Select a source-map section.');const text=prompt('Split at source second:',String((row.start_s+row.end_s)/2));if(text===null)return;change(splitMap(s.rows,s.active,Number(text)));});
 $('map-merge').onclick=()=>host.action(async()=>change(mergeMap(s.rows,s.active)));
 return {load,reset,render,analyze,discard,dirty:()=>s.dirty,hasMap:()=>!!s.rows.length,
  heard(clip){const row=s.rows.find(r=>r.start_s===clip.start_s&&r.end_s===clip.end_s);if(clip.source_id===s.sid&&row){s.heard.add(mapKey(s.sid,row));render();}},
  draw(ctx,w,h,start,span){if(!s.rows.length||s.sid!==host.source()?.source_id)return;ctx.font='10px sans-serif';for(let i=0;i<s.rows.length;i++){const row=s.rows[i],x=(row.start_s-start)/span*w,end=(row.end_s-start)/span*w;if(end<0||x>w)continue;ctx.fillStyle=row.confirmed?'#9bce8c':'#e5bd73';ctx.fillText(`${i+1} ${row.label.slice(0,22)}`,Math.max(2,x+4),41);if(i){ctx.strokeStyle=row.confirmed?'#9bce8c':'#e5bd73';ctx.setLineDash(row.confirmed?[]:[4,4]);ctx.beginPath();ctx.moveTo(x,44);ctx.lineTo(x,h-29);ctx.stroke();ctx.setLineDash([]);}}},
  dragStart(event,tolerance){if(host.off()||!s.rows.length)return false;const r=$('waveform').getBoundingClientRect(),src=host.source();const span=src.duration_s/host.zoom(),at=host.viewStart()+(event.clientX-r.left)/r.width*span;const i=s.rows.findIndex((row,i)=>i&&Math.abs(at-row.start_s)<=tolerance*span/r.width);if(i<=0)return false;s.drag={index:i};host.stop();return true;},
  dragMove(at){if(!s.drag)return false;try{s.rows=moveBoundary(s.rows,s.drag.index,at);s.dirty=true;host.draw();}catch{}return true;},
  dragEnd(){if(!s.drag)return false;s.drag=null;render();return true;}
 };
}
