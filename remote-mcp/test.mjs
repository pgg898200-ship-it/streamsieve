import {test} from "node:test";
import assert from "node:assert/strict";
import worker from "./dist/server/index.js";

const base = "https://rankfence.example", ns = "io.modelcontextprotocol/";
const modern = "2026-07-28", legacy = "2025-11-25";
const meta = {[ns+"protocolVersion"]: modern, [ns+"clientCapabilities"]: {}};
function post(body, extra={}, path="/mcp") {
  const headers = {"content-type":"application/json","accept":"application/json, text/event-stream", ...extra};
  return worker.fetch(new Request(base + path, {method:"POST", headers,
    body: typeof body==="string" ? body : JSON.stringify(body)}));
}
const rpc = (method,params={},id=1)=>({jsonrpc:"2.0", id, method, params});
const modernHeaders = (method, name)=>({"MCP-Protocol-Version":modern, "MCP-Method":method,
  ...(name ? {"MCP-Name":name} : {})});
async function runTool(args, modernCall=false) {
  const name = "rankfence_analyze", method = "tools/call";
  const body = rpc(method,{name,arguments:args,...(modernCall?{_meta:meta}:{})});
  const response = await post(body, modernCall ? modernHeaders(method,name) : {"MCP-Protocol-Version":legacy});
  assert.equal(response.status, 200);
  return (await response.json()).result;
}
test("legacy initialization, notification, discovery, ping and real replay",async()=>{
  const init = await post(rpc("initialize",{protocolVersion:legacy,capabilities:{},clientInfo:{name:"test",version:"1"}}));
  assert.equal((await init.json()).result.protocolVersion,legacy);
  const ready = await post({jsonrpc:"2.0",method:"notifications/initialized"},{"MCP-Protocol-Version":legacy});
  assert.equal(ready.status,202); assert.equal(await ready.text(),"");
  const tools = await post(rpc("tools/list"),{"MCP-Protocol-Version":legacy});
  assert.equal((await tools.json()).result.tools[0].name,"rankfence_analyze");
  const ping = await post(rpc("ping"),{"MCP-Protocol-Version":legacy});
  assert.deepEqual((await ping.json()).result,{});
  const args={rankings:[["a","b"],["a","c"]],k:1,page_size:1};
  assert.deepEqual((await runTool(args)).structuredContent,{ids:["a"],ids_read_in_replay:2,
    ids_in_full_query:4,ids_saved_in_replay:2,matches_exhaustive:true});
});
test("modern stateless discovery, mirrored headers and public alias",async()=>{
  const method="server/discover", body=rpc(method,{_meta:meta});
  const response = await post(body,modernHeaders(method),"/rpc");
  assert.deepEqual((await response.json()).result.supportedVersions,[modern,legacy]);
  const mismatch = await post(body, modernHeaders("tools/list"));
  assert.equal(mismatch.status,400); assert.equal((await mismatch.json()).error.code,-32020);
  const badVersion = await post(body,{"MCP-Protocol-Version":"2077-01-01"});
  assert.equal((await badVersion.json()).error.code,-32022);
  const result=await runTool({rankings:[["a"],["b"]],source_groups:["a","b"],k:2},true);
  assert.equal(result.resultType,"complete"); assert.deepEqual(result.structuredContent.ids,["a","b"]);
  const params = {name:"rankfence_analyze",arguments:{rankings:[[]],k:1},_meta:meta};
  const encoded = "=?base64?"+btoa(params.name)+"?=";
  const encodedRes = await post(rpc("tools/call",params),modernHeaders("tools/call",encoded));
  assert.deepEqual((await encodedRes.json()).result.structuredContent.ids,[]);
});
test("root, key and methods",async()=>{
  const page=await worker.fetch(new Request(base));
  assert.equal(page.status,200); assert.match(await page.text(),/guide.html/);
  const auth=await worker.fetch(new Request(base+"/.well-known/mcp-registry-auth"));
  assert.match(await auth.text(),/^v=MCPv1; k=ed25519; p=\S+/);
  for(const method of ["GET","DELETE"]) {
    assert.equal((await worker.fetch(new Request(base+"/mcp",{method}))).status,405);
  }
  const allowed = await worker.fetch(new Request(base+"/mcp",{method:"OPTIONS",headers:{Origin:base}}));
  assert.equal(allowed.status,204); assert.equal(allowed.headers.get("access-control-allow-origin"),base);
  assert.equal((await post(rpc("ping"),{Origin:"https://other.example"})).status,403);
});
test("bounded input and errors",async()=>{
  assert.equal((await post("x".repeat(256*1024+1))).status,413);
  assert.equal((await post("not JSON")).status,400);
  const bad = [
    {rankings:Array.from({length:33},()=>[]),k:1},
    {rankings:[["x","x"]],k:1},
    {rankings:[["x"],["x"]],source_groups:["a","b"],k:1},
    {rankings:Array.from({length:5},()=>Array.from({length:512},(_,i)=>String(i))),k:1},
    {rankings:[[]],k:1,c:null},
    {rankings:[[]],k:1,backend:"https://unrelated.example"},
  ];
  for (const args of bad) assert.equal((await runTool(args)).isError,true);
});
test("sampled replay winners match independent complete RRF integer scores",async()=>{
  // With c=0 and rankings up to six IDs, 60/rank is always an integer.
  let seed=32588;
  const random=()=>{seed=(Math.imul(1664525,seed)+1013904223)>>>0; return seed/4294967296;};
  for(let n=0;n<64;n++) {
    const lists=Array.from({length:6},(_,i)=>{
      const result=Array.from({length:6},(_,j)=>String(i%2)+"-"+j);
      for(let k=5;k>0;k--){let j=Math.floor(random()*(k+1));[result[j],result[k]]=[result[k],result[j]];}
      return result.slice(0,Math.floor(random()*7));
    });
    const scores=new Map();
    for(const row of lists) row.forEach((x,i)=>scores.set(x,(scores.get(x)||0)+60/(i+1)));
    const k=1+Math.floor(random()*6);
    const expected=[...scores.keys()].sort((a,b)=>scores.get(b)-scores.get(a)||(a<b?-1:1)).slice(0,k);
    const out=await runTool({rankings:lists,k,c:0,page_size:1,source_groups:["0","1","0","1","0","1"]});
    assert.deepEqual(out.structuredContent.ids,expected);
  }
});
