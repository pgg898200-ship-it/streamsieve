import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { gzipSync } from 'node:zlib';
import { EdgeChime } from '../index.js';

const output = resolve(process.argv[2] ?? 'edgechime-hfdata');
const trace = readFileSync(new URL('./data/trace.json', import.meta.url));
const traceHash = '4197da9bf36caf10235da69b073d503d2c6404c74541e5a94942225cf8c5d134';
assert.equal(createHash('sha256').update(trace).digest('hex'), traceHash);

const trips = JSON.parse(trace);
const points = trips.flat();
const seed = 270113;
let state = seed;
function randomInt(size) {
  state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
  return Math.floor(state / 4294967296 * size);
}

// Center subscriptions around a repeatable sample of positions. Every fourth
// zone anchors a lower or upper X edge at a measured longitude for tie cases.
const boxes = Array.from({ length: 256 }, (_, id) => {
  const [x, y] = points[randomInt(points.length)];
  const extent = [250, 1500, 6000][id % 3];
  const halfWidth = extent + randomInt(extent);
  const halfHeight = extent + randomInt(extent);
  const centerX = x + (id % 8 === 0 ? halfWidth : id % 8 === 4 ? -halfWidth : 0);
  return [centerX - halfWidth, y - halfHeight, centerX + halfWidth, y + halfHeight];
});

const rows = [];
let stationary = 0;
let changed = 0;
let nEnter = 0;
let nExit = 0;
for (const [trip, track] of trips.entries()) {
  for (let step = 0; step < track.length - 1; step++) {
    const [x0, y0] = track[step];
    const [x1, y1] = track[step + 1];
    const entered = [], exited = [];
    let priorCount = 0;
    for (const [id, [left, bottom, right, top]] of boxes.entries()) {
      const before = left <= x0 && x0 < right && bottom <= y0 && y0 < top;
      const after = left <= x1 && x1 < right && bottom <= y1 && y1 < top;
      if (before) priorCount++;
      if (!before && after) entered.push(id);
      if (before && !after) exited.push(id);
    }
    rows.push({ trip, step, x0, y0, x1, y1, prior_count: priorCount, entered, exited });
    if (x0 === x1 && y0 === y1) stationary++;
    if (entered.length || exited.length) changed++;
    nEnter += entered.length;
    nExit += exited.length;
  }
}

// The independent endpoint oracle above defines the labels. Check each actual
// callback below, including its direction and multiplicity, before exporting.
const engine = new EdgeChime(boxes);
for (const row of rows) {
  const entered = [], exited = [];
  engine.move(row.x0, row.y0, row.x1, row.y1, (id, inside) => {
    (inside ? entered : exited).push(id);
  });
  assert.deepEqual(entered.sort((a, b) => a - b), row.entered);
  assert.deepEqual(exited.sort((a, b) => a - b), row.exited);
  assert.equal(engine.at(row.x0, row.y0, () => {}), row.prior_count);
}

assert.equal(rows.length, 4469);
const payload = rows.map(row => JSON.stringify(row)).join('\n') + '\n';
const compressed = gzipSync(payload, { level: 9 });
const zones = '[\n' + boxes.map(box => JSON.stringify(box)).join(',\n') + '\n]\n';
const stats = {
  seed, zones: boxes.length, trips: trips.length, positions: points.length, moves: rows.length,
  stationary, changed, unchanged: rows.length - changed, entered: nEnter, exited: nExit,
  compressed_bytes: compressed.length,
  zones_sha256: createHash('sha256').update(zones).digest('hex'),
  uncompressed_label_sha256: createHash('sha256').update(payload).digest('hex'),
};
const cardTemplate = readFileSync(new URL('./dataset-card.md', import.meta.url), 'utf8');
const card = cardTemplate.replace(/\{\{(\w+)\}\}/g, (_, field) => {
  assert.ok(Object.hasOwn(stats, field), 'unknown dataset card field ' + field);
  return String(stats[field]);
});

mkdirSync(output, { recursive: true });
writeFileSync(join(output, 'zones.json'), zones);
writeFileSync(join(output, 'moves.jsonl.gz'), compressed);
writeFileSync(join(output, 'README.md'), card);
console.log(JSON.stringify(stats, null, 2));
