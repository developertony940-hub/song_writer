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

const DECL_EPS = 1e-6;
function declarationClipKey(c) {
  return JSON.stringify([
    c.source_id, c.start_s, c.end_s, c.gain_db, c.fade_in_s, c.fade_out_s,
    c.label ?? "", c.confirmation?.confirmed === true,
    c.confirmation?.source_sha256 ?? null,
    c.confirmation?.start_s ?? null,
    c.confirmation?.end_s ?? null,
  ]);
}
function declarationSameNumber(a, b) {
  return Number.isFinite(Number(a)) && Math.abs(Number(a) - Number(b)) <= DECL_EPS;
}
export function buildDeclaration(base, current, baseRevision, baseRecipeSha) {
  if (!Array.isArray(base) || !Array.isArray(current) || !base.length || !current.length) return null;
  if (!Number.isInteger(baseRevision) || baseRevision < 1) return null;
  if (typeof baseRecipeSha !== "string" || !baseRecipeSha) return null;
  const baseCopy = copy(base);
  const currentCopy = copy(current);
  const baseById = new Map(baseCopy.map(c => [c.clip_id, c]));
  const currentById = new Map(currentCopy.map(c => [c.clip_id, c]));
  if (baseById.size !== baseCopy.length || currentById.size !== currentCopy.length) return null;
  const baseIds = baseCopy.map(c => c.clip_id);
  const currentIds = currentCopy.map(c => c.clip_id);
  if (baseIds.length === currentIds.length &&
      baseIds.every((id, i) => id === currentIds[i] &&
        declarationClipKey(baseById.get(id)) === declarationClipKey(currentById.get(id)))) {
    return null;
  }
  const removed = baseIds.filter(id => !currentById.has(id));
  const added = currentIds.filter(id => !baseById.has(id));
  const actions = [];
  const splitOld = new Set();
  const splitNew = new Set();
  for (const oldId of removed) {
    if (splitOld.has(oldId)) continue;
    const old = baseById.get(oldId);
    let found = null;
    for (const leftId of added) {
      if (splitNew.has(leftId)) continue;
      const left = currentById.get(leftId);
      if (!left || left.source_id !== old.source_id) continue;
      for (const rightId of added) {
        if (rightId === leftId || splitNew.has(rightId)) continue;
        const right = currentById.get(rightId);
        if (!right || right.source_id !== old.source_id) continue;
        if (declarationSameNumber(left.start_s, old.start_s) &&
            declarationSameNumber(right.end_s, old.end_s) &&
            declarationSameNumber(left.end_s, right.start_s) &&
            declarationSameNumber(left.gain_db, old.gain_db) &&
            declarationSameNumber(right.gain_db, old.gain_db) &&
            declarationSameNumber(left.fade_in_s, Math.min(old.fade_in_s, left.end_s - old.start_s)) &&
            declarationSameNumber(left.fade_out_s, 0) &&
            declarationSameNumber(right.fade_in_s, 0) &&
            declarationSameNumber(right.fade_out_s, Math.min(old.fade_out_s, old.end_s - right.start_s)) &&
            (left.label ?? "") === (old.label ?? "") &&
            (right.label ?? "") === (old.label ?? "")) {
          found = { leftId, rightId, at: left.end_s };
          break;
        }
      }
      if (found) break;
    }
    if (found) {
      actions.push({ kind: "split", clip_id: oldId, at_s: found.at, left_clip_id: found.leftId, right_clip_id: found.rightId });
      splitOld.add(oldId);
      splitNew.add(found.leftId);
      splitNew.add(found.rightId);
    }
  }
  for (const oldId of removed) {
    if (splitOld.has(oldId)) continue;
    const old = baseById.get(oldId);
    actions.push({ kind: "remove", clip_id: oldId, delta_s: -(old.end_s - old.start_s) });
  }
  for (const id of baseIds) {
    if (!currentById.has(id)) continue;
    const old = baseById.get(id);
    const now = currentById.get(id);
    if (old.source_id !== now.source_id) return null;
    if (declarationClipKey(old) !== declarationClipKey(now)) {
      actions.push({ kind: "replace", clip_id: id, delta_s: (now.end_s - now.start_s) - (old.end_s - old.start_s) });
    }
  }
  let working = baseCopy.map(c => copy(c));
  for (const action of actions) {
    if (action.kind !== "split") continue;
    const index = working.findIndex(c => c.clip_id === action.clip_id);
    if (index < 0) return null;
    working.splice(index, 1, copy(currentById.get(action.left_clip_id)), copy(currentById.get(action.right_clip_id)));
  }
  working = working.filter(c => !actions.some(a => a.kind === "remove" && a.clip_id === c.clip_id));
  for (const action of actions) {
    if (action.kind !== "replace") continue;
    const index = working.findIndex(c => c.clip_id === action.clip_id);
    if (index < 0) return null;
    working[index] = copy(currentById.get(action.clip_id));
  }
  let pointer = 0;
  for (const id of currentIds) {
    if (splitNew.has(id) || baseById.has(id)) {
      const index = working.findIndex(c => c.clip_id === id);
      if (index >= pointer) pointer = index + 1;
      continue;
    }
    const clip = currentById.get(id);
    const anchor = pointer < working.length ? working[pointer].clip_id : null;
    actions.push({
      kind: "insert", clip_id: id,
      placement: anchor ? "before" : "append",
      anchor_clip_id: anchor,
      delta_s: clip.end_s - clip.start_s,
    });
    working.splice(pointer, 0, copy(clip));
    pointer += 1;
  }
  for (let i = 0; i < currentIds.length; i += 1) {
    if (working[i]?.clip_id === currentIds[i]) continue;
    const index = working.findIndex(c => c.clip_id === currentIds[i]);
    if (index < 0) return null;
    actions.push({ kind: "reorder", clip_id: currentIds[i], to_index: i });
    const [moving] = working.splice(index, 1);
    working.splice(i, 0, moving);
  }
  if (working.length !== currentIds.length) return null;
  if (!working.every((clip, i) => clip.clip_id === currentIds[i] &&
      declarationClipKey(clip) === declarationClipKey(currentById.get(currentIds[i])))) {
    return null;
  }
  if (!actions.length) return null;
  const total = currentCopy.reduce((sum, c) => sum + c.end_s - c.start_s, 0) -
    baseCopy.reduce((sum, c) => sum + c.end_s - c.start_s, 0);
  return {
    base_revision: baseRevision,
    base_recipe_sha256: baseRecipeSha,
    delta_s: total,
    final_order: currentIds,
    actions,
  };
}

