import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const cli = fileURLToPath(new URL('../bin/edgechime.js', import.meta.url));
function run(lines) {
  const dir = mkdtempSync(join(tmpdir(), 'edgechime-'));
  try {
    const path = join(dir, 'zones.json');
    writeFileSync(path, '[[0,0,2,2],[1,1,4,4]]');
    return spawnSync(process.execPath, [cli, path], { input: lines, encoding: 'utf8', timeout: 5000 });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test('independent agents, quiet samples, empty lines, forget and reuse', () => {
  const events = [
    { agent:'a', x:0, y:0 }, { agent:'b', x:3, y:3 },
    { agent:'b', x:2, y:2 }, { agent:'a', x:1, y:1 },
    { agent:'b', forget:true }, { agent:'b', forget:true }, { agent:'b', x:0, y:0 }
  ];
  const result = run(events.map(JSON.stringify).join('\n\n'));
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(result.stdout.trim().split('\n').map(s => JSON.parse(s)), [
    {agent:'a', entered:[0], exited:[]}, {agent:'b', entered:[1], exited:[]},
    {agent:'a', entered:[1], exited:[]}, {agent:'b', entered:[], exited:[1]},
    {agent:'b', entered:[0], exited:[]}
  ]);
});

test('invalid input includes the failing line and a nonzero exit', () => {
  const result = run('\n{"agent":"a","y":1}\n');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /input line 2: expected finite x and y/);
});
