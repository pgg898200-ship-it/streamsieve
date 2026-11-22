"use strict";
const el = id => document.getElementById(id);
let rows = [];
function inspect(index) {
  el("topic").selectedIndex = index;
  const [topic, read, total] = rows[index];
  el("stat").textContent = read.toLocaleString() + " / " + total.toLocaleString() + " entries read";
  [...el("bars").children].forEach((bar, i) => bar.classList.toggle("active", i === index));
}
function render() {
  const sharded = el("dataset").value === "sharded";
  el("mode").disabled = sharded;
  if (sharded) el("mode").value = "";
  el("caption").textContent = sharded
    ? "Historical TREC rankings, partitioned into eight simulated shards. Pages of 16. The global coordinator read 78,956 IDs across 100 topics. Solving each shard independently and merging the scores took 206,934."
    : "Historical unsharded rankings, replayed in pages of 32. An independent exact streaming baseline stopped on the same page for every topic.";
  rows = window.replays[el("dataset").value + el("mode").value];
  const read = rows.reduce((n, r) => n + r[1], 0);
  const full = rows.reduce((n, r) => n + r[2], 0);
  el("skipped").textContent = (100 * (1 - read/full)).toFixed(2) + "%";
  el("early").textContent = rows.filter(r => r[1] < r[2]).length + "/" + rows.length;
  el("same").textContent = rows.length + "/" + rows.length;
  el("bars").replaceChildren(); el("topic").replaceChildren();
  rows.forEach(([topic, n, total], i) => {
    const bar = document.createElement("button");
    const label = "Topic " + topic + ": " + n + " of " + total + " read";
    bar.title = label; bar.setAttribute("aria-label", label); bar.tabIndex = -1;
    bar.style.setProperty("--h", (100*n/total) + "%");
    bar.addEventListener("click", () => inspect(i));
    el("bars").append(bar);
    el("topic").add(new Option(topic, i));
  });
  inspect(0);
}
el("dataset").addEventListener("change", render);
el("mode").addEventListener("change", render);
el("topic").addEventListener("change", () => inspect(el("topic").selectedIndex));
render();
