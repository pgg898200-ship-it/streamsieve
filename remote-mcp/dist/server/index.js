"use strict";
// Independent exact scan in the browser. A common integer denominator makes
// every possible RRF term exact within the bounded, user-supplied windows.
function replayRankings(input, k, page, useGroups) {
  const {lists, groups} = input;
  if (!Number.isInteger(k) || k < 1 || k > 100 || !Number.isInteger(page) || page < 1 || page > 256)
    throw Error("Choose k from 1 to 100 and page size from 1 to 256.");
  if (!Array.isArray(lists) || lists.length < 1 || lists.length > 64 ||
      !Array.isArray(groups) || groups.length !== lists.length || !groups.every(s => typeof s === "string"))
    throw Error("Supply 1–64 lists and one string group name per list.");
  const owner = new Map();
  let total = 0, depth = 0;
  lists.forEach((ids,i) => {
    if (!Array.isArray(ids) || ids.length > 2048 || !ids.every(id => typeof id === "string") ||
        new Set(ids).size !== ids.length) throw Error("Each list must contain up to 2,048 unique string IDs.");
    total += ids.length; depth = Math.max(depth, ids.length);
    for (const id of ids) {
      if (owner.has(id) && owner.get(id) !== groups[i]) throw Error("A document appears in different groups: " + id);
      owner.set(id, groups[i]);
    }
  });
  if (total > 20000) throw Error("This browser replay accepts up to 20,000 total IDs. Use Python for larger traces.");
  const c = input.c ?? 60;
  if (!Number.isInteger(c) || c < 0 || c > 1024) throw Error("Choose integer c from 0 to 1,024.");
  const gcd = (a,b) => {while (b) [a,b]=[b,a%b]; return a;};
  let base = 1n;
  for (let n=1; n<=depth+1; n++) {const d=BigInt(c+n);base=(base/gcd(base,d))*d;}
  const weight = n => base / BigInt(c+n);
  const weights = Array.from({length:depth+2},(_,n)=>n?weight(n):0n);
  const group = groups.map(g => useGroups ? g : "");
  const depths = lists.map(() => 0);
  const seen = lists.map(() => new Set());
  const scores = new Map();
  // JS string ordering uses UTF-16. Compare code points for Python-compatible ties.
  const idCompare = (a,b) => {
    const aa = Array.from(a), bb = Array.from(b);
    for(let i=0;i<Math.min(aa.length,bb.length);i++) {
      const delta=aa[i].codePointAt(0)-bb[i].codePointAt(0);
      if(delta) return delta;
    }
    return aa.length-bb.length;
  };
  const compare = (a,b,score) => score(a)>score(b)?-1:score(a)<score(b)?1:idCompare(a,b);
  let result = null, consumed = null;
  do {
    const budgets = new Map(), tails = [];
    for (let i=0;i<lists.length;i++) {
      const stop = Math.min(lists[i].length, depths[i]+page);
      while(depths[i]<stop) {
        const d=lists[i][depths[i]++];
        seen[i].add(d); scores.set(d,(scores.get(d)||0n)+weights[depths[i]]);
      }
      const tail=stop===lists[i].length?0n:weights[stop+1];
      tails.push(tail); budgets.set(group[i],(budgets.get(group[i])||0n)+tail);
    }
    const all = depths.every((n,i)=>n===lists[i].length);
    const lower = d=>scores.get(d);
    const sorted = [...scores.keys()].sort((a,b)=>compare(a,b,lower));
    const top=sorted.slice(0,k);
    if (all) {
      if (result && JSON.stringify(result)!==JSON.stringify(top)) throw Error("Exhaustive check failed.");
      return {ids:top, read:consumed ?? total, total};
    }
    if(result || top.length<k) continue;
    const weakest=top[k-1], w=lower(weakest);
    if ([...budgets.values()].some(t=>t>=w)) continue;
    const upper = d => {
      let high=lower(d), scope=useGroups?owner.get(d):"";
      for (let i=0;i<lists.length;i++) if(group[i]===scope&&!seen[i].has(d)) high+=tails[i];
      return high;
    };
    const beats=(a,b)=>lower(a)>upper(b)||(lower(a)===upper(b)&&idCompare(a,b)<0);
    if (!sorted.slice(k).every(d=>beats(weakest,d))) continue;
    if (!top.slice(1).every((d,i)=>beats(top[i],d))) continue;
    result=top;consumed=depths.reduce((a,b)=>a+b,0);
  } while (true);
}


