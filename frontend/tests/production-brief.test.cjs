const { test } = require('node:test');
const assert = require('node:assert/strict');
const corePromise = import('../static/production-brief-core.mjs');

class FakeElement {
  constructor(id = '') {
    this.id = id;
    this.value = '';
    this.textContent = '';
    this.className = '';
    this.checked = false;
    this.disabled = false;
    this.readOnly = false;
    this.children = [];
    this.listeners = {};
    this.onclick = null;
    this.type = '';
    this.placeholder = '';
    this.size = 0;
    this.title = '';
  }

  addEventListener(type, handler) {
    (this.listeners[type] ||= []).push(handler);
  }

  dispatch(type) {
    for (const handler of this.listeners[type] || []) handler({ target: this, type });
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  replaceChildren(...nodes) {
    this.children = [...nodes];
  }

  setAttribute(name, value) {
    this[name] = value;
  }

  getAttribute(name) {
    return this[name];
  }
}

class FakeDocument {
  constructor(ids) {
    this.elements = new Map(ids.map((id) => [id, new FakeElement(id)]));
  }

  getElementById(id) {
    if (!this.elements.has(id)) throw new Error(`missing fake element: ${id}`);
    return this.elements.get(id);
  }

  createElement(tag) {
    const element = new FakeElement();
    element.tagName = tag;
    return element;
  }
}

const ELEMENT_IDS = [
  'notice', 'project-id', 'load-project', 'status-output',
  'raw-prompt', 'raw-research', 'draft-note', 'structured-json',
  'lyrics-text', 'lyrics-origin', 'save-draft', 'dirty-state',
  'draft-output', 'checklist', 'revisions-list',
  'compare-from', 'compare-to', 'compare-btn', 'compare-output',
  'review-revision', 'review-hash', 'review-target', 'review-note',
  'review-confirm', 'review-btn', 'review-output',
  'context-revision', 'context-section', 'context-occurrence',
  'context-budget', 'context-btn', 'context-output',
];

function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return body; },
  };
}

function statusUrl(pid) { return `/api/projects/${pid}/production-brief`; }
function revisionUrl(pid, revision) { return `${statusUrl(pid)}/revisions/${revision}`; }
function draftUrl(pid) { return `${statusUrl(pid)}/drafts`; }
function reviewUrl(pid, revision) { return `${statusUrl(pid)}/revisions/${revision}/review`; }

function statusDto(pid, currentRevision = null, overrides = {}) {
  return {
    project_id: pid,
    current_revision: currentRevision,
    revisions: currentRevision ? [{
      revision: currentRevision,
      reviewed: false,
      review_stale: false,
      content_sha256: 'a'.repeat(64),
      note: 'saved',
    }] : [],
    review: null,
    reviewed: false,
    review_stale: false,
    capabilities: [],
    limits: {},
    generation: false,
    audio_processing: false,
    model_calls: false,
    tool_permissions: false,
    legacy_music_approval_transferred: false,
    ...overrides,
  };
}

function richRecord(overrides = {}) {
  return {
    revision: 1,
    content_sha256: 'a'.repeat(64),
    raw_prompt: 'exact prompt',
    raw_research: 'untrusted research',
    note: 'saved note',
    key: {
      value: 'C major',
      origin: 'user_assertion',
      note: 'key note',
      evidence_refs: ['ref|one', 'line\nref'],
      evidence_verified: false,
    },
    tempo_bpm: {
      value: 80,
      origin: 'measured_evidence',
      note: 'tempo note',
      evidence_refs: ['tempo ref'],
      evidence_verified: false,
    },
    meter: {
      value: '4/4',
      origin: 'documented_evidence',
      note: 'meter note',
      evidence_refs: [],
      evidence_verified: false,
    },
    intended_sections: [{
      name: 'Verse 1',
      occurrence: 1,
      origin: 'user_assertion',
      note: 'section | note',
    }],
    lyrics_text: '[Verse 1]\nExact | line\n',
    lyrics_origin: 'user_assertion',
    stanzas: [],
    hard_constraints: [{
      value: 'Keep | exact\nwords',
      origin: 'user_assertion',
      note: 'constraint note',
      evidence_refs: ['constraint ref'],
      evidence_verified: false,
    }],
    asset_references: [{
      asset_id: '0f26194c08',
      relative_path: null,
      sha256: null,
      label: 'take | one',
      origin: 'user_assertion',
      verified: false,
    }],
    capabilities: [
      { kind: 'score', reference: 'score.pdf', status: 'unverified' },
    ],
    ...overrides,
  };
}

