#!/usr/bin/env node
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { cpus } from 'node:os';
import { performance } from 'node:perf_hooks';
import { EdgeChime } from '../index.js';
import { GridBaseline } from './grid.mjs';
import Flatbush from './vendor/flatbush.js';

const config = { boxes: 8192, steps: 2048, repeats: 5, scenario: 'all', seed: 270113 };
const scenarios = ['wide-local', 'sparse-local', 'wide-teleport', 'stationary', 'porto'];
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i].slice(2);
  if (!process.argv[i].startsWith('--') || !Object.hasOwn(config, key)) throw Error('Unknown option ' + process.argv[i]);
  const value = process.argv[i + 1];
  if (key === 'scenario') {
    if (value !== 'all' && !scenarios.includes(value)) throw Error('Expected one of all, ' + scenarios.join(', '));
    config.scenario = value;
  } else {
    config[key] = Number(value);
    if (!Number.isSafeInteger(config[key]) || config[key] < 1) throw Error('Expected positive integer for --' + key);
  }
}
function rng(seed) {
  return () => {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    return seed / 4294967296;
  };
}

function syntheticTracks(name) {
  const random = rng(config.seed + 1);
  const count = Math.min(16, config.steps);
  const tracks = Array.from({ length: count }, () => [[0.2 + 0.6 * random(), 0.2 + 0.6 * random()]]);
  for (let step = 0; step < config.steps; step++) {
    const track = tracks[step % count];
    let [x, y] = track.at(-1);
    if (name === 'wide-teleport') {
      x = random();
      y = random();
    } else if (name !== 'stationary') {
      x = Math.max(0, Math.min(1, x + 0.0006 * (2 * random() - 1)));
      y = Math.max(0, Math.min(1, y + 0.0006 * (2 * random() - 1)));
    }
    track.push([x, y]);
  }
  return tracks;
}

function makeWorkload(name) {
  const measured = name === 'porto';
  const tracks = measured ? JSON.parse(readFileSync(new URL('./data/trace.json', import.meta.url))) : syntheticTracks(name);
  const starts = tracks.map(track => track[0]);
  const bounds = measured ? [Infinity, Infinity, -Infinity, -Infinity] : [0, 0, 1, 1];
  if (measured) {
    for (const track of tracks) for (const [x, y] of track) {
      bounds[0] = Math.min(bounds[0], x);
      bounds[1] = Math.min(bounds[1], y);
      bounds[2] = Math.max(bounds[2], x);
      bounds[3] = Math.max(bounds[3], y);
    }
  }
  const moves = [];
  for (let pos = 1; moves.length < config.steps; pos++) {
    const previous = moves.length;
    for (let id = 0; id < tracks.length && moves.length < config.steps; id++) {
      if (pos < tracks[id].length) moves.push([id, ...tracks[id][pos - 1], ...tracks[id][pos]]);
    }
    if (moves.length === previous) break;
  }
  const widthRange = name === 'sparse-local' ? [0.001, 0.010] : measured ? [0.02, 0.12] : [0.15, 0.55];
  const random = rng(config.seed);
  const extentX = bounds[2] - bounds[0], extentY = bounds[3] - bounds[1];
  const width = () => widthRange[0] + random() * (widthRange[1] - widthRange[0]);
  const boxes = Array.from({ length: config.boxes }, () => {
    const x = bounds[0] + extentX * random(), y = bounds[1] + extentY * random();
    const hw = 0.5 * extentX * width(), hh = 0.5 * extentY * width();
    return [x - hw, y - hh, x + hw, y + hh];
  });
  return { starts, moves, boxes, widthRange, bounds };
}

// Store the predecessor of each exclusive upper bound. Flatbush's inclusive
// intersection now gives exact half-open point membership without a JS filter.
const bits = new DataView(new ArrayBuffer(8));
function nextDown(x) {
  if (x === 0) return -Number.MIN_VALUE;
  bits.setFloat64(0, x);
  const hi = bits.getUint32(0), lo = bits.getUint32(4);
  if (x > 0) {
    bits.setUint32(4, lo - 1);
    if (lo === 0) bits.setUint32(0, hi - 1);
  } else {
    bits.setUint32(4, lo + 1);
    if (lo === 0xffffffff) bits.setUint32(0, hi + 1);
  }
  return bits.getFloat64(0);
}

function packedTree(boxes) {
  const tree = new Flatbush(boxes.length);
  for (const [x, y, xx, yy] of boxes) tree.add(x, y, nextDown(xx), nextDown(yy));
  tree.finish();
  return tree;
}

// Cache each agent's previous hit list and N/8 bytes of membership bits.
// A shared stamp table identifies exits in O(oldHits + newHits), without sets,
// sorting, a second point query, or clearing the whole table between moves.
function pointDiff(tree, starts) {
  const marks = new Uint32Array(tree.numItems);
  const agents = starts.map(([x, y]) => {
    const hits = tree.search(x, y, x, y);
    const membership = new Uint8Array(Math.ceil(tree.numItems / 8));
    for (const id of hits) membership[id >>> 3] |= 1 << (id & 7);
    return { hits, membership };
  });
  let generation = 0;
  return (actor, x0, y0, x, y, emit) => {
    if (x0 === x && y0 === y) return;
    if (++generation > 0xffffffff) { marks.fill(0); generation = 1; }
    const agent = agents[actor], membership = agent.membership;
    const hits = tree.search(x, y, x, y);
    for (const id of hits) {
      marks[id] = generation;
      const byte = id >>> 3, mask = 1 << (id & 7);
      if (!(membership[byte] & mask)) {
        membership[byte] |= mask;
        emit(id, true);
      }
    }
    for (const id of agent.hits) {
      if (marks[id] !== generation) {
        membership[id >>> 3] &= ~(1 << (id & 7));
        emit(id, false);
      }
    }
    agent.hits = hits;
  };
}

