// Production-brief helpers and the project-scoped workspace state machine.
// Pure helpers have no DOM/network. createBriefWorkspace accepts injected
// document/window/fetch/confirm dependencies so page state can be tested
// without installing a DOM package or claiming browser acceptance.
export const ORIGINS = [
  'user_assertion',
  'documented_evidence',
  'measured_evidence',
  'proposal',
  'contradiction',
  'unknown',
];
export const CAPABILITY_KINDS = [
  'score',
  'alignment',
  'tempo_meter_map',
  'guide',
  'midi',
  'musicxml',
  'notation',
];
export const CAPABILITY_STATUSES = ['unavailable', 'unverified', 'provided_unverified'];
export const STRUCTURED_KEYS = [
  'key',
  'tempo_bpm',
  'meter',
  'intended_sections',
  'hard_constraints',
  'asset_references',
  'capabilities',
];
const PROJECT_ID_RE = /^[0-9a-f]{12}$/;
const EDITOR_IDS = [
  'raw-prompt', 'raw-research', 'draft-note', 'structured-json',
  'lyrics-text', 'lyrics-origin',
];
const WRITE_KINDS = new Set(['save', 'review', 'context']);
const VIEW_KINDS = new Set(['project', 'revision']);

function origin(value) {
  const text = String(value || 'unknown').trim();
  if (!ORIGINS.includes(text)) throw new Error(`unknown origin: ${text}`);
  return text;
}

export function emptyStructured() {
  return {
    key: null,
    tempo_bpm: null,
    meter: null,
    intended_sections: [],
    hard_constraints: [],
    asset_references: [],
    capabilities: [],
  };
}

export function structuredFromRecord(record) {
  const source = record || {};
  return JSON.parse(JSON.stringify({
    key: source.key ?? null,
    tempo_bpm: source.tempo_bpm ?? null,
    meter: source.meter ?? null,
    intended_sections: source.intended_sections ?? [],
    hard_constraints: source.hard_constraints ?? [],
    asset_references: source.asset_references ?? [],
    capabilities: source.capabilities ?? [],
  }));
}

export function parseStructuredJson(text) {
  const raw = String(text ?? '').trim();
  if (!raw) return emptyStructured();
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    throw new Error(`Structured fields are not valid JSON: ${error.message}`);
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Structured fields must be a JSON object.');
  }
  for (const key of Object.keys(parsed)) {
    if (!STRUCTURED_KEYS.includes(key)) {
      throw new Error(`Unsupported structured field: ${key}`);
    }
  }
  for (const key of ['intended_sections', 'hard_constraints', 'asset_references', 'capabilities']) {
    if (parsed[key] !== undefined && !Array.isArray(parsed[key])) {
      throw new Error(`Structured field ${key} must be an array.`);
    }
  }
  return parsed;
}

export function buildDraftPayload(input) {
  const structured = input.structured ?? parseStructuredJson(input.structured_json);
  for (const key of Object.keys(structured)) {
    if (!STRUCTURED_KEYS.includes(key)) {
      throw new Error(`Unsupported structured field: ${key}`);
    }
  }
  for (const key of ['intended_sections', 'hard_constraints', 'asset_references', 'capabilities']) {
    if (structured[key] !== undefined && !Array.isArray(structured[key])) {
      throw new Error(`Structured field ${key} must be an array.`);
    }
  }
  return {
    raw_prompt: String(input.raw_prompt ?? ''),
    raw_research: String(input.raw_research ?? ''),
    note: String(input.note ?? ''),
    key: structured.key ?? null,
    tempo_bpm: structured.tempo_bpm ?? null,
    meter: structured.meter ?? null,
    intended_sections: structured.intended_sections ?? [],
    lyrics_text: String(input.lyrics_text ?? ''),
    lyrics_origin: origin(input.lyrics_origin ?? 'user_assertion'),
    hard_constraints: structured.hard_constraints ?? [],
    asset_references: structured.asset_references ?? [],
    capabilities: structured.capabilities ?? [],
  };
}

