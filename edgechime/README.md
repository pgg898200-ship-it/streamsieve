# EdgeChime

Emit the zone IDs that a moving agent entered or left. One immutable index serves every agent. Supply the previous and current position and a callback. The index keeps no per-agent membership sets.

**[Try it on Hugging Face](https://huggingface.co/spaces/pgg-engineering/edgechime) · [Download the source](https://edgechime.tancurl3.chatgpt.site/source.zip) · [Use the labeled replay](https://huggingface.co/datasets/pgg-engineering/edgechime-bench)**

## Measured performance

At **100,000 broad, overlapping rectangles**, short moves took **4.66 µs/update**. Upstream Flatbush with one cached point query and a membership diff took **535.75 µs**, and an independent boundary grid took **48.58 µs**. All three produced the same 84,551 enter and exit events. On the full Porto GPS replay with 8,192 generated zones the times were **0.34, 1.31 and 0.49 µs/update**. The GPS observations are real. The subscriptions are synthetic.

| Workload | EdgeChime | Flatbush 4.7.0 + diff | Perimeter grid |
| --- | ---: | ---: | ---: |
| Short moves, 100k broad zones | **4.66** | 535.75 | 48.58 |
| Porto, 4,469 moves, 8,192 zones | **0.34** | 1.31 | 0.49 |
| Short moves, 8,192 sparse zones | 0.22 | 0.29 | **0.14** |
| Random jumps, 8,192 broad zones | 88.46 | **41.58** | 112.87 |

Values are median run means in microseconds per update on Node 24.22.3, AMD EPYC 9V74, one pinned logical CPU. They include delivering every changed ID to the same callback. Build time for the 100k index was 210 ms. Relative speed depends on subscription geometry, movement and runtime. On three additional seeds with 8,192 broad zones and short moves, EdgeChime was 55.8–70.4× faster than the Flatbush adapter and 3.5–4.3× faster than the perimeter grid. [Method and raw timings](bench/README.md) · [100k run](bench/wide-100k.json) · [Full Porto run](bench/porto-full.json) · [Default workloads](bench/results.json).

A separate in-process replay interleaved 10,000 moving agents through 100,000 zones. The median was 1.36 million position updates/second, with five passes ranging from 0.32 to 2.09 million. This figure covers the move loop after initialization. [Fleet replay](bench/fleet-results.json).

A second run on a Hugging Face CPU job (Intel Xeon Platinum 8375C, Node 22.23.5) clocked the 100k-zone short moves at **3.99 µs/update**, versus **457.04 µs** for Flatbush and **38.89 µs** for the grid. On full Porto the three times were **0.44, 1.97 and 0.58 µs**. Flatbush again won on random jumps. Both hosts ran the same project-maintained harness. [Second-host measurements](bench/hf-cpu.json).

## How it works

The index sorts horizontal and vertical boundary coordinates. A short move visits coordinates between the sampled endpoints and tests the other dimension only for candidate boxes. The two axes share ownership of diagonal transitions, so a corner never causes duplicate callbacks. A coordinate lookup table and optional orthogonal strips narrow the candidate search. Strip construction has a posting budget. For large candidate sets and initial point queries a packed bounding-box tree prunes rectangles away from both endpoints. Large moves can still cost linear time in dense scenes.

## Use it

For Node 18+ or a browser with ES modules, copy `index.js` into your project. The runtime has no dependencies. For an installed Node package and CLI:

```sh
npm install https://edgechime.tancurl3.chatgpt.site/edgechime-0.1.0.tgz
```

```js
import { EdgeChime } from '@pgg-engineering/edgechime';
// When copying the file directly, import from './index.js'.
const rooms = new EdgeChime([
  [0, 0, 10, 10],     // zone 0
  [5, 0, 15, 10],     // zone 1
]);
rooms.move(4, 3, 6, 3, (id, entered) => console.log(id, entered));
// 1 true
rooms.move(6, 3, 10, 3, (id, entered) => console.log(id, entered));
// 0 false
```

**Use cases.** Wake an agent when a tracked object changes rooms, update area-of-interest subscriptions in a multiplayer simulation, or route sampled positions to dashboards whose rectangles gained or lost the object. Keep the last point per object in the application. Call `at` once to seed membership when you need a complete initial snapshot.

**Geometry.** A zone contains `minX ≤ x < maxX` and `minY ≤ y < maxY`. Coordinates are finite Cartesian numbers. IDs are zero-based insertion positions. Identical rectangles retain separate IDs. Callbacks run synchronously, in unspecified order, once per changed ID. Both query methods return the callback count. A jump from outside to outside produces no event, even if the straight line passes through the zone. Project geographic coordinates or handle longitude wrap in the caller as needed. Rebuild the index when subscriptions change.

## Stream positions from multiple agents

```sh
printf '%s\n' '[[0,0,10,10],[5,0,15,10]]' > zones.json
printf '%s\n' '{"agent":"a","x":4,"y":3}' '{"agent":"a","x":6,"y":3}' \
  '{"agent":"a","x":10,"y":3}' '{"agent":"a","forget":true}' | npx --no-install edgechime zones.json
```

```jsonl
{"agent":"a","entered":[0],"exited":[]}
{"agent":"a","entered":[1],"exited":[]}
{"agent":"a","entered":[],"exited":[0]}
{"agent":"a","entered":[],"exited":[1]}
```

The CLI consumes newline-delimited JSON in arrival order. It stores a pair of coordinates for each live agent, skips empty change records, respects stdout backpressure and releases an agent on `forget`. Applications with event-time ordering should order each agent’s samples before passing them in. The in-process API avoids parsing, serialization and I/O for latency-sensitive workloads.

## Integrations

Stream vehicle positions from Eclipse SUMO floating-car data and route them to matching zones. The runnable example includes a versioned upstream regression fixture and expected events. Game developers can import rectangular triggers from a named Tiled object layer. Both examples run from the source checkout. [SUMO adapter](examples/sumo/README.md) · [Tiled adapter](examples/tiled/README.md).

## Reproduce

From the source checkout, run `npm test`, `npm run bench` and `node bench/fleet.mjs`. The comparison harness includes pinned Flatbush 4.7.0 and its dependency, a specialized perimeter-grid baseline and a historical taxi replay. The benchmark can run offline. Source, selection and license for the Porto positions are in [the trace note](bench/trace.md). Zone subscriptions are generated.

## Related work

Range monitoring and sweep-and-prune have a long history. EdgeChime specializes endpoint changes for moving points in immutable 2D half-open rectangles. Flatbush offers a packed Hilbert R-tree for general spatial searches. Tile38 supplies server-side geofencing, network delivery and separate enter, exit and cross events. Polygon geometry, frequent zone edits, segment crossings and persistent network delivery fall outside the interface. [Flatbush](https://github.com/mourner/flatbush) · [Tile38 geofencing](https://tile38.com/topics/geofencing) · [Continuous range monitoring](https://mcn.cse.psu.edu/paper/other/cai06.pdf).

The runtime, CLI, tests and demo are MIT-licensed. Benchmark dependencies and movement data retain the licenses identified in `bench/`. The SUMO fixture retains its accompanying Eclipse Public License.
