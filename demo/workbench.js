"use strict";

const input = document.getElementById("input");
const k = document.getElementById("k");
const page = document.getElementById("page");
const output = document.getElementById("output");
const runButton = document.getElementById("run");
const exampleButton = document.getElementById("example");

function resetExample() {
  input.value = `{
  "rankings": [
    ["west/alpha", "west/beta", "west/gamma"],
    ["west/alpha", "west/gamma", "west/beta"],
    ["east/delta", "east/epsilon", "east/zeta"],
    ["east/epsilon", "east/zeta", "east/delta"]
  ],
  "source_groups": ["west", "west", "east", "east"]
}`;
  k.value = "1";
  page.value = "1";
  run();
}

function replay(args) {
  const worker = new Worker("worker.js");
  return new Promise((resolve, reject) => {
    worker.onmessage = event => {
      worker.terminate();
      if (event.data.error) reject(new Error(event.data.error));
      else resolve(event.data);
    };
    worker.onerror = event => {
      worker.terminate();
      reject(new Error(event.message));
    };
    worker.postMessage(args);
  });
}

async function run() {
  output.textContent = "Checking ranked windows…";
  runButton.disabled = true;
  exampleButton.disabled = true;
  try {
    if (input.value.length > 2000000) throw new Error("Input exceeds 2 MB. Use the Python release.");
    const query = JSON.parse(input.value);
    const lists = query.rankings ?? query.lists;
    if (!Array.isArray(lists)) throw new Error("Supply an array of ranked lists in rankings.");
    const groups = query.source_groups ?? query.groups ?? lists.map(() => "all");
    const result = await replay({input: {lists, groups, c: query.c}, k: Number(k.value), page: Number(page.value)});
    output.textContent = [
      "Ordered winners    " + JSON.stringify(result.scoped.ids),
      "With shard map     " + result.scoped.read + " IDs read",
      "Without shard map  " + result.flat.read + " IDs read",
      "Full input         " + result.scoped.total + " IDs",
      "Exhaustive check   matched"
    ].join("\n");
  } catch (error) {
    output.textContent = error.message;
  } finally {
    runButton.disabled = false;
    exampleButton.disabled = false;
  }
}

// A pasted MCP request can include replay settings. The controls remain editable.
input.addEventListener("input", () => {
  try {
    const query = JSON.parse(input.value);
    if (query.k !== undefined) k.value = query.k;
    if (query.page_size !== undefined) page.value = query.page_size;
  } catch {
    // Partial JSON is expected while typing. Run replay reports a parse error.
  }
});
exampleButton.addEventListener("click", resetExample);
runButton.addEventListener("click", run);
resetExample();
