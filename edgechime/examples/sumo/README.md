# Replay SUMO vehicle positions through EdgeChime

Run these commands from the EdgeChime repository root with Python 3.8+ and Node.js 18+. The included floating-car-data (FCD) recording contains 117 positions from two simulated vehicles. Two adjoining zones cover a portion of their route.

```sh
python3 examples/sumo/fcd.py examples/sumo/shape-filter.fcd.xml \
  | node bin/edgechime.js examples/sumo/zones.json
```

Both vehicles enter zone 0, move into zone 1, and leave it. Each ID is the original SUMO string ID.

```jsonl
{"agent":"0","entered":[0],"exited":[]}
{"agent":"1","entered":[0],"exited":[]}
{"agent":"0","entered":[1],"exited":[0]}
{"agent":"1","entered":[1],"exited":[0]}
{"agent":"0","entered":[],"exited":[1]}
{"agent":"1","entered":[],"exited":[1]}
```

## Connect a running simulation

On Unix, use a named pipe to keep SUMO's normal console output separate from the FCD stream. Supply your own simulation configuration and rectangles in the simulation's coordinate system.

```sh
stream_dir=$(mktemp -d)
mkfifo "$stream_dir/live.xml"
sumo -c your-network.sumocfg --fcd-output "$stream_dir/live.xml" &
python3 examples/sumo/fcd.py "$stream_dir/live.xml" | node bin/edgechime.js your-zones.json
wait
rm -r "$stream_dir"
```

The adapter also accepts XML from standard input and saved `.xml.gz` files. Each vehicle produces one line with a string `agent` and numeric `x`, `y`, and `time` fields. The adapter passes through positions at the center of the front bumper and annotates them with their enclosing timestep in simulation seconds. SUMO normally writes Cartesian meters. With `--fcd-output.geo`, use longitude for x and latitude for y. The CLI consumes the stream in input order and outputs agent IDs and zone changes. Consumers needing simulation timestamps can read the adapter's position stream. [SUMO FCD format and options](https://sumo.dlr.de/docs/Simulation/Output/FCDOutput.html)

Each record is available as soon as its XML start tag arrives and the current input chunk has been parsed. The parser keeps a fixed-size input buffer and the current XML element stack. File replay runs as quickly as the consumer can read. Vehicle records are processed independently of person or container records.

SUMO can filter positions by location, equipment, or sampling period. An omitted vehicle generates no event. When it appears again, EdgeChime compares with its last observation. Likewise, an empty timestep or end of file leaves its last observed state intact. To retire a vehicle when an external lifecycle feed confirms its departure, insert `{"agent":"0","forget":true}` into the CLI input. That emits exits for its active zones and releases its state. The CLI retains one last position per distinct ID until a forget record or process exit.

## Fixture provenance

`shape-filter.fcd.xml` is the unmodified `shape_filter/fcd.sumo` fixture from Eclipse SUMO tag `v1_24_0` (Git blob `71b67ec9668ee246c5fa46a9ea68c37bf7d61323`). Its own license notice applies. The Eclipse Public License 2.0 accompanies the fixture. The zones and adapter are part of EdgeChime. [Upstream fixture](https://github.com/eclipse-sumo/sumo/blob/v1_24_0/tests/sumo/output/fcd-output/shape_filter/fcd.sumo) · [Eclipse Public License 2.0](EPL-2.0.txt)
