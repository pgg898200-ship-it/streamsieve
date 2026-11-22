import { EdgeChime } from '../index.js';
import { cpus } from 'node:os';
import { performance } from 'node:perf_hooks';

// Interleaved updates to independent agents. Points and fences use a unit square.
// This measures the in-process move API. Initial memberships and NDJSON I/O are excluded.
const agents = Number(process.env.AGENTS || 10_000);
const fences = Number(process.env.FENCES || 100_000);
const rounds = Number(process.env.ROUNDS || 10);
if (![agents, fences, rounds].every(v => Number.isSafeInteger(v) && v > 0)) throw Error('expected positive integers');
let seed = 0xa7fd1b01;
function random() {
  seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
  return seed / 4294967296;
}
const rects = Array.from({length:fences}, () => {
  const x = random(), y = random();
  const w = 0.02 + random() * 0.28, h = 0.02 + random() * 0.28;
  return [x - w/2, y - h/2, x + w/2, y + h/2];
});
const start = new Float64Array(agents * 2);
const velocity = new Float64Array(start.length);
for (let i = 0; i < start.length; i++) {
  start[i] = random();
  velocity[i] = (random() - 0.5) * 0.0001;
}
const buildStart = performance.now();
const index = new EdgeChime(rects);
const buildMs = performance.now() - buildStart;
const sample = () => {
  const positions = start.slice();
  let edges = 0, checksum = 0;
  const report = (id, entered) => { edges++; checksum = (checksum + (entered ? id + 1 : -id - 1)) | 0; };
  const begin = performance.now();
  for (let t = 0; t < rounds; ++t) {
    for (let i = 0; i < positions.length; i += 2) {
      const x = positions[i], y = positions[i+1];
      const nx = x + velocity[i], ny = y + velocity[i+1];
      index.move(x, y, nx, ny, report);
      positions[i] = nx; positions[i+1] = ny;
    }
  }
  return { ms:performance.now() - begin, edges, checksum };
};
sample();
const samples = Array.from({length:5}, sample);
if (!samples.every(s => s.edges === samples[0].edges && s.checksum === samples[0].checksum)) {
  throw Error('replay mismatch');
}
const medianMs = samples.map(s => s.ms).sort((a,b)=>a-b)[2];
console.log(JSON.stringify({
  node:process.version, cpu:cpus()[0]?.model, fences, agents, rounds,
  updates:agents * rounds, coordinateBytes:start.byteLength, buildMs,
  samples, medianUpdatesPerSecond:agents * rounds * 1000 / medianMs
}, null, 2));
