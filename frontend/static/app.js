// song_writer - single-page master UI
// Talks to the FastAPI backend at the same origin. No build step required.

const API = "/api";
let currentProject = null;

// ---------- helpers ----------
function toast(msg, kind = "ok") {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast " + kind;
  el.hidden = false;
  clearTimeout(window._toastT);
  window._toastT = setTimeout(() => (el.hidden = true), 4000);
}
async function api(path, opts = {}) {
  const controller = new AbortController();
  const timeoutMs = opts.timeout || 3600000; // default 1h for long generations
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(API + path, {
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      ...opts,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch {}
      throw new Error(detail);
    }
    if (res.status === 204) return null;
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) return res.json();
    return res.text();
  } finally {
    clearTimeout(timer);
  }
}
function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    e.append(c.nodeType ? c : document.createTextNode(c));
  }
  return e;
}
function assetUrl(pid, aid) { return `${API}/projects/${pid}/assets/${aid}`; }

// ---------- runtime status ----------
async function refreshStatus() {
  const elS = document.getElementById("runtime-status");
  try {
    const s = await api("/runtime/status");
    const parts = [];
    for (const [k, v] of Object.entries(s)) {
      if (k === "vram") continue;
      parts.push(`${k}:${v.online ? "on" : "off"}`);
    }
    elS.textContent = parts.join("  |  ");
    elS.style.color = s.lmstudio?.online ? "var(--ok)" : "var(--warn)";
  } catch {
    elS.textContent = "runtime check failed";
    elS.style.color = "var(--err)";
  }
}
setInterval(refreshStatus, 15000);

// ---------- projects ----------
async function loadProjects() {
  const list = document.getElementById("project-list");
  list.innerHTML = "";
  const projects = await api("/projects");
  for (const p of projects) {
    const li = el("li", {
      class: p.id === currentProject?.id ? "active" : "",
      onclick: () => openProject(p.id),
    }, `${p.name} (${p.asset_counts ? Object.values(p.asset_counts).reduce((a, b) => a + b, 0) : 0})`);
    list.append(li);
  }
}

document.getElementById("new-project-btn").addEventListener("click", async () => {
  const name = prompt("Project name?");
  if (!name) return;
  const p = await api("/projects", {
    method: "POST",
    body: JSON.stringify({ name, brief: "", brand_voice: "professional", target_audience: "", notes: "" }),
  });
  await loadProjects();
  openProject(p.id);
});

async function openProject(pid) {
  currentProject = await api(`/projects/${pid}`);
  document.getElementById("empty-state").hidden = true;
  document.getElementById("project-view").hidden = false;
  document.getElementById("proj-name").textContent = currentProject.name;
  document.getElementById("proj-brief").textContent = currentProject.brief || "(no brief)";
  // brief tab
  document.getElementById("brf-name").value = currentProject.name;
  document.getElementById("brf-voice").value = currentProject.brand_voice || "";
  document.getElementById("brf-audience").value = currentProject.target_audience || "";
  document.getElementById("brf-brief").value = currentProject.brief || "";
  document.getElementById("brf-notes").value = currentProject.notes || "";
  renderStageStatus();
  await loadProjects();
  await refreshAllAssets();
  await refreshPlanWorkspace();
  await refreshAudiobook();
}

// ---------- stage approval ----------
function renderStageStatus() {
  const el = document.getElementById("stage-status");
  if (!currentProject?.stages) { el.innerHTML = ""; return; }
  el.innerHTML = "";
  for (const s of STAGE_ORDER) {
    const st = currentProject.stages[s];
    const badge = el("span", { class: `stage-badge ${st}` }, `${s}: ${st}`);
    el.append(badge);
  }
  // Update approve buttons disabled state
  document.querySelectorAll(".approve-btn").forEach(b => {
    const stage = b.dataset.stage;
    b.disabled = currentProject.stages[stage] === "approved";
    b.textContent = currentProject.stages[stage] === "approved" ? "Approved ✓" : `Approve ${stage}`;
  });
}
async function setStage(stage, status) {
  const body = { stage, status };
  await api(`/projects/${currentProject.id}/stage`, { method: "PATCH", body: JSON.stringify(body) });
  currentProject = await api(`/projects/${currentProject.id}`);
  renderStageStatus();
  toast(`Stage "${stage}" ${status}`);
}
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".approve-btn");
  if (!btn) return;
  setStage(btn.dataset.stage, "approved");
});

// ---------- tabs (with sequential approval gating) ----------
const STAGE_ORDER = ["copy", "images", "video", "audio", "final"];
document.getElementById("tabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".tab");
  if (!btn) return;
  switchTab(btn.dataset.tab);
});
function switchTab(tab) {
  if (tab === "timeline" || tab === "brief" || tab === "audiobook") {
    activateTab(tab);
    return;
  }
  // Gate: can't open a generation tab until all previous content stages are approved
  const idx = STAGE_ORDER.indexOf(tab);
  if (idx > 0 && currentProject?.stages) {
    const prevStage = STAGE_ORDER[idx - 1];
    if (currentProject.stages[prevStage] !== "approved") {
      toast(`Approve ${prevStage} before editing ${tab}`, "err");
      return;
    }
  }
  activateTab(tab);
}
function activateTab(tab) {
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach(p => {
    const show = p.dataset.panel === tab;
    p.classList.toggle("active", show);
    p.hidden = !show;
  });
  if (tab === "timeline") renderTimeline();
  if (tab === "audiobook") refreshAudiobook();
}

// ---------- assets rendering ----------
async function refreshAllAssets() {
  await Promise.all([
    renderAssets("copy", "copy-assets"),
    renderAssets("image", "image-assets"),
    renderAssets("video", "video-assets"),
    renderAssets("audio", "audio-assets"),
    renderAssets("final", "final-assets"),
  ]);
  await populateAssembleSelects();
  await populateCopyView();
  await populateImageRefSelect();
  await populateVideoSourceSelect();
}