function revisionDto(record) {
  return {
    record,
    review: null,
    reviewed: false,
    review_stale: false,
    draft_not_approved: true,
    human_review_is_self_asserted: true,
  };
}

async function workspace(fetchImpl, confirmImpl = () => false) {
  const c = await corePromise;
  const document = new FakeDocument(ELEMENT_IDS);
  const window = { location: { search: '' }, confirm: confirmImpl };
  const app = c.createBriefWorkspace({
    document,
    window,
    fetch: fetchImpl,
    confirm: confirmImpl,
    search: '',
  });
  app.start();
  return { c, app, document, window };
}

function type(document, id, text) {
  const element = document.getElementById(id);
  if (element.disabled || element.readOnly) return false;
  element.value = text;
  element.dispatch('input');
  return true;
}

function setChecked(document, id, checked) {
  const element = document.getElementById(id);
  if (element.disabled) return false;
  element.checked = checked;
  element.dispatch('change');
  return true;
}

function firstRevisionButton(document) {
  const rows = document.getElementById('revisions-list').children;
  for (const row of rows) {
    for (const child of row.children || []) {
      if (child.className === 'pb-row' && child.children && child.children.length) {
        return child.children[0];
      }
    }
  }
  return null;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

test('structured JSON round-trips notes, evidence, numeric types, pipes and newlines', async () => {
  const c = await corePromise;
  const record = richRecord();
  const structured = c.structuredFromRecord(record);
  assert.equal(structured.tempo_bpm.value, 80);
  assert.deepEqual(structured.key.evidence_refs, ['ref|one', 'line\nref']);
  assert.equal(structured.hard_constraints[0].value, 'Keep | exact\nwords');
  assert.equal(structured.asset_references[0].asset_id, '0f26194c08');
  structured.key.note = 'mutated';
  assert.equal(record.key.note, 'key note');
  const payload = c.buildDraftPayload({
    raw_prompt: record.raw_prompt,
    raw_research: record.raw_research,
    note: record.note,
    lyrics_text: record.lyrics_text,
    lyrics_origin: record.lyrics_origin,
    structured_json: JSON.stringify(structured),
  });
  assert.equal(payload.tempo_bpm.value, 80);
  assert.equal(payload.key.note, 'mutated');
  assert.deepEqual(payload.key.evidence_refs, ['ref|one', 'line\nref']);
  assert.equal(payload.hard_constraints[0].note, 'constraint note');
  assert.equal(payload.asset_references[0].asset_id, '0f26194c08');
  assert.equal(payload.capabilities[0].status, 'unverified');
});

test('structured JSON rejects malformed, non-object and unsupported fields', async () => {
  const c = await corePromise;
  assert.deepEqual(c.parseStructuredJson(''), c.emptyStructured());
  assert.throws(() => c.parseStructuredJson('{'));
  assert.throws(() => c.parseStructuredJson('[]'));
  assert.throws(() => c.parseStructuredJson('{"extra": true}'));
  assert.throws(() => c.buildDraftPayload({
    raw_prompt: 'p', structured_json: '{"intended_sections": {}}',
  }), /must be an array/);
  const payload = c.buildDraftPayload({
    raw_prompt: 'p', lyrics_text: 'line', structured_json: '',
  });
  assert.deepEqual(payload.intended_sections, []);
  assert.equal(payload.raw_prompt, 'p');
});

test('project switching commits identity only after a successful load and preserves failures', async () => {
  const A = 'aaaaaaaaaaaa';
  const B = 'bbbbbbbbbbbb';
  const C = 'cccccccccccc';
  const recordA = richRecord();
  let bRevision = null;
  let savedBody = null;
  let bFetches = 0;
  const fetchImpl = async (url, options = {}) => {
    if (url === statusUrl(A)) return response(200, statusDto(A, 1));
    if (url === revisionUrl(A, 1)) return response(200, revisionDto(recordA));
    if (url === statusUrl(B)) {
      bFetches += 1;
      return response(200, statusDto(B, bRevision));
    }
    if (url === draftUrl(B) && options.method === 'POST') {
      savedBody = JSON.parse(options.body);
      bRevision = 2;
      return response(200, {
        record: { ...recordA, revision: 2, content_sha256: 'b'.repeat(64), raw_prompt: savedBody.raw_prompt },
        review: null,
        reviewed: false,
        draft_not_approved: true,
      });
    }
    if (url === statusUrl(C)) return response(500, { detail: 'synthetic failure' });
    throw new Error(`unexpected fetch: ${url}`);
  };
  const { app, document } = await workspace(fetchImpl);

  assert.equal(await app.loadProject(A), true);
  assert.equal(app.state.pid, A);
  assert.equal(document.getElementById('raw-prompt').value, 'exact prompt');

  assert.equal(await app.loadProject(B), true);
  assert.equal(app.state.pid, B);
  assert.equal(document.getElementById('raw-prompt').value, '');
  assert.deepEqual(JSON.parse(document.getElementById('structured-json').value), (await corePromise).emptyStructured());
  assert.equal(document.getElementById('review-target').textContent.includes('No saved revision'), true);

  assert.equal(type(document, 'raw-prompt', 'B only'), true);
  assert.equal(app.isDirty(), true);
  assert.equal(await app.saveDraft(), true);
  assert.equal(savedBody.raw_prompt, 'B only');
  assert.equal(app.state.pid, B);

  assert.equal(await app.loadProject(C), false);
  assert.equal(app.state.pid, B);
  assert.equal(document.getElementById('raw-prompt').value, 'B only');
  assert.match(document.getElementById('notice').textContent, /Load failed/);
  assert.equal(bFetches >= 1, true);
});

test('page save preserves structured metadata and numeric types without a lossy round-trip', async () => {
  const A = 'aaaaaaaaaaaa';
  const recordA = richRecord();
  let savedBody = null;
  let revision = 1;
  const fetchImpl = async (url, options = {}) => {
    if (url === statusUrl(A)) return response(200, statusDto(A, revision));
    if (url === revisionUrl(A, 1)) return response(200, revisionDto(recordA));
    if (url === draftUrl(A) && options.method === 'POST') {
      savedBody = JSON.parse(options.body);
      revision = 2;
      return response(200, {
        record: {
          ...recordA,
          revision: 2,
          content_sha256: 'b'.repeat(64),
          ...savedBody,
          stanzas: recordA.stanzas,
        },
        review: null,
        reviewed: false,
        draft_not_approved: true,
      });
    }
    throw new Error(`unexpected fetch: ${url}`);
  };
  const { app } = await workspace(fetchImpl);
  await app.loadProject(A);
  assert.equal(await app.saveDraft(), true);
  assert.equal(savedBody.tempo_bpm.value, 80);
  assert.equal(savedBody.key.note, 'key note');
  assert.deepEqual(savedBody.key.evidence_refs, ['ref|one', 'line\nref']);
  assert.equal(savedBody.hard_constraints[0].note, 'constraint note');
  assert.deepEqual(savedBody.hard_constraints[0].evidence_refs, ['constraint ref']);
  assert.equal(savedBody.asset_references[0].asset_id, '0f26194c08');
  assert.equal(savedBody.capabilities[0].status, 'unverified');
  assert.equal(savedBody.raw_prompt, recordA.raw_prompt);
  assert.equal(savedBody.lyrics_text, recordA.lyrics_text);
});

test('dirty forms block switching unless confirmed; review consent resets', async () => {
  const A = 'aaaaaaaaaaaa';
  const B = 'bbbbbbbbbbbb';
  const recordA = richRecord();
  let confirmAnswer = false;
  let bFetches = 0;
  let reviewPosts = 0;
  const fetchImpl = async (url, options = {}) => {
    if (url === statusUrl(A)) return response(200, statusDto(A, 1));
    if (url === revisionUrl(A, 1)) return response(200, revisionDto(recordA));
    if (url === revisionUrl(A, 2)) {
      return response(200, revisionDto({ ...recordA, revision: 2, content_sha256: 'b'.repeat(64) }));
    }
    if (url === statusUrl(B)) { bFetches += 1; return response(200, statusDto(B)); }
    if (url === reviewUrl(A, 1) && options.method === 'POST') {
      reviewPosts += 1;
      return response(200, {
        ...revisionDto(recordA),
        reviewed: true,
        draft_not_approved: false,
        review: { revision: 1, content_sha256: 'a'.repeat(64), attestation: 'operator_self_asserted_via_ui' },
      });
    }
    throw new Error(`unexpected fetch: ${url}`);
  };
  const { app, document } = await workspace(fetchImpl, () => confirmAnswer);
  await app.loadProject(A);

  assert.equal(type(document, 'raw-prompt', 'unsaved'), true);
  assert.equal(app.isDirty(), true);
  assert.equal(document.getElementById('review-btn').disabled, true);

  assert.equal(await app.loadProject(B), false);
  assert.equal(app.state.pid, A);
  assert.equal(document.getElementById('raw-prompt').value, 'unsaved');
  assert.equal(bFetches, 0);

  confirmAnswer = true;
  assert.equal(await app.loadProject(B), true);
  assert.equal(app.state.pid, B);
  assert.equal(bFetches, 1);

  await app.loadProject(A);
  assert.equal(setChecked(document, 'review-confirm', true), true);
  document.getElementById('review-hash').dispatch('input');
  assert.equal(document.getElementById('review-confirm').checked, false);

  assert.equal(setChecked(document, 'review-confirm', true), true);
  assert.equal(await app.review(), true);
  assert.equal(reviewPosts, 1);
  assert.equal(document.getElementById('review-confirm').checked, false);
  assert.match(document.getElementById('review-output').textContent, /operator_self_asserted/);
});

test('typing is unavailable while a save is pending and conflicting actions are blocked', async () => {
  const A = 'aaaaaaaaaaaa';
  const B = 'bbbbbbbbbbbb';
  const recordA = richRecord();
  const pending = deferred();
  let savedBody = null;
  let revision = 1;
  const fetchImpl = async (url, options = {}) => {
    if (url === statusUrl(A)) return response(200, statusDto(A, revision));
    if (url === revisionUrl(A, 1)) return response(200, revisionDto(recordA));
    if (url === draftUrl(A) && options.method === 'POST') {
      savedBody = JSON.parse(options.body);
      return pending.promise;
    }
    throw new Error(`unexpected fetch: ${url}`);
  };
  const { app, document } = await workspace(fetchImpl);
  await app.loadProject(A);
  const button = firstRevisionButton(document);
  assert.equal(button.disabled, false);

  const savePromise = app.saveDraft();
  assert.equal(document.getElementById('raw-prompt').disabled, true);
  assert.equal(document.getElementById('raw-prompt').readOnly, true);
  assert.equal(document.getElementById('structured-json').readOnly, true);
  assert.equal(button.disabled, true);
  assert.equal(type(document, 'raw-prompt', 'New words typed while saving'), false);
  assert.equal(document.getElementById('raw-prompt').value, recordA.raw_prompt);

  await assert.rejects(app.loadProject(B), /write is outstanding/);
  await assert.rejects(app.loadRevision(2), /write is outstanding/);
  await assert.rejects(app.saveDraft(), /already in progress/);
  await assert.rejects(app.review(), /already in progress/);
  await assert.rejects(app.assembleContext(), /already in progress/);
  await assert.rejects(app.compare(), /already in progress/);

  revision = 2;
  pending.resolve(response(200, {
    record: {
      ...recordA,
      revision: 2,
      content_sha256: 'b'.repeat(64),
      ...savedBody,
      stanzas: recordA.stanzas,
    },
    review: null,
    reviewed: false,
    draft_not_approved: true,
  }));
  assert.equal(await savePromise, true);
  assert.equal(savedBody.raw_prompt, recordA.raw_prompt);
  assert.equal(document.getElementById('raw-prompt').value, recordA.raw_prompt);
  assert.equal(app.isDirty(), false);
  assert.equal(document.getElementById('raw-prompt').disabled, false);
  assert.equal(document.getElementById('structured-json').readOnly, false);
  assert.equal(firstRevisionButton(document).disabled, false);
});

test('typing is unavailable during project load; a failed load restores dirty content', async () => {
  const A = 'aaaaaaaaaaaa';
  const B = 'bbbbbbbbbbbb';
  const recordA = richRecord();
  const pending = deferred();
  const fetchImpl = async (url) => {
    if (url === statusUrl(A)) return response(200, statusDto(A, 1));
    if (url === revisionUrl(A, 1)) return response(200, revisionDto(recordA));
    if (url === statusUrl(B)) return pending.promise;
    throw new Error(`unexpected fetch: ${url}`);
  };
  const { app, document } = await workspace(fetchImpl, () => true);
  await app.loadProject(A);
  assert.equal(type(document, 'raw-prompt', 'dirty before load'), true);
  assert.equal(app.isDirty(), true);

  const loadPromise = app.loadProject(B);
  assert.equal(document.getElementById('raw-prompt').disabled, true);
  assert.equal(type(document, 'raw-prompt', 'typed while loading B'), false);
  assert.equal(document.getElementById('raw-prompt').value, 'dirty before load');

  pending.reject(new Error('synthetic load failure'));
  assert.equal(await loadPromise, false);
  assert.equal(app.state.pid, A);
  assert.equal(document.getElementById('raw-prompt').value, 'dirty before load');
  assert.equal(app.isDirty(), true);
  assert.equal(document.getElementById('raw-prompt').disabled, false);
  assert.equal(document.getElementById('structured-json').readOnly, false);
  assert.match(document.getElementById('notice').textContent, /Load failed/);
});

test('a failed revision load restores dirty content and unlocks controls', async () => {
  const A = 'aaaaaaaaaaaa';
  const recordA = richRecord();
  const pending = deferred();
  const fetchImpl = async (url) => {
    if (url === statusUrl(A)) return response(200, statusDto(A, 1));
    if (url === revisionUrl(A, 1)) return response(200, revisionDto(recordA));
    if (url === revisionUrl(A, 2)) return pending.promise;
    throw new Error(`unexpected fetch: ${url}`);
  };
  const { app, document } = await workspace(fetchImpl, () => true);
  await app.loadProject(A);
  assert.equal(type(document, 'raw-prompt', 'dirty before revision load'), true);

  const loadPromise = app.loadRevision(2);
  assert.equal(document.getElementById('raw-prompt').disabled, true);
  assert.equal(type(document, 'raw-prompt', 'typed while loading revision'), false);
  pending.reject(new Error('synthetic revision load failure'));
  assert.equal(await loadPromise, false);
  assert.equal(app.state.record.revision, 1);
  assert.equal(document.getElementById('raw-prompt').value, 'dirty before revision load');
  assert.equal(app.isDirty(), true);
  assert.equal(document.getElementById('raw-prompt').disabled, false);
  assert.match(document.getElementById('notice').textContent, /Revision load failed/);
});

test('typing is unavailable during revision load and success replaces the view cleanly', async () => {
  const A = 'aaaaaaaaaaaa';
  const recordA = richRecord();
  const recordB = richRecord({
    revision: 2,
    content_sha256: 'b'.repeat(64),
    raw_prompt: 'revision two prompt',
  });
  const pending = deferred();
  const fetchImpl = async (url) => {
    if (url === statusUrl(A)) return response(200, statusDto(A, 1));
    if (url === revisionUrl(A, 1)) return response(200, revisionDto(recordA));
    if (url === revisionUrl(A, 2)) return pending.promise;
    throw new Error(`unexpected fetch: ${url}`);
  };
  const { app, document } = await workspace(fetchImpl);
  await app.loadProject(A);

  const loadPromise = app.loadRevision(2);
  assert.equal(document.getElementById('raw-prompt').disabled, true);
  assert.equal(type(document, 'raw-prompt', 'typed while loading revision'), false);
  assert.equal(document.getElementById('raw-prompt').value, recordA.raw_prompt);

  pending.resolve(response(200, revisionDto(recordB)));
  assert.equal(await loadPromise, true);
  assert.equal(app.state.record.revision, 2);
  assert.equal(document.getElementById('raw-prompt').value, 'revision two prompt');
  assert.equal(app.isDirty(), false);
  assert.equal(document.getElementById('raw-prompt').disabled, false);
  assert.equal(document.getElementById('structured-json').readOnly, false);
});

test('a failed save retains dirty editor content and unlocks controls', async () => {
  const A = 'aaaaaaaaaaaa';
  const recordA = richRecord();
  const pending = deferred();
  const fetchImpl = async (url, options = {}) => {
    if (url === statusUrl(A)) return response(200, statusDto(A, 1));
    if (url === revisionUrl(A, 1)) return response(200, revisionDto(recordA));
    if (url === draftUrl(A) && options.method === 'POST') return pending.promise;
    throw new Error(`unexpected fetch: ${url}`);
  };
  const { app, document } = await workspace(fetchImpl);
  await app.loadProject(A);
  assert.equal(type(document, 'raw-prompt', 'dirty before failed save'), true);
  assert.equal(app.isDirty(), true);

  const savePromise = app.saveDraft();
  assert.equal(document.getElementById('raw-prompt').disabled, true);
  assert.equal(type(document, 'raw-prompt', 'typed while saving'), false);
  pending.reject(new Error('synthetic save failure'));
  assert.equal(await savePromise, false);
  assert.equal(document.getElementById('raw-prompt').value, 'dirty before failed save');
  assert.equal(app.isDirty(), true);
  assert.equal(document.getElementById('raw-prompt').disabled, false);
  assert.match(document.getElementById('notice').textContent, /synthetic save failure/);
});

test('a failed review clears consent and unlocks controls', async () => {
  const A = 'aaaaaaaaaaaa';
  const recordA = richRecord();
  const fetchImpl = async (url, options = {}) => {
    if (url === statusUrl(A)) return response(200, statusDto(A, 1));
    if (url === revisionUrl(A, 1)) return response(200, revisionDto(recordA));
    if (url === reviewUrl(A, 1) && options.method === 'POST') {
      return response(500, { detail: 'synthetic review failure' });
    }
    throw new Error(`unexpected fetch: ${url}`);
  };
  const { app, document } = await workspace(fetchImpl);
  await app.loadProject(A);
  assert.equal(setChecked(document, 'review-confirm', true), true);
  const reviewPromise = app.review();
  assert.equal(document.getElementById('raw-prompt').disabled, true);
  assert.equal(await reviewPromise, false);
  assert.equal(document.getElementById('review-confirm').checked, false);
  assert.equal(document.getElementById('raw-prompt').disabled, false);
  assert.match(document.getElementById('notice').textContent, /synthetic review failure/);
});

test('unsaved editor changes block reviewing the saved revision', async () => {
  const A = 'aaaaaaaaaaaa';
  const recordA = richRecord();
  const fetchImpl = async (url) => {
    if (url === statusUrl(A)) return response(200, statusDto(A, 1));
    if (url === revisionUrl(A, 1)) return response(200, revisionDto(recordA));
    throw new Error(`unexpected fetch: ${url}`);
  };
  const { app, document } = await workspace(fetchImpl);
  await app.loadProject(A);
  assert.equal(type(document, 'lyrics-text', 'edited'), true);
  assert.equal(setChecked(document, 'review-confirm', true), false);
  await assert.rejects(app.review(), /unsaved editor changes/);
});

test('out-of-order project loads cannot overwrite the newer view', async () => {
  const A = 'aaaaaaaaaaaa';
  const B = 'bbbbbbbbbbbb';
  const C = 'cccccccccccc';
  let resolveB;
  const bPending = new Promise((resolve) => { resolveB = resolve; });
  const fetchImpl = async (url) => {
    if (url === statusUrl(A)) return response(200, statusDto(A, 1));
    if (url === revisionUrl(A, 1)) return response(200, revisionDto(richRecord()));
    if (url === statusUrl(B)) return bPending;
    if (url === statusUrl(C)) return response(200, statusDto(C));
    throw new Error(`unexpected fetch: ${url}`);
  };
  const { app, document } = await workspace(fetchImpl);
  await app.loadProject(A);
  const bLoad = app.loadProject(B);
  const cLoad = app.loadProject(C);
  await cLoad;
  assert.equal(app.state.pid, C);
  assert.equal(document.getElementById('raw-prompt').value, '');
  resolveB(response(200, statusDto(B, 1)));
  await bLoad;
  assert.equal(app.state.pid, C);
  assert.equal(document.getElementById('status-output').textContent.includes('cccccccccccc'), true);
});

test('helpers retain origin and review labels', async () => {
  const c = await corePromise;
  const payload = c.buildDraftPayload({
    raw_prompt: 'p', raw_research: '', note: '', key: null, tempo_bpm: null, meter: null,
    lyrics_text: 'line', lyrics_origin: 'user_assertion', structured_json: JSON.stringify({
      intended_sections: [{ name: 'Verse 1', occurrence: 1, origin: 'unknown', note: '' }],
      hard_constraints: [],
      asset_references: [],
      capabilities: [],
    }),
  });
  const checklist = c.requiredChecklist(payload);
  assert.match(checklist.label, /not model-extracted/);
  assert.equal(checklist.items.find((item) => item.field === 'raw_prompt').ok, true);
  const origins = c.summarizeOrigins(payload);
  assert.equal(origins.unknown, 1);
  assert.equal(origins.evidence_verified, false);
  assert.equal(c.detectedSectionTags('[Verse 1]\nline\n[Chorus]')[0], 'Verse 1');
  assert.match(c.formatReviewState({ current_revision: 1, review: null }), /draft/);
});
