import { readFileSync } from 'node:fs';
import { buildTiledZones } from './zones.mjs';

const map = JSON.parse(readFileSync(new URL('./station.tmj', import.meta.url), 'utf8'));
const { index, objects } = buildTiledZones(map, ['Station', 'Zones']);
index.at(40, 8, id => console.log('initial', objects[id].id, objects[id].name));
index.move(40, 8, 48, 8, (id, entered) => console.log(entered ? 'enter' : 'leave', objects[id].id, objects[id].name));