// ---------- character reference select ----------
async function populateImageRefSelect() {
  if (!currentProject) return;
  const sel = document.getElementById("img-ref");
  const prev = sel.value;
  sel.innerHTML = "";
  const ph = document.createElement("option");
  ph.value = "";
  ph.textContent = "(none — fresh character)";
  sel.append(ph);
  let images = [];
  try { images = await api(`/projects/${currentProject.id}/assets?kind=image`); } catch {}
  for (const a of images) {
    const opt = document.createElement("option");
    opt.value = a.id;
    opt.textContent = a.filename;
    sel.append(opt);
  }
  if (prev && images.some(a => a.id === prev)) sel.value = prev;
  else {
    // Default to character_reference if present
    const ref = images.find(a => a.filename === "character_reference.png");
    if (ref) sel.value = ref.id;
  }
}

// ---------- video source image select ----------
async function populateVideoSourceSelect() {
  if (!currentProject) return;
  const sel = document.getElementById("vid-source");
  const prev = sel.value;
  sel.innerHTML = "";
  const ph = document.createElement("option");
  ph.value = "";
  ph.textContent = "— select image —";
  sel.append(ph);
  let images = [];
  try { images = await api(`/projects/${currentProject.id}/assets?kind=image`); } catch {}
  for (const a of images) {
    const opt = document.createElement("option");
    opt.value = a.id;
    opt.textContent = `${a.filename} (${a.id})`;
    sel.append(opt);
  }
  if (prev && images.some(a => a.id === prev)) sel.value = prev;
}

// ---------- copy output display ----------
async function populateCopyView() {
  if (!currentProject) return;
  const sel = document.getElementById("copy-view");
  const prev = sel.value;
  sel.innerHTML = "";
  let copies = [];
  try { copies = await api(`/projects/${currentProject.id}/assets?kind=copy`); } catch {}
  const ph = document.createElement("option");
  ph.value = "";
  ph.textContent = copies.length ? "— select copy asset —" : "(no copy assets)";
  sel.append(ph);
  for (const a of copies) {
    const opt = document.createElement("option");
    opt.value = a.id;
    opt.textContent = a.filename;
    sel.append(opt);
  }
  // restore previous selection if it still exists
  if (prev && copies.some(a => a.id === prev)) sel.value = prev;
  // else auto-select the most recent copy asset
  else if (copies.length) sel.value = copies[copies.length - 1].id;
  if (sel.value) await loadCopyIntoView(sel.value);
}
async function loadCopyIntoView(aid) {
  const out = document.getElementById("copy-output");
  if (!aid) { out.textContent = "No copy generated yet."; return; }
  out.textContent = "Loading…";
  try {
    const res = await fetch(assetUrl(currentProject.id, aid));
    if (!res.ok) throw new Error(res.statusText);
    out.textContent = await res.text();
  } catch (e) {
    out.textContent = `Failed to load copy: ${e.message}`;
  }
}
document.getElementById("copy-view").addEventListener("change", (e) => loadCopyIntoView(e.target.value));
document.getElementById("copy-refresh-btn").addEventListener("click", () => populateCopyView());
async function populateAssembleSelects() {
  if (!currentProject) return;
  const [videos, audios, musics] = await Promise.all([
    api(`/projects/${currentProject.id}/assets?kind=video`).catch(() => []),
    api(`/projects/${currentProject.id}/assets?kind=audio`).catch(() => []),
    api(`/projects/${currentProject.id}/assets?kind=music`).catch(() => []),
  ]);
  fillSelect("asm-video", videos);
  fillSelect("asm-audio", audios);
  fillSelect("asm-music", musics, true);
}
function fillSelect(selectId, assets, isOptional = false) {
  const sel = document.getElementById(selectId);
  const prev = sel.value;
  sel.innerHTML = "";
  const ph = document.createElement("option");
  ph.value = "";
  ph.textContent = isOptional ? "— none —" : `— select ${selectId.replace("asm-", "")} —`;
  sel.append(ph);
  for (const a of assets) {
    const opt = document.createElement("option");
    opt.value = a.id;
    opt.textContent = `${a.filename} (${a.id})`;
    sel.append(opt);
  }
  sel.value = prev;
}
async function renderAssets(kind, containerId) {
  const container = document.getElementById(containerId);
  container.innerHTML = "";
  if (!currentProject) return;
  try {
    const assets = await api(`/projects/${currentProject.id}/assets?kind=${kind}`);
    for (const a of assets) {
      container.append(renderAssetCard(a));
    }
  } catch (e) {
    container.append(el("p", { class: "muted" }, `load failed: ${e.message}`));
  }
}
function renderAssetCard(a) {
  const card = el("div", { class: "asset-card" });
  const url = assetUrl(a.project_id, a.id);
  if (a.kind === "image") {
    card.append(el("img", { src: url, alt: a.filename }));
  } else if (a.kind === "video" || a.kind === "final") {
    card.append(el("video", { src: url, controls: "true" }));
  } else if (a.kind === "audio" || a.kind === "music") {
    card.append(el("audio", { src: url, controls: "true" }));
  } else if (a.kind === "copy") {
    card.append(el("pre", { style: "max-height:160px;overflow:auto;font-size:11px;white-space:pre-wrap;" }));
    fetch(url).then(r => r.text()).then(t => { card.querySelector("pre").textContent = t.slice(0, 1000); });
  }
  card.append(el("div", { class: "name" }, a.filename));
  card.append(el("div", { class: "id" }, a.id));
  if (a.meta && Object.keys(a.meta).length) {
    card.append(el("div", { class: "meta" }, JSON.stringify(a.meta).slice(0, 160)));
  }
  // Trashcan delete button
  const del = el("button", { class: "del-btn", title: "Delete asset", onclick: () => deleteAsset(a) }, "🗑");
  card.append(del);
  return card;
}