function scanEvents(boxes, move) {
  const [, x0, y0, x, y] = move;
  const events = [];
  for (let id = 0; id < boxes.length; id++) {
    const [a, b, c, d] = boxes[id];
    const old = a <= x0 && x0 < c && b <= y0 && y0 < d;
    const now = a <= x && x < c && b <= y && y < d;
    if (old !== now) events.push(id * 2 + Number(now));
  }
  return events;
}
function validate(methods, w) {
  const runners = methods.map(method => method.runner());
  const directEvery = Math.max(1, Math.floor(w.moves.length / 16));
  let total = 0;
  for (let i = 0; i < w.moves.length; i++) {
    const move = w.moves[i];
    const results = runners.map(run => {
      const events = [];
      run(...move, (id, entered) => events.push(id * 2 + Number(entered)));
      return events.sort((a, b) => a - b);
    });
    for (let j = 1; j < results.length; j++) {
      assert.deepEqual(results[j], results[0], 'Event IDs or directions differ on move ' + i);
    }
    if (i % directEvery === 0 || i === w.moves.length - 1) assert.deepEqual(results[0], scanEvents(w.boxes, move));
    total += results[0].length;
  }
  return total;
}
function quantile(values, q) {
  const sorted = values.slice().sort((a, b) => a - b);
  return sorted[Math.ceil(q * sorted.length) - 1];
}
const blockSize = 64;
function measure(method, moves, blocks) {
  const before = performance.now(), run = method.runner();
  const setupMs = performance.now() - before;
  let events = 0, sum = 0, elapsed = 0;
  const emit = (id, entered) => { events++; sum = (sum + (entered ? id + 1 : -id - 1)) | 0; };
  for (let start = 0; start < moves.length; start += blockSize) {
    const end = Math.min(start + blockSize, moves.length);
    const t0 = performance.now();
    for (let i = start; i < end; i++) {
      const m = moves[i];
      run(m[0], m[1], m[2], m[3], m[4], emit);
    }
    const duration = performance.now() - t0;
    elapsed += duration;
    if (end - start === blockSize) blocks.push(1000 * duration / blockSize);
  }
  return { events, sum, setupMs, meanUs: elapsed * 1000 / moves.length };
}

function benchmark(name) {
  const w = makeWorkload(name);
  const methods = [];
  for (const [label, build] of [['edgechime', boxes => new EdgeChime(boxes)],
    ['flatbush', packedTree], ['perimeterGrid', boxes => new GridBaseline(boxes)]]) {
    const t0 = performance.now();
    const index = build(w.boxes);
    const buildMs = performance.now() - t0;
    const runner = label === 'flatbush' ? () => pointDiff(index, w.starts) :
      () => (_actor, x, y, xx, yy, emit) => index.move(x, y, xx, yy, emit);
    methods.push({ label, buildMs, runner, index, samples: [], blocks: [] });
  }
  const tree = methods.find(m => m.label === 'flatbush').index;
  const meanStartMembers = w.starts.reduce((sum, [x, y]) => sum + tree.search(x, y, x, y).length, 0) / w.starts.length;
  const verifiedEvents = validate(methods, w);
  for (const method of methods) measure(method, w.moves, []);
  for (let rep = 0; rep < config.repeats; rep++) {
    for (let offset = 0; offset < methods.length; offset++) {
      const method = methods[(rep + offset) % methods.length];
      const sample = measure(method, w.moves, method.blocks);
      assert.equal(sample.events, verifiedEvents);
      if (methods[0].samples.length) assert.equal(sample.sum, methods[0].samples[0].sum);
      method.samples.push(sample);
    }
  }
  const timings = {};
  for (const method of methods) {
    const samples = method.samples.map(s => s.meanUs);
    const g = method.index;
    timings[method.label] = {
      buildMs: method.buildMs, runnerSetupMs: method.samples.map(s => s.setupMs),
      meanUsPerUpdateSamples: samples, medianMeanUsPerUpdate: quantile(samples, 0.5),
      p95BlockMeanUsPerUpdate: method.blocks.length ? quantile(method.blocks, 0.95) : null,
      ...(method.label === 'perimeterGrid' ? { gridSide: g.side, gridPostings: g.postingCount } : {})
    };
  }
  return { name, updates: w.moves.length, agents: w.starts.length, events: verifiedEvents, meanStartMembers,
    bounds: w.bounds, boxDimensionsAsFractionOfBounds: w.widthRange, timings };
}

const report = { formatVersion: 1, timestamp: new Date().toISOString(), node: process.version, cpu: cpus()[0]?.model,
  platform: process.platform, arch: process.arch, config, blockSize, flatbushVersion: '4.7.0',
  scenarios: (config.scenario === 'all' ? scenarios : [config.scenario]).map(benchmark) };
console.log(JSON.stringify(report, null, 2));