export function requiredChecklist(payload) {
  const items = [
    ['raw_prompt', Boolean(payload.raw_prompt && payload.raw_prompt.trim()), 'Raw supplied prompt is present'],
    ['lyrics_text', Boolean(payload.lyrics_text && payload.lyrics_text.trim()), 'Exact supplied lyrics are present'],
    ['intended_sections', payload.intended_sections.length > 0, 'At least one intended section is recorded'],
    ['hard_constraints', payload.hard_constraints.length > 0, 'At least one hard constraint is recorded'],
  ];
  return {
    label: 'Deterministic required-field checklist; not model-extracted and not an approval',
    items: items.map(([key, ok, text]) => ({ field: key, ok, label: text })),
  };
}

export function summarizeOrigins(payload) {
  const counts = {
    user_assertion: 0, documented_evidence: 0, measured_evidence: 0,
    proposal: 0, contradiction: 0, unknown: 0,
  };
  const values = [payload.key, payload.tempo_bpm, payload.meter]
    .filter(Boolean)
    .concat(payload.intended_sections, payload.hard_constraints);
  for (const item of values) counts[item.origin] = (counts[item.origin] || 0) + 1;
  return {
    counts,
    unknown: counts.unknown,
    proposal: counts.proposal,
    contradiction: counts.contradiction,
    evidence_verified: false,
    note: 'Evidence links remain unverified unless separately checked.',
  };
}

export function detectedSectionTags(lyrics) {
  const found = [];
  for (const line of String(lyrics || '').split(/\r?\n/)) {
    const match = line.trim().match(/^\[([^\]]+)\]$/);
    if (match) found.push(match[1].trim());
  }
  return found;
}

export function formatReviewState(status) {
  if (!status || !status.current_revision) return 'No saved production brief.';
  const review = status.review;
  if (!review) return `Revision ${status.current_revision} is a draft; not reviewed or approved.`;
  if (status.review_stale) return `Revision ${status.current_revision} has a stale review record; save a successor and review it again.`;
  return `Revision ${status.current_revision} has a self-asserted operator review. This is not generation or music approval.`;
}

