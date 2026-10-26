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
      try {
        const data = await res.json();
        detail = data.detail || detail;
        if (detail && typeof detail === "object") detail = detail.detail || JSON.stringify(detail);
      } catch {}
      throw new Error(String(detail));
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
  const masteringLink = document.getElementById("mastering-link");
  if (masteringLink) masteringLink.href = `/mastering?project=${encodeURIComponent(pid)}`;
  if (typeof clearProfilePreview === "function") clearProfilePreview("");
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
  await refreshMusic();
  if (typeof refreshProfiles === "function") await refreshProfiles();
}

// ---------- stage approval ----------
function renderStageStatus() {
  const box = document.getElementById("stage-status");
  if (!currentProject?.stages) { box.innerHTML = ""; return; }
  box.innerHTML = "";
  for (const s of STAGE_ORDER) {
    const st = currentProject.stages[s];
    const badge = el("span", { class: `stage-badge ${st}` }, `${s}: ${st}`);
    box.append(badge);
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
  if (tab === "timeline" || tab === "brief" || tab === "audiobook" || tab === "music" || tab === "artists") {
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
  if (tab === "music") refreshMusic();
  if (tab === "artists" && typeof refreshProfiles === "function") refreshProfiles();
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
  populateMusicKeys();
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

// ---------- bounded ACE-Step music workflow ----------
const MUSIC_KEYS = [
  "C major", "C# major", "Db major", "D major", "D# major", "Eb major",
  "E major", "F major", "F# major", "Gb major", "G major", "G# major",
  "Ab major", "A major", "A# major", "Bb major", "B major",
  "C minor", "C# minor", "Db minor", "D minor", "D# minor", "Eb minor",
  "E minor", "F minor", "F# minor", "Gb minor", "G minor", "G# minor",
  "Ab minor", "A minor", "A# minor", "Bb minor", "B minor",
];
let musicState = null;

function populateMusicKeys() {
  const sel = document.getElementById("music-keyscale");
  if (!sel || sel.options.length) return;
  for (const key of MUSIC_KEYS) {
    const opt = document.createElement("option");
    opt.value = key;
    opt.textContent = key;
    sel.append(opt);
  }
  sel.value = "C major";
}

let musicArrangementState = {};
const MUSIC_SECTION_RE = /^\[([a-zA-Z0-9 _-]+)\][ \t]*\r?$/gm;
const MUSIC_SECTION_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$/;

function musicLyricSections() {
  const counts = new Map();
  const text = document.getElementById("music-lyrics").value;
  for (const match of text.matchAll(MUSIC_SECTION_RE)) {
    const name = match[1];
    counts.set(name, (counts.get(name) || 0) + 1);
  }
  return counts;
}

function musicEffectiveTagsPreview() {
  const direction = document.getElementById("music-direction").value.trim();
  const directives = Object.entries(musicArrangementState)
    .filter(([, directive]) => String(directive).trim())
    .map(([section, directive]) => `${section}: ${String(directive).trim()}`);
  return directives.length ? `${direction}; ${directives.join("; ")}` : direction;
}

function normalizeArrangementDirective(text) {
  return text
    .replace(/\t+/g, "; ")
    .replace(/ {2,}/g, "; ")
    .replace(/;\s*(?:;\s*)+/g, "; ")
    .replace(/\s*;\s*$/, "")
    .trim();
}

const MUSIC_TIME_RANGE_RE = /\b\d{1,2}:\d{2}(?:\.\d{1,3})?\s*[–—-]\s*\d{1,2}:\d{2}(?:\.\d{1,3})?\b/;

function splitMusicArrangementLine(line) {
  const tab = line.indexOf("\t");
  if (tab > 0) {
    return {
      section: line.slice(0, tab).trim(),
      directive: normalizeArrangementDirective(line.slice(tab + 1)),
      separator: "\t",
    };
  }
  const pipe = line.indexOf("|");
  if (pipe > 0) {
    return {
      section: line.slice(0, pipe).trim(),
      directive: line.slice(pipe + 1).trim(),
      separator: "|",
    };
  }
  const gap = line.match(/ {2,}/);
  if (gap) {
    const at = line.indexOf(gap[0]);
    return {
      section: line.slice(0, at).trim(),
      directive: normalizeArrangementDirective(line.slice(at + gap[0].length)),
      separator: "  ",
    };
  }
  const timed = line.match(MUSIC_TIME_RANGE_RE);
  if (timed && timed.index > 0) {
    const section = line.slice(0, timed.index).replace(/[:\s]+$/, "").trim();
    if (section && MUSIC_SECTION_NAME_RE.test(section)) {
      return {
        section,
        directive: normalizeArrangementDirective(line.slice(timed.index)),
        separator: " ",
      };
    }
  }
  const colon = line.indexOf(":");
  if (colon > 0) {
    const section = line.slice(0, colon).trim();
    const directive = line.slice(colon + 1).trim();
    if (section && directive) {
      return { section, directive, separator: ":" };
    }
  }
  return null;
}

function parseMusicArrangementText(text) {
  const entries = [];
  const problems = [];
  String(text || "").split("\n").forEach((raw, lineIndex) => {
    const line = raw.replace(/\r$/, "");
    if (!line.trim()) return;
    const parsed = splitMusicArrangementLine(line);
    if (!parsed) {
      problems.push(`line ${lineIndex + 1} not saved (separate section and guidance with TAB, "|", 2+ spaces, or "section: "): ${line.trim()}`);
      return;
    }
    if (!MUSIC_SECTION_NAME_RE.test(parsed.section)) {
      problems.push(`line ${lineIndex + 1} not saved (section name must start with a letter/digit, max 40 chars): ${line.trim()}`);
      return;
    }
    entries.push({ ...parsed, lineIndex });
  });
  return { entries, problems };
}

function arrangementDictFromText(text) {
  const { entries, problems } = parseMusicArrangementText(text);
  const dict = Object.create(null);
  const merged = new Set();
  for (const entry of entries) {
    if (!entry.directive) {
      problems.push(`line ${entry.lineIndex + 1} not saved (empty guidance for "${entry.section}")`);
      continue;
    }
    if (entry.section in dict) {
      dict[entry.section] = `${dict[entry.section]}; ${entry.directive}`;
      merged.add(entry.section);
      continue;
    }
    dict[entry.section] = entry.directive;
  }
  for (const section of merged) {
    problems.push(
      `section "${section}" has multiple lines; merged into one guidance ` +
      "(one guidance per section name, applied to every occurrence)");
  }
  return { dict, problems };
}

function musicArrangementProblems() {
  return arrangementDictFromText(
    document.getElementById("music-arrangement-text").value).problems;
}

function validateMusicArrangementForSave() {
  const { entries, problems } = parseMusicArrangementText(
    document.getElementById("music-arrangement-text").value);
  const empty = entries.filter(entry => !entry.directive);
  if (problems.length || empty.length) {
    throw new Error("Draft not saved: fix the arrangement line notes or remove empty guidance first. Your text is unchanged.");
  }
}

function updateMusicArrangementPreview() {
  const info = document.getElementById("music-arrangement-info");
  const preview = document.getElementById("music-arrangement-preview");
  if (info) {
    const { problems } = arrangementDictFromText(
      document.getElementById("music-arrangement-text").value);
    info.textContent =
      "Freeform plan: one section per line. Paste a table (section<TAB>guidance, or \"|\" or 2+ spaces " +
      "between them) or type \"section: guidance\". Guidance - instrumentation, performance direction, " +
      "approximate timing - is appended to the ACE-Step tags prompt as prose guidance. Model adherence " +
      "is not guaranteed: timing and global Duration are requests, not verified audio boundaries or an exact output length. One guidance applies " +
      "to every [section] sharing the exact same name (including case); Verse 1, Verse 2, Chorus 2, and Final Chorus stay distinct. Repeated identical section names are merged into one guidance " +
      "(occurrence counts are reference-only). " +
      "Directives stay separate from the sung lyrics." +
      (problems.length ? ` Line notes: ${problems.join("; ")}` : "");
  }
  if (preview) preview.textContent = `Tags prompt preview (not a guarantee of model adherence): ${musicEffectiveTagsPreview()}`;
}

let musicArrangementAuditionStopAt = null;
let musicArrangementAuditionTimer = null;

function parseArrangementTiming(directive) {
  const matches = [...String(directive || "").matchAll(/(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?/g)];
  if (matches.length < 2) return null;
  const seconds = (match) => Number(match[1]) * 60 + Number(match[2]) +
    (match[3] ? Number(`0.${match[3]}`) : 0);
  const start = seconds(matches[0]);
  const end = seconds(matches[1]);
  return end > start ? { start, end } : null;
}

function stopMusicArrangementPlayback() {
  musicArrangementAuditionStopAt = null;
  if (musicArrangementAuditionTimer !== null) {
    clearInterval(musicArrangementAuditionTimer);
    musicArrangementAuditionTimer = null;
  }
  const audio = document.getElementById("music-arrangement-audio");
  if (audio && !audio.paused) audio.pause();
  const out = document.getElementById("music-arrangement-playback");
  if (out) out.textContent = "";
  const stop = document.getElementById("music-arrangement-stop");
  if (stop) stop.disabled = true;
}

function playMusicArrangementItem(section, directive) {
  const pid = currentProject && currentProject.id;
  const assetId = musicRepaintCurrentAsset() ||
    (musicRepaintTakes.length ? musicRepaintTakes[musicRepaintTakes.length - 1].asset_id : null);
  if (!pid || !assetId) {
    toast("Select a source take in Section Edit first", "err");
    return;
  }
  const boundary = musicRepaintBoundaryFor({ section, occurrence: 1 }, assetId);
  const plan = parseArrangementTiming(directive);
  const range = boundary
    ? { start: boundary.start_s, end: boundary.end_s, source: "listening-confirmed" }
    : (plan ? { start: plan.start, end: plan.end, source: "plan timing (suggested)" } : null);
  if (!range) {
    toast("Plan item has no usable start–end timing", "err");
    return;
  }
  const audio = document.getElementById("music-arrangement-audio");
  const out = document.getElementById("music-arrangement-playback");
  const stop = document.getElementById("music-arrangement-stop");
  stopMusicArrangementPlayback();
  audio.src = assetUrl(pid, assetId);
  audio.currentTime = range.start;
  musicArrangementAuditionStopAt = range.end;
  const label = `[${section}] ${formatMusicTimecode(range.start)}\u2013${formatMusicTimecode(range.end)} (${range.source})`;
  if (out) out.textContent = `playing ${label}`;
  if (stop) stop.disabled = false;
  audio.play().catch((error) => toast(`Audition failed: ${error.message}`, "err"));
  musicArrangementAuditionTimer = setInterval(() => {
    if (musicArrangementAuditionStopAt !== null &&
        audio.currentTime >= musicArrangementAuditionStopAt - 0.01) {
      const limit = musicArrangementAuditionStopAt;
      audio.pause();
      try { audio.currentTime = limit; } catch {}
      musicArrangementAuditionStopAt = null;
      if (musicArrangementAuditionTimer !== null) {
        clearInterval(musicArrangementAuditionTimer);
        musicArrangementAuditionTimer = null;
      }
      if (out) out.textContent = `played ${label}`;
      if (stop) stop.disabled = true;
      return;
    }
    if (out) out.textContent = `playing ${label} - position ${formatMusicTimecode(audio.currentTime)}`;
  }, 100);
}

function renderMusicArrangement() {
  const rowsEl = document.getElementById("music-arrangement-rows");
  const textEl = document.getElementById("music-arrangement-text");
  if (!rowsEl || !textEl) return;
  rowsEl.innerHTML = "";
  const { dict } = arrangementDictFromText(textEl.value);
  musicArrangementState = dict;
  const { entries } = parseMusicArrangementText(textEl.value);
  const lineBySection = new Map();
  const linesBySection = new Map();
  const directiveBySection = new Map();
  for (const entry of entries) {
    if (!linesBySection.has(entry.section)) {
      linesBySection.set(entry.section, []);
      lineBySection.set(entry.section, entry.lineIndex);
      directiveBySection.set(entry.section, entry.directive);
    }
    linesBySection.get(entry.section).push(entry.lineIndex);
  }
  const counts = musicLyricSections();
  const names = [...counts.keys()];
  for (const entry of entries) {
    if (!names.includes(entry.section)) names.push(entry.section);
  }
  if (!names.length) {
    rowsEl.append(el("p", { class: "muted" },
      "No sections yet. Paste the plan above, add [verse]/[chorus] tags in the lyrics, or add a section below."));
  }
  for (const name of names) {
    const count = counts.get(name) || 0;
    const row = el("div", { class: "music-arr-row" });
    const label = el("label", { class: "music-arr-section" }, `[${name}]`);
    if (count > 1) {
      label.append(el("span", { class: "arr-badge", title: "Reference-only: one guidance applies to every occurrence of this section name" }, `x${count}`));
    }
    if (count === 0) {
      label.append(el("span", { class: "arr-badge arr-warn", title: "No exact matching lyric tag. Instrumental-only guidance may not need sung text; review preflight notes." }, "no tag"));
    }
    let lineIndex = lineBySection.has(name) ? lineBySection.get(name) : null;
    const input = el("input", {
      placeholder: "guidance: approximate timing, instrumentation, performance direction",
      oninput: () => {
        const lines = textEl.value.split("\n");
        if (lineIndex === null) {
          lines.push(`${name}\t${input.value}`);
          lineIndex = lines.length - 1;
        } else {
          lines[lineIndex] = `${name}\t${input.value}`;
          for (const extra of linesBySection.get(name) || []) {
            if (extra !== lineIndex) lines[extra] = "";
          }
        }
        textEl.value = lines.join("\n");
        if (String(input.value).trim()) musicArrangementState[name] = input.value;
        else delete musicArrangementState[name];
        updateMusicArrangementPreview();
      },
    });
    input.value = dict[name] ?? directiveBySection.get(name) ?? "";
    const remove = el("button", {
      title: "Remove this section guidance from the plan",
      onclick: () => {
        const lines = textEl.value.split("\n");
        const indexes = parseMusicArrangementText(textEl.value).entries
          .filter(entry => entry.section === name).map(entry => entry.lineIndex)
          .sort((a, b) => b - a);
        for (const index of indexes) lines.splice(index, 1);
        textEl.value = lines.join("\n");
        renderMusicArrangement();
      },
    }, "Remove");
    const assetId = musicRepaintCurrentAsset();
    const boundary = musicRepaintBoundaryFor({ section: name, occurrence: 1 }, assetId);
    const play = el("button", {
      type: "button",
      title: "Play the generated source at this item's confirmed or planned timing",
      onclick: () => playMusicArrangementItem(name, input.value),
    }, "Play");
    row.append(label, input);
    if (boundary) {
      row.append(el("span", {
        class: "muted",
        title: "Listening-confirmed in Section Edit; not auto-detected",
      }, `confirmed ${formatMusicTimecode(boundary.start_s)}\u2013${formatMusicTimecode(boundary.end_s)}`));
    }
    row.append(play, remove);
    rowsEl.append(row);
  }
  updateMusicArrangementPreview();
}

function musicFormSettings() {
  const number = (id, fallback) => {
    const value = Number(document.getElementById(id).value);
    return Number.isFinite(value) ? value : fallback;
  };
  const knob = (id) => {
    const raw = document.getElementById(id).value;
    if (raw === null || String(raw).trim() === "") return null;
    const value = Number(raw);
    if (!Number.isFinite(value)) throw new Error(`Invalid number for ${id}`);
    return value;
  };
  return {
    musical_direction: document.getElementById("music-direction").value.trim(),
    lyrics: document.getElementById("music-lyrics").value,
    duration_s: number("music-duration", 30),
    language: document.getElementById("music-language").value,
    bpm: number("music-bpm", 120),
    time_signature: document.getElementById("music-timesig").value,
    key_scale: document.getElementById("music-keyscale").value,
    generate_audio_codes: document.getElementById("music-audio-codes").checked,
    seed: Math.max(0, Math.trunc(number("music-seed", 0))),
    arrangement: arrangementDictFromText(
      document.getElementById("music-arrangement-text").value).dict,
    temperature: knob("music-temperature"),
    top_p: knob("music-topp"),
    cfg_scale: knob("music-cfg"),
  };
}

function fillMusicForm(settings) {
  if (!settings) return;
  document.getElementById("music-direction").value = settings.musical_direction || "";
  document.getElementById("music-lyrics").value = settings.lyrics || "";
  document.getElementById("music-arrangement-text").value = Object.entries(
    settings.arrangement || {})
    .map(([section, directive]) => `${section}\t${directive}`)
    .join("\n");
  renderMusicArrangement();
  document.getElementById("music-duration").value = settings.duration_s ?? 30;
  document.getElementById("music-language").value = settings.language || "en";
  document.getElementById("music-bpm").value = settings.bpm ?? 120;
  document.getElementById("music-timesig").value = settings.time_signature || "4";
  document.getElementById("music-keyscale").value = settings.key_scale || "C major";
  document.getElementById("music-audio-codes").checked = settings.generate_audio_codes !== false;
  document.getElementById("music-seed").value = settings.seed ?? 0;
  document.getElementById("music-temperature").value = settings.temperature ?? "";
  document.getElementById("music-topp").value = settings.top_p ?? "";
  document.getElementById("music-cfg").value = settings.cfg_scale ?? "";
}

async function refreshMusic() {
  if (!currentProject) return;
  const pid = currentProject.id;
  const runtimeEl = document.getElementById("music-runtime");
  const statusEl = document.getElementById("music-status");
  const blockersEl = document.getElementById("music-blockers");
  const attemptsEl = document.getElementById("music-attempts");
  try {
    const s = await api(`/music/${pid}`);
    musicState = s;
    if (s.draft?.settings) fillMusicForm(s.draft.settings);

    const r = s.runtime || {};
    const capacityLabel = r.capacity_verified
      ? "capacity verified"
      : r.capacity_override_authorized
        ? "experimental capacity override (NOT verified)"
        : "live capacity unverified";
    runtimeEl.textContent = [
      r.ready ? "ACE-Step offline runtime ready" : "ACE-Step offline runtime not ready",
      r.checkpoint_available ? "checkpoint visible" : "checkpoint missing",
      r.workflow_valid ? "workflow valid" : "workflow invalid",
      capacityLabel,
      r.live_ready ? "live ready" : "live generation disabled or not ready",
      ...(r.errors || []),
    ].join(" | ");
    runtimeEl.className = r.ready ? "muted" : "err";

    const cap = s.effective_cap ?? s.lifetime_cap ?? "?";
    const approval = s.approval
      ? `approved v${s.approval.draft_version} cap ${s.approval.submission_cap}`
      : "lyrics/settings not approved";
    statusEl.textContent = [
      s.draft ? `draft v${s.draft.version}` : "no draft",
      approval,
      `submissions ${s.journal.submissions}/${cap}`,
      `completed ${s.journal.completed}`,
      `failed ${s.journal.failed}`,
      `uncertain ${s.journal.uncertain}`,
      s.capacity_hold ? `capacity hold: ${s.capacity_hold.reason}` : "",
      s.blocked_reason ? `blocked: ${s.blocked_reason}` : "",
    ].filter(Boolean).join(" | ");
    statusEl.className = s.can_generate ? "muted" : "err";
    const blockers = s.blockers || [];
    blockersEl.textContent = blockers.length
      ? blockers.map(b => b.message).join(" | ")
      : "No blocking conditions.";
    blockersEl.className = blockers.length ? "err" : "muted";

    const current = s.current_attempt;
    attemptsEl.textContent = current
      ? `attempt ${current.id}: ${current.status}${current.prompt_id ? ` (prompt ${current.prompt_id})` : ""}${current.error ? ` - ${current.error}` : ""}`
      : "no music attempt yet";
    attemptsEl.className = current && current.status !== "completed" ? "err" : "muted";

    const preflightEl = document.getElementById("music-preflight");
    const warnings = s.preflight || [];
    preflightEl.textContent = warnings.length
      ? `Preflight: ${warnings.map(w => w.message).join(" | ")}`
      : "Preflight: no warnings.";
    preflightEl.className = warnings.length ? "err" : "muted";

    const assets = s.assets || [];
    let latest = current?.asset_id ? assets.find(a => a.id === current.asset_id) : null;
    if (!latest && assets.length) latest = assets[assets.length - 1];
    const player = document.getElementById("music-player");
    const download = document.getElementById("music-download");
    const latestEl = document.getElementById("music-latest");
    if (latest) {
      player.src = assetUrl(pid, latest.id);
      player.style.display = "block";
      download.href = assetUrl(pid, latest.id);
      download.download = latest.filename;
      download.hidden = false;
      latestEl.textContent = `${latest.filename} (${latest.id})`;
    } else {
      player.removeAttribute("src");
      player.style.display = "none";
      download.hidden = true;
      latestEl.textContent = "No completed music asset yet.";
    }
    const audioApproval = s.audio_approval;
    const audioApproved = Boolean(latest && audioApproval && audioApproval.asset_id === latest.id);
    document.getElementById("music-audio-status").textContent = audioApproved
      ? `exact audio approved: ${audioApproval.audio_sha256} - ${audioApproval.note}`
      : "exact audio not approved";
    document.getElementById("music-audio-status").className = audioApproved ? "muted" : "err";

    document.getElementById("music-approve-btn").disabled = false;
    document.getElementById("music-generate-btn").disabled = !s.can_generate;
    document.getElementById("music-generate-btn").title = s.blocked_reason || "";
    const canCancel = Boolean(current && (
      current.status === "running"
      || (current.prompt_id && (current.status === "failed" || current.status === "uncertain"))
    ));
    document.getElementById("music-cancel-btn").disabled = !canCancel;
    document.getElementById("music-recover-btn").disabled = !s.recovery_required;
    document.getElementById("music-audio-approve-btn").disabled = !(
      latest && current && current.status === "completed" && !audioApproved);
    await Promise.all([
  renderAssets("music", "music-assets"),
  refreshMusicRevisions(),
  refreshMusicTakes(),
  refreshMusicRepaint(),
  ]);
  } catch (e) {
    musicState = null;
    runtimeEl.textContent = "music runtime unavailable: " + e.message;
    runtimeEl.className = "err";
    statusEl.textContent = "";
    blockersEl.textContent = "";
    attemptsEl.textContent = "";
    document.getElementById("music-player").style.display = "none";
    document.getElementById("music-download").hidden = true;
  }
}

document.getElementById("music-save-btn").addEventListener("click", disabledWhile(
  document.getElementById("music-save-btn"),
  async () => {
    validateMusicArrangementForSave();
    const problems = musicArrangementProblems();
    const result = await api(`/music/${currentProject.id}/draft`, {
      method: "PUT",
      body: JSON.stringify({ project_id: currentProject.id, settings: musicFormSettings() }),
    });
    const label = result.revision_created
      ? `Music draft saved as v${result.draft.version}`
      : "Music draft saved (no changes)";
    toast(problems.length
      ? `${label}; ${problems.length} plan note(s) (see plan notes)`
      : label, problems.length ? "err" : "ok");
    await refreshMusic();
  }
));

document.getElementById("music-arrangement-add").addEventListener("click", () => {
  const textEl = document.getElementById("music-arrangement-text");
  const input = document.getElementById("music-arrangement-new-section");
  const name = input.value.trim();
  if (!name) return;
  if (!MUSIC_SECTION_NAME_RE.test(name)) {
    toast("Section name must start with a letter/digit; letters, digits, space, _ or - (max 40 chars)", "err");
    return;
  }
  const { entries } = parseMusicArrangementText(textEl.value);
  if (entries.some(entry => entry.section === name)) {
    toast("A plan line already exists for that section", "err");
    return;
  }
  const value = textEl.value.replace(/\s+$/, "");
  textEl.value = `${value}${value ? "\n" : ""}${name}\t`;
  input.value = "";
  renderMusicArrangement();
});

document.getElementById("music-lyrics").addEventListener("input", renderMusicArrangement);

document.getElementById("music-arrangement-text").addEventListener("input", renderMusicArrangement);

document.getElementById("music-direction").addEventListener("input", updateMusicArrangementPreview);

document.getElementById("music-revision-btn").addEventListener("click", disabledWhile(
  document.getElementById("music-revision-btn"),
  async () => {
    validateMusicArrangementForSave();
    const name = document.getElementById("music-revision-name").value.trim();
    const result = await api(`/music/${currentProject.id}/draft`, {
      method: "PUT",
      body: JSON.stringify({
        project_id: currentProject.id,
        settings: musicFormSettings(),
        revision_name: name || null,
      }),
    });
    if (result.revision_created) {
      toast(`Revision v${result.draft.version} created`);
      document.getElementById("music-revision-name").value = "";
    } else {
      toast("Draft content unchanged; a name alone does not create or rename a revision. Edit lyrics or settings first.", "err");
    }
    await refreshMusic();
  }
));

let musicRevisions = [];
let musicCurrentVersion = null;

async function refreshMusicRevisions() {
  const out = document.getElementById("music-revisions");
  out.innerHTML = "";
  if (!currentProject) return;
  try {
    const data = await api(`/music/${currentProject.id}/revisions`);
    musicRevisions = data.revisions || [];
    musicCurrentVersion = data.current_version;
    if (!musicRevisions.length) {
      out.append(el("p", { class: "muted" }, "No revisions yet. Edit the draft and use Save draft or Create revision."));
    } else {
      const list = el("ul", { class: "music-list" });
      for (const r of [...musicRevisions].reverse()) {
        const row = el("li", {});
        const btn = el("button", {
          title: "Create a new revision from this historical revision (nothing is overwritten)",
          onclick: () => restoreMusicRevision(r.version, btn),
        }, "Restore");
        if (r.version === musicCurrentVersion) {
          btn.disabled = true;
          btn.title = "This is the current draft revision";
        }
        row.append(btn, " ");
        row.append(el("div", {},
          `v${r.version}${r.name ? ` "${r.name}"` : ""}`,
          r.parent_version ? ` (parent v${r.parent_version})` : "",
          ` - ${r.source}`,
          ` - ${r.take_count} take${r.take_count === 1 ? "" : "s"}`,
          ` - ${r.critique_count} critique${r.critique_count === 1 ? "" : "s"}`,
          r.version === musicCurrentVersion ? " - current" : "",
        ));
        list.append(row);
      }
      out.append(list);
    }
  } catch (e) {
    out.append(el("p", { class: "err" }, `load failed: ${e.message}`));
  }
  fillMusicCompareSelects();
}

function fillMusicCompareSelects() {
  const fromSel = document.getElementById("music-compare-from");
  const toSel = document.getElementById("music-compare-to");
  const previous = { from: fromSel.value, to: toSel.value };
  for (const sel of [fromSel, toSel]) {
    sel.innerHTML = "";
    for (const r of musicRevisions) {
      sel.append(el("option", { value: String(r.version) },
        `v${r.version}${r.name ? ` - ${r.name}` : ""}`));
    }
  }
  if (musicRevisions.length >= 2) {
    const known = value => musicRevisions.some(r => String(r.version) === value);
    fromSel.value = known(previous.from)
      ? previous.from : String(musicRevisions[0].version);
    toSel.value = known(previous.to)
      ? previous.to : String(musicRevisions[musicRevisions.length - 1].version);
  }
  document.getElementById("music-compare-btn").disabled = musicRevisions.length < 2;
}

document.getElementById("music-compare-btn").addEventListener("click", disabledWhile(
  document.getElementById("music-compare-btn"),
  async () => {
    const fromVersion = Number(document.getElementById("music-compare-from").value);
    const toVersion = Number(document.getElementById("music-compare-to").value);
    if (!Number.isInteger(fromVersion) || !Number.isInteger(toVersion)) {
      throw new Error("Select two revisions to compare");
    }
    const diff = await api(
      `/music/${currentProject.id}/revisions/compare?from_version=${fromVersion}&to_version=${toVersion}`);
    renderMusicCompare(diff);
  }
));

function renderMusicCompare(diff) {
  const out = document.getElementById("music-compare-out");
  out.innerHTML = "";
  out.append(el("h4", {},
    `v${diff.from.version}${diff.from.name ? ` "${diff.from.name}"` : ""}`,
    " to ",
    `v${diff.to.version}${diff.to.name ? ` "${diff.to.name}"` : ""}`));
  const noSettings = !(diff.settings_changes || []).length;
  const noLyrics = (diff.lyrics_diff || []).every(op => op.op === "equal");
  if (noSettings && noLyrics) {
    out.append(el("p", { class: "muted" }, "No differences."));
    return;
  }
  if (noSettings) {
    out.append(el("p", { class: "muted" }, "No settings changes."));
  } else {
    const settingsList = el("ul", { class: "music-list" });
    for (const change of diff.settings_changes) {
      settingsList.append(el("li", {},
        `${change.field}: ${JSON.stringify(change.from)} to ${JSON.stringify(change.to)}`));
    }
    out.append(el("h4", {}, "Settings changes"));
    out.append(settingsList);
  }
  const lyricsOut = el("pre", { class: "music-diff" });
  for (const op of diff.lyrics_diff || []) {
    if (op.op === "equal") {
      lyricsOut.append(`${op.count} unchanged line${op.count === 1 ? "" : "s"}\n`);
    } else if (op.op === "delete") {
      for (const line of op.from_lines || []) lyricsOut.append(`- ${line}\n`);
    } else if (op.op === "insert") {
      for (const line of op.to_lines || []) lyricsOut.append(`+ ${line}\n`);
    } else {
      for (const line of op.from_lines || []) lyricsOut.append(`- ${line}\n`);
      for (const line of op.to_lines || []) lyricsOut.append(`+ ${line}\n`);
    }
  }
  out.append(el("h4", {}, "Lyrics diff"));
  out.append(lyricsOut);
}

async function refreshMusicTakes() {
  const out = document.getElementById("music-takes");
  out.innerHTML = "";
  if (!currentProject) return;
  try {
    const data = await api(`/music/${currentProject.id}/takes`);
    const preferredEl = document.getElementById("music-preferred");
    preferredEl.textContent = data.preferred_take
      ? `Preferred take: asset ${data.preferred_take.asset_id} (attempt ${data.preferred_take.attempt_id}, revision ${data.preferred_take.revision ?? "?"}) - ${data.preferred_take.note}`
      : "No preferred take selected yet.";
    preferredEl.className = data.preferred_take ? "muted" : "err";
    const takes = data.takes || [];
    if (!takes.length) {
      out.append(el("p", { class: "muted" }, "No completed takes yet."));
      return;
    }
    const list = el("ul", { class: "music-list" });
    for (const t of [...takes].reverse()) {
      const row = el("li", {});
      const btn = el("button", {
        onclick: () => preferMusicTake(t.asset_id, btn),
      }, t.preferred ? "Preferred" : "Prefer");
      if (t.preferred) btn.disabled = true;
      row.append(btn, " ");
      const head = el("div", {},
        `attempt ${t.attempt_id} - revision ${t.revision ?? "?"}${t.revision_name ? ` "${t.revision_name}"` : ""}${t.plan_label ? ` - plan ${t.plan_label}` : ""}`,
        t.audio_approval ? " - audio approved" : "");
      if (t.legacy) {
        head.append(el("span", {
          class: "arr-badge arr-warn",
          title: (t.take_provenance && t.take_provenance.detail) || "legacy take",
        }, "legacy"));
      }
      row.append(head);
      row.append(el("div", { class: "muted" },
        `asset ${t.asset_id} - sha256 ${(t.audio_sha256 || "").slice(0, 12)}...`));
      row.append(el("a", {
        class: "download-link",
        href: `/api/music/${currentProject.id}/takes/${t.asset_id}/package`,
        download: `take-${t.asset_id}-package.zip`,
        title: "Export archival take package (provenance, audio, markers, hash manifest)",
      }, "Export take package"));
      if (t.legacy && t.take_provenance) {
        row.append(el("div", { class: "muted" },
          `preserved from ${t.take_provenance.basis}; uncertain: ${t.take_provenance.detail || "revision linkage unknown"}`));
      }
      if (t.critiques && t.critiques.length) {
        for (const c of t.critiques) {
          row.append(el("div", { class: "muted" }, `feedback: ${c.summary}`));
        }
      }
      list.append(row);
    }
    out.append(list);
  } catch (e) {
    out.append(el("p", { class: "err" }, `load failed: ${e.message}`));
  }
}

async function restoreMusicRevision(version, btn) {
  btn.disabled = true;
  showProgress(true);
  try {
    const result = await api(
      `/music/${currentProject.id}/revisions/${version}/restore`, { method: "POST" });
    if (result.revision_created) {
      toast(`Revision v${result.draft.version} restored from v${version}`);
    } else {
      toast(`v${version} already matches the current draft`);
    }
  } catch (e) {
    toast(e.message, "err");
  } finally {
    showProgress(false);
    btn.disabled = false;
    await refreshMusic();
  }
}

let musicRepaintSelected = null;
let musicRepaintConfirmed = null;
let musicRepaintTakes = [];
let musicRepaintProposals = [];
let musicRepaintBoundaries = [];
let musicRepaintReviewFlags = [];
let musicRepaintSelectedOccurrence = null;
let musicRepaintConfirmedLabel = "";
let musicRepaintSharedStartFrom = null;
let musicRepaintBoundarySupport = true;
let musicRepaintBoundaryError = "";

function musicRepaintTimeSuggestions() {
  const items = [];
  const counts = new Map();
  let arrangementIndex = 0;
  for (const [section, directive] of Object.entries(musicArrangementState)) {
    const matches = [...String(directive).matchAll(/(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?/g)];
    const points = matches.map((match) => ({
      seconds: Number(match[1]) * 60 + Number(match[2]) +
        (match[3] ? Number(`0.${match[3]}`) : 0),
      text: match[0],
    }));
    for (let index = 0; index + 1 < points.length; index += 2) {
      const occurrence = (counts.get(section) || 0) + 1;
      counts.set(section, occurrence);
      items.push({
        section,
        occurrence,
        arrangementIndex,
        start: points[index].seconds,
        end: points[index + 1].seconds,
        label: `${points[index].text}\u2013${points[index + 1].text}`,
      });
    }
    arrangementIndex += 1;
  }
  return items;
}

function musicRepaintOccurrenceKey(item) {
  return `${item.section}#${item.occurrence}`;
}

function musicRepaintCurrentAsset() {
  return document.getElementById("music-repaint-source").value;
}

function musicRepaintBoundaryFor(item, assetId) {
  return musicRepaintBoundaries.find((boundary) =>
    boundary.source_asset_id === assetId &&
    boundary.section === item.section &&
    boundary.occurrence === item.occurrence) || null;
}

function updateMusicRepaintConfirmed() {
  const span = document.getElementById("music-repaint-confirmed");
  const label = musicRepaintConfirmedLabel ? `${musicRepaintConfirmedLabel} ` : "";
  span.textContent = musicRepaintConfirmed
    ? `confirmed ${label}${formatMusicTimecode(musicRepaintConfirmed.start)} to ${formatMusicTimecode(musicRepaintConfirmed.end)} (listening-confirmed)`
    : "boundaries not confirmed";
  span.className = musicRepaintConfirmed ? "muted" : "err";
}

function selectMusicRepaintOccurrence(item) {
  musicRepaintSelectedOccurrence = item;
  musicRepaintSharedStartFrom = null;
  stopMusicRepaintSelection();
  const boundary = musicRepaintBoundaryFor(item, musicRepaintCurrentAsset());
  document.getElementById("music-repaint-start").value =
    formatMusicTimecode(boundary ? boundary.start_s : item.start);
  document.getElementById("music-repaint-end").value =
    formatMusicTimecode(boundary ? boundary.end_s : item.end);
  musicRepaintConfirmed = null;
  musicRepaintConfirmedLabel = "";
  updateMusicRepaintConfirmed();
  updateMusicRepaintPlayback();
  renderMusicRepaintSectionSelect();
  renderMusicRepaintChips();
  renderMusicRepaintSharedStart();
}

function renderMusicRepaintSectionSelect() {
  const select = document.getElementById("music-repaint-section");
  if (!select) return;
  const items = musicRepaintTimeSuggestions();
  select.innerHTML = "";
  if (!items.length) {
    select.append(el("option", { value: "" }, "no arrangement sections"));
    return;
  }
  select.append(el("option", { value: "" }, "select a section occurrence"));
  const assetId = musicRepaintCurrentAsset();
  for (const item of items) {
    const boundary = musicRepaintBoundaryFor(item, assetId);
    const state = boundary
      ? `listening-confirmed ${boundary.start_s.toFixed(3)}-${boundary.end_s.toFixed(3)}s`
      : `suggested ${item.label}`;
    const option = el("option", { value: musicRepaintOccurrenceKey(item) },
      `${item.section} #${item.occurrence} - ${state}`);
    if (musicRepaintSelectedOccurrence &&
        musicRepaintOccurrenceKey(musicRepaintSelectedOccurrence) === musicRepaintOccurrenceKey(item)) {
      option.selected = true;
    }
    select.append(option);
  }
}

function renderMusicRepaintChips() {
  const out = document.getElementById("music-repaint-chips");
  if (!out) return;
  out.innerHTML = "";
  const items = musicRepaintTimeSuggestions();
  if (!items.length) {
    out.textContent = "No arrangement sections to map.";
    return;
  }
  const assetId = musicRepaintCurrentAsset();
  const row = el("div", { class: "row" });
  for (const item of items) {
    const boundary = musicRepaintBoundaryFor(item, assetId);
    const selected = musicRepaintSelectedOccurrence &&
      musicRepaintOccurrenceKey(musicRepaintSelectedOccurrence) === musicRepaintOccurrenceKey(item);
    row.append(el("button", {
      type: "button",
      class: boundary ? "arr-badge" : "arr-badge arr-warn",
      title: boundary
        ? "Listening-confirmed timing for this occurrence"
        : "Suggested timing from arrangement text; not a detected audio boundary",
      onclick: () => selectMusicRepaintOccurrence(item),
    }, `${item.section} #${item.occurrence} ` +
      (boundary
        ? `listening-confirmed ${formatMusicTimecode(boundary.start_s)}\u2013${formatMusicTimecode(boundary.end_s)}`
        : `suggested ${item.label}`) +
      (selected ? " - SELECTED" : "")));
  }
  out.append(row);
}

function renderMusicRepaintReview() {
  const out = document.getElementById("music-repaint-review");
  if (!out) return;
  if (!musicRepaintBoundarySupport) {
    out.textContent = "Boundary storage unavailable on the running backend (HTTP 404). Restart the backend, then reload Section Edit.";
    out.className = "err";
    return;
  }
  if (musicRepaintBoundaryError) {
    out.textContent = musicRepaintBoundaryError;
    out.className = "err";
    return;
  }
  if (!musicRepaintReviewFlags.length) {
    out.textContent = "No gaps or overlaps among confirmed sections for this source.";
    out.className = "muted";
    return;
  }
  out.textContent = "Review: " + musicRepaintReviewFlags.map((flag) => flag.message).join("; ");
  out.className = "err";
}

function renderMusicRepaintSharedStart() {
  const button = document.getElementById("music-repaint-shared-start");
  if (!button) return;
  const selected = musicRepaintSelectedOccurrence;
  const items = musicRepaintTimeSuggestions();
  let previous = null;
  if (selected) {
    const index = items.findIndex((item) =>
      musicRepaintOccurrenceKey(item) === musicRepaintOccurrenceKey(selected));
    if (index > 0) {
      const candidate = items[index - 1];
      const boundary = musicRepaintBoundaryFor(candidate, musicRepaintCurrentAsset());
      if (boundary) previous = { item: candidate, boundary };
    }
  }
  if (!selected || !previous) {
    button.disabled = true;
    button.title = "No previous confirmed section for this source";
    return;
  }
  button.disabled = false;
  button.title = `Set ${selected.section} #${selected.occurrence} start to confirmed ` +
    `${previous.item.section} #${previous.item.occurrence} end (${formatMusicTimecode(previous.boundary.end_s)}); ` +
    "this section remains unconfirmed.";
}

async function refreshMusicRepaintBoundaries() {
  const assetId = musicRepaintCurrentAsset();
  if (!currentProject || !assetId) {
    musicRepaintBoundaries = [];
    musicRepaintReviewFlags = [];
    renderMusicRepaintSectionSelect();
    renderMusicRepaintChips();
    renderMusicRepaintReview();
    renderMusicRepaintSharedStart();
    return;
  }
  try {
    const data = await api(
      `/music/${currentProject.id}/section-boundaries?asset_id=${encodeURIComponent(assetId)}`);
    musicRepaintBoundaries = data.boundaries || [];
    musicRepaintReviewFlags = data.review_flags || [];
    musicRepaintBoundarySupport = true;
    musicRepaintBoundaryError = "";
  } catch (error) {
    musicRepaintBoundaries = [];
    musicRepaintReviewFlags = [];
    const message = String(error.message || error);
    if (message === "Not Found" || message.includes("404")) {
      musicRepaintBoundarySupport = false;
      musicRepaintBoundaryError = "";
    } else {
      musicRepaintBoundaryError = `Boundary load failed: ${message}`;
    }
  }
  renderMusicRepaintSectionSelect();
  renderMusicRepaintChips();
  renderMusicRepaintSuggestions();
  renderMusicRepaintReview();
  renderMusicRepaintSharedStart();
  renderMusicArrangement();
}

let musicRepaintSelectionStopAt = null;
let musicRepaintPlaybackTimer = null;

function musicRepaintSourceDuration() {
  const select = document.getElementById("music-repaint-source");
  const take = musicRepaintTakes.find((item) => item.asset_id === select.value);
  const fromSettings = take && take.effective_settings
    ? Number(take.effective_settings.duration_s) : NaN;
  if (Number.isFinite(fromSettings) && fromSettings > 0) return fromSettings;
  const audio = document.getElementById("music-repaint-audio");
  if (audio && Number.isFinite(audio.duration) && audio.duration > 0) return audio.duration;
  return null;
}

function updateMusicRepaintPlayback() {
  const out = document.getElementById("music-repaint-playback");
  if (!out) return;
  const audio = document.getElementById("music-repaint-audio");
  const start = parseMusicTimecode(document.getElementById("music-repaint-start").value);
  const end = parseMusicTimecode(document.getElementById("music-repaint-end").value);
  const selection = Number.isFinite(start) && Number.isFinite(end) && end > start
    ? end - start : null;
  const position = audio && Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
  out.textContent = `position ${formatMusicTimecode(position)}` +
    (selection !== null
      ? ` - selection ${formatMusicTimecode(selection)} (${selection.toFixed(3)}s)` +
        ` - ${formatMusicTimecode(start)} to ${formatMusicTimecode(end)}`
      : "");
}

function formatMusicTimecode(seconds) {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total < 0) return "";
  const minutes = Math.floor(total / 60);
  const rest = total - minutes * 60;
  return `${minutes}:${rest.toFixed(3).padStart(6, "0")}`;
}

function parseMusicTimecode(text) {
  const raw = String(text ?? "").trim();
  if (!raw) return NaN;
  if (raw.includes(":")) {
    const parts = raw.split(":");
    if (parts.length === 2) {
      const minutes = Number(parts[0]);
      const seconds = Number(parts[1]);
      if (!Number.isInteger(minutes) || minutes < 0) return NaN;
      if (!Number.isFinite(seconds) || seconds < 0 || seconds >= 60) return NaN;
      return minutes * 60 + seconds;
    }
    if (parts.length === 3) {
      const hours = Number(parts[0]);
      const minutes = Number(parts[1]);
      const seconds = Number(parts[2]);
      if (!Number.isInteger(hours) || hours < 0) return NaN;
      if (!Number.isInteger(minutes) || minutes < 0 || minutes >= 60) return NaN;
      if (!Number.isFinite(seconds) || seconds < 0 || seconds >= 60) return NaN;
      return hours * 3600 + minutes * 60 + seconds;
    }
    return NaN;
  }
  const seconds = Number(raw);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : NaN;
}

function validateMusicRepaintSelection() {
  const audio = document.getElementById("music-repaint-audio");
  if (!audio || !audio.getAttribute("src")) throw new Error("Select a source take first");
  const start = parseMusicTimecode(document.getElementById("music-repaint-start").value);
  const end = parseMusicTimecode(document.getElementById("music-repaint-end").value);
  if (!Number.isFinite(start) || !Number.isFinite(end)) {
    throw new Error("Use M:SS.mmm on a 60-second clock (e.g., 1:31.000) or seconds");
  }
  if (start < 0) throw new Error("Start must be >= 0");
  if (end <= start) throw new Error("End must be greater than start");
  const duration = musicRepaintSourceDuration();
  if (duration !== null && end > duration + 0.0005) {
    throw new Error(`End must be <= source duration ${formatMusicTimecode(duration)} (${duration.toFixed(3)}s)`);
  }
  return { start, end };
}

function stopMusicRepaintSelection() {
  musicRepaintSelectionStopAt = null;
  if (musicRepaintPlaybackTimer !== null) {
    clearInterval(musicRepaintPlaybackTimer);
    musicRepaintPlaybackTimer = null;
  }
  const audio = document.getElementById("music-repaint-audio");
  if (audio && !audio.paused) audio.pause();
  updateMusicRepaintPlayback();
}

function finishMusicRepaintSelection() {
  const audio = document.getElementById("music-repaint-audio");
  const limit = musicRepaintSelectionStopAt;
  if (limit === null) return;
  audio.pause();
  try { audio.currentTime = limit; } catch {}
  musicRepaintSelectionStopAt = null;
  if (musicRepaintPlaybackTimer !== null) {
    clearInterval(musicRepaintPlaybackTimer);
    musicRepaintPlaybackTimer = null;
  }
  const item = musicRepaintSelectedOccurrence;
  const label = item ? `${item.section} #${item.occurrence}` : "Selection";
  toast(`Played ${label} to ${formatMusicTimecode(limit)}; click Confirm Boundaries to save.`);
  updateMusicRepaintPlayback();
}

function startMusicRepaintStopMonitor() {
  if (musicRepaintPlaybackTimer !== null) {
    clearInterval(musicRepaintPlaybackTimer);
    musicRepaintPlaybackTimer = null;
  }
  const audio = document.getElementById("music-repaint-audio");
  musicRepaintPlaybackTimer = setInterval(() => {
    if (musicRepaintSelectionStopAt !== null &&
        audio.currentTime >= musicRepaintSelectionStopAt - 0.01) {
      finishMusicRepaintSelection();
      return;
    }
    updateMusicRepaintPlayback();
  }, 100);
}

function playMusicRepaintSelection() {
  try {
    const { start, end } = validateMusicRepaintSelection();
    const audio = document.getElementById("music-repaint-audio");
    stopMusicRepaintSelection();
    audio.currentTime = start;
    musicRepaintSelectionStopAt = end;
    updateMusicRepaintPlayback();
    const playing = audio.play();
    if (playing && playing.catch) {
      playing.catch((error) => toast(`Playback failed: ${error.message}`, "err"));
    }
    startMusicRepaintStopMonitor();
  } catch (error) {
    toast(error.message, "err");
  }
}

function renderMusicRepaintSuggestions() {
  const out = document.getElementById("music-repaint-suggestions");
  out.textContent = "";
  const items = musicRepaintTimeSuggestions();
  if (!items.length) {
    out.textContent = "No arrangement timestamps. Timestamps are suggestions, never detected audio boundaries.";
    return;
  }
  out.append(el("div", {},
    "Arrangement timestamps are suggestions, not detected audio boundaries. Click to fill, then confirm."));
  const assetId = musicRepaintCurrentAsset();
  const row = el("div", { class: "row" });
  for (const item of items) {
    const boundary = musicRepaintBoundaryFor(item, assetId);
    row.append(el("button", {
      type: "button",
      class: boundary ? "arr-badge" : "arr-badge arr-warn",
      title: boundary
        ? "Listening-confirmed timing for this occurrence"
        : "Suggested timing from arrangement text; not a detected audio boundary",
      onclick: () => selectMusicRepaintOccurrence(item),
    }, `${item.section} #${item.occurrence} ` +
      (boundary
        ? `listening-confirmed ${formatMusicTimecode(boundary.start_s)}\u2013${formatMusicTimecode(boundary.end_s)}`
        : `suggested ${item.label}`)));
  }
  out.append(row);
}

function renderMusicRepaintSource() {
  const pid = currentProject && currentProject.id;
  const assetId = document.getElementById("music-repaint-source").value;
  const meta = document.getElementById("music-repaint-source-meta");
  const audio = document.getElementById("music-repaint-audio");
  stopMusicRepaintSelection();
  const take = musicRepaintTakes.find((item) => item.asset_id === assetId);
  if (!pid || !take) {
    meta.textContent = "Select a completed source take.";
    audio.removeAttribute("src");
    updateMusicRepaintPlayback();
    return;
  }
  const settings = take.effective_settings || {};
  meta.textContent =
    `source asset ${take.asset_id} - sha256 ${(take.audio_sha256 || "").slice(0, 16)}...` +
    ` - revision ${take.revision ?? "?"}` +
    (take.legacy ? " (legacy take: settings unknown)" : " (settings recorded)") +
    ` | inherited: bpm ${settings.bpm ?? "?"}, key ${settings.key_scale ?? "?"},` +
    ` seed ${settings.seed ?? "?"}, duration ${settings.duration_s ? formatMusicTimecode(settings.duration_s) : "?"} (${settings.duration_s ?? "?"}s)` +
    ` | model ace-step-1.5 (repaint)`;
  audio.src = assetUrl(pid, take.asset_id);
  updateMusicRepaintPlayback();
}

function renderMusicRepaintStatus() {
  const out = document.getElementById("music-repaint-status");
  const proposal = musicRepaintProposals.find(
    (item) => item.proposal_id === musicRepaintSelected);
  const status = (musicState && musicState.repaint) || {};
  const lines = [];
  if (proposal) {
    lines.push(
      `proposal ${proposal.proposal_id} - ${proposal.status}` +
      (proposal.authorized_fingerprint ? " - authorization bound" : " - not authorized") +
      ` - interval ${proposal.start_sample}-${proposal.end_sample} samples` +
      ` (${proposal.start_s.toFixed(3)}-${proposal.end_s.toFixed(3)}s)` +
      ` - revision ${proposal.source_revision}`);
    if (proposal.candidate) {
      lines.push(
        `candidate master ${proposal.candidate.master_sha256.slice(0, 16)}...` +
        ` - transition ${proposal.candidate.assembly.crossfade_ms} ms` +
        ` - PCM outside transition verified: ${proposal.candidate.verification.pcm_equal_outside_transition}`);
    }
    if (proposal.error) lines.push(`note: ${proposal.error}`);
  } else {
    lines.push("No proposal selected.");
  }
  lines.push(status.enabled
    ? "submission gate: enabled in this isolated process"
    : "submission gate: disabled (live gate false)");
  lines.push(status.capacity_gate
    ? (status.capacity_gate.approved
      ? `capacity gate: approved (measured ${status.capacity_gate.measured_free_vram_mib} MiB, GPU${status.capacity_gate.gpu_index})`
      : "capacity gate: proposed, CLOSED")
    : "capacity gate: not proposed (closed)");
  if (status.recovery_required) {
    lines.push(`RECOVERY HOLD: ${status.recovery_reason || "confirm provider stop"}`);
  }
  out.textContent = lines.join(" | ");
  out.className = status.recovery_required ? "err" : "muted";
  const statusValue = proposal ? proposal.status : "";
  const editable = ["proposed", "authorized"].includes(statusValue);
  document.getElementById("music-repaint-authorize").disabled = statusValue !== "proposed";
  document.getElementById("music-repaint-revise").disabled = !editable;
  document.getElementById("music-repaint-submit").disabled = statusValue !== "authorized";
  document.getElementById("music-repaint-apply").disabled =
    !(proposal && proposal.candidate && statusValue === "candidate");
  document.getElementById("music-repaint-reject").disabled =
    !["proposed", "authorized", "candidate"].includes(statusValue);
}

function renderMusicRepaintList(proposals) {
  const out = document.getElementById("music-repaint-list");
  out.innerHTML = "";
  if (!proposals.length) {
    out.append(el("p", { class: "muted" }, "No repaint proposals yet."));
    renderMusicRepaintStatus();
    return;
  }
  const list = el("ul", { class: "music-list" });
  for (const proposal of [...proposals].reverse()) {
    const row = el("li", {
      style: "cursor:pointer",
      onclick: () => {
        musicRepaintSelected = proposal.proposal_id;
        renderMusicRepaintList(proposals);
      },
    });
    row.append(el("div", {},
      `${proposal.status}` +
      `${proposal.authorized_fingerprint ? " - authorized" : " - not authorized"}` +
      ` - ${proposal.start_s.toFixed(3)}-${proposal.end_s.toFixed(3)}s` +
      `${proposal.candidate ? ` - candidate ${proposal.candidate.master_sha256.slice(0, 12)}...` : ""}` +
      (proposal.proposal_id === musicRepaintSelected ? " - SELECTED" : "")));
    row.append(el("div", { class: "muted" }, proposal.prompt));
    list.append(row);
  }
  out.append(list);
  renderMusicRepaintStatus();
}

async function refreshMusicRepaint() {
  if (!currentProject) return;
  const info = document.getElementById("music-repaint-info");
  try {
    const [takesData, proposalsData, gateData] = await Promise.all([
      api(`/music/${currentProject.id}/takes`),
      api(`/music/${currentProject.id}/repaint/proposals`),
      api(`/music/${currentProject.id}/repaint/capacity-gate`)
        .catch(() => ({ gate: null })),
    ]);
    musicRepaintTakes = takesData.takes || [];
    musicRepaintProposals = proposalsData.proposals || [];
    if (musicRepaintSelected && !musicRepaintProposals.some(
      (item) => item.proposal_id === musicRepaintSelected)) {
      musicRepaintSelected = null;
    }
    const status = (musicState && musicState.repaint) || {};
    const gate = gateData.gate;
    info.textContent = [
      status.enabled
        ? "repaint submissions enabled (isolated process only)"
        : "repaint submissions disabled (live gate false)",
      gate
        ? (gate.approved
          ? `capacity gate approved (measured ${gate.measured_free_vram_mib} MiB, GPU${gate.gpu_index})`
          : "capacity gate proposed but CLOSED")
        : "capacity gate not proposed (closed)",
      status.recovery_required
        ? `RECOVERY HOLD: ${status.recovery_reason || "confirm provider stop"}`
        : "no recovery hold",
      `transition extent: ${status.crossfade_ms ?? 10} ms declared`,
      "Only PCM outside the declared edit/transition region is preserved; other sounds inside the interval may change.",
    ].join(" | ");
    info.className = status.recovery_required ? "err" : "muted";
    const sourceSelect = document.getElementById("music-repaint-source");
    const previous = sourceSelect.value;
    sourceSelect.innerHTML = "";
    if (!musicRepaintTakes.length) {
      sourceSelect.append(el("option", { value: "" }, "no completed takes"));
    }
    for (const take of musicRepaintTakes) {
      sourceSelect.append(el("option", { value: take.asset_id },
        `asset ${take.asset_id} - revision ${take.revision ?? "?"}` +
        (take.legacy ? " (legacy, unknown settings)" : "")));
    }
    if (previous && [...sourceSelect.options].some((option) => option.value === previous)) {
      sourceSelect.value = previous;
    } else if (sourceSelect.options.length) {
      sourceSelect.value = sourceSelect.options[sourceSelect.options.length - 1].value;
    }
    renderMusicRepaintSource();
    renderMusicRepaintSuggestions();
    renderMusicRepaintList(musicRepaintProposals);
    updateMusicRepaintConfirmed();
    updateMusicRepaintPlayback();
    await refreshMusicRepaintBoundaries();
  } catch (e) {
    info.textContent = `Section edit unavailable: ${e.message}`;
  }
}

function repaintConfirmedBoundaries() {
  if (!musicRepaintConfirmed) {
    throw new Error("Confirm the start/end boundaries first");
  }
  return musicRepaintConfirmed;
}

function repaintProposalBody(extra = {}) {
  return {
    project_id: currentProject.id,
    source_asset_id: document.getElementById("music-repaint-source").value,
    start_s: repaintConfirmedBoundaries().start,
    end_s: repaintConfirmedBoundaries().end,
    prompt: document.getElementById("music-repaint-prompt").value.trim(),
    ...extra,
  };
}

async function compareMusicRepaint() {
  if (!currentProject || !musicRepaintSelected) return;
  try {
    const data = await api(
      `/music/${currentProject.id}/repaint/proposals/${musicRepaintSelected}/compare`);
    const out = document.getElementById("music-repaint-compare");
    const interval = data.interval;
    const contextStart = Math.max(0, interval.start_s - 3);
    out.textContent =
      `source ${data.source.asset_id} sha ${data.source.sha256.slice(0, 16)}...` +
      ` revision ${data.source.revision} | interval ${interval.start_sample}-${interval.end_sample} samples` +
      ` (${interval.start_s.toFixed(3)}-${interval.end_s.toFixed(3)}s)` +
      ` | context ${interval.context_start_s.toFixed(3)}-${interval.context_end_s.toFixed(3)}s` +
      ` | transition ${data.candidate ? data.candidate.assembly.crossfade_ms : 0} ms | ` +
      (data.candidate
        ? `candidate ${data.candidate.master_sha256.slice(0, 16)}... (PCM outside transition verified)`
        : "no candidate yet");
    const sourceAudio = document.getElementById("music-repaint-source-context");
    const candidateAudio = document.getElementById("music-repaint-candidate-audio");
    sourceAudio.src = assetUrl(currentProject.id, data.source.asset_id);
    sourceAudio.addEventListener("loadedmetadata", () => {
      sourceAudio.currentTime = contextStart;
    }, { once: true });
    if (data.candidate) {
      candidateAudio.src = assetUrl(
        currentProject.id, data.candidate.master_asset_id);
      candidateAudio.addEventListener("loadedmetadata", () => {
        candidateAudio.currentTime = contextStart;
      }, { once: true });
    } else {
      candidateAudio.removeAttribute("src");
    }
  } catch (e) {
    toast(e.message, "err");
  }
}

async function preferMusicTake(assetId, btn) {
  btn.disabled = true;
  showProgress(true);
  try {
    const note = document.getElementById("music-prefer-note").value.trim();
    if (!note) throw new Error("Preferred-take note is required");
    await api(`/music/${currentProject.id}/take/prefer`, {
      method: "POST",
      body: JSON.stringify({
        project_id: currentProject.id,
        asset_id: assetId,
        note,
      }),
    });
    toast("Preferred take selected");
  } catch (e) {
    toast(e.message, "err");
  } finally {
    showProgress(false);
    btn.disabled = false;
    await Promise.all([refreshMusic(), refreshMusicTakes()]);
  }
}

document.getElementById("music-approve-btn").addEventListener("click", disabledWhile(
  document.getElementById("music-approve-btn"),
  async () => {
    const note = document.getElementById("music-approval-note").value.trim();
    const cap = Number(document.getElementById("music-cap").value);
    if (!note) throw new Error("Approval note is required");
    validateMusicArrangementForSave();
    const problems = musicArrangementProblems();
    const saved = await api(`/music/${currentProject.id}/draft`, {
      method: "PUT",
      body: JSON.stringify({
        project_id: currentProject.id,
        settings: musicFormSettings(),
      }),
    });
    await api(`/music/${currentProject.id}/approve`, {
      method: "POST",
      body: JSON.stringify({
        project_id: currentProject.id,
        note,
        submission_cap: cap,
      }),
    });
    await refreshMusic();
    const label = saved.revision_created
      ? `Saved v${saved.draft.version} and approved the exact on-screen lyrics/settings`
      : "Approved the exact on-screen lyrics/settings";
    const planNote = problems.length
      ? `; ${problems.length} plan note(s) (see plan notes)` : "";
    toast(`${label}${planNote}`, "ok");
  }
));

document.getElementById("music-generate-btn").addEventListener("click", async () => {
  const btn = document.getElementById("music-generate-btn");
  btn.disabled = true;
  showProgress(true);
  try {
    const result = await api(`/music/${currentProject.id}/generate`, {
      method: "POST",
      body: JSON.stringify({ project_id: currentProject.id }),
    });
    toast(result.current_attempt?.status === "completed" ? "Music generated" : "Music attempt completed");
  } catch (e) {
    toast(e.message, "err");
  } finally {
    showProgress(false);
    await refreshMusic();
  }
});

document.getElementById("music-cancel-btn").addEventListener("click", async () => {
  try {
    await api(`/music/${currentProject.id}/cancel`, {
      method: "POST",
      body: JSON.stringify({ project_id: currentProject.id }),
    });
    toast("Cancel requested; terminal state will be recorded");
    await refreshMusic();
  } catch (e) {
    toast(e.message, "err");
  }
});

document.getElementById("music-recover-btn").addEventListener("click", async () => {
  if (!confirm("Confirm the prior music attempt is stopped. This records recovery evidence but does not delete the journal.")) return;
  try {
    await api(`/music/${currentProject.id}/recover`, {
      method: "POST",
      body: JSON.stringify({ project_id: currentProject.id, confirm_stopped: true }),
    });
    toast("Music recovery recorded");
    await refreshMusic();
  } catch (e) {
    toast(e.message, "err");
  }
});

document.getElementById("music-audio-approve-btn").addEventListener("click", disabledWhile(
  document.getElementById("music-audio-approve-btn"),
  async () => {
    const note = document.getElementById("music-audio-note").value.trim();
    if (!note) throw new Error("Audio approval note is required");
    const current = musicState?.current_attempt;
    const latest = current?.asset_id
      ? (musicState.assets || []).find(a => a.id === current.asset_id)
      : null;
    if (!latest) throw new Error("No completed music asset to approve");
    await api(`/music/${currentProject.id}/audio/approve`, {
      method: "POST",
      body: JSON.stringify({
        project_id: currentProject.id,
        asset_id: latest.id,
        note,
      }),
    });
    toast("Exact music audio approved");
    await refreshMusic();
  }
));

document.getElementById("music-repaint-source").addEventListener("change", () => {
  musicRepaintConfirmed = null;
  musicRepaintConfirmedLabel = "";
  musicRepaintSharedStartFrom = null;
  musicRepaintSelectedOccurrence = null;
  stopMusicArrangementPlayback();
  updateMusicRepaintConfirmed();
  renderMusicRepaintSource();
  refreshMusicRepaintBoundaries();
});
document.getElementById("music-repaint-play-selection").addEventListener("click", playMusicRepaintSelection);
document.getElementById("music-arrangement-stop").addEventListener("click", stopMusicArrangementPlayback);
document.getElementById("music-repaint-stop").addEventListener("click", stopMusicRepaintSelection);
const repaintAudioEl = document.getElementById("music-repaint-audio");
repaintAudioEl.addEventListener("timeupdate", () => {
  if (!repaintAudioEl.paused && musicRepaintSelectionStopAt === null) {
    try {
      const { end } = validateMusicRepaintSelection();
      if (repaintAudioEl.currentTime < end - 0.05) {
        musicRepaintSelectionStopAt = end;
        startMusicRepaintStopMonitor();
      }
    } catch { }
  }
  if (musicRepaintSelectionStopAt !== null &&
      repaintAudioEl.currentTime >= musicRepaintSelectionStopAt - 0.01) {
    finishMusicRepaintSelection();
    return;
  }
  updateMusicRepaintPlayback();
});
repaintAudioEl.addEventListener("play", () => {
  if (musicRepaintSelectionStopAt !== null) return;
  let end;
  try {
    ({ end } = validateMusicRepaintSelection());
  } catch {
    return;
  }
  if (repaintAudioEl.currentTime < end - 0.05) {
    musicRepaintSelectionStopAt = end;
    startMusicRepaintStopMonitor();
    updateMusicRepaintPlayback();
  }
});
repaintAudioEl.addEventListener("pause", () => {
  if (musicRepaintSelectionStopAt === null && musicRepaintPlaybackTimer !== null) {
    clearInterval(musicRepaintPlaybackTimer);
    musicRepaintPlaybackTimer = null;
  }
  updateMusicRepaintPlayback();
});
repaintAudioEl.addEventListener("loadedmetadata", updateMusicRepaintPlayback);
for (const id of ["music-repaint-start", "music-repaint-end"]) {
  const input = document.getElementById(id);
  input.addEventListener("input", () => {
    stopMusicRepaintSelection();
    musicRepaintConfirmed = null;
    musicRepaintConfirmedLabel = "";
    musicRepaintSharedStartFrom = null;
    updateMusicRepaintConfirmed();
    updateMusicRepaintPlayback();
  });
  input.addEventListener("blur", () => {
    const seconds = parseMusicTimecode(input.value);
    if (Number.isFinite(seconds)) {
      input.value = formatMusicTimecode(seconds);
      updateMusicRepaintPlayback();
    }
  });
}
document.getElementById("music-repaint-confirm").addEventListener("click", async () => {
  const item = musicRepaintSelectedOccurrence;
  if (!item) {
    toast("Select a section occurrence first", "err");
    return;
  }
  if (!musicRepaintBoundarySupport) {
    toast("Boundary storage unavailable on the running backend (HTTP 404). Restart the backend, then reload Section Edit.", "err");
    return;
  }
  if (musicRepaintBoundaryError) {
    toast(musicRepaintBoundaryError, "err");
    return;
  }
  try {
    const { start, end } = validateMusicRepaintSelection();
    musicRepaintConfirmed = null;
    musicRepaintConfirmedLabel = "";
    updateMusicRepaintConfirmed();
    const assetId = musicRepaintCurrentAsset();
    await api(`/music/${currentProject.id}/section-boundaries`, {
      method: "POST",
      body: JSON.stringify({
        project_id: currentProject.id,
        asset_id: assetId,
        section: item.section,
        occurrence: item.occurrence,
        arrangement_index: item.arrangementIndex,
        start_s: start,
        end_s: end,
        suggested_start_s: item.start,
        suggested_end_s: item.end,
        shared_start_from: musicRepaintSharedStartFrom,
      }),
    });
    musicRepaintConfirmed = { start, end };
    musicRepaintConfirmedLabel = `${item.section} #${item.occurrence}`;
    updateMusicRepaintConfirmed();
    await refreshMusicRepaintBoundaries();
    toast(`Listening-confirmed ${item.section} #${item.occurrence}`);
  } catch (error) {
    musicRepaintConfirmed = null;
    musicRepaintConfirmedLabel = "";
    updateMusicRepaintConfirmed();
    const message = String(error.message || error);
    if (message === "Not Found" || message.includes("404")) {
      musicRepaintBoundarySupport = false;
      renderMusicRepaintReview();
      toast("Boundary save failed: the running backend does not expose section-boundaries (HTTP 404). Restart the backend, then reload Section Edit.", "err");
      return;
    }
    if (/completed source take|source take|source revision|source take bytes|asset .*not found/i.test(message)) {
      toast(`Boundary not saved: the selected source take or occurrence is stale. Reload Section Edit and select a completed source take. (${message})`, "err");
      return;
    }
    toast(`Boundary save failed: ${message}`, "err");
  }
});
document.getElementById("music-repaint-section").addEventListener("change", (event) => {
  const item = musicRepaintTimeSuggestions().find((candidate) =>
    musicRepaintOccurrenceKey(candidate) === event.target.value);
  if (item) selectMusicRepaintOccurrence(item);
});
document.getElementById("music-repaint-shared-start").addEventListener("click", () => {
  const selected = musicRepaintSelectedOccurrence;
  if (!selected) return;
  const items = musicRepaintTimeSuggestions();
  const index = items.findIndex((item) =>
    musicRepaintOccurrenceKey(item) === musicRepaintOccurrenceKey(selected));
  const previous = index > 0 ? items[index - 1] : null;
  const boundary = previous
    ? musicRepaintBoundaryFor(previous, musicRepaintCurrentAsset()) : null;
  if (!boundary) {
    toast("No previous confirmed section for this source", "err");
    return;
  }
  const startInput = document.getElementById("music-repaint-start");
  const currentStart = parseMusicTimecode(startInput.value);
  if (Number.isFinite(currentStart) && Math.abs(currentStart - boundary.end_s) > 0.0005) {
    const message = `Set Start to ${formatMusicTimecode(boundary.end_s)} from ` +
      `${previous.section} #${previous.occurrence} confirmed end?\n\n` +
      `This replaces the current Start (${formatMusicTimecode(currentStart)}).`;
    if (!window.confirm(message)) {
      return;
    }
  }
  startInput.value = formatMusicTimecode(boundary.end_s);
  musicRepaintSharedStartFrom = `${previous.section} #${previous.occurrence}`;
  musicRepaintConfirmed = null;
  musicRepaintConfirmedLabel = "";
  stopMusicRepaintSelection();
  updateMusicRepaintConfirmed();
  updateMusicRepaintPlayback();
  toast(`Start set to confirmed ${previous.section} #${previous.occurrence} end; ` +
    "edit Start to change; this section remains unconfirmed.");
});
document.getElementById("music-repaint-save").addEventListener("click", async () => {
  try {
    const result = await api(`/music/${currentProject.id}/repaint/proposals`, {
      method: "POST", body: JSON.stringify(repaintProposalBody()),
    });
    musicRepaintSelected = result.proposal.proposal_id;
    toast(`Proposal ${result.proposal.proposal_id} saved (${result.crossfade_samples} sample transition)`);
    await refreshMusicRepaint();
  } catch (e) { toast(e.message, "err"); }
});
document.getElementById("music-repaint-authorize").addEventListener("click", async () => {
  const note = document.getElementById("music-repaint-note").value.trim();
  try {
    await api(`/music/${currentProject.id}/repaint/proposals/${musicRepaintSelected}/authorize`, {
      method: "POST", body: JSON.stringify({ project_id: currentProject.id, note }),
    });
    await refreshMusicRepaint();
  } catch (e) { toast(e.message, "err"); }
});
document.getElementById("music-repaint-revise").addEventListener("click", async () => {
  try {
    await api(`/music/${currentProject.id}/repaint/proposals/${musicRepaintSelected}/revise`, {
      method: "POST", body: JSON.stringify(repaintProposalBody()),
    });
    toast("Proposal revised; any authorization was invalidated");
    await refreshMusicRepaint();
  } catch (e) { toast(e.message, "err"); }
});
document.getElementById("music-repaint-submit").addEventListener("click", async () => {
  try {
    await api(`/music/${currentProject.id}/repaint/proposals/${musicRepaintSelected}/submit`, { method: "POST" });
    await refreshMusic();
  } catch (e) {
    toast(e.message, "err");
    await refreshMusic();
  }
});
document.getElementById("music-repaint-recover").addEventListener("click", async () => {
  try {
    await api(`/music/${currentProject.id}/repaint/proposals/${musicRepaintSelected}/recover`, {
      method: "POST", body: JSON.stringify({ project_id: currentProject.id, confirm_stopped: true }),
    });
    toast("Provider stop confirmed; recovery hold cleared");
    await refreshMusic();
  } catch (e) { toast(e.message, "err"); }
});
document.getElementById("music-repaint-apply").addEventListener("click", async () => {
  try {
    const result = await api(`/music/${currentProject.id}/repaint/proposals/${musicRepaintSelected}/apply`, { method: "POST" });
    toast(`Applied as v${result.draft_version}`);
    await refreshMusic();
  } catch (e) { toast(e.message, "err"); }
});
document.getElementById("music-repaint-reject").addEventListener("click", async () => {
  try {
    await api(`/music/${currentProject.id}/repaint/proposals/${musicRepaintSelected}/reject`, {
      method: "POST", body: JSON.stringify({ project_id: currentProject.id, reason: "rejected in UI" }),
    });
    await refreshMusicRepaint();
  } catch (e) { toast(e.message, "err"); }
});
document.getElementById("music-repaint-list").addEventListener("click", compareMusicRepaint);

// ---------- artist profiles (separate versioned production presets) ----------
let profileListState = [];
let selectedProfile = null;
let selectedProfileRecord = null;
let profilePreviewState = null;
let profilePreviewToken = null;

function profileText(value) {
  if (value === null || value === undefined) return "(none)";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function profileOptionFields() {
  return [
    ["profile-exclude-bpm", "bpm"],
    ["profile-exclude-key", "key_scale"],
    ["profile-exclude-timesig", "time_signature"],
    ["profile-exclude-seed", "seed"],
    ["profile-exclude-duration", "duration_s"],
    ["profile-exclude-arrangement", "arrangement"],
  ];
}

function profileExclusions() {
  return profileOptionFields()
    .filter(([id]) => document.getElementById(id).checked)
    .map(([, field]) => field);
}

function profileDefaultsFromForm() {
  const defaults = {};
  const direction = document.getElementById("profile-default-direction").value.trim();
  const language = document.getElementById("profile-default-language").value.trim();
  if (direction) defaults.musical_direction = direction;
  if (language) defaults.language = language;
  if (document.getElementById("profile-default-codes").checked) {
    defaults.generate_audio_codes = true;
  }
  for (const [id, field] of [
    ["profile-default-temperature", "temperature"],
    ["profile-default-topp", "top_p"],
    ["profile-default-cfg", "cfg_scale"],
  ]) {
    const raw = document.getElementById(id).value.trim();
    if (!raw) continue;
    const value = Number(raw);
    if (!Number.isFinite(value)) throw new Error(`invalid ${field}`);
    defaults[field] = value;
  }
  return defaults;
}

function fillProfileForm(record) {
  document.getElementById("profile-name").value = record.name || "";
  document.getElementById("profile-artist").value = record.artist_description || "";
  document.getElementById("profile-vocal").value = record.vocal_description || "";
  const defaults = record.production_defaults || {};
  document.getElementById("profile-default-direction").value = defaults.musical_direction || "";
  document.getElementById("profile-default-language").value = defaults.language || "";
  document.getElementById("profile-default-codes").checked = !!defaults.generate_audio_codes;
  document.getElementById("profile-default-temperature").value =
    Number.isFinite(defaults.temperature) ? defaults.temperature : "";
  document.getElementById("profile-default-topp").value =
    Number.isFinite(defaults.top_p) ? defaults.top_p : "";
  document.getElementById("profile-default-cfg").value =
    Number.isFinite(defaults.cfg_scale) ? defaults.cfg_scale : "";
}

function renderProfileList() {
  const out = document.getElementById("profile-list");
  out.innerHTML = "";
  out.className = "muted";
  if (!profileListState.length) {
    out.textContent = "No profiles saved yet. Save one from a completed take.";
    return;
  }
  for (const meta of profileListState) {
    const row = el("div", { class: "profile-item" });
    const btn = el("button", {
      type: "button",
      class: selectedProfile?.id === meta.id ? "primary" : "",
      onclick: () => selectProfile(meta.id),
    }, meta.name);
    row.append(btn);
    row.append(el("div", { class: "muted" },
      `profile ${meta.id} | latest v${meta.latest_version} | ${meta.version_count} version(s) | updated ${meta.updated_at}`));
    out.append(row);
  }
  out.className = "";
}

function renderProfileDetails(record, versions) {
  const out = document.getElementById("profile-details");
  out.innerHTML = "";
  out.className = "";
  const source = record.source || {};
  out.append(el("div", { class: "profile-label" }, record.label || "production preset; singer identity not verified"));
  out.append(el("div", {},
    `Version ${record.version} "${record.name}" created ${record.created_at}`));
  out.append(el("div", { class: "muted" },
    `Source: project ${source.project_id} | asset ${source.asset_id} | attempt ${source.attempt_id} | revision ${source.revision ?? "unknown"}`));
  out.append(el("div", { class: "muted" },
    `Engine ${source.engine || "unknown"} | checkpoint ${source.checkpoint || "unknown"} | workflow ${source.workflow || "unknown"}`));
  out.append(el("div", { class: "muted" },
    `Checkpoint sha256 ${source.checkpoint_sha256 || "unknown"}`));
  out.append(el("div", { class: "muted" },
    `Recorded settings source: ${source.settings_source || "none"} | audio sha256 ${source.audio_sha256 || "unknown"}`));

  if (record.provenance_gaps && record.provenance_gaps.length) {
    out.append(el("div", { class: "err" }, "Provenance gaps (not reconstructed):"));
    const gaps = el("ul", {});
    for (const gap of record.provenance_gaps) gaps.append(el("li", {}, gap));
    out.append(gaps);
  } else {
    out.append(el("div", { class: "muted" }, "No provenance gaps recorded."));
  }

  out.append(el("h4", {}, "Recorded settings (captured from take)"));
  out.append(record.recorded_settings
    ? el("pre", { class: "profile-pre" }, JSON.stringify(record.recorded_settings, null, 2))
    : el("div", { class: "err" }, "No recorded settings. This profile cannot be loaded; later versions keep the gap."));

  out.append(el("h4", {}, "User-added descriptions"));
  out.append(el("div", {}, `Artist: ${record.artist_description || "(none)"}`));
  out.append(el("div", {}, `Vocal: ${record.vocal_description || "(none)"}`));

  out.append(el("h4", {}, "User-added production defaults"));
  out.append(el("pre", { class: "profile-pre" },
    JSON.stringify(record.production_defaults || {}, null, 2)));

  const select = document.getElementById("profile-version");
  select.innerHTML = "";
  for (const version of versions) {
    select.append(el("option", { value: String(version) },
      `v${version}${version === record.version ? " (shown)" : ""}`));
  }
  select.value = String(record.version);
  document.getElementById("profile-update").disabled = false;
}

async function selectProfile(profileId) {
  try {
    const data = await api(`/profiles/${profileId}`);
    selectedProfile = data.profile;
    selectedProfileRecord = data.version;
    renderProfileList();
    fillProfileForm(data.version);
    renderProfileDetails(data.version, data.versions);
    clearProfilePreview("Select preview to check compatibility and changes.");
    document.getElementById("profile-status").textContent = data.compatibility.status === "supported"
      ? "Provider compatibility supported."
      : data.compatibility.status === "unverified"
        ? "Provider compatibility unverified: " + data.compatibility.issues.join("; ")
        : "Provider compatibility unsupported: " + data.compatibility.issues.join("; ");
    document.getElementById("profile-status").className =
      data.compatibility.status === "supported" ? "muted" : "err";
  } catch (e) {
    toast(e.message, "err");
  }
}

async function selectProfileVersion(version) {
  if (!selectedProfile) return;
  try {
    const data = await api(`/profiles/${selectedProfile.id}?version=${version}`);
    selectedProfile = data.profile;
    selectedProfileRecord = data.version;
    fillProfileForm(data.version);
    renderProfileDetails(data.version, data.versions);
    clearProfilePreview("Version changed; preview again.");
  } catch (e) {
    toast(e.message, "err");
  }
}

async function refreshProfiles() {
  const listOut = document.getElementById("profile-list");
  try {
    const data = await api("/profiles");
    profileListState = data.profiles || [];
    if (selectedProfile) {
      selectedProfile = profileListState.find(p => p.id === selectedProfile.id) || selectedProfile;
    }
    renderProfileList();
  } catch (e) {
    listOut.textContent = `profile list failed: ${e.message}`;
    listOut.className = "err";
  }
  const select = document.getElementById("profile-take");
  select.innerHTML = "";
  if (!currentProject) {
    select.append(el("option", { value: "" }, "open a project to choose a take"));
    document.getElementById("profile-save").disabled = true;
    return;
  }
  try {
    const data = await api(`/music/${currentProject.id}/takes`);
    const takes = data.takes || [];
    for (const take of takes) {
      const suffix = take.legacy ? " | legacy: provenance gaps may be recorded" : "";
      select.append(el("option", { value: take.asset_id },
        `asset ${take.asset_id} | attempt ${take.attempt_id} | revision ${take.revision ?? "?"} | sha ${(take.audio_sha256 || "").slice(0, 12)}...${suffix}`));
    }
    if (!takes.length) {
      select.append(el("option", { value: "" }, "no completed takes in this project"));
    }
    document.getElementById("profile-save").disabled = !takes.length;
  } catch (e) {
    select.append(el("option", { value: "" }, `take list failed: ${e.message}`));
    document.getElementById("profile-save").disabled = true;
  }
}

async function saveProfileFromTake() {
  if (!currentProject) { toast("Open a project first", "err"); return; }
  const assetId = document.getElementById("profile-take").value;
  const name = document.getElementById("profile-name").value.trim();
  if (!assetId) { toast("Choose a completed take", "err"); return; }
  if (!name) { toast("Profile name is required", "err"); return; }
  let defaults;
  try {
    defaults = profileDefaultsFromForm();
  } catch (e) {
    toast(e.message, "err");
    return;
  }
  const status = document.getElementById("profile-status");
  try {
    const result = await api("/profiles", {
      method: "POST",
      body: JSON.stringify({
        project_id: currentProject.id,
        asset_id: assetId,
        name,
        artist_description: document.getElementById("profile-artist").value,
        vocal_description: document.getElementById("profile-vocal").value,
        production_defaults: defaults,
      }),
    });
    status.className = "muted";
    status.textContent = `Saved ${result.profile.name} (${result.profile.id}) v1. ` +
      `${(result.version.provenance_gaps || []).length} provenance gap(s) recorded.`;
    toast(`Artist profile v1 saved from asset ${assetId}`);
    await refreshProfiles();
    await selectProfile(result.profile.id);
  } catch (e) {
    status.className = "err";
    status.textContent = `save failed: ${e.message}`;
    toast(e.message, "err");
  }
}

async function updateProfileVersion() {
  if (!selectedProfile) { toast("Select a profile first", "err"); return; }
  let defaults;
  try {
    defaults = profileDefaultsFromForm();
  } catch (e) {
    toast(e.message, "err");
    return;
  }
  const name = document.getElementById("profile-name").value.trim();
  try {
    const result = await api(`/profiles/${selectedProfile.id}`, {
      method: "PUT",
      body: JSON.stringify({
        name: name || null,
        artist_description: document.getElementById("profile-artist").value,
        vocal_description: document.getElementById("profile-vocal").value,
        production_defaults: defaults,
      }),
    });
    toast(`Saved ${result.profile.name} as v${result.version.version}; earlier versions unchanged`);
    await refreshProfiles();
    await selectProfile(result.profile.id);
  } catch (e) {
    toast(e.message, "err");
  }
}

function clearProfilePreview(message) {
  profilePreviewState = null;
  profilePreviewToken = null;
  const out = document.getElementById("profile-preview-out");
  out.innerHTML = "";
  out.className = "muted";
  if (message) out.textContent = message;
  updateProfileLoadEnabled();
}

function updateProfileLoadEnabled() {
  const btn = document.getElementById("profile-load");
  if (!profilePreviewState || !profilePreviewToken) {
    btn.disabled = true;
    return;
  }
  const compat = profilePreviewState.compatibility || {};
  const acknowledged = document.getElementById("profile-ack-unverified").checked;
  const blocked = (profilePreviewState.unsupported_fields || []).length > 0 ||
    compat.status === "unsupported" ||
    (compat.status === "unverified" && !acknowledged);
  btn.disabled = !!blocked;
}

function renderProfilePreview(preview) {
  const out = document.getElementById("profile-preview-out");
  out.innerHTML = "";
  out.className = "";
  const compat = preview.compatibility || {};
  const compatClass = compat.status === "supported" ? "muted" :
    compat.status === "unverified" ? "err" : "err";
  out.append(el("div", { class: compatClass },
    `Provider compatibility: ${compat.status}. ${(compat.issues || []).join("; ")}`));
  if (compat.status === "unverified") {
    out.append(el("div", { class: "err" },
      "Unverified compatibility requires the acknowledgement checkbox; the load will not silently substitute a provider."));
  }
  if ((preview.unsupported_fields || []).length) {
    out.append(el("div", { class: "err" },
      `Unsupported fields (not loaded): ${preview.unsupported_fields.join(", ")}`));
  } else {
    out.append(el("div", { class: "muted" }, "Unsupported fields: none."));
  }
  out.append(el("h4", {}, "Changes on confirm"));
  if (!(preview.changes || []).length) {
    out.append(el("div", { class: "muted" }, "No recorded setting changes."));
  } else {
    const ul = el("ul", {});
    for (const change of preview.changes) {
      ul.append(el("li", {},
        `${change.field}: ${profileText(change.from)} -> ${profileText(change.to)}`));
    }
    out.append(ul);
  }
  out.append(el("div", { class: "muted" },
    `Excluded: ${(preview.excluded || []).map(item => item.label).join(", ") || "none"}`));
  out.append(el("div", { class: "muted" },
    `Preserved: ${(preview.preserved || []).join("; ")}`));
  out.append(el("div", { class: "muted" },
    "Confirmation creates a new draft revision through the normal approval rules; it cannot raise caps or copy audio approvals."));
}

async function previewProfileLoad() {
  if (!selectedProfile || !selectedProfileRecord) { toast("Select a profile first", "err"); return; }
  if (!currentProject) { toast("Open a project first", "err"); return; }
  try {
    const preview = await api(
      `/profiles/${selectedProfile.id}/versions/${selectedProfileRecord.version}/preview`, {
        method: "POST",
        body: JSON.stringify({
          project_id: currentProject.id,
          exclude: profileExclusions(),
          apply_defaults: document.getElementById("profile-apply-defaults").checked,
          acknowledge_unverified: document.getElementById("profile-ack-unverified").checked,
        }),
      });
    profilePreviewState = preview;
    profilePreviewToken = preview.preview_token;
    renderProfilePreview(preview);
    updateProfileLoadEnabled();
  } catch (e) {
    clearProfilePreview(`preview failed: ${e.message}`);
    toast(e.message, "err");
  }
}

async function loadProfileIntoProject() {
  if (!selectedProfile || !selectedProfileRecord || !profilePreviewToken) {
    toast("Preview the load first", "err");
    return;
  }
  const compat = profilePreviewState?.compatibility || {};
  if (compat.status === "unverified" &&
      !document.getElementById("profile-ack-unverified").checked) {
    toast("Acknowledge unverified provider compatibility first", "err");
    return;
  }
  const changes = (profilePreviewState?.changes || []).length;
  if (!window.confirm(
    `Load profile "${selectedProfileRecord.name}" v${selectedProfileRecord.version} into ` +
    `project ${currentProject.id}?\n\nThis creates a new settings revision (${changes} setting change(s)), ` +
    "preserves current lyrics, and clears the existing approval. Audio approvals, submission " +
    "allowances, capacity permissions, and live authorization are never copied.")) {
    return;
  }
  try {
    const result = await api(
      `/profiles/${selectedProfile.id}/versions/${selectedProfileRecord.version}/load`, {
        method: "POST",
        body: JSON.stringify({
          project_id: currentProject.id,
          exclude: profileExclusions(),
          apply_defaults: document.getElementById("profile-apply-defaults").checked,
          acknowledge_unverified: document.getElementById("profile-ack-unverified").checked,
          preview_token: profilePreviewToken,
          confirm: true,
        }),
      });
    const draft = result.draft?.draft || {};
    toast(`Loaded profile as draft v${draft.version ?? "?"}; approval invalidated`);
    clearProfilePreview("Loaded. Preview again to load another version.");
    await openProject(currentProject.id);
  } catch (e) {
    clearProfilePreview(`load failed: ${e.message}`);
    toast(e.message, "err");
  }
}

document.getElementById("profile-save").addEventListener("click", saveProfileFromTake);
document.getElementById("profile-update").addEventListener("click", updateProfileVersion);
document.getElementById("profile-preview").addEventListener("click", previewProfileLoad);
document.getElementById("profile-load").addEventListener("click", loadProfileIntoProject);
document.getElementById("profile-version").addEventListener("change", (event) => {
  selectProfileVersion(Number(event.target.value));
});
for (const [id] of profileOptionFields()) {
  document.getElementById(id).addEventListener("change", () =>
    clearProfilePreview("Options changed; preview again."));
}
document.getElementById("profile-apply-defaults").addEventListener("change", () =>
  clearProfilePreview("Options changed; preview again."));
document.getElementById("profile-ack-unverified").addEventListener("change", updateProfileLoadEnabled);

renderMusicArrangement();