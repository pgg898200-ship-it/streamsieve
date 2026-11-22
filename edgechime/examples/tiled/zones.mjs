import { EdgeChime } from '../../index.js';

/** Index static rectangles from one named object layer in an orthogonal Tiled map. */
export function buildTiledZones(map, path) {
  if (map.orientation !== 'orthogonal') throw Error('Expected an orthogonal map');
  if (!Array.isArray(path) || !path.length || path.some(name => typeof name !== 'string' || !name.length)) {
    throw Error('Specify a nonempty path of layer names');
  }
  let offsetX = 0, offsetY = 0;
  let layers = map.layers, layer;
  for (let i = 0; i < path.length; i++) {
    const matches = layers.filter(candidate => candidate.name === path[i]);
    if (matches.length !== 1) throw Error('Missing or ambiguous layer: ' + path.slice(0, i + 1).join(' / '));
    layer = matches[0];
    const expected = i === path.length - 1 ? 'objectgroup' : 'group';
    if (layer.type !== expected) throw Error('Expected layer of type ' + expected + ': ' + layer.name);
    if ((layer.x ?? 0) !== 0 || (layer.y ?? 0) !== 0) throw Error('Unsupported nonzero layer x/y: ' + layer.name);
    if ((layer.parallaxx ?? 1) !== 1 || (layer.parallaxy ?? 1) !== 1) throw Error('Parallax layer: ' + layer.name);
    const dx = layer.offsetx ?? 0, dy = layer.offsety ?? 0;
    if (!Number.isFinite(dx) || !Number.isFinite(dy)) throw Error('Invalid layer offset: ' + layer.name);
    offsetX += dx;
    offsetY += dy;
    layers = layer.layers;
  }
  if (!Array.isArray(layer.objects)) throw Error('Expected an objects array on ' + layer.name);
  const objects = layer.objects.slice(); // Keep the ID mapping stable if the map's array is reordered.
  const ids = new Set();
  const boxes = objects.map(object => {
    if (!Number.isSafeInteger(object.id) || object.id <= 0 || ids.has(object.id)) throw Error('Invalid or duplicate Tiled object ID: ' + object.id);
    ids.add(object.id);
    if ((object.rotation ?? 0) !== 0 || object.ellipse || object.capsule || object.point ||
        ['gid', 'text', 'polygon', 'polyline', 'template'].some(key => key in object)) {
      throw Error('Expected an unrotated rectangle for Tiled object ' + object.id);
    }
    if (![object.x, object.y, object.width, object.height].every(Number.isFinite) ||
        object.width <= 0 || object.height <= 0) throw Error('Invalid rectangle for Tiled object ' + object.id);
    const x = offsetX + object.x, y = offsetY + object.y;
    return [x, y, x + object.width, y + object.height];
  });
  return { index: new EdgeChime(boxes), objects };
}
