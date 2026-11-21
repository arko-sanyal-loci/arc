#!/usr/bin/env python3
"""Paired LOCI dynamic-compaction experiment on a synthetic long conversation."""
import json
from pathlib import Path
import sys,time,urllib.request,uuid,subprocess
from datetime import datetime,timezone
from math import sqrt

HERE=Path(__file__).resolve().parent; RAW=HERE/"raw"
sys.path.insert(0,str(HERE/"reference"))
from context_compaction import compact
ENDPOINTS={"arc_b70_vulkan":"http://127.0.0.1:8000","rtx_5060ti_cuda":"http://127.0.0.1:11435"}
MODELS={"8b":"qwen3-vl:8b-instruct","27b":"qwen3.6:27b-q4_K_M"}

def request(base,obj):
 req=urllib.request.Request(base+"/api/chat",data=json.dumps(obj).encode(),headers={"Content-Type":"application/json"})
 with urllib.request.urlopen(req,timeout=300) as r:return json.load(r)

def retrieve_nomic(query):
 docs=[
  {"id":"AT-7","text":"Fictional log record AT-7: sensor at 07:18 was exactly 18.4 C; filter changed to F-9 at 07:25; final shelf B3 at 07:40."},
  {"id":"AT-8","text":"Fictional record AT-8: sensor 18.9 C at 07:18; filter F-8; shelf C2."},
  {"id":"AT-9","text":"Fictional record AT-9: sensor 17.4 C at 08:18; filter F-9; shelf B4."},
  {"id":"AT-10","text":"Fictional record AT-10: sensor reading 18.4 C at 07:18; filter F-8 at 07:25; final shelf B3 at 07:40."},
  {"id":"AT-11","text":"Fictional unrelated delivery log: parcel Q-3 arrived at dock D and moved to shelf A1."},
 ]
 payload={"model":"nomic-embed-text","input":[*("search_document: "+d["text"] for d in docs),"search_query: "+query]}
 req=urllib.request.Request("http://127.0.0.1:11434/api/embed",data=json.dumps(payload).encode(),headers={"Content-Type":"application/json"})
 with urllib.request.urlopen(req,timeout=45) as r:vec=json.load(r)["embeddings"]
 q=vec[-1]; qn=sqrt(sum(x*x for x in q)); ranked=[]
 for d,v in zip(docs,vec[:-1]):ranked.append((sum(a*b for a,b in zip(q,v))/(qn*sqrt(sum(x*x for x in v))),d))
 return [{"score":score,**doc} for score,doc in sorted(ranked,reverse=True,key=lambda x:x[0])]

def fixture():
 msgs=[{"role":"system","content":"Answer from the fictional conversation evidence. Return concise JSON. Do not invent missing facts."}]
 fact="Fictional log: record ID AT-7 says exact measured sensor value at 07:18 was 18.4 C; filter changed at 07:25 was F-9; final shelf was B3 at 07:40. Preserve times and units exactly."
 for i in range(10):
  msgs.append({"role":"user","content":f"Earlier discussion {i}: " + (fact if i==0 else "Routine planning note; no sensor, filter, parcel, or shelf fact is stated here. ")+ ("The following generic maintenance planning context is included to exercise long-history compaction. "*13)})
  msgs.append({"role":"assistant","content":f"Acknowledged earlier discussion {i}. "+("Routine planning acknowledged; no exact values added. "*14)})
 msgs.append({"role":"user","content":"For record AT-7, give the exact sensor value and unit at 07:18, the filter at 07:25, and shelf at 07:40."})
 return msgs

def main():
 RAW.mkdir(exist_ok=True,parents=True); rid="context-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid.uuid4().hex[:8]
 original=fixture(); reduced=compact(original)
 ranked=retrieve_nomic(original[-1]["content"]); selected=ranked[0]
 compact_plus=[*reduced[:-1],{"role":"system","content":"Retrieved LOCI evidence (synthetic): "+selected["text"]},reduced[-1]]
 manifest={"run_id":rid,"created_utc":datetime.now(timezone.utc).isoformat(),"method":"frozen reference/context_compaction.py wrapper loading LOCI compressor from pinned Git commit, threshold 6500 chars; Nomic top-1 semantic retrieval from isolated synthetic corpus; direct Ollama chat","original_char_count":sum(len(m["content"]) for m in original),"compacted_char_count":sum(len(m["content"]) for m in reduced),"original_messages":len(original),"compacted_messages":len(reduced),"fact_in_original":any("18.4 C" in m["content"] for m in original),"fact_in_compacted":any("18.4 C" in m["content"] for m in reduced),"retrieved_top1_id":selected["id"],"retrieval_top_k":[{"id":r["id"],"cosine":r["score"]} for r in ranked],"retrieval_expected_top1":"AT-7","question":original[-1]["content"],"models":MODELS,"endpoints":ENDPOINTS,"privacy":"fictional data only"}
 (RAW/f"{rid}_context_fixture.json").write_text(json.dumps({"original_messages":original,"compacted_messages":reduced,"retrieval_candidates":ranked,"question":original[-1]},ensure_ascii=False,indent=2)+"\n")
 (RAW/f"{rid}_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
 rows=[]; ix=0
 for model_key,model in MODELS.items():
  for device,base in ENDPOINTS.items():
   for mode,msgs in (("full",original),("loci_compacted",reduced),("compacted_plus_nomic_retrieval",compact_plus)):
    ix+=1; start=time.time_ns(); row={"run_id":rid,"trial":ix,"model_key":model_key,"model":model,"device":device,"context_mode":mode,"messages":len(msgs),"input_chars":sum(len(m["content"]) for m in msgs),"retrieval_top1_id":selected["id"] if "retrieval" in mode else None,"started_utc":datetime.now(timezone.utc).isoformat()}
    try:
     res=request(base,{"model":model,"messages":msgs,"stream":False,"think":False,"options":{"num_ctx":8192,"num_predict":120,"temperature":0,"seed":8100+ix},"keep_alive":"2m"})
     row.update({"ok":True,"response":res.get("message",{}).get("content",""),"done_reason":res.get("done_reason"),"total_duration_ns":res.get("total_duration"),"load_duration_ns":res.get("load_duration"),"prompt_eval_count":res.get("prompt_eval_count"),"prompt_eval_duration_ns":res.get("prompt_eval_duration"),"eval_count":res.get("eval_count"),"eval_duration_ns":res.get("eval_duration"),"wall_duration_ns":time.time_ns()-start})
    except Exception as e:row.update({"ok":False,"error_type":type(e).__name__,"error":str(e)[:500],"wall_duration_ns":time.time_ns()-start})
    row["exact_fact_terms"]={t:(t.lower() in row.get("response","").lower()) for t in ("18.4","F-9","B3")}; rows.append(row); print(f"{ix}/12 {model_key} {device} {mode} ok={row['ok']}",flush=True)
 out=RAW/f"{rid}_trials.jsonl"; out.write_text("".join(json.dumps(x,ensure_ascii=False)+"\n" for x in rows)); print(json.dumps({"manifest":str(RAW/f"{rid}_manifest.json"),"trials":str(out),"fact_in_compacted":manifest["fact_in_compacted"],"retrieved_top1":selected["id"]}))

if __name__=="__main__":main()
