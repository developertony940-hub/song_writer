// Read preserved PCM once, before handing bytes to the browser media element.
// No retries: HTTP failures remain visible rather than becoming codec errors.
export async function fetchPlaybackBlob(url, fetcher=fetch) {
  const response=await fetcher(url);
  if(!response.ok){
    let detail='';
    try { const body=await response.json(); detail=typeof body.detail==='string'?body.detail:body.detail?.message||''; } catch {}
    throw Error(`Cannot load preserved playback audio (HTTP ${response.status})${detail?': '+detail:'.'} No source was changed. Reload saved state after checking the error; do not re-import or generate audio.`);
  }
  const blob=await response.blob();
  const header=new Uint8Array(await blob.slice(0,12).arrayBuffer());
  const text=String.fromCharCode(...header);
  if(text.slice(0,4)!=='RIFF'||text.slice(8,12)!=='WAVE')throw Error('Playback endpoint did not return a PCM WAV file. No source was changed.');
  return new Blob([blob],{type:'audio/wav'});
}
