# Tiled map zone notifications

Index a named object layer once, then pass the previous and current world-pixel positions of each player, NPC, or vehicle to `move`. The example requires an orthogonal Tiled JSON export (`.tmj`) with static, unrotated rectangular trigger zones. It uses the tracked object's point anchor, such as its feet.

```sh
node examples/tiled/demo.mjs
node --test examples/tiled/zones.test.mjs
```

The demo starts on Platform (Tiled ID 10). At x=48 the player leaves Platform and enters Concourse (ID 20). Callbacks for one move may arrive in either order.

```js
import { buildTiledZones } from './zones.mjs';

const { index, objects } = buildTiledZones(map, ['Station', 'Zones']);
index.at(player.x, player.y, id => showInitialZone(objects[id]));
index.move(player.x, player.y, next.x, next.y, (id, entered) => {
  notifyPlayer(objects[id].id, entered);
});
```

Select a layer with an array of names, from the outermost group to the object layer. Names must identify a single layer at each level. The adapter adds each group and layer's pixel offsets. Object and layer visibility affect rendering only. This permits an invisible trigger layer. The adapter rejects parallax, nonzero layer tile offsets, template instances and other shapes in the selected layer. Rebuild the index after map edits. [Tiled JSON object and layer format](https://doc.mapeditor.org/en/stable/reference/json-map-format/).

This adapter opts into EdgeChime’s half-open bounds. Left and top edges belong to a rectangle. Right and bottom edges belong to the neighboring rectangle if it starts there. A jump across an entire zone with both endpoints outside emits no event.

For comparison, WorkAdventure’s own area API distinguishes initial subscriptions from movement. Its implementation at the revision below includes both rectangle limits, shifts the player’s y-coordinate by 16, dispatches leaves before entries and provides all current areas alongside the changes. A WorkAdventure integration would need to preserve these behaviors. [Area API](https://docs.workadventu.re/developer/map-scripting/references/api-mapeditor/) · [Area dispatch](https://github.com/workadventure/workadventure/blob/621f2bf29b36e1aa3aacb2e4a64299356f2cf412/libs/map-editor/src/GameMap/GameMapAreas.ts) · [Rectangle predicate](https://github.com/workadventure/workadventure/blob/621f2bf29b36e1aa3aacb2e4a64299356f2cf412/libs/math-utils/src/MathUtils.ts).
