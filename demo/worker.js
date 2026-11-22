importScripts("replay.js");
self.onmessage=event=>{
  try {
    const {input,k,page}=event.data;
    const scoped=replayRankings(input,k,page,true);
    const flat=replayRankings(input,k,page,false);
    if(JSON.stringify(scoped.ids)!==JSON.stringify(flat.ids))throw Error("Method disagreement.");
    self.postMessage({scoped,flat});
  } catch(error){self.postMessage({error:error.message});}
};
