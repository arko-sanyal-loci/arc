#!/usr/bin/env python3
"""Test exact-source selection after LOCI-style top-k semantic retrieval."""
import json,urllib.request,uuid
from datetime import datetime,timezone
from math import sqrt
from pathlib import Path
HERE=Path(__file__).resolve().parent;RAW=HERE/"raw"
MODELS={"4b":"qwen3.5:4b-q4_K_M","8b":"qwen3-vl:8b-instruct","26b_moe":"gemma4:26b-a4b-it-q4_K_M","27b":"qwen3.6:27b-q4_K_M"}
ENDPOINTS={"arc_b70_vulkan":"http://127.0.0.1:8000","rtx_5060ti_cuda":"http://127.0.0.1:11435"}
DOCS=[
 {"id":"AT-7","text":"Fictional log record AT-7: sensor at 07:18 was exactly 18.4 C; filter changed to F-9 at 07:25; final shelf B3 at 07:40."},
 {"id":"AT-8","text":"Fictional record AT-8: sensor 18.9 C at 07:18; filter F-8; shelf C2."},
 {"id":"AT-9","text":"Fictional record AT-9: sensor 17.4 C at 08:18; filter F-9; shelf B4."},
 {"id":"AT-10","text":"Fictional record AT-10: sensor reading 18.4 C at 07:18; filter F-8 at 07:25; final shelf B3 at 07:40."},
 {"id":"AT-11","text":"Fictional unrelated delivery log: parcel Q-3 arrived at dock D and moved to shelf A1."}]
QUERY="From record AT-7 only, return the exact sensor value, filter and shelf with times. If AT-7 is absent, say unavailable; never borrow details from another record."
def embed():
 payload={"model":"nomic-embed-text","input":[*("search_document: "+d['text'] for d in DOCS),"search_query: "+QUERY]}
 req=urllib.request.Request("http://127.0.0.1:11434/api/embed",data=json.dumps(payload).encode(),headers={"Content-Type":"application/json"})
 with urllib.request.urlopen(req,timeout=45) as r:v=json.load(r)["embeddings"]
 q=v[-1];qn=sqrt(sum(x*x for x in q));rank=[]
 for d,x in zip(DOCS,v[:-1]):rank.append((sum(a*b for a,b in zip(q,x))/(qn*sqrt(sum(y*y for y in x))),d))
 return [{"score":s,**d} for s,d in sorted(rank,key=lambda z:z[0],reverse=True)]
def chat(base,model,records,seed):
 content="Requested record: AT-7\nRetrieved evidence candidates:\n"+"\n".join(json.dumps(x,ensure_ascii=False) for x in records)+"\n\n"+QUERY
 p={"model":model,"messages":[{"role":"system","content":"Treat each record ID as a distinct primary source. Do not merge facts across IDs. If the requested ID is absent, abstain."},{"role":"user","content":content}],"stream":False,"think":False,"options":{"num_ctx":4096,"num_predict":100,"temperature":0,"seed":seed},"keep_alive":"2m"}
 req=urllib.request.Request(base+"/api/chat",data=json.dumps(p).encode(),headers={"Content-Type":"application/json"})
 with urllib.request.urlopen(req,timeout=300) as r:return json.load(r)
def main():
 RAW.mkdir(parents=True,exist_ok=True); rank=embed(); rid="retrieval-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid.uuid4().hex[:8]
 (RAW/f"{rid}_manifest.json").write_text(json.dumps({"run_id":rid,"created_utc":datetime.now(timezone.utc).isoformat(),"retriever":"Nomic embeddings; cosine top-k from five fictional records","query":QUERY,"ranking":[{"id":x['id'],"score":x['score']} for x in rank],"target_id":"AT-7","models":MODELS,"endpoints":ENDPOINTS,"privacy":"fictional data only"},indent=2)+"\n")
 rows=[];i=0
 for mk,model in MODELS.items():
  for device,base in ENDPOINTS.items():
   for k in (1,4):
    i+=1; subset=rank[:k]; started=datetime.now(timezone.utc); ans=chat(base,model,subset,9100+i)
    response=ans.get("message",{}).get("content","")
    row={"run_id":rid,"trial":i,"model_key":mk,"model":model,"device":device,"retrieved_k":k,"retrieved_ids":[x['id'] for x in subset],"response":response,"done":ans.get("done"),"done_reason":ans.get("done_reason"),"total_duration_ns":ans.get("total_duration"),"load_duration_ns":ans.get("load_duration"),"prompt_eval_count":ans.get("prompt_eval_count"),"prompt_eval_duration_ns":ans.get("prompt_eval_duration"),"eval_count":ans.get("eval_count"),"eval_duration_ns":ans.get("eval_duration"),"started_utc":started.isoformat(),"mentions_target_id":"AT-7" in response,"mentions_correct_filter":"F-9" in response,"mentions_wrong_filter":"F-8" in response}
    row["correct_answer"]=("AT-7" in row["retrieved_ids"] and row["mentions_target_id"] and row["mentions_correct_filter"] and not row["mentions_wrong_filter"])
    rows.append(row);print(f"{i}/16 {mk} {device} top{k} correct={row['correct_answer']}",flush=True)
 out=RAW/f"{rid}_trials.jsonl";out.write_text("".join(json.dumps(x,ensure_ascii=False)+"\n" for x in rows));print(json.dumps({"manifest":str(RAW/f"{rid}_manifest.json"),"trials":str(out)}))
if __name__=="__main__":main()
