import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { buildTiledZones } from './zones.mjs';

const map = JSON.parse(readFileSync(new URL('./station.tmj', import.meta.url), 'utf8'));
const path = ['Station', 'Zones'];
test('uses nested pixel offsets and original object IDs at a shared edge', () => {
  const { index, objects } = buildTiledZones(map, path);
  const initial = [], events = [];
  index.at(16, 0, id => initial.push(objects[id].id));
  index.move(47, 15, 48, 15, (id, enter) => events.push([objects[id].id, enter]));
  assert.deepEqual(initial, [10]);
  assert.deepEqual(events.sort((a, b) => a[0] - b[0]), [[10, false], [20, true]]);
  assert.equal(index.at(80, 16, () => {}), 0);
});
test('rejects unsupported geometries, orientation and transforms', () => {
  for (const shape of [{ gid: 2 }, { rotation: 90 }, { point: true }, { ellipse: true },
    { capsule: true }, { polyline: [] }, { polygon: [] }, { text: {} }, { template: 'other.tx' }]) {
    const edited = structuredClone(map);
    Object.assign(edited.layers[0].layers[0].objects[0], shape);
    assert.throws(() => buildTiledZones(edited, path), /unrotated rectangle/);
  }
  assert.throws(() => buildTiledZones({ ...map, orientation: 'isometric' }, path), /orthogonal/);
  for (const transform of [{ x: 2 }, { parallaxx: 0.5 }, { offsety: '12' }]) {
    const edited = structuredClone(map);
    Object.assign(edited.layers[0], transform);
    assert.throws(() => buildTiledZones(edited, path));
  }
});
test('rejects a missing or ambiguous selection and ignores unrelated object layers', () => {
  assert.throws(() => buildTiledZones(map, ['Zones']), /Missing/);
  assert.throws(() => buildTiledZones(map, ['Station']), /objectgroup/);
  assert.throws(() => buildTiledZones({ ...map, layers: [...map.layers, ...map.layers] }, path), /ambiguous/);
  const extra = { name: 'Graphics', type: 'objectgroup', objects: [{ gid: 2 }] };
  assert.equal(buildTiledZones({ ...map, layers: [extra, ...map.layers] }, path).objects.length, 2);
});
