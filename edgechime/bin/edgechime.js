#!/usr/bin/env node
import { readFile } from 'node:fs/promises';
import { once } from 'node:events';
import { createInterface } from 'node:readline';
import { EdgeChime } from '../index.js';

const help = `Usage: edgechime BOXES.json < positions.ndjson
BOXES.json contains [minX,minY,maxX,maxY] arrays in zone ID order.
Each input line is {"agent":"id","x":2,"y":3} or {"agent":"id","forget":true}.
Output has agent, entered and exited fields, only when a membership changes.
The first sample enters all matching zones. Forget emits exits and removes the agent.
`;

async function run() {
  const args = process.argv.slice(2);
  if (args.length === 1 && ['-h', '--help'].includes(args[0])) {
    process.stdout.write(help);
    return;
  }
  if (args.length !== 1) throw new Error(help.trimEnd());
  const index = new EdgeChime(JSON.parse(await readFile(args[0], 'utf8')));
  const positions = new Map();
  let lineNumber = 0;
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  try {
    for await (const line of lines) {
      ++lineNumber;
      if (!line.trim()) continue;
      const record = JSON.parse(line);
      if (!record || typeof record.agent !== 'string' || !record.agent.length) {
        throw new Error('agent must be a nonempty string');
      }
      const { agent, x, y } = record;
      const previous = positions.get(agent);
      const entered = [];
      const exited = [];
      if (record.forget === true) {
        if (x !== undefined || y !== undefined) throw new Error('forget cannot include coordinates');
        if (previous) {
          index.at(previous[0], previous[1], id => exited.push(id));
          positions.delete(agent);
        }
      } else {
        if (!Number.isFinite(x) || !Number.isFinite(y)) throw new Error('expected finite x and y');
        if (previous) {
          index.move(previous[0], previous[1], x, y, (id, inside) => {
            (inside ? entered : exited).push(id);
          });
        } else {
          index.at(x, y, id => entered.push(id));
        }
        positions.set(agent, [x, y]);
      }
      if (entered.length || exited.length) {
        const ready = process.stdout.write(JSON.stringify({ agent, entered, exited }) + '\n');
        if (!ready) await once(process.stdout, 'drain');
      }
    }
  } catch (error) {
    throw new Error(`input line ${lineNumber}: ${error.message}`);
  } finally {
    lines.close();
  }
}

run().catch(error => {
  process.stderr.write(`edgechime: ${error.message}\n`);
  process.exitCode = 1;
  process.stdin.destroy();
});
