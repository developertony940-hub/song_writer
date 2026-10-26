// Source maps are observations/suggestions, never arrangement-time estimates.
export function labelsFromLyrics(lyrics='') {
  return String(lyrics).split(/\r?\n/).map(s=>s.trim()).filter(s=>/^\[[^\]\r\n]{1,120}\]$/.test(s)).map(s=>s.slice(1,-1)).slice(0,64);
}
export function validateMap(rows,duration) {
  if(!Array.isArray(rows)||!rows.length||rows.length>64)throw Error('A section map needs 1–64 sections.');
  let end=0;
  for(const row of rows){
    if(typeof row.label!=='string'||!row.label.trim()||row.label.length>120)throw Error('Each section needs a name up to 120 characters.');
    if(!Number.isFinite(row.start_s)||!Number.isFinite(row.end_s)||Math.abs(row.start_s-end)>1e-6||row.end_s<=row.start_s||row.end_s>duration+1e-6)throw Error('Source map must cover the source in order without gaps or overlaps.');
    if(typeof row.confirmed!=='boolean')throw Error('Section confirmation must be explicit.');
    end=row.end_s;
  }
  if(Math.abs(end-duration)>1e-6)throw Error('Source map must reach the end of the source.');
  return true;
}
export const mapKey=(sid,row)=>`${sid}:${row.start_s}:${row.end_s}`;
export function moveBoundary(rows,index,value){
  if(!Number.isFinite(value)||index<1||index>=rows.length||value<=rows[index-1].start_s||value>=rows[index].end_s)throw Error('Boundary must remain between its neighboring boundaries.');
  const next=structuredClone(rows);next[index-1].end_s=value;next[index].start_s=value;
  next[index-1].confirmed=next[index].confirmed=false;return next;
}
export function splitMap(rows,index,at){
  const row=rows[index];if(!row||rows.length>=64||!Number.isFinite(at)||at<=row.start_s||at>=row.end_s)throw Error('Split must be inside a section (maximum 64 sections).');
  const next=structuredClone(rows);next.splice(index,1,{...row,end_s:at,confirmed:false},{...row,label:`${row.label.slice(0,108)} (part 2)`,start_s:at,confirmed:false});return next;
}
export function mergeMap(rows,index){
  if(index<0||index>=rows.length-1)throw Error('Select a section with a following section to merge.');
  const next=structuredClone(rows);next.splice(index,2,{...next[index],end_s:next[index+1].end_s,confirmed:false});return next;
}
export function mapToClips(rows,sid,id){return rows.map(row=>({clip_id:id(),source_id:sid,start_s:row.start_s,end_s:row.end_s,label:row.label,gain_db:0,fade_in_s:0,fade_out_s:0,confirmation:{confirmed:row.confirmed===true,source_sha256:sid,start_s:row.start_s,end_s:row.end_s}}));}