async function deleteAsset(a) {
  const name = a.filename || a.id;
  if (!confirm(`Delete "${name}"? This cannot be undone.`)) return;
  try {
    await api(`/projects/${a.project_id}/assets/${a.id}`, { method: "DELETE" });
    toast("Deleted");
    await refreshAllAssets();
  } catch (e) {
    toast(e.message, "err");
  }
}

// ---------- generation handlers ----------
function disabledWhile(btn, fn) {
  return async () => {
    btn.disabled = true;
    showProgress(true);
    try { await fn(); }
    catch (e) { toast(e.message, "err"); }
    finally {
      btn.disabled = false;
      showProgress(false);
    }
  };
}
function showProgress(on) {
  const el = document.getElementById("progress");
  if (!el) return;
  el.hidden = !on;
  if (on) {
    const bar = document.getElementById("progress-bar");
    bar.style.width = "0%";
    // indeterminate animation via CSS
    bar.classList.add("indeterminate");
  } else {
    const bar = document.getElementById("progress-bar");
    bar.classList.remove("indeterminate");
    bar.style.width = "100%";
    setTimeout(() => { el.hidden = true; }, 300);
  }
}

document.getElementById("gen-copy-btn").addEventListener("click", disabledWhile(
  document.getElementById("gen-copy-btn"),
  async () => {
    const body = {
      project_id: currentProject.id,
      prompt: document.getElementById("copy-prompt").value,
      model: document.getElementById("copy-model").value || null,
      max_tokens: parseInt(document.getElementById("copy-maxtokens").value) || 4096,
      save_as: document.getElementById("copy-saveas").value || null,
    };
    await api("/generate/copy", { method: "POST", body: JSON.stringify(body) });
    toast("Copy generated");
    await refreshAllAssets();
  }
));

document.getElementById("img-rescan-btn").addEventListener("click", disabledWhile(
  document.getElementById("img-rescan-btn"),
  async () => {
    const res = await fetch(`/api/projects/${currentProject.id}/assets/rescan`, { method: "POST" });
    if (!res.ok) {
      let d = res.statusText; try { d = (await res.json()).detail || d; } catch {}
      throw new Error(d);
    }
    const summary = await res.json();
    toast(`Rescan: +${summary.added.length} added, -${summary.removed.length} removed, ${summary.total} total`);
    await refreshAllAssets();
  }
));

document.getElementById("img-upload-btn").addEventListener("click", disabledWhile(
  document.getElementById("img-upload-btn"),
  async () => {
    const fileEl = document.getElementById("img-upload");
    if (!fileEl.files || !fileEl.files.length) {
      toast("Choose a file first", "err");
      return;
    }
    const file = fileEl.files[0];
    const fd = new FormData();
    fd.append("file", file);
    const saveAs = document.getElementById("img-upload-saveas").value.trim();
    let url = `/api/projects/${currentProject.id}/assets?kind=image`;
    if (saveAs) url += `&save_as=${encodeURIComponent(saveAs)}`;
    const res = await fetch(url, { method: "POST", body: fd });
    if (!res.ok) {
      let d = res.statusText; try { d = (await res.json()).detail || d; } catch {}
      throw new Error(d);
    }
    const asset = await res.json();
    toast(`Uploaded ${asset.filename}`);
    fileEl.value = "";
    document.getElementById("img-upload-saveas").value = "";
    await refreshAllAssets();
  }
));

document.getElementById("gen-image-btn").addEventListener("click", disabledWhile(
  document.getElementById("gen-image-btn"),
  async () => {
    const seedVal = document.getElementById("img-seed").value;
    const refVal = document.getElementById("img-ref").value;
    const denoiseVal = document.getElementById("img-denoise").value;
    const body = {
      project_id: currentProject.id,
      prompt: document.getElementById("img-prompt").value,
      negative_prompt: document.getElementById("img-neg").value,
      width: parseInt(document.getElementById("img-w").value),
      height: parseInt(document.getElementById("img-h").value),
      steps: parseInt(document.getElementById("img-steps").value),
      seed: seedVal ? parseInt(seedVal) : null,
      save_as: document.getElementById("img-saveas").value || null,
      reference_image: refVal || null,
      denoise: refVal && denoiseVal !== "" ? parseFloat(denoiseVal) : null,
    };
    toast(refVal ? "Generating image with character ref… (30-90s)" : "Generating image… (30-90s)");
    await api("/generate/image", { method: "POST", body: JSON.stringify(body) });
    toast("Image generated");
    await refreshAllAssets();
  }
));

document.getElementById("gen-video-btn").addEventListener("click", disabledWhile(
  document.getElementById("gen-video-btn"),
  async () => {
    const body = {
      project_id: currentProject.id,
      mode: document.getElementById("vid-mode").value,
      source_image: document.getElementById("vid-source").value || null,
      prompt: document.getElementById("vid-prompt").value,
      duration_s: parseFloat(document.getElementById("vid-dur").value),
      fps: parseInt(document.getElementById("vid-fps").value),
      width: parseInt(document.getElementById("vid-w").value),
      height: parseInt(document.getElementById("vid-h").value),
      save_as: document.getElementById("vid-saveas").value || null,
    };
    toast("Generating video (this may take minutes)…");
    await api("/generate/video", { method: "POST", body: JSON.stringify(body) });
    toast("Video generated");
    await refreshAllAssets();
  }
));

document.getElementById("gen-audio-btn").addEventListener("click", disabledWhile(
  document.getElementById("gen-audio-btn"),
  async () => {
    const body = {
      project_id: currentProject.id,
      text: document.getElementById("aud-text").value,
      voice: document.getElementById("aud-voice").value || null,
      speed: parseFloat(document.getElementById("aud-speed").value),
      save_as: document.getElementById("aud-saveas").value || null,
    };
    await api("/generate/audio", { method: "POST", body: JSON.stringify(body) });
    toast("Audio generated");
    await refreshAllAssets();
  }
));

