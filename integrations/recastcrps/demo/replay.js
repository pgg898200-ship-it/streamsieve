const el = id => document.getElementById(id);
const controls = ["target", "play", "next", "position", "omit"];
const dateText = s => s.slice(0, 4) + "-" + s.slice(4, 6) + "-" + s.slice(6, 8) + " " + s.slice(8) + "Z";
const groups = new Map();
let ids, rows, engine, timer, position, priorTarget;
let active = [];
const format = value => value.toFixed(2);

function stop() {
  clearInterval(timer);
  timer = undefined;
  el("play").textContent = "Play";
}

function plot() {
  if (!rows) return;
  const canvas = el("distribution");
  const width = canvas.clientWidth, height = canvas.clientHeight;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  const values = active.filter(Number.isFinite);
  const obs = rows[0].obs;
  const maxWind = Math.ceil(Math.max(obs, ...rows.flatMap(row => row.values)) / 20) * 20 + 10;
  const bins = Array(Math.ceil(maxWind / 5) + 1).fill(0);
  values.forEach(v => bins[Math.floor(v / 5)]++);
  const ceiling = Math.max(4, Math.ceil(Math.max(...bins) / 2) * 2);
  const left = 28, right = width - 15, top = 13, floor = height - 35;
  const x = v => left + (right - left) * v / (bins.length * 5);
  const y = n => floor - (floor - top) * n / ceiling;
  ctx.font = "12px system-ui";
  for (const n of [0, ceiling / 2, ceiling]) {
    ctx.strokeStyle = "#d9e2df"; ctx.beginPath();ctx.moveTo(left, y(n));ctx.lineTo(right,y(n));ctx.stroke();
    ctx.textAlign = "right";ctx.fillStyle="#465d67";ctx.fillText(n,left-5,y(n)+4);
  }
  ctx.fillStyle="#14767b";
  bins.forEach((n, i) => ctx.fillRect(x(5 * i) + 1, y(n), Math.max(1,x(5)-left-2), floor-y(n)));
  ctx.strokeStyle="#af4d22";ctx.lineWidth=2;ctx.setLineDash([4,3]);ctx.beginPath();ctx.moveTo(x(obs),top);ctx.lineTo(x(obs),floor);ctx.stroke();
  ctx.fillStyle="#465d67";ctx.textAlign="center";
  const tick = width < 450 ? 40 : 20;
  for(let v=0;v<=maxWind;v+=tick) ctx.fillText(String(v),x(v),floor+16);
  ctx.fillText("Wind speed (kn)",(left+right)/2,height-2);
  canvas.setAttribute("aria-label",values.length+" member forecasts in 5-knot bins. Observed wind "+obs+" knots.");
}

function updateOmission() {
  const obs = rows[0].obs, slot = Number(el("omit").value);
  const ptr = engine.omission_scores(obs);
  const scores = new Float64Array(engine.memory.buffer, ptr, ids.length);
  const value = scores[slot];
  if (!Number.isFinite(value)) {
    el("counterfactual").textContent = active[slot] === undefined ? "Member has no active forecast" : "Withdrawal would empty the ensemble";
    return;
  }
  const diff = value - engine.ensemble_score(obs);
  el("counterfactual").textContent = format(value)+" kn after withdrawal ("+(diff>=0?"+":"")+diff.toFixed(3)+" kn)";
}

function draw(n) {
  n = Math.min(Math.max(n,1), rows.length * ids.length);
  if (position === undefined || n < position || priorTarget !== el("target").value) {
    position = 0;
    priorTarget = el("target").value;
    active = Array(ids.length);
    const limit = Math.max(...rows.flatMap(r => r.values));
    if (engine.reset_ensemble(limit, ids.length) !== 0) throw Error("Forecast grid exceeds demo limits");
  }
  while (position < n) {
    const slot = position % ids.length;
    const v = rows[Math.floor(position/ids.length)].values[slot];
    if (engine.set_member(slot, v, 1) !== 0) throw Error("Invalid forecast");
    active[slot] = v;
    position++;
  }
  const slot = (n - 1) % ids.length, row = rows[Math.floor((n-1)/ids.length)];
  el("position").value = n;
  el("score").textContent = format(engine.ensemble_score(row.obs));
  el("observed").textContent = row.obs;
  el("members").textContent = active.filter(Number.isFinite).length;
  el("step").textContent = n+" / "+el("position").max;
  el("status").textContent = dateText(row.init)+" cycle · "+ids[slot]+" forecast "+row.values[slot]+" kn";
  el("next").disabled = n === Number(el("position").max);
  updateOmission();
  plot();
}
function changeTarget() {
  stop();
  rows = groups.get(el("target").value);
  el("position").max = rows.length * ids.length;
  draw(ids.length);
}
async function load() {
  const loadFile = async url => {
    const response = await fetch(url);
    if (!response.ok) throw Error("Could not load "+url);
    return response;
  };
  const [wasm, csv] = await Promise.all([loadFile("recast.wasm").then(r=>r.arrayBuffer()),loadFile("../replay/lee_gefs.csv").then(r=>r.text())]);
  const {instance} = await WebAssembly.instantiate(wasm, {});
  engine = instance.exports;
  engine._initialize();
  const lines = csv.trim().split(/\r?\n/);
  ids = lines.shift().split(",").slice(3);
  for (const line of lines) {
    const fields = line.split(","), target = fields[0];
    if (!groups.has(target)) groups.set(target, []);
    groups.get(target).push({init:fields[1],obs:Number(fields[2]),values:fields.slice(3).map(Number)});
  }
  for (const key of groups.keys()) el("target").add(new Option(dateText(key),key));
  for (const [i,id] of ids.entries()) el("omit").add(new Option(id,String(i)));
  el("target").value = groups.has("2023090812") ? "2023090812" : [...groups.keys()][0];
  controls.forEach(id=>{el(id).disabled=false;});
  changeTarget();
  el("target").onchange=changeTarget;
  el("omit").onchange=updateOmission;
  el("position").oninput=e=>{stop();draw(Number(e.target.value));};
  el("next").onclick=()=>{stop();draw(position+1);};
  el("play").onclick=()=>{
    if (timer !== undefined) {stop();return;}
    if (position >= Number(el("position").max)) draw(1);
    el("play").textContent="Pause";
    timer=setInterval(()=>{draw(position+1);if(position>=Number(el("position").max))stop();},90);
  };
  new ResizeObserver(plot).observe(el("distribution"));
}
load().catch(error=>{ controls.forEach(id=>{el(id).disabled=true;});el("status").textContent="Replay unavailable. "+error.message; });
