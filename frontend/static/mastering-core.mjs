export const copy = value => JSON.parse(JSON.stringify(value));
export function time(seconds) { const n=Math.max(0,Number(seconds)||0); return `${String(Math.floor(n/60)).padStart(2,'0')}:${(n%60).toFixed(3).padStart(6,'0')}`; }
export function duration(sections) { return sections.reduce((s,c)=>s+Number(c.end_s)-Number(c.start_s),0); }
export function bound(sections) { let t=0; return sections.map(c=>{ const row={...c,master_start_s:t,master_end_s:t+c.end_s-c.start_s};t=row.master_end_s;return row; }); }
// Ripple insertion changes order only. Never trim or replace a neighbouring clip.
export function insertClip(sections,clip,placement='append',anchorId=null) {
  if(!['append','before','after'].includes(placement))throw Error('Choose append, insert before, or insert after.');
  if(sections.some(c=>c.clip_id===clip.clip_id))throw Error('Inserted section must have a new identifier.');
  if(!Number.isFinite(clip.start_s)||!Number.isFinite(clip.end_s)||clip.end_s<=clip.start_s)throw Error('Inserted section must have a positive finite duration.');
  let index=sections.length;
  if(placement!=='append'){
    index=sections.findIndex(c=>c.clip_id===anchorId);
    if(index<0)throw Error('Insertion anchor no longer exists. Choose a destination again.');
    if(placement==='after')index++;
  }
  const result=copy(sections);result.splice(index,0,copy(clip));return result;
}
export function confirmation(c) { return !!(c.confirmation?.confirmed && c.confirmation.source_sha256===c.source_id && c.confirmation.start_s===c.start_s && c.confirmation.end_s===c.end_s); }
export function invalidate(c) { return {...c,confirmation:{confirmed:false,source_sha256:c.source_id,start_s:c.start_s,end_s:c.end_s}}; }
export function recipeSections(sections) { return sections.map(c=>({clip_id:c.clip_id,source_id:c.source_id,label:c.label,start_s:c.start_s,end_s:c.end_s,gain_db:c.gain_db,fade_in_s:c.fade_in_s,fade_out_s:c.fade_out_s,confirmation:{confirmed:c.confirmation?.confirmed===true,source_sha256:c.source_id,start_s:c.confirmation?.start_s,end_s:c.confirmation?.end_s}})); }
export function splitClip(c,at,ids) { if(!Number.isFinite(at)||at<=c.start_s||at>=c.end_s)throw Error('Split must be strictly inside the selected source interval.');const a=invalidate({...copy(c),clip_id:ids[0],end_s:at,fade_out_s:0});const b=invalidate({...copy(c),clip_id:ids[1],start_s:at,fade_in_s:0});a.fade_in_s=Math.min(a.fade_in_s,at-a.start_s);b.fade_out_s=Math.min(b.fade_out_s,b.end_s-at);return [a,b]; }
export function validate(sections,sources,{requireConfirmation=true}={}) { if(!sections.length)throw Error('Add at least one section.');const ids=new Set();for(const c of sections){const src=sources.find(s=>s.source_id===c.source_id);if(!src)throw Error('A section source is missing.');if(ids.has(c.clip_id))throw Error('Section identifiers must be unique.');ids.add(c.clip_id);if(![c.start_s,c.end_s,c.gain_db,c.fade_in_s,c.fade_out_s].every(Number.isFinite))throw Error('Section settings must be finite numbers.');const d=c.end_s-c.start_s;if(c.start_s<0||d<=0||c.end_s>src.duration_s+0.000001)throw Error('Section boundaries must be inside the source audio.');if(c.gain_db < -60||c.gain_db>12)throw Error('Gain must be between −60 and +12 dB.');if(c.fade_in_s<0||c.fade_out_s<0||c.fade_in_s+c.fade_out_s>d)throw Error('Fades must fit inside the section without overlap.');if(requireConfirmation&&!confirmation(c))throw Error('Listen to and confirm every section boundary before building a master. Draft saving does not require confirmation.');}return true; }
export class EditHistory { constructor(){this.past=[];this.future=[];} push(value){this.past.push(copy(value));if(this.past.length>100)this.past.shift();this.future=[];} undo(current){if(!this.past.length)return null;this.future.push(copy(current));return this.past.pop();} redo(current){if(!this.future.length)return null;this.past.push(copy(current));return this.future.pop();} clear(){this.past=[];this.future=[];} }