document.getElementById("assemble-btn").addEventListener("click", disabledWhile(
  document.getElementById("assemble-btn"),
  async () => {
    const videoId = document.getElementById("asm-video").value;
    const audioId = document.getElementById("asm-audio").value;
    if (!videoId && !audioId) {
      toast("Select at least a video or audio asset to assemble", "err");
      return;
    }
    const body = {
      project_id: currentProject.id,
      video_asset_id: videoId || null,
      audio_asset_id: audioId || null,
      music_asset_id: document.getElementById("asm-music").value || null,
      subtitle_text: document.getElementById("asm-subs").value || null,
      output_name: document.getElementById("asm-out").value || "final",
    };
    await api("/generate/assemble", { method: "POST", body: JSON.stringify(body) });
    toast("Assembly complete");
    await refreshAllAssets();
  }
));

document.getElementById("upscale-btn").addEventListener("click", disabledWhile(
  document.getElementById("upscale-btn"),
  async () => {
    const srcId = document.getElementById("ups-src").value.trim();
    if (!srcId) {
      toast("Enter a video asset id to upscale", "err");
      return;
    }
    const body = {
      project_id: currentProject.id,
      video_asset_id: srcId,
      model_name: document.getElementById("ups-model").value,
      target_size: parseInt(document.getElementById("ups-target").value),
      save_as: document.getElementById("ups-saveas").value || null,
    };
    toast("Upscaling video (this may take minutes)…");
    await api("/generate/upscale", { method: "POST", body: JSON.stringify(body) });
    toast("Video upscaled");
    await refreshAllAssets();
  }
));

document.getElementById("save-brief-btn").addEventListener("click", async () => {
  const body = new URLSearchParams({
    name: document.getElementById("brf-name").value,
    brand_voice: document.getElementById("brf-voice").value,
    target_audience: document.getElementById("brf-audience").value,
    brief: document.getElementById("brf-brief").value,
    notes: document.getElementById("brf-notes").value,
  });
  await api(`/projects/${currentProject.id}?${body}`, { method: "PATCH" });
  toast("Brief saved");
  await openProject(currentProject.id);
});

// ---------- timeline composition ----------
function defaultComposition() {
  if (currentProject?.composition) return currentProject.composition;
  return { total_duration_s: 30, video_clips: [], audio_clips: [], subtitles: [] };
}
function fmtTime(s) {
  s = Math.max(0, s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}
async function renderTimeline() {
  if (!currentProject) return;
  const comp = defaultComposition();
  document.getElementById("tl-duration").value = comp.total_duration_s || 30;
  const tracks = document.getElementById("tl-tracks");
  tracks.innerHTML = "";

  // Video track
  const vTrack = el("div", { class: "tl-track" });
  vTrack.append(el("div", { class: "tl-track-label" }, "VIDEO"));
  const vLane = el("div", { class: "tl-lane" });
  vTrack.append(vLane);
  tracks.append(vTrack);
  for (const c of comp.video_clips || []) {
    const dur = (c.end_s ?? comp.total_duration_s) - c.start_s;
    vLane.append(tlClip(c, c.start_s, dur, "video"));
  }

  // Audio track
  const aTrack = el("div", { class: "tl-track" });
  aTrack.append(el("div", { class: "tl-track-label" }, "AUDIO"));
  const aLane = el("div", { class: "tl-lane" });
  aTrack.append(aLane);
  tracks.append(aTrack);
  for (const c of comp.audio_clips || []) {
    const dur = (c.end_s ?? comp.total_duration_s) - c.start_s;
    aLane.append(tlClip(c, c.start_s, dur, "audio"));
  }

  // Subtitle track
  const sTrack = el("div", { class: "tl-track" });
  sTrack.append(el("div", { class: "tl-track-label" }, "SUBTITLES"));
  const sLane = el("div", { class: "tl-lane" });
  sTrack.append(sLane);
  tracks.append(sTrack);
  for (const s of comp.subtitles || []) {
    const dur = s.end_s - s.start_s;
    sLane.append(tlSub(s, s.start_s, dur));
  }

  renderPlayhead();
  renderPreview();
}
function tlClip(clip, start, dur, kind) {
  const total = defaultComposition().total_duration_s || 1;
  const w = (dur / total) * 100;
  const left = (start / total) * 100;
  const elClip = el("div", { class: `tl-clip ${kind}` });
  elClip.style.left = left + "%";
  elClip.style.width = w + "%";
  const assetId = clip.asset_id || "";
  elClip.title = `${assetId}  ${fmtTime(start)} → ${fmtTime(start + dur)}`;
  elClip.textContent = assetId.slice(0, 12);
  return elClip;
}
function renderPlayhead() {
  document.querySelectorAll(".tl-lane").forEach(lane => {
    lane.classList.remove("has-playhead");
    // playhead overlays via CSS on the track container instead
  });
}
async function renderPreview() {
  const comp = defaultComposition();
  const t = parseFloat(document.getElementById("tl-time").value) || 0;
  const el = document.getElementById("tl-preview");
  if (!comp.video_clips?.length) {
    el.innerHTML = "<p class='muted'>Add a video clip to preview.</p>";
    el.hidden = false;
    return;
  }
  el.innerHTML = "<p class='muted'>Rendering preview…</p>";
  el.hidden = false;
  try {
    const res = await fetch(API + "/generate/preview-frame", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: currentProject.id, timestamp_s: t, composition: comp }),
    });
    if (!res.ok) {
      let d = res.statusText; try { d = (await res.json()).detail || d; } catch {}
      el.innerHTML = `<p class="err">Preview failed: ${d}</p>`;
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    el.innerHTML = "";
    const img = el("img", { src: url, class: "preview-img" });
    el.append(el("div", { class: "preview-time" }, `t=${fmtTime(t)}`));
    el.append(img);
  } catch (e) {
    el.innerHTML = `<p class="err">Preview failed: ${e.message}</p>`;
  }
}

