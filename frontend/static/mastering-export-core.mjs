// Source-only exports deliberately exclude arrangement gain, fades and approval.
export function sectionExportRequest(source, range, label){
 if(!source||!/^([a-f0-9]{64})$/.test(source.source_id||''))throw Error('Select a verified source first.');
 const {start,end}=range||{};
 if(typeof start!=='number'||typeof end!=='number'||!Number.isFinite(start)||!Number.isFinite(end)||start<0||end<=start||end>source.duration_s)throw Error('Select a non-empty interval within this source.');
 if(end-start>1800)throw Error('Export one section of at most 30 minutes.');
 const name=String(label??'').trim();
 if(!name||name.length>120||/[\u0000-\u001f\u007f]/.test(name))throw Error('Give this section a name of 1–120 characters without control characters.');
 return {source_sha256:source.source_id,start_s:start,end_s:end,label:name};
}

export function sectionExportLinks(record, base){
 // Build same-origin URLs from validated IDs, never trust stored URL strings.
 if(record?.status!=='completed'||!/^([a-f0-9]{32})$/.test(record.export_id||''))return null;
 const prefix=`${base}/section-exports/${record.export_id}`;
 return {wav:prefix+'/wav',manifest:prefix+'/manifest'};
}
