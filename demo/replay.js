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

if(typeof module!=="undefined") module.exports={replayRankings};