document.getElementById("tl-render-preview").addEventListener("click", () => renderPreview());
document.getElementById("tl-save-btn").addEventListener("click", async () => {
  const comp = defaultComposition();
  comp.total_duration_s = parseFloat(document.getElementById("tl-duration").value) || 30;
  currentProject = await api(`/projects/${currentProject.id}/composition`, {
    method: "PUT",
    body: JSON.stringify({ composition: comp }),
  });
  // The server response is authoritative: it contains persisted 30-fps edit points.
  toast(`Timeline saved (${Math.round(currentProject.composition.total_duration_s * 30)} frames)`);
  renderTimeline();
});
document.getElementById("tl-render-master").addEventListener("click", disabledWhile(
  document.getElementById("tl-render-master"),
  async () => {
    if (!currentProject?.composition) {
      toast("Save a timeline before rendering a master", "err");
      return;
    }
    const output_name = document.getElementById("tl-output-name").value.trim() || "timeline_master";
    toast("Rendering saved timeline master (QC will run before it appears)…");
    await api("/generate/assemble-saved-composition", {
      method: "POST", body: JSON.stringify({ project_id: currentProject.id, output_name }),
    });
    currentProject = await api(`/projects/${currentProject.id}`);
    renderStageStatus();
    await refreshAllAssets();
    toast("Timeline master passed QC and is ready for review");
  }
));
document.getElementById("tl-add-sub").addEventListener("click", () => {
  const comp = defaultComposition();
  comp.subtitles = comp.subtitles || [];
  comp.subtitles.push({ start_s: 0, end_s: 3, text: "New subtitle", x: 540, y: 1500, width: 936, height: 120, font_size: 48, color: "white", bg_color: "black@0.5", align: "center" });
  currentProject.composition = comp;
  renderTimeline();
});

// ---------- model dropdown (from LM Studio) ----------
async function populateModelDropdown() {
  const sel = document.getElementById("copy-model");
  const prev = sel.value;
  sel.innerHTML = "";
  try {
    const r = await fetch(API + "/runtime/lmstudio/models");
    if (!r.ok) throw new Error(r.statusText);
    const data = await r.json();
    const models = data.models || [];
    for (const m of models) {
      const opt = document.createElement("option");
      opt.value = m;
      opt.textContent = m;
      sel.append(opt);
    }
    // Keep the UI selection aligned with settings.toml's documented default;
    // live inventory still wins when the operator has loaded a different model.
    const def = "qwen3-coder-30b-a3b-instruct";
    const defMatch = models.find(m => m.toLowerCase().includes(def));
    if (defMatch) sel.value = defMatch;
    else if (models.length) sel.value = models[0];
    if (prev && models.includes(prev)) sel.value = prev;
  } catch (e) {
    sel.innerHTML = "";
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "LM Studio offline: " + e.message;
    sel.append(opt);
  }
}