// Remote adapter. The replay algorithm above is vendored from the released
// browser demo. Every query and result is scoped to a single HTTP request.
const registryPublicKey = "SImtBlS8O0GENuw4kwo2044pJfvZzXpiCd0hlF8LcPc=";
const guide = "https://rankfence-pgg.tancurl3.chatgpt.site/guide.html";
const source = "https://github.com/pgg898200-ship-it/streamsieve/tree/rankfence-mcp-0.1.0";
const current = "2026-07-28", legacy = "2025-11-25";
const ns = "io.modelcontextprotocol/";
const info = {name: "rankfence", version: "0.1.0"};
const capabilities = {tools: {}};
const string = {type: "string", maxLength: 256};
const schema = {
  type: "object", additionalProperties: false, required: ["rankings", "k"],
  properties: {
    rankings: {type: "array", minItems: 1, maxItems: 32,
      items: {type: "array", maxItems: 512, uniqueItems: true, items: string}},
    k: {type: "integer", minimum: 1, maximum: 100},
    page_size: {type: "integer", minimum: 1, maximum: 256, default: 8},
    c: {type: "integer", minimum: 0, maximum: 1024, default: 60},
    source_groups: {type: "array", minItems: 1, maxItems: 32, items: string}
  }
};
const analyzeTool = {
  name: "rankfence_analyze",
  description: "Replay a fully supplied reciprocal-rank-fusion query in equal page rounds. " +
    "Return exact winners, simulated ranked IDs read and saved, and exhaustive agreement. " +
    "Supply source_groups only for disjoint ID namespaces. Up to 2,048 ID occurrences.",
  annotations: {readOnlyHint: true, idempotentHint: true, openWorldHint: false, destructiveHint: false},
  inputSchema: schema
};
const record = x => x !== null && typeof x === "object" && !Array.isArray(x);

function analyze(args) {
  if (!record(args) || Object.keys(args).some(k => !Object.hasOwn(schema.properties, k)))
    throw Error("Expected rankings, k and optionally source_groups, c and page_size.");
  const {rankings, k, source_groups, c = 60, page_size = 8} = args;
  if (!Array.isArray(rankings) || rankings.length < 1 || rankings.length > 32)
    throw Error("Supply 1–32 source rankings.");
  const shortString = id => typeof id === "string" && id.length <= 256;
  if (!rankings.every(row => Array.isArray(row) && row.length <= 512 && row.every(shortString)))
    throw Error("Each ranking accepts up to 512 IDs of up to 256 UTF-16 code units.");
  const total = rankings.reduce((n, row) => n + row.length, 0);
  if (total > 2048) throw Error("Supply at most 2,048 ID occurrences.");
  const groups = source_groups === undefined ? rankings.map(() => "") : source_groups;
  if (!Array.isArray(groups) || groups.length !== rankings.length || !groups.every(shortString))
    throw Error("source_groups must name the group of each source.");
  if (!Number.isInteger(c) || !Number.isInteger(page_size) || page_size < 1)
    throw Error("c and page_size must be integers, with positive page_size.");
  const rounds = Math.ceil(Math.max(...rankings.map(row => row.length)) / page_size);
  if (rounds * total * Math.max(4, rankings.length) > 750000)
    throw Error("Replay work limit exceeded. Increase page_size or supply a smaller sample.");
  const replay = replayRankings({lists: rankings, groups, c}, k, page_size, source_groups !== undefined);
  return {ids: replay.ids, ids_read_in_replay: replay.read, ids_in_full_query: replay.total,
    ids_saved_in_replay: replay.total - replay.read, matches_exhaustive: true};
}

async function readPayload(request) {
  const limit = 256 * 1024;
  if (Number(request.headers.get("content-length")) > limit) throw RangeError("body too large");
  if (!request.body) throw Error("empty input");
  const reader = request.body.getReader(), chunks = [];
  let length = 0;
  try {
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      length += value.length;
      if (length > limit) {
        await reader.cancel();
        throw RangeError("body too large");
      }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(bytes));
}

function decodeHeader(value) {
  if (!value?.startsWith("=?base64?") || !value.endsWith("?=")) return value;
  try {
    return new TextDecoder("utf-8", {fatal: true}).decode(
      Uint8Array.from(atob(value.slice(9, -2)), x => x.charCodeAt(0)));
  } catch { return null; }
}

