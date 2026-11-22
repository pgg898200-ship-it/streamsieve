import assert from 'node:assert/strict';
import test from 'node:test';
import { EdgeChime } from '../index.js';

// Direct per-box oracle, independent of the index and traversal order.
const contains = ([xMin, yMin, xMax, yMax], x, y) =>
  xMin <= x && x < xMax && yMin <= y && y < yMax;

function inspectPoint(index, boxes, x, y) {
  const ids = [];
  const count = index.at(x, y, id => ids.push(id));
  const expected = boxes.flatMap((box, id) => contains(box, x, y) ? [id] : []);
  assert.equal(count, expected.length);
  assert.deepEqual(ids.sort((a, b) => a - b), expected);
}

function inspectMove(index, boxes, x0, y0, x1, y1) {
  const events = [];
  const count = index.move(x0, y0, x1, y1, (id, entered) => events.push([id, entered]));
  const expected = [];
  boxes.forEach((box, id) => {
    const wasInside = contains(box, x0, y0);
    const isInside = contains(box, x1, y1);
    if (wasInside !== isInside) expected.push([id, isInside]);
  });
  assert.equal(count, expected.length);
  assert.deepEqual(events.sort((a, b) => a[0] - b[0]), expected);
}

test('empty index, stationary points, and jumps over both boundaries', () => {
  for (const boxes of [[], [[0, 0, 1, 1]]]) {
    const index = new EdgeChime(boxes);
    inspectPoint(index, boxes, 0, 0);
    inspectMove(index, boxes, 0, 0, 0, 0);
    inspectMove(index, boxes, -100, 0.5, 100, 0.5);
    inspectMove(index, boxes, 0.5, 100, 0.5, -100);
    inspectMove(index, boxes, -1, -1, 4, 4);
  }
});

test('corners, shared edges, duplicate subscriptions, and half-open membership', () => {
  const boxes = [
    [0, 0, 2, 2], [0, 0, 2, 2], [2, 0, 4, 2],
    [0, 2, 2, 4], [2, 2, 4, 4], [-1, -1, 3, 3],
  ];
  const index = new EdgeChime(boxes);
  const points = [[-1, -1], [-0, 0], [0, 1], [1, 0], [1, 1],
    [2, 0], [2, 1], [0, 2], [1, 2], [2, 2], [3, 3], [4, 4]];
  for (const [x0, y0] of points) {
    inspectPoint(index, boxes, x0, y0);
    for (const [x1, y1] of points) inspectMove(index, boxes, x0, y0, x1, y1);
  }
});

test('finite floating-point bounds retain their original precision', () => {
  const epsilon = Number.EPSILON;
  const huge = Number.MAX_VALUE;
  const tiny = Number.MIN_VALUE;
  const boxes = [
    [1, -1, 1 + epsilon, 1], [1 + epsilon, -1, 1 + 2 * epsilon, 1],
    [-huge, -huge, huge, huge], [-tiny, -tiny, tiny, tiny],
  ];
  const index = new EdgeChime(boxes);
  const points = [[1, 0], [1 + epsilon, 0], [1 + 2 * epsilon, 0],
    [-huge, -huge], [huge, huge], [-tiny, -tiny], [-0, 0], [tiny, 0]];
  for (const from of points) {
    inspectPoint(index, boxes, ...from);
    for (const to of points) inspectMove(index, boxes, ...from, ...to);
  }
});

test('rejects invalid boxes and non-finite query coordinates', () => {
  for (const box of [[1, 0, 1, 1], [0, 1, 1, 1], [2, 0, 1, 1],
    [0, 2, 1, 1], [0, 0, Infinity, 1], [-Infinity, 0, 1, 1],
    [0, NaN, 1, 1], [0, 0, 1], ['0', 0, 1, 1]]) {
    assert.throws(() => new EdgeChime([box]));
  }
  const index = new EdgeChime([[0, 0, 1, 1]]);
  for (const invalid of [NaN, Infinity, -Infinity]) {
    for (const [method, length] of [['at', 2], ['move', 4]]) {
      for (let i = 0; i < length; i++) {
        const coords = Array(length).fill(0);
        coords[i] = invalid;
        assert.throws(() => index[method](...coords, () => {}));
      }
    }
  }
});

test('the constructed index is independent of later input edits', () => {
  const input = [[0, 0, 2, 2], [3, 3, 8, 8]];
  const original = input.map(box => [...box]);
  const index = new EdgeChime(input);
  input[0][2] = 0;
  input.reverse();
  input.length = 0;
  inspectPoint(index, original, 1, 1);
  inspectMove(index, original, 1, 1, 4, 4);
});

test('callback queries on the same index cannot interfere with outer traversal', () => {
  const boxes = Array.from({length: 180}, (_, i) => [i % 13, i % 5, 16 + i % 17, 9 + i % 11]);
  const index = new EdgeChime(boxes);
  const outer = [];
  const count = index.move(-1, -1, 13, 7, (id, entered) => {
    outer.push([id, entered]);
    if (outer.length === 1 || outer.length === 70) {
      inspectMove(index, boxes, 13, 7, 30, 15);
      inspectPoint(index, boxes, 15, 10);
    }
  });
  assert.equal(count, boxes.length);
  assert.deepEqual(outer.sort((a, b) => a[0] - b[0]), boxes.map((_, id) => [id, true]));
});

test('random updates agree exactly with scanning every rectangle', () => {
  let state = 0x64ac82f1;
  function draw(limit) {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return Math.floor((state / 0x100000000) * limit);
  }
  const coord = () => draw(97) - 48;
  for (const n of [1, 7, 31, 64, 129, 513]) {
    const boxes = Array.from({length: n}, () => {
      const x = coord(), y = coord();
      return [x, y, x + draw(40) + 1, y + draw(40) + 1];
    });
    const index = new EdgeChime(boxes);
    for (let i = 0; i < 200; i++) {
      const [l, b, r, t] = boxes[draw(n)];
      const x = i % 3 === 0 ? l : coord();
      const y = i % 3 === 0 ? b : coord();
      const u = i % 4 === 0 ? r : i % 4 === 1 ? x : coord();
      const v = i % 4 === 0 ? t : i % 4 === 2 ? y : coord();
      inspectPoint(index, boxes, x, y);
      inspectMove(index, boxes, x, y, u, v);
      inspectMove(index, boxes, u, v, x, y);
    }
  }
});