export function createBriefWorkspace(deps = {}) {
  const doc = deps.document ?? (typeof document !== 'undefined' ? document : null);
  const win = deps.window ?? (typeof window !== 'undefined' ? window : null);
  const fetchFn = deps.fetch ?? (typeof fetch !== 'undefined' ? fetch : null);
  const ask = deps.confirm ?? (win && typeof win.confirm === 'function'
    ? (message) => win.confirm(message)
    : () => false);
  if (!doc) throw new Error('createBriefWorkspace requires a document');
  if (!fetchFn) throw new Error('createBriefWorkspace requires fetch');

  const $ = (id) => doc.getElementById(id);
  const state = {
    pid: '',
    status: null,
    record: null,
    response: null,
    operation: 0,
    busy: false,
    pendingKind: null,
    writePending: false,
    dirty: false,
    snapshot: null,
  };
  const dynamicButtons = [];

  function notice(message, error = false) {
    const node = $('notice');
    node.textContent = message || '';
    node.className = error ? 'pb-error' : 'pb-ok';
  }

  async function api(path, options = {}) {
    const response = await fetchFn(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    let payload = null;
    try { payload = await response.json(); } catch { payload = null; }
    if (!response.ok) {
      const detail = payload && (payload.detail || payload);
      const message = typeof detail === 'string' ? detail : JSON.stringify(detail);
      throw new Error(`${response.status}: ${message}`);
    }
    return payload;
  }

  function fillSelect(select, values) {
    select.replaceChildren();
    for (const value of values) {
      const option = doc.createElement('option');
      option.value = value;
      option.textContent = value;
      select.append(option);
    }
  }

  function fillOriginSelects() {
    fillSelect($('lyrics-origin'), ORIGINS);
    $('lyrics-origin').value = 'user_assertion';
  }

  function captureEditableForm() {
    return {
      raw_prompt: $('raw-prompt').value,
      raw_research: $('raw-research').value,
      note: $('draft-note').value,
      structured_json: $('structured-json').value,
      lyrics_text: $('lyrics-text').value,
      lyrics_origin: $('lyrics-origin').value || 'user_assertion',
    };
  }

  function applyEditableForm(form) {
    $('raw-prompt').value = form.raw_prompt ?? '';
    $('raw-research').value = form.raw_research ?? '';
    $('draft-note').value = form.note ?? '';
    $('structured-json').value = form.structured_json ?? JSON.stringify(emptyStructured(), null, 2);
    $('lyrics-text').value = form.lyrics_text ?? '';
    $('lyrics-origin').value = form.lyrics_origin || 'user_assertion';
  }

  function snapshotForm() {
    return JSON.stringify(captureEditableForm());
  }

  function isDirty() {
    return state.snapshot !== null && JSON.stringify(captureEditableForm()) !== state.snapshot;
  }

  function setReviewTarget(record) {
    if (record) {
      $('review-revision').value = record.revision;
      $('review-hash').value = record.content_sha256;
      $('review-target').textContent =
        `Loaded saved revision ${record.revision} — ${record.content_sha256}. ` +
        'Unsaved editor content is not reviewed.';
      $('context-revision').value = record.revision;
    } else {
      $('review-revision').value = '';
      $('review-hash').value = '';
      $('review-target').textContent = 'No saved revision loaded for review.';
      $('context-revision').value = '';
    }
  }

  function clearReviewConsent() {
    $('review-confirm').checked = false;
  }

  function clearOutputs() {
    for (const id of ['draft-output', 'review-output', 'compare-output', 'context-output']) {
      $(id).textContent = '';
    }
  }

  function emptyForm() {
    applyEditableForm({
      raw_prompt: '',
      raw_research: '',
      note: '',
      structured_json: JSON.stringify(emptyStructured(), null, 2),
      lyrics_text: '',
      lyrics_origin: 'user_assertion',
    });
    setReviewTarget(null);
    clearReviewConsent();
    $('review-note').value = '';
    $('compare-from').value = '';
    $('compare-to').value = '';
    $('context-section').value = '';
    $('context-occurrence').value = '';
    $('context-budget').value = '8000';
    clearOutputs();
  }

  function populate(record) {
    $('raw-prompt').value = record.raw_prompt || '';
    $('raw-research').value = record.raw_research || '';
    $('draft-note').value = record.note || '';
    $('structured-json').value = JSON.stringify(structuredFromRecord(record), null, 2);
    $('lyrics-text').value = record.lyrics_text || '';
    $('lyrics-origin').value = record.lyrics_origin || 'user_assertion';
    setReviewTarget(record);
    clearReviewConsent();
    $('review-note').value = '';
    $('review-output').textContent = '';
  }

  function readFormPayload() {
    return buildDraftPayload({
      ...captureEditableForm(),
      structured_json: $('structured-json').value,
    });
  }

  function renderChecklist() {
    const box = $('checklist');
    box.replaceChildren();
    const title = doc.createElement('p');
    title.className = 'pb-note';
    try {
      const payload = readFormPayload();
      const checklist = requiredChecklist(payload);
      title.textContent = checklist.label;
      box.append(title);
      const list = doc.createElement('ul');
      for (const item of checklist.items) {
        const row = doc.createElement('li');
        row.textContent = `${item.ok ? 'OK' : 'MISSING'} - ${item.label}`;
        list.append(row);
      }
      box.append(list);
      const origins = summarizeOrigins(payload);
      const source = doc.createElement('p');
      source.className = 'pb-note';
      source.textContent = `Origins: unknown=${origins.unknown}, proposal=${origins.proposal}, contradiction=${origins.contradiction}. ${origins.note}`;
      box.append(source);
      const tags = detectedSectionTags(payload.lyrics_text);
      if (tags.length) {
        const tagNote = doc.createElement('p');
        tagNote.className = 'pb-note';
        tagNote.textContent = `Bracketed lyrics tags found (read-only, not intended sections): ${tags.join(', ')}`;
        box.append(tagNote);
      }
    } catch (error) {
      title.textContent = 'Checklist could not be built.';
      box.append(title);
      const problem = doc.createElement('p');
      problem.className = 'pb-error';
      problem.textContent = String(error.message || error);
      box.append(problem);
    }
  }

  function renderRevisions(status) {
    const box = $('revisions-list');
    box.replaceChildren();
    dynamicButtons.length = 0;
    if (!status.revisions || !status.revisions.length) {
      const empty = doc.createElement('p');
      empty.className = 'pb-note';
      empty.textContent = 'No saved production brief revision yet.';
      box.append(empty);
      return;
    }
    for (const revision of status.revisions) {
      const row = doc.createElement('div');
      row.className = 'pb-revision';
      const text = doc.createElement('p');
      text.textContent = `Revision ${revision.revision} - ${revision.reviewed ? 'reviewed (self-asserted)' : 'draft, not approved'}${revision.review_stale ? ' - STALE REVIEW' : ''}`;
      row.append(text);
      const hash = doc.createElement('p');
      hash.className = 'pb-note';
      hash.textContent = `content_sha256: ${revision.content_sha256}`;
      row.append(hash);
      if (revision.note) {
        const note = doc.createElement('p');
        note.className = 'pb-note';
        note.textContent = `Note: ${revision.note}`;
        row.append(note);
      }
      const actions = doc.createElement('div');
      actions.className = 'pb-row';
      const open = doc.createElement('button');
      open.type = 'button';
      open.textContent = 'Open revision';
      open.disabled = state.busy;
      open.onclick = () => loadRevision(revision.revision).catch((error) =>
        notice(String(error.message || error), true));
      dynamicButtons.push(open);
      actions.append(open);
      row.append(actions);
      box.append(row);
    }
  }

  function updateDirty() {
    state.dirty = isDirty();
    $('dirty-state').textContent = !state.pid
      ? 'No project loaded.'
      : state.dirty
        ? 'Unsaved editor changes; save them before review.'
        : 'Editor matches the loaded saved revision.';
    updateControls();
  }

  function applyEditorLock() {
    for (const id of EDITOR_IDS) {
      const element = $(id);
      element.disabled = state.busy;
      if ('readOnly' in element) element.readOnly = state.busy;
    }
  }

  function assertCanStart(kind) {
    if (VIEW_KINDS.has(kind)) {
      if (state.writePending) {
        throw new Error('A write is outstanding; wait for it to finish before switching project or revision.');
      }
      if (state.busy && !VIEW_KINDS.has(state.pendingKind)) {
        throw new Error('Another operation is in progress.');
      }
      return;
    }
    if (state.busy) throw new Error('A request is already in progress.');
  }

  function beginOperation(kind) {
    state.operation += 1;
    state.busy = true;
    state.pendingKind = kind;
    state.writePending = WRITE_KINDS.has(kind);
    updateControls();
    return state.operation;
  }

  function endOperation(token) {
    if (token !== state.operation) return;
    state.busy = false;
    state.pendingKind = null;
    state.writePending = false;
    updateControls();
  }

  function updateControls() {
    const busy = state.busy;
    applyEditorLock();
    $('load-project').disabled = busy;
    $('project-id').disabled = busy;
    $('save-draft').disabled = busy || !state.pid;
    $('review-btn').disabled = busy || !state.record || state.dirty;
    $('review-confirm').disabled = busy || !state.record || state.dirty;
    $('compare-btn').disabled = busy || !state.pid;
    $('context-btn').disabled = busy || !state.record;
    for (const button of dynamicButtons) button.disabled = busy;
  }

  function onEdit() {
    if (state.busy) return;
    clearReviewConsent();
    updateDirty();
    renderChecklist();
  }

  function renderStatus(status) {
    state.status = status;
    $('status-output').textContent = JSON.stringify({
      project_id: status.project_id,
      current_revision: status.current_revision,
      reviewed: status.reviewed,
      review_stale: status.review_stale,
      state: formatReviewState(status),
      limits: status.limits,
      permissions: {
        generation: status.generation,
        audio_processing: status.audio_processing,
        model_calls: status.model_calls,
        tool_permissions: status.tool_permissions,
        legacy_music_approval_transferred: status.legacy_music_approval_transferred,
      },
    }, null, 2);
    renderRevisions(status);
  }

  function capturePrevious() {
    return {
      pid: state.pid,
      status: state.status,
      record: state.record,
      response: state.response,
      snapshot: state.snapshot,
      form: captureEditableForm(),
      reviewTarget: $('review-target').textContent,
      reviewNote: $('review-note').value,
      reviewConfirm: $('review-confirm').checked,
      contextSection: $('context-section').value,
      contextOccurrence: $('context-occurrence').value,
      contextBudget: $('context-budget').value,
    };
  }

  function restorePrevious(previous) {
    state.pid = previous.pid;
    state.status = previous.status;
    state.record = previous.record;
    state.response = previous.response;
    state.snapshot = previous.snapshot;
    if (previous.pid) {
      $('project-id').value = previous.pid;
      applyEditableForm(previous.form);
      setReviewTarget(previous.record);
      $('review-target').textContent = previous.reviewTarget;
      $('review-note').value = previous.reviewNote;
      $('review-confirm').checked = previous.reviewConfirm;
      $('context-section').value = previous.contextSection;
      $('context-occurrence').value = previous.contextOccurrence;
      $('context-budget').value = previous.contextBudget;
      state.dirty = isDirty();
    } else {
      $('project-id').value = '';
      state.pid = '';
      state.status = null;
      state.record = null;
      state.response = null;
      state.snapshot = null;
      emptyForm();
      state.dirty = false;
    }
    if (previous.status) renderStatus(previous.status);
    else $('status-output').textContent = 'No project loaded.';
    updateDirty();
  }

  async function loadProject(pid, options = {}) {
    if (!PROJECT_ID_RE.test(pid)) {
      throw new Error('Project ID must be 12 lowercase hex characters.');
    }
    assertCanStart('project');
    if (!options.force && state.pid && isDirty()) {
      if (!ask(`Discard unsaved editor changes in ${state.pid} and load ${pid}?`)) {
        notice('Project switch cancelled; unsaved edits preserved.', true);
        return false;
      }
    }
    const previous = capturePrevious();
    const token = beginOperation('project');
    try {
      const status = await api(`/api/projects/${encodeURIComponent(pid)}/production-brief`);
      if (token !== state.operation) return false;
      let response = null;
      if (status.current_revision) {
        response = await api(`/api/projects/${encodeURIComponent(pid)}/production-brief/revisions/${status.current_revision}`);
        if (token !== state.operation) return false;
      }
      state.pid = pid;
      state.status = status;
      state.record = response ? response.record : null;
      state.response = response;
      clearOutputs();
      if (response) populate(response.record);
      else emptyForm();
      $('project-id').value = pid;
      state.snapshot = snapshotForm();
      state.dirty = false;
      clearReviewConsent();
      renderStatus(status);
      updateDirty();
      notice(`Loaded project ${pid}. Saving writes only production-brief records.`, false);
      return true;
    } catch (error) {
      if (token !== state.operation) return false;
      restorePrevious(previous);
      notice(`Load failed; ${previous.pid ? `still editing ${previous.pid}` : 'no project active'}. ${error.message}`, true);
      return false;
    } finally {
      endOperation(token);
    }
  }

  async function loadRevision(revision, options = {}) {
    if (!state.pid) throw new Error('Load a project first.');
    assertCanStart('revision');
    if (!options.force && isDirty()) {
      if (!ask(`Discard unsaved editor changes and open revision ${revision}?`)) {
        notice('Revision switch cancelled; unsaved edits preserved.', true);
        return false;
      }
    }
    const pid = state.pid;
    const previous = capturePrevious();
    const token = beginOperation('revision');
    try {
      const response = await api(`/api/projects/${encodeURIComponent(pid)}/production-brief/revisions/${revision}`);
      if (token !== state.operation) return false;
      state.record = response.record;
      state.response = response;
      clearOutputs();
      populate(response.record);
      state.snapshot = snapshotForm();
      state.dirty = false;
      clearReviewConsent();
      updateDirty();
      notice(`Opened revision ${response.record.revision}. Prior revisions are immutable.`, false);
      return true;
    } catch (error) {
      if (token !== state.operation) return false;
      restorePrevious(previous);
      notice(`Revision load failed; restored the previous saved view. ${error.message}`, true);
      return false;
    } finally {
      endOperation(token);
    }
  }

  async function saveDraft() {
    if (!state.pid) throw new Error('Load a project first.');
    assertCanStart('save');
    const payload = readFormPayload();
    const pid = state.pid;
    const token = beginOperation('save');
    try {
      const response = await api(`/api/projects/${encodeURIComponent(pid)}/production-brief/drafts`, {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (token !== state.operation) return false;
      state.record = response.record;
      state.response = response;
      populate(response.record);
      state.snapshot = snapshotForm();
      state.dirty = false;
      clearReviewConsent();
      $('draft-output').textContent = JSON.stringify({
        revision: response.record.revision,
        content_sha256: response.record.content_sha256,
        draft_not_approved: response.draft_not_approved,
        reviewed: response.reviewed,
        note: 'Save is not approval. This draft has no inherited review.',
      }, null, 2);
      try {
        const status = await api(`/api/projects/${encodeURIComponent(pid)}/production-brief`);
        if (token === state.operation) renderStatus(status);
      } catch {
        // The saved record remains available; status refresh is non-destructive.
      }
      updateDirty();
      notice(`Saved production brief revision ${response.record.revision} as a draft.`, false);
      return true;
    } catch (error) {
      if (token === state.operation) {
        notice(String(error.message || error), true);
      }
      return false;
    } finally {
      endOperation(token);
    }
  }

  async function review() {
    if (!state.record) throw new Error('Open a saved revision first.');
    assertCanStart('review');
    if (isDirty()) throw new Error('Save or discard unsaved editor changes before reviewing the saved revision.');
    if (!$('review-confirm').checked) throw new Error('Check the explicit operator review box first.');
    const record = state.record;
    if (Number($('review-revision').value) !== record.revision
        || $('review-hash').value.trim() !== record.content_sha256) {
      throw new Error('Review target does not match the loaded saved revision; reload it.');
    }
    const pid = state.pid;
    const token = beginOperation('review');
    try {
      const response = await api(`/api/projects/${encodeURIComponent(pid)}/production-brief/revisions/${record.revision}/review`, {
        method: 'POST',
        body: JSON.stringify({
          expected_content_sha256: record.content_sha256,
          note: $('review-note').value,
          confirm: true,
        }),
      });
      if (token !== state.operation) return false;
      clearReviewConsent();
      $('review-output').textContent = JSON.stringify(response, null, 2);
      try {
        const status = await api(`/api/projects/${encodeURIComponent(pid)}/production-brief`);
        if (token === state.operation) renderStatus(status);
      } catch {
        // The review response is retained even if the status refresh fails.
      }
      notice(`Recorded a self-asserted operator review for revision ${record.revision}. It grants no generation or legacy music approval.`, false);
      return true;
    } catch (error) {
      if (token === state.operation) {
        clearReviewConsent();
        notice(String(error.message || error), true);
      }
      return false;
    } finally {
      endOperation(token);
    }
  }

  async function compare() {
    if (!state.pid) throw new Error('Load a project first.');
    assertCanStart('compare');
    const pid = state.pid;
    const from = Number($('compare-from').value);
    const to = Number($('compare-to').value);
    const token = beginOperation('compare');
    try {
      const response = await api(`/api/projects/${encodeURIComponent(pid)}/production-brief/revisions/compare?from_version=${from}&to_version=${to}`);
      if (token !== state.operation) return false;
      $('compare-output').textContent = JSON.stringify(response, null, 2);
      notice('Compared two saved revisions. No files were changed.', false);
      return true;
    } catch (error) {
      if (token === state.operation) notice(String(error.message || error), true);
      return false;
    } finally {
      endOperation(token);
    }
  }

  async function assembleContext() {
    if (!state.pid) throw new Error('Load a project first.');
    assertCanStart('context');
    const pid = state.pid;
    const revision = Number($('context-revision').value);
    const body = { max_tokens: Number($('context-budget').value || 8000) };
    const section = $('context-section').value.trim();
    const occurrence = $('context-occurrence').value.trim();
    if (section) body.section = section;
    if (occurrence) body.occurrence = Number(occurrence);
    const token = beginOperation('context');
    try {
      const response = await api(`/api/projects/${encodeURIComponent(pid)}/production-brief/revisions/${revision}/context`, {
        method: 'POST',
        body: JSON.stringify(body),
      });
      if (token !== state.operation) return false;
      $('context-output').textContent = [
        `context_sha256=${response.context_sha256}`,
        `token_estimate=${response.token_estimate} (${response.token_count_kind})`,
        `budget_tokens=${response.budget_tokens}`,
        `template=${response.template_id}@${response.template_version} sha256=${response.template_sha256}`,
        `prompt_injection_eliminated=${response.prompt_injection_eliminated}`,
        '',
        response.context,
      ].join('\n');
      notice(`Assembled context ${response.context_sha256}. No model or provider was called.`, false);
      return true;
    } catch (error) {
      if (token === state.operation) notice(String(error.message || error), true);
      return false;
    } finally {
      endOperation(token);
    }
  }

  function start() {
    fillOriginSelects();
    for (const id of ['raw-prompt', 'raw-research', 'draft-note', 'structured-json', 'lyrics-text']) {
      $(id).addEventListener('input', onEdit);
    }
    $('lyrics-origin').addEventListener('change', onEdit);
    for (const id of ['review-revision', 'review-hash']) {
      $(id).addEventListener('input', () => { clearReviewConsent(); updateControls(); });
    }
    $('review-confirm').addEventListener('change', updateControls);
    $('load-project').onclick = () => {
      const pid = $('project-id').value.trim();
      if (!pid) { notice('Enter a project ID.', true); return; }
      loadProject(pid).catch((error) => notice(String(error.message || error), true));
    };
    $('save-draft').onclick = () => {
      if (!state.pid) { notice('Load a project first.', true); return; }
      saveDraft().catch((error) => notice(String(error.message || error), true));
    };
    $('review-btn').onclick = () => {
      if (!state.pid) { notice('Load a project first.', true); return; }
      review().catch((error) => notice(String(error.message || error), true));
    };
    $('compare-btn').onclick = () => {
      if (!state.pid) { notice('Load a project first.', true); return; }
      compare().catch((error) => notice(String(error.message || error), true));
    };
    $('context-btn').onclick = () => {
      if (!state.pid) { notice('Load a project first.', true); return; }
      assembleContext().catch((error) => notice(String(error.message || error), true));
    };
    const search = deps.search ?? (win && win.location ? win.location.search : '');
    const pid = new URLSearchParams(search).get('project');
    if (pid) {
      loadProject(pid).catch((error) => notice(String(error.message || error), true));
    } else {
      renderChecklist();
      updateDirty();
    }
  }

  return {
    state,
    start,
    loadProject,
    loadRevision,
    saveDraft,
    review,
    compare,
    assembleContext,
    readFormPayload,
    isDirty,
    updateDirty,
    updateControls,
    emptyForm,
    populate,
    clearReviewConsent,
  };
}