// ---------- init ----------
(async () => {
  await refreshStatus();
  await populateModelDropdown();
  await loadProjects();
})();
// ---------- Phase 2 canonical VideoPlan ----------
let latestPlanPreview = null;
async function refreshPlanWorkspace() {
  if (!currentProject) return;
  const status = document.getElementById("plan-status");
  try {
    const saved = await api(`/projects/${currentProject.id}/video-plan`);
    document.getElementById("plan-json").value = JSON.stringify(saved.plan, null, 2);
    status.textContent = `Saved VideoPlan ${saved.fingerprint}; ${currentProject.preview_approval ? "proxy approved" : "proxy not approved"}.`;
  } catch { status.textContent = "Save a VideoPlan to begin plan-driven production."; }
  const candidates = await api(`/projects/${currentProject.id}/candidates`).catch(() => []);
  const container = document.getElementById("candidate-assets");
  container.innerHTML = "";
  for (const candidate of candidates) {
    const card = el("div", { class: "asset-card" });
    const asset = candidate.asset_id;
    if (candidate.kind === "image") card.append(el("img", { src: assetUrl(currentProject.id, asset), alt: candidate.scene_id }));
    else card.append(el("video", { src: assetUrl(currentProject.id, asset), controls: "true" }));
    card.append(el("div", { class: "name" }, `${candidate.scene_id} · ${candidate.kind} · ${candidate.status}${candidate.selected ? " ✓ selected" : ""}`));
    card.append(el("div", { class: "meta" }, `seed ${candidate.seed}${candidate.reference_asset_id ? ` · ref ${candidate.reference_asset_id}` : ""}${candidate.parent_candidate_id ? ` · revision of ${candidate.parent_candidate_id}` : ""}`));
    const accept = el("button", { onclick: () => reviewPlanCandidate(candidate, "accepted", true) }, "Accept + Select");
    const reject = el("button", { onclick: () => reviewPlanCandidate(candidate, "rejected", false) }, "Reject");
    const revise = el("button", { onclick: () => revisePlanCandidate(candidate) }, "Revise");
    card.append(el("div", { class: "row" }, accept, reject, revise)); container.append(card);
  }
  const previews = await api(`/projects/${currentProject.id}/assets?kind=preview`).catch(() => []);
  const previewBox = document.getElementById("plan-preview-assets"); if (previewBox) { previewBox.innerHTML = ""; previews.forEach(a => { const card = renderAssetCard(a); card.append(el("button", { onclick: () => { latestPlanPreview = a.id; toast(`Selected proxy ${a.name || a.id} for approval.`); } }, "Select for approval")); previewBox.append(card); }); if (previews.length && !latestPlanPreview) latestPlanPreview = previews[previews.length - 1].id; }
}
async function reviewPlanCandidate(candidate, status, select) {
  const reason = prompt(status === "accepted" ? "Why does this candidate work?" : "Why reject this candidate?", status === "accepted" ? "Matches the plan and is safe to use." : "Does not meet the brief.");
  if (!reason) return;
  await api(`/projects/${currentProject.id}/candidates/${candidate.id}`, { method: "PATCH", body: JSON.stringify({ status, select, reason }) });
  currentProject = await api(`/projects/${currentProject.id}`); renderStageStatus(); await refreshPlanWorkspace();
}
async function revisePlanCandidate(candidate) {
  const instructions = prompt("Revision direction (keeps a parent, seed, and reference trail):"); if (!instructions) return;
  await api("/generate/plan/revise", { method: "POST", body: JSON.stringify({ project_id: currentProject.id, candidate_id: candidate.id, instructions }) });
  await refreshPlanWorkspace();
}
document.getElementById("plan-draft-btn").addEventListener("click", disabledWhile(document.getElementById("plan-draft-btn"), async () => {
  const result = await api("/generate/video-plan", { method: "POST", body: JSON.stringify({ project_id: currentProject.id, platform: "tiktok" }) });
  currentProject = result.project; renderStageStatus(); await refreshPlanWorkspace(); toast("Validated VideoPlan draft generated; review and edit it before approval.");
}));
document.getElementById("plan-save-btn").addEventListener("click", disabledWhile(document.getElementById("plan-save-btn"), async () => {
  let plan; try { plan = JSON.parse(document.getElementById("plan-json").value); } catch { throw new Error("VideoPlan must be valid JSON"); }
  currentProject = await api(`/projects/${currentProject.id}/video-plan`, { method: "PUT", body: JSON.stringify({ plan }) }); renderStageStatus(); await refreshPlanWorkspace(); toast("Validated VideoPlan saved; approve copy/content before downstream stages.");
}));
function planGeneration(buttonId, kind) { document.getElementById(buttonId).addEventListener("click", disabledWhile(document.getElementById(buttonId), async () => {
  await api("/generate/plan/candidates", { method: "POST", body: JSON.stringify({ project_id: currentProject.id, kind }) }); await refreshPlanWorkspace(); toast(`${kind} candidates generated; review every scene.`);
})); }
planGeneration("plan-images-btn", "image"); planGeneration("plan-videos-btn", "video");
document.getElementById("plan-audio-btn").addEventListener("click", disabledWhile(document.getElementById("plan-audio-btn"), async () => { await api("/generate/plan/audio", { method: "POST", body: JSON.stringify({ project_id: currentProject.id }) }); currentProject = await api(`/projects/${currentProject.id}`); renderStageStatus(); await refreshPlanWorkspace(); toast("Plan narration generated."); }));
document.getElementById("plan-preview-btn").addEventListener("click", disabledWhile(document.getElementById("plan-preview-btn"), async () => { const asset = await api("/generate/plan/preview", { method: "POST", body: JSON.stringify({ project_id: currentProject.id, output_name: document.getElementById("plan-preview-name").value || "plan_preview" }) }); latestPlanPreview = asset.id; await refreshPlanWorkspace(); toast("Proxy rendered; watch it and explicitly approve it."); }));
document.getElementById("plan-approve-preview-btn").addEventListener("click", async () => { if (!latestPlanPreview) { toast("Render/select a proxy first", "err"); return; } await api("/generate/plan/approve-preview", { method: "POST", body: JSON.stringify({ project_id: currentProject.id, preview_asset_id: latestPlanPreview }) }); currentProject = await api(`/projects/${currentProject.id}`); await refreshPlanWorkspace(); toast("Proxy explicitly approved. Master render is now unlocked."); });
document.getElementById("plan-master-btn").addEventListener("click", disabledWhile(document.getElementById("plan-master-btn"), async () => { await api("/generate/plan/render-master", { method: "POST", body: JSON.stringify({ project_id: currentProject.id, output_name: document.getElementById("plan-master-name").value || "final_master" }) }); currentProject = await api(`/projects/${currentProject.id}`); renderStageStatus(); await refreshAllAssets(); toast("Plan master passed delivery QC and awaits final approval."); }));

// ---------- Phase 1 audiobook slice editor (isolated panel) ----------
let abState = null;

function abEsc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function abShort(h) { return h ? h.slice(0, 10) + "…" : "—"; }

async function refreshAudiobook() {
  if (!currentProject) return;
  const pid = currentProject.id;
  try {
    const status = await api(`/audiobook/${pid}/status`);
    abState = status.state;
    await renderAudiobookChapter(pid, status);
    renderAudiobookSpeakers();
    renderAudiobookSpans();
    renderAudiobookChunks();
    renderAudiobookMaster(status);
    renderAudiobookRecovery(status);
    const note = document.getElementById("ab-render-note");
    note.textContent = status.live_generation_authorized
      ? "live generation: authorized"
      : "live generation: held server-side (real render will be refused)";
    note.className = status.live_generation_authorized ? "muted" : "err";
  } catch (e) {
    abState = null;
    document.getElementById("ab-chapter-view").innerHTML = "<p class='muted'>No chapter imported yet.</p>";
    document.getElementById("ab-coverage").textContent = "";
    document.getElementById("ab-speakers").innerHTML = "";
    document.getElementById("ab-spans").innerHTML = "";
    document.getElementById("ab-exclusions").innerHTML = "";
    document.getElementById("ab-chunks").innerHTML = "";
    document.getElementById("ab-master").textContent = "";
    document.getElementById("ab-verification").textContent = "";
    document.getElementById("ab-recovery-box").hidden = true;
  }
}