async function handle(request) {
  const url = new URL(request.url);
  const headers = {"cache-control": "no-store", "vary": "Origin", "x-content-type-options": "nosniff"};
  const origin = request.headers.get("Origin");
  if (origin !== null) {
    const allowed = [url.origin, new URL(guide).origin, "https://chatgpt.com", "https://claude.ai",
      "http://localhost:6274", "http://127.0.0.1:6274"];
    if (!allowed.includes(origin)) return new Response("Origin denied", {status: 403, headers});
    headers["access-control-allow-origin"] = origin;
  }
  const reply = (body, status = 200, type = "application/json") =>
    new Response(body === null ? null : type === "application/json" ? JSON.stringify(body) : body,
      {status, headers: {...headers, "content-type": type}});
  const err = (id, code, message, status = 400, data) =>
    reply({jsonrpc: "2.0", id, error: {code, message, ...(data ? {data} : {})}}, status);
  if (url.pathname === "/.well-known/mcp-registry-auth" && request.method === "GET")
    return reply("v=MCPv1; k=ed25519; p=" + registryPublicKey + "\n", 200, "text/plain; charset=utf-8");
  if (url.pathname === "/" && request.method === "GET") {
    const html = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>RankFence MCP</title>' +
      '<main><h1>RankFence MCP</h1><p>Replay paged search rankings and count how many document IDs you can leave unread while preserving the exact top results.</p>' +
      '<p>In your MCP client add a remote server, select Streamable HTTP, and use <code>https://rankfence-tools-pgg.tancurl3.chatgpt.site/rpc</code> as the endpoint. Call <code>rankfence_analyze</code> with a complete example query.</p>' +
      '<pre>{"rankings":[["a","b"],["a","c"]],"k":1,"page_size":1}</pre>' +
      '<p>This example reads two of four IDs. Supplied IDs are processed for the request. Use the local Python tool for private inputs.</p>' +
      '<p><a href="' + guide + '">RankFence guide</a> · <a href="' + source + '">Source and tests</a></p></main></html>';
    return reply(html, 200, "text/html; charset=utf-8");
  }
  if (!["/mcp", "/rpc"].includes(url.pathname)) return reply("Not found", 404, "text/plain");
  headers.allow = "POST, OPTIONS";
  if (request.method === "OPTIONS") {
    headers["access-control-allow-methods"] = "POST, OPTIONS";
    headers["access-control-allow-headers"] = "Accept, Content-Type, MCP-Protocol-Version, MCP-Method, MCP-Name, Authorization";
    return reply(null, 204);
  }
  if (request.method !== "POST") return err(null, -32600, "Use POST", 405);
  if (request.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/json")
    return err(null, -32600, "Use application/json", 415);
  const accept = request.headers.get("accept") || "";
  if (!/application\/json|\*\/\*/i.test(accept)) return err(null, -32600, "Accept application/json", 406);
  let m;
  try { m = await readPayload(request); }
  catch (e) { return err(null, -32700, e instanceof RangeError ? "Request exceeds 256 KiB" : "Invalid JSON",
                          e instanceof RangeError ? 413 : 400); }
  if (!record(m) || m.jsonrpc !== "2.0" || typeof m.method !== "string" ||
      ("id" in m && typeof m.id !== "string" && !Number.isSafeInteger(m.id)))
    return err(null, -32600, "Invalid request");
  const p = m.params === undefined ? {} : m.params, id = m.id;
  if (!record(p)) return err(id ?? null, -32602, "params must be an object");
  if (!Object.hasOwn(m, "id")) return reply(null, 202);
  const meta = p._meta === undefined ? {} : p._meta;
  if (!record(meta)) return err(id, -32602, "_meta must be an object");
  const version = meta[ns + "protocolVersion"];
  const header = request.headers.get("MCP-Protocol-Version");
  const modern = version !== undefined || (header !== null && header !== legacy);
  if (modern) {
    if ((header !== null && ![current, legacy].includes(header)) ||
        (typeof version === "string" && ![current, legacy].includes(version)))
      return err(id, -32022, "Unsupported protocol version", 400,
                 {supported: [current, legacy], requested: String(version ?? header).slice(0, 128)});
    if (header !== current || version !== current || request.headers.get("MCP-Method") !== m.method ||
        (m.method === "tools/call" && decodeHeader(request.headers.get("MCP-Name")) !== p.name))
      return err(id, -32020, "Required header missing or does not match request");
    if (!record(meta[ns + "clientCapabilities"])) return err(id, -32602, "Missing clientCapabilities");
  } else if (m.method !== "initialize" && header !== legacy) {
    return err(id, -32020, "Send MCP-Protocol-Version");
  }
  let result;
  if (m.method === "server/discover" && modern) {
    result = {supportedVersions: [current, legacy], capabilities};
  } else if (m.method === "initialize" && !modern) {
    if (typeof p.protocolVersion !== "string" || !record(p.capabilities) || !record(p.clientInfo) ||
        typeof p.clientInfo.name !== "string" || typeof p.clientInfo.version !== "string")
      return err(id, -32602, "Invalid initialize parameters");
    result = {protocolVersion: legacy, serverInfo: info, capabilities};
  } else if (m.method === "ping") {
    result = {};
  } else if (m.method === "tools/list") {
    if (p.cursor !== undefined) return err(id, -32602, "Unknown cursor");
    result = {tools: [analyzeTool]};
  } else if (m.method === "tools/call") {
    if (p.name !== analyzeTool.name || !record(p.arguments)) return err(id, -32602, "Unknown tool or invalid arguments");
    try {
      const data = analyze(p.arguments);
      result = {content: [{type: "text", text: JSON.stringify(data)}], structuredContent: data};
    } catch (e) {
      result = {isError: true, content: [{type: "text", text: e.message}]};
    }
  } else return err(id, -32601, "Method not found", modern ? 404 : 200);
  if (modern) Object.assign(result, {resultType: "complete", _meta: {[ns + "serverInfo"]: info}});
  return reply({jsonrpc: "2.0", id, result});
}
export default {fetch: handle};
