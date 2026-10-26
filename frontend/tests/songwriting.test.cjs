// Offline source-level UI tests: native Node, no server, browser, or generation.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');

function fixture() {
  const nodes = new Map();
  function node(attrs = {}, ...children) {
    return Object.assign({ value: '', checked: false, disabled: false, children,
      append(...items) { this.children.push(...items); },
      addEventListener(event, handler) { this[event] = handler; },
      set innerHTML(value) { this.children = []; },
      get innerHTML() { return ''; },
    }, attrs);
  }
  const document = { getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, node());
    return nodes.get(id);
  } };
  const calls = [];
  const context = vm.createContext({ document,
    el: (tag, attrs, ...children) => node({ tag, ...attrs }, ...children),
    disabledWhile: (button, callback) => callback,
    currentProject: { id: 'offline-project' },
    api: async (...args) => { calls.push(args); return {}; },
    toast() {}, showProgress() {},
    musicRepaintCurrentAsset: () => null,
    musicRepaintBoundaryFor: () => null,
  });
  vm.runInContext(source.slice(source.indexOf('let musicArrangementState ='),
    source.indexOf('let musicRepaintSelected =')), context);
  vm.runInContext(source.slice(source.indexOf('document.getElementById("music-approve-btn").addEventListener'),
    source.indexOf('document.getElementById("music-generate-btn").addEventListener')), context);
  const get = id => document.getElementById(id);
  const run = code => vm.runInContext(code, context);
  return { get, run, calls, context };
}

test('CRLF tags retain exact numbered/final identities and repeated counts', () => {
  const f = fixture();
  f.get('music-lyrics').value = '[Verse 1]\r\nOne\r\n[Verse 2]\r\nTwo\r\n[Chorus 2]\r\n[Final Chorus]\r\n[Verse 1]\r\n';
  assert.deepEqual(Array.from(f.run('musicLyricSections()'), entry => Array.from(entry)),
    [['Verse 1', 2], ['Verse 2', 1], ['Chorus 2', 1], ['Final Chorus', 1]]);
});

test('arrangement separators, numeric colon timing, exact keys, and duplicates', () => {
  const f = fixture();
  f.context.input = 'Verse 1\tquiet\r\nVerse 2|build\nChorus 2  full\nFinal Chorus: 1:00-1:20 lift\nVerse 1: soft\nverse 1: distinct\nconstructor: safe';
  const result = JSON.parse(f.run('JSON.stringify(arrangementDictFromText(input))'));
  assert.equal(result.dict['Verse 1'], 'quiet; soft');
  assert.equal(result.dict['Verse 2'], 'build');
  assert.equal(result.dict['Chorus 2'], 'full');
  assert.equal(result.dict['Final Chorus'], '1:00-1:20 lift');
  assert.equal(result.dict['verse 1'], 'distinct');
  assert.equal(result.dict.constructor, 'safe');
  assert.equal(result.problems.length, 1);
});

test('timestamped plan lines keep section names separate from timing', () => {
  const f = fixture();
  f.context.input = [
    'Intro 0:00–0:12 Instrumental only; relaxed acoustic two-step.',
    'Verse 1 0:12–0:58 Warm intimate male vocal; acoustic guitar.',
    'Chorus 2 2:23–3:02 Full country groove; richer harmonies.',
    'Final Chorus 4:00–5:00 Biggest uplifting section; full band.',
    'Outro 5:00–5:08 Instrumental only; lingering steel.',
  ].join('\n');
  const result = JSON.parse(f.run('JSON.stringify(arrangementDictFromText(input))'));
  assert.equal(result.dict.Intro, '0:00–0:12 Instrumental only; relaxed acoustic two-step.');
  assert.equal(result.dict['Verse 1'], '0:12–0:58 Warm intimate male vocal; acoustic guitar.');
  assert.equal(result.dict['Chorus 2'], '2:23–3:02 Full country groove; richer harmonies.');
  assert.equal(result.dict['Final Chorus'], '4:00–5:00 Biggest uplifting section; full band.');
  assert.equal(result.dict.Outro, '5:00–5:08 Instrumental only; lingering steel.');
  assert.equal(result.problems.length, 0);
});

test('save and named revision refuse malformed or empty plan without writes or text loss', async () => {

  for (const button of ['music-save-btn', 'music-revision-btn']) {
    for (const input of ['malformed line', 'Verse 1\t']) {
      const f = fixture();
      f.get('music-arrangement-text').value = input;
      await assert.rejects(f.get(button).click(), /Draft not saved/);
      assert.equal(f.calls.length, 0);
      assert.equal(f.get('music-arrangement-text').value, input);
    }
  }
});

test('approval refuses malformed or empty plan before any PUT/POST and preserves text', async () => {
  for (const input of ['malformed line', 'Verse 1\t']) {
    const f = fixture();
    f.get('music-approval-note').value = 'Reviewed the draft';
    f.get('music-cap').value = '1';
    f.get('music-arrangement-text').value = input;
    f.get('music-lyrics').value = '[Verse 1]\nKeep these words';
    await assert.rejects(f.get('music-approve-btn').click(), /Draft not saved/);
    assert.equal(f.calls.length, 0);
    assert.equal(f.get('music-arrangement-text').value, input);
    assert.equal(f.get('music-lyrics').value, '[Verse 1]\nKeep these words');
  }
});