async function renderAudiobookChapter(pid, status) {
  const view = document.getElementById("ab-chapter-view");
  const cov = document.getElementById("ab-coverage");
  let chapter = { imported: false, text: "" };
  try { chapter = await api(`/audiobook/${pid}/chapter`); } catch {}
  if (!chapter.imported) {
    view.innerHTML = "<p class='muted'>No chapter imported yet.</p>";
    cov.textContent = "";
    return;
  }
  const text = chapter.text;
  const ann = (status.state && status.state.annotations) || { spans: [], exclusions: [] };
  const marks = [
    ...(ann.spans || []).map(s => ({ ...s, _t: "span" })),
    ...(ann.exclusions || []).map(e => ({ ...e, _t: "excl" })),
  ].sort((a, b) => a.start_char - b.start_char);
  let html = "", cursor = 0;
  for (const m of marks) {
    if (m.start_char > cursor) html += abEsc(text.slice(cursor, m.start_char));
    const slice = abEsc(text.slice(m.start_char, m.end_char));
    if (m._t === "span") {
      const cls = m.needs_review ? "ab-span review" : "ab-span";
      const pct = Math.round((m.confidence ?? 1) * 100);
      html += `<mark class="${cls}" title="${abEsc(m.speaker)} (${pct}%)">${slice}</mark>`;
    } else {
      html += `<del title="${abEsc(m.reason || "excluded")}">${slice}</del>`;
    }
    cursor = m.end_char;
  }
  if (cursor < text.length) html += abEsc(text.slice(cursor));
  view.innerHTML = html.replace(/\n/g, "<br>");
  const errors = status.coverage_errors || [];
  const unresolved = status.unresolved_spans || [];
  cov.textContent = errors.length
    ? "Coverage: " + errors.join(" | ")
    : `Coverage complete${unresolved.length ? " · unresolved: " + unresolved.join(", ") : ""}.`;
  cov.className = errors.length ? "err" : "muted";
}

function abSpeakerRow(name, voice, speed) {
  const row = el("div", { class: "row ab-row" });
  row.append(el("input", { class: "ab-name", value: name ?? "", placeholder: "name" }));
  row.append(el("input", { class: "ab-voice", value: voice ?? "", placeholder: "voice" }));
  row.append(el("input", { class: "ab-speed", type: "number", step: "0.05", min: "0.1", max: "4", value: speed ?? 1 }));
  row.append(el("button", { class: "ab-del", title: "Remove", onclick: () => row.remove() }, "✕"));
  return row;
}
function renderAudiobookSpeakers() {
  const box = document.getElementById("ab-speakers");
  box.innerHTML = "";
  for (const [name, sp] of Object.entries(abState?.speakers || {})) {
    box.append(abSpeakerRow(name, sp.voice, sp.speed));
  }
}

function abSpanRow(s) {
  const row = el("div", { class: "row ab-row" });
  row.append(el("input", { class: "ab-id", value: s.id ?? "", placeholder: "id" }));
  row.append(el("input", { class: "ab-speaker", value: s.speaker ?? "", placeholder: "speaker" }));
  row.append(el("input", { class: "ab-start", type: "number", value: s.start_char ?? 0 }));
  row.append(el("input", { class: "ab-end", type: "number", value: s.end_char ?? 0 }));
  row.append(el("input", { class: "ab-confidence", type: "number", step: "0.05", min: "0", max: "1", value: s.confidence ?? 1 }));
  const label = el("label", { class: "ab-inline" }, "review");
  const cb = el("input", { class: "ab-needs-review", type: "checkbox" });
  if (s.needs_review) cb.checked = true;
  label.prepend(cb);
  row.append(label);
  row.append(el("button", { class: "ab-del", title: "Remove", onclick: () => row.remove() }, "✕"));
  return row;
}
function abExclusionRow(e) {
  const row = el("div", { class: "row ab-row" });
  row.append(el("input", { class: "ab-ex-start", type: "number", value: e.start_char ?? 0 }));
  row.append(el("input", { class: "ab-ex-end", type: "number", value: e.end_char ?? 0 }));
  row.append(el("input", { class: "ab-ex-reason", value: e.reason ?? "", placeholder: "reason" }));
  row.append(el("button", { class: "ab-del", title: "Remove", onclick: () => row.remove() }, "✕"));
  return row;
}
function renderAudiobookSpans() {
  const spans = document.getElementById("ab-spans");
  const exclusions = document.getElementById("ab-exclusions");
  spans.innerHTML = "";
  exclusions.innerHTML = "";
  for (const s of (abState?.annotations?.spans || [])) spans.append(abSpanRow(s));
  for (const e of (abState?.annotations?.exclusions || [])) exclusions.append(abExclusionRow(e));
}

function readAudiobookSpans() {
  const spans = [...document.querySelectorAll("#ab-spans .ab-row")].map(row => ({
    id: row.querySelector(".ab-id").value.trim(),
    speaker: row.querySelector(".ab-speaker").value.trim(),
    start_char: parseInt(row.querySelector(".ab-start").value, 10),
    end_char: parseInt(row.querySelector(".ab-end").value, 10),
    confidence: parseFloat(row.querySelector(".ab-confidence").value),
    needs_review: row.querySelector(".ab-needs-review").checked,
  }));
  const exclusions = [...document.querySelectorAll("#ab-exclusions .ab-row")].map(row => ({
    start_char: parseInt(row.querySelector(".ab-ex-start").value, 10),
    end_char: parseInt(row.querySelector(".ab-ex-end").value, 10),
    reason: row.querySelector(".ab-ex-reason").value.trim(),
  }));
  return { spans, exclusions };
}

function renderAudiobookChunks() {
  const box = document.getElementById("ab-chunks");
  box.innerHTML = "";
  const chunks = abState?.chunks || [];
  if (!chunks.length) { box.append(el("p", { class: "muted" }, "No chunks planned yet.")); return; }
  for (const c of chunks) {
    box.append(el("div", { class: "row ab-row" },
      `${c.span_id} · ${c.speaker} · ${c.status} · [${c.start_char}, ${c.end_char})`));
  }
}

