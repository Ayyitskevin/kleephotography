const { test } = require('node:test');
const assert = require('node:assert/strict');
const { layout, number } = require('../../static/studio-map.js');

const row = (id, extra = {}) => ({ id, stage: 'proposal_sent', age: 10, quiet: 3, value: 100000, ...extra });

test('shared value scale aligns equal values in different stages', () => {
  const r = layout([row('1'), row('2', { stage: 'contract_signed' }), row('3', { value: 200000 })], 'quiet', 30);
  const a = r.points.find(p => p.items[0].row.id === '1');
  const b = r.points.find(p => p.items[0].row.id === '2');
  assert.equal(a.x, b.x);
  assert.equal(r.maxValue, 200000);
});

test('window boundary is inclusive; missing/future ages stay outside instead of becoming fresh', () => {
  const r = layout([row('1', { quiet: 30 }), row('2', { quiet: 31 }), row('3', { quiet: '' }), row('4', { quiet: -1 })], 'quiet', 30);
  assert.equal(r.count, 1);
  assert.equal(r.points[0].items[0].age, 30);
  assert.deepEqual(r.excluded.map(p => p.row.id), ['2', '3', '4']);
  assert.equal(number(''), null);
  assert.equal(number('NaN'), null);
});

test('clock changes replot and change membership without changing the value domain', () => {
  const rows = [row('1', { age: 60, quiet: 1 }), row('2', { value: 400000, age: 3, quiet: 45 })];
  const quiet = layout(rows, 'quiet', 30);
  const stage = layout(rows, 'age', 30);
  assert.equal(quiet.points[0].items[0].row.id, '1');
  assert.equal(stage.points[0].items[0].row.id, '2');
  assert.equal(quiet.maxValue, stage.maxValue);
});

test('all-ages domain includes old projects and stays finite when empty', () => {
  assert.equal(layout([row('1', { quiet: 500 })], 'quiet', null).maxAge, 500);
  const empty = layout([], 'quiet', null);
  assert.equal(empty.maxAge, 1);
  assert.equal(empty.maxValue, 1);
  assert.deepEqual(empty.points, []);
});

test('nearby projects cluster deterministically without losing any project', () => {
  const rows = [row('3'), row('1'), row('2')];
  const a = layout(rows, 'quiet', 30);
  const b = layout([...rows].reverse(), 'quiet', 30);
  assert.deepEqual(a, b);
  assert.equal(a.points.length, 1);
  assert.deepEqual(a.points[0].items.map(p => p.row.id), ['1', '2', '3']);
});

test('nonpositive values occupy a separate gutter, away from priced projects', () => {
  const r = layout([row('1', { value: 0 }), row('2', { value: '' }), row('3', { value: 1 }), row('4', { value: 200000 })], 'quiet', 30);
  const unknown = r.points.find(p => p.x === 14);
  assert.equal(unknown.items.length, 2);
  const priced = r.points.filter(p => p.x !== 14);
  assert.equal(priced.reduce((n, p) => n + p.items.length, 0), 2);
  assert.ok(priced.every(p => Number.isFinite(p.x) && p.x >= 42 && p.x <= 186));
});
