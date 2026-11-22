"use strict";
const $ = id => document.getElementById(id);
function example(){
  const lists=[],groups=[];
  for(let g=0;g<4;g++)for(let r=0;r<3;r++){
    groups.push("shard-"+g);
    lists.push(Array.from({length:64},(_,i)=>g+"/"+String(g===0?i:(i+17*r)%64).padStart(2,"0")));
  }
  $("input").value=JSON.stringify({groups,lists},null,1);
}
async function run(){
  const output=$("output");
  output.textContent="Checking ranked windows…";$("run").disabled=true;
  try{
    if($("input").value.length>2000000)throw Error("Input exceeds 2 MB. Use the Python release.");
    const input=JSON.parse($("input").value);
    const worker=new Worker("worker.js");
    const result=await new Promise((resolve,reject)=>{
      worker.onmessage=e=>{worker.terminate();e.data.error?reject(Error(e.data.error)):resolve(e.data);};
      worker.onerror=e=>{worker.terminate();reject(Error(e.message));};
      worker.postMessage({input,k:Number($("k").value),page:Number($("page").value)});
    });
    output.textContent="With shard map     "+result.scoped.read+" IDs\nWithout shard map  "+result.flat.read+" IDs\nFull input         "+result.scoped.total+" IDs\n\nExact ordered IDs  "+JSON.stringify(result.scoped.ids);
  }catch(e){output.textContent=e.message;}
  finally{$("run").disabled=false;}
}
$("example").onclick=()=>{example();run();};$("run").onclick=run;example();run();