function renderAudiobookMaster(status) {
  const st = status.state || {};
  const box = document.getElementById("ab-master");
  const parts = [];
  parts.push(st.master
    ? `master wav ${abShort(st.master.wav_sha256)} · mp3 ${abShort(st.master.mp3_sha256)} · pause ${st.master.pause_ms}ms`
    : "No master assembled yet.");
  parts.push(st.review ? `reviewed ${abShort(st.review.wav_sha256)}/${abShort(st.review.mp3_sha256)}` : "not reviewed");
  if (st.approval) parts.push(`approved ${st.approval.approved_at}`);
  box.textContent = parts.join(" · ");
  const v = status.verification || {};
  const vb = document.getElementById("ab-verification");
  const reasons = (v.reasons || []).join("; ");
  vb.textContent = "verification: " + (v.approved ? "approved" : "not approved" + (reasons ? " (" + reasons + ")" : ""));
  vb.className = v.approved ? "muted" : "err";
}

function renderAudiobookRecovery(status) {
  const box = document.getElementById("ab-recovery-box");
  const blocked = status.state?.recovery?.blocked || status.capacity_hold;
  box.hidden = !blocked;
  document.getElementById("ab-recovery-msg").textContent =
    status.state?.recovery?.reason || (status.capacity_hold ? status.capacity_hold.reason : "");
}

document.getElementById("ab-import-btn").addEventListener("click", disabledWhile(
  document.getElementById("ab-import-btn"),
  async () => {
    const text = document.getElementById("ab-chapter-text").value;
    if (!text.trim()) { toast("Paste chapter text first", "err"); return; }
    await api(`/audiobook/${currentProject.id}/chapter`, { method: "POST", body: JSON.stringify({
      text, filename: document.getElementById("ab-filename").value.trim() || "chapter.txt",
    }) });
    toast("Chapter imported and locked");
    await refreshAudiobook();
  }
));
document.getElementById("ab-add-speaker-btn").addEventListener("click", () =>
  document.getElementById("ab-speakers").append(abSpeakerRow("", "", 1)));
document.getElementById("ab-save-speakers-btn").addEventListener("click", disabledWhile(
  document.getElementById("ab-save-speakers-btn"),
  async () => {
    const speakers = [...document.querySelectorAll("#ab-speakers .ab-row")].map(r => ({
      name: r.querySelector(".ab-name").value.trim(),
      voice: r.querySelector(".ab-voice").value.trim(),
      speed: parseFloat(r.querySelector(".ab-speed").value) || 1,
    })).filter(s => s.name && s.voice);
    if (!speakers.length) { toast("Add at least one speaker with a name and voice", "err"); return; }
    await api(`/audiobook/${currentProject.id}/speakers`, { method: "PUT", body: JSON.stringify({ speakers }) });
    toast("Speakers saved");
    await refreshAudiobook();
  }
));
document.getElementById("ab-add-span-btn").addEventListener("click", () =>
  document.getElementById("ab-spans").append(abSpanRow({})));
document.getElementById("ab-add-exclusion-btn").addEventListener("click", () =>
  document.getElementById("ab-exclusions").append(abExclusionRow({})));
document.getElementById("ab-save-spans-btn").addEventListener("click", disabledWhile(
  document.getElementById("ab-save-spans-btn"),
  async () => {
    const { spans, exclusions } = readAudiobookSpans();
    await api(`/audiobook/${currentProject.id}/annotations`, { method: "PUT", body: JSON.stringify({ spans, exclusions }) });
    document.getElementById("ab-span-errors").textContent = "";
    toast("Annotations saved");
    await refreshAudiobook();
  }
));
document.getElementById("ab-plan-btn").addEventListener("click", disabledWhile(
  document.getElementById("ab-plan-btn"),
  async () => {
    await api(`/audiobook/${currentProject.id}/chunks/plan`, { method: "POST", body: "{}" });
    toast("Chunks planned");
    await refreshAudiobook();
  }
));
document.getElementById("ab-render-btn").addEventListener("click", disabledWhile(
  document.getElementById("ab-render-btn"),
  async () => {
    await api(`/audiobook/${currentProject.id}/render`, { method: "POST", body: JSON.stringify({ force: false }) });
    toast("Render complete");
    await refreshAudiobook();
  }
));
document.getElementById("ab-assemble-btn").addEventListener("click", disabledWhile(
  document.getElementById("ab-assemble-btn"),
  async () => {
    await api(`/audiobook/${currentProject.id}/assemble`, { method: "POST", body: JSON.stringify({ pause_ms: 300 }) });
    toast("Assembled; listen to both files, then record the listen-back");
    await refreshAudiobook();
  }
));
document.getElementById("ab-review-btn").addEventListener("click", disabledWhile(
  document.getElementById("ab-review-btn"),
  async () => {
    await api(`/audiobook/${currentProject.id}/review`, { method: "POST", body: JSON.stringify({
      listened_wav: document.getElementById("ab-listened-wav").checked,
      listened_mp3: document.getElementById("ab-listened-mp3").checked,
    }) });
    toast("Listen-back recorded");
    await refreshAudiobook();
  }
));
document.getElementById("ab-approve-btn").addEventListener("click", disabledWhile(
  document.getElementById("ab-approve-btn"),
  async () => {
    await api(`/audiobook/${currentProject.id}/approve`, { method: "POST" });
    toast("Approved (bytes bound)");
    await refreshAudiobook();
  }
));
document.getElementById("ab-verify-btn").addEventListener("click", async () => {
  const v = await api(`/audiobook/${currentProject.id}/approval`);
  toast(v.approved ? "Verified: approval is current" : "Not approved: " + (v.reasons || []).join("; "), v.approved ? "ok" : "err");
});
document.getElementById("ab-clear-recovery-btn").addEventListener("click", disabledWhile(
  document.getElementById("ab-clear-recovery-btn"),
  async () => {
    await api(`/audiobook/${currentProject.id}/recovery/clear`, { method: "POST", body: JSON.stringify({
      worker_stopped: document.getElementById("ab-worker-stopped").checked,
      queue_drained: document.getElementById("ab-queue-drained").checked,
      evidence: document.getElementById("ab-evidence").value.trim(),
    }) });
    toast("Recovery cleared; shared capacity released");
    await refreshAudiobook();
  }
));