test('duplicate exact-name merge stays informational through approval', async () => {
  const f = fixture();
  f.get('music-approval-note').value = 'Reviewed the draft';
  f.get('music-cap').value = '1';
  f.get('music-arrangement-text').value = 'Verse 1\tquiet\nVerse 1\tsoft';
  f.context.musicState = null;
  f.context.api = async (...args) => { f.calls.push(args); return { revision_created: false }; };
  f.run('refreshMusic = async () => {}');
  await f.get('music-approve-btn').click();
  assert.equal(f.calls.length, 2);
  assert.equal(f.calls[0][0], '/music/offline-project/draft');
  assert.equal(f.calls[0][1].method, 'PUT');
  assert.equal(JSON.parse(f.calls[0][1].body).settings.arrangement['Verse 1'], 'quiet; soft');
  assert.equal(f.calls[1][0], '/music/offline-project/approve');
  assert.equal(f.calls[1][1].method, 'POST');
  assert.deepEqual(JSON.parse(f.calls[1][1].body), {
    project_id: 'offline-project', note: 'Reviewed the draft', submission_cap: 1,
  });
});

test('approval reports success even when generation is separately blocked', async () => {
  const f = fixture();
  const toasts = [];
  f.context.toast = (...args) => toasts.push(args);
  f.get('music-approval-note').value = 'Reviewed the draft';
  f.get('music-cap').value = '1';
  f.get('music-lyrics').value = '[Verse 1]\nKeep these words';
  f.get('music-arrangement-text').value = 'Verse 1\tquiet';
  f.context.musicState = { can_generate: false, blocked_reason: 'Live music generation is disabled' };
  f.context.api = async (...args) => { f.calls.push(args); return { revision_created: false }; };
  f.run('refreshMusic = async () => {}');
  await f.get('music-approve-btn').click();
  assert.equal(toasts.length, 1);
  assert.match(toasts[0][0], /Approved the exact on-screen lyrics\/settings/);
  assert.equal(toasts[0][1], 'ok');
  assert.doesNotMatch(toasts[0][0], /Generate is blocked/);
});

test('fill/form roundtrip keeps settings and exact section names; save payload carries them', async () => {

  const f = fixture();
  const settings = { musical_direction: 'country', lyrics: '[Verse 1]\nOne\n[Verse 2]\nTwo',
    duration_s: 120, language: 'en', bpm: 112, time_signature: '4', key_scale: 'G major',
    generate_audio_codes: true, seed: 17,
    arrangement: { 'Verse 1': 'quiet', 'Verse 2': 'build', 'Chorus 2': 'full', 'Final Chorus': 'lift' },
    temperature: null, top_p: 0.9, cfg_scale: 3 };
  f.context.settings = settings;
  f.run('fillMusicForm(settings)');
  assert.deepEqual(JSON.parse(f.run('JSON.stringify(musicFormSettings())')), settings);
  // Stub status refresh only; this tests the real save handler, not a runtime.
  f.run('refreshMusic = async () => {}');
  f.context.api = async (...args) => { f.calls.push(args); return { revision_created: true, draft: { version: 2 } }; };
  await f.get('music-save-btn').click();
  assert.equal(f.calls[0][0], '/music/offline-project/draft');
  assert.deepEqual(JSON.parse(f.calls[0][1].body).settings, settings);
});

test('row removal refreshes preview and handles a newly entered guidance row', () => {
  const f = fixture();
  f.get('music-lyrics').value = '[Verse 1]\nOne\n[Verse 2]\nTwo';
  f.get('music-arrangement-text').value = 'Verse 2\tbuild';
  f.run('renderMusicArrangement()');
  const row = f.get('music-arrangement-rows').children[0];
  const input = row.children.find(child => child.tag === 'input');
  input.value = 'quiet';
  input.oninput();
  const remove = row.children.find(child => child.tag === 'button' && child.children[0] === 'Remove');
  remove.onclick();
  assert.equal(f.get('music-arrangement-text').value, 'Verse 2\tbuild');
  assert.equal(f.run('Object.hasOwn(musicArrangementState, "Verse 1")'), false);
  assert.match(f.get('music-arrangement-preview').textContent, /Verse 2: build/);
  assert.doesNotMatch(f.get('music-arrangement-preview').textContent, /Verse 1: quiet/);
});

test('compare selections survive refresh and render saved settings/lyrics differences', () => {
  const f = fixture();
  f.run('musicRevisions = [{version: 1, name: "first"}, {version: 2, name: "second"}, {version: 3}]; fillMusicCompareSelects()');
  f.get('music-compare-from').value = '2';
  f.get('music-compare-to').value = '3';
  f.run('fillMusicCompareSelects()');
  assert.equal(f.get('music-compare-from').value, '2');
  assert.equal(f.get('music-compare-to').value, '3');
  f.run('renderMusicCompare({from: {version: 2}, to: {version: 3}, settings_changes: [{field: "arrangement", from: {}, to: {"Final Chorus": "lift"}}], lyrics_diff: [{op: "insert", to_lines: ["new lyric"]}]})');
  const text = JSON.stringify(f.get('music-compare-out').children);
  assert.match(text, /Final Chorus/);
  assert.match(text, /new lyric/);
});

test('restore targets the historical version and then refreshes without generation', async () => {
  const f = fixture();
  let refreshed = 0;
  f.context.api = async (...args) => { f.calls.push(args); return { revision_created: true, draft: { version: 4 } }; };
  f.context.markRefresh = () => { refreshed++; };
  f.run('refreshMusic = async () => markRefresh()');
  await f.run('restoreMusicRevision(2, {disabled: false})');
  assert.deepEqual(JSON.parse(JSON.stringify(f.calls)), [['/music/offline-project/revisions/2/restore', {method: 'POST'}]]);
  assert.equal(refreshed, 1);
});

test('guidance makes no exact duration or model-adherence promise', () => {
  const f = fixture();
  f.run('updateMusicArrangementPreview()');
  assert.match(f.get('music-arrangement-info').textContent, /not guaranteed/);
  assert.doesNotMatch(f.get('music-arrangement-info').textContent, /only the global Duration is exact/);
});
