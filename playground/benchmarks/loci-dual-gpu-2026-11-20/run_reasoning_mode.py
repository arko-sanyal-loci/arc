#!/usr/bin/env python3
"""Check final-answer completion with optional thinking enabled; never save thoughts."""
import json,urllib.request,uuid
from datetime import datetime,timezone
from pathlib import Path
HERE=Path(__file__).resolve().parent;RAW=HERE/"raw"
MODELS={"4b":"qwen3.5:4b-q4_K_M","26b_moe":"gemma4:26b-a4b-it-q4_K_M","27b":"qwen3.6:27b-q4_K_M"}
ENDPOINTS={"arc_b70_vulkan":"http://127.0.0.1:8000","rtx_5060ti_cuda":"http://127.0.0.1:11435"}
PROMPT=("Use the fictional records to validate the conflict rule and produce only final JSON with current_status, source, time, preserved_older_status, and a one-sentence limitation. "
"Rules: for same source choose later timestamp; a lower-priority source does not override a higher-priority source. Records: panel A, 09:00, amber; panel A, 09:07, green; panel B, 09:12, amber, lower priority. Do not infer real-world meaning.")
def main():
 RAW.mkdir(parents=True,exist_ok=True);rid="reasoning-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid.uuid4().hex[:8];rows=[];i=0
 for mk,model in MODELS.items():
  for device,base in ENDPOINTS.items():
   i+=1;p={"model":model,"messages":[{"role":"user","content":PROMPT}],"stream":False,"think":True,"options":{"num_ctx":4096,"num_predict":1024,"temperature":0,"seed":10100+i},"keep_alive":"2m"}
   req=urllib.request.Request(base+"/api/chat",data=json.dumps(p).encode(),headers={"Content-Type":"application/json"})
   started=datetime.now(timezone.utc)
   try:
    with urllib.request.urlopen(req,timeout=360) as r:ans=json.load(r)
    msg=ans.get("message",{});content=msg.get("content","");thinking=msg.get("thinking","")
    row={"run_id":rid,"trial":i,"model_key":mk,"model":model,"device":device,"think":True,"num_ctx":4096,"num_predict":1024,"response":content,"response_nonempty":bool(content.strip()),"thinking_chars_not_saved":len(thinking),"done":ans.get("done"),"done_reason":ans.get("done_reason"),"total_duration_ns":ans.get("total_duration"),"load_duration_ns":ans.get("load_duration"),"prompt_eval_count":ans.get("prompt_eval_count"),"prompt_eval_duration_ns":ans.get("prompt_eval_duration"),"eval_count":ans.get("eval_count"),"eval_duration_ns":ans.get("eval_duration"),"started_utc":started.isoformat(),"error":None}
   except Exception as e:row={"run_id":rid,"trial":i,"model_key":mk,"model":model,"device":device,"think":True,"response":"","response_nonempty":False,"thinking_chars_not_saved":None,"error":type(e).__name__+": "+str(e)[:300],"started_utc":started.isoformat()}
   rows.append(row);print(f"{i}/6 {mk} {device} final_nonempty={row['response_nonempty']} done={row.get('done_reason')} eval={row.get('eval_count')}",flush=True)
 manifest={"run_id":rid,"created_utc":datetime.now(timezone.utc).isoformat(),"test":"think=true final answer completion; 1024-token generation budget","prompt":PROMPT,"models":MODELS,"endpoints":ENDPOINTS,"privacy":"fictional evidence only","reasoning_privacy":"internal thinking text intentionally not stored; only character count is recorded"}
 (RAW/f"{rid}_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n");out=RAW/f"{rid}_trials.jsonl";out.write_text("".join(json.dumps(x,ensure_ascii=False)+"\n" for x in rows));print(json.dumps({"manifest":str(RAW/f"{rid}_manifest.json"),"trials":str(out)}))
if __name__=="__main__":main()
