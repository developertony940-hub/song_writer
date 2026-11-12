const { test } = require('node:test');
const assert = require('node:assert/strict');
const ready = import('../static/mastering-core.mjs');
const sha = 'a'.repeat(64);
const recipe = 'f'.repeat(64);

function clip(id, start, end, extra = {}) {
  return {
    clip_id: id,
    source_id: sha,
    label: id,
    start_s: start,
    end_s: end,
    gain_db: 0,
    fade_in_s: 0,
    fade_out_s: 0,
    confirmation: {
      confirmed: false,
      source_sha256: sha,
      start_s: start,
      end_s: end,
    },
    ...extra,
  };
}

function base() {
  return [clip('a', 0, 0.5), clip('b', 0.5, 1.0), clip('c', 1.0, 1.5)];
}

test('insert declaration names the new clip, anchor and additive delta', async () => {
  const c = await ready;
  const before = base();
  const current = [before[0], clip('x', 0.2, 0.7), before[1], before[2]];
  const declaration = c.buildDeclaration(before, current, 1, recipe);
  assert.ok(declaration);
  assert.deepEqual(declaration.final_order, ['a', 'x', 'b', 'c']);
  assert.equal(declaration.delta_s, 0.5);
  const insert = declaration.actions.find(action => action.kind === 'insert');
  assert.equal(insert.clip_id, 'x');
  assert.equal(insert.anchor_clip_id, 'b');
  assert.equal(insert.placement, 'before');
});

test('replace declaration carries the explicit length delta', async () => {
  const c = await ready;
  const before = base();
  const current = [before[0], clip('b', 0.5, 0.9), before[2]];
  const declaration = c.buildDeclaration(before, current, 1, recipe);
  assert.ok(declaration);
  assert.deepEqual(declaration.final_order, ['a', 'b', 'c']);
  assert.ok(Math.abs(declaration.delta_s + 0.1) < 1e-6);
  const replace = declaration.actions.find(action => action.kind === 'replace');
  assert.equal(replace.clip_id, 'b');
  assert.ok(Math.abs(replace.delta_s + 0.1) < 1e-6);
});

test('label-only and confirmation-invalidating edits are declared replaces', async () => {
  const c = await ready;
  const before = [clip('a', 0, 0.5), clip('b', 0.5, 1.0, { confirmation: { confirmed: true, source_sha256: sha, start_s: 0.5, end_s: 1.0 } }), clip('c', 1.0, 1.5)];
  const current = [before[0], clip('b', 0.5, 1.0, { label: 'B label' }), before[2]];
  const declaration = c.buildDeclaration(before, current, 1, recipe);
  assert.ok(declaration);
  const replace = declaration.actions.find(action => action.kind === 'replace');
  assert.equal(replace.clip_id, 'b');
  assert.equal(replace.delta_s, 0);
  assert.equal(declaration.delta_s, 0);
});

test('reorder declaration moves the clip to its declared index', async () => {
  const c = await ready;
  const before = base();
  const current = [before[1], before[0], before[2]];
  const declaration = c.buildDeclaration(before, current, 1, recipe);
  assert.ok(declaration);
  assert.deepEqual(declaration.final_order, ['b', 'a', 'c']);
  assert.equal(declaration.delta_s, 0);
  assert.ok(declaration.actions.some(action => action.kind === 'reorder' && action.clip_id === 'b' && action.to_index === 0));
});

test('remove declaration subtracts exactly the removed length', async () => {
  const c = await ready;
  const before = base();
  const current = [before[0], before[2]];
  const declaration = c.buildDeclaration(before, current, 1, recipe);
  assert.ok(declaration);
  assert.deepEqual(declaration.final_order, ['a', 'c']);
  assert.equal(declaration.delta_s, -0.5);
  const remove = declaration.actions.find(action => action.kind === 'remove');
  assert.equal(remove.clip_id, 'b');
  assert.equal(remove.delta_s, -0.5);
});

test('split declaration preserves the old source interval and net duration', async () => {
  const c = await ready;
  const before = base();
  const left = clip('bL', 0.5, 0.75, { label: 'b' });
  const right = clip('bR', 0.75, 1.0, { label: 'b' });
  const current = [before[0], left, right, before[2]];
  const declaration = c.buildDeclaration(before, current, 1, recipe);
  assert.ok(declaration);
  assert.deepEqual(declaration.final_order, ['a', 'bL', 'bR', 'c']);
  assert.equal(declaration.delta_s, 0);
  const split = declaration.actions.find(action => action.kind === 'split');
  assert.equal(split.clip_id, 'b');
  assert.equal(split.at_s, 0.75);
  assert.equal(split.left_clip_id, 'bL');
  assert.equal(split.right_clip_id, 'bR');
});

test('composite declaration combines replace, insert and reorder', async () => {
  const c = await ready;
  const before = base();
  const current = [before[2], before[0], clip('x', 0.2, 0.7), clip('b', 0.5, 0.9)];
  const declaration = c.buildDeclaration(before, current, 1, recipe);
  assert.ok(declaration);
  assert.deepEqual(declaration.final_order, ['c', 'a', 'x', 'b']);
  assert.ok(Math.abs(declaration.delta_s - 0.4) < 1e-6);
  assert.ok(declaration.actions.some(action => action.kind === 'replace' && action.clip_id === 'b'));
  assert.ok(declaration.actions.some(action => action.kind === 'insert' && action.clip_id === 'x'));
  assert.ok(declaration.actions.some(action => action.kind === 'reorder' && action.clip_id === 'c'));
});

test('a no-op yields no declaration and inputs are not mutated', async () => {
  const c = await ready;
  const before = base();
  const snapshot = structuredClone(before);
  assert.equal(c.buildDeclaration(before, structuredClone(before), 1, recipe), null);
  assert.deepEqual(before, snapshot);
});

