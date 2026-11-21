#!/usr/bin/env python3
"""Deterministic scoring and summary for the raw synthetic matrix."""
import json
from pathlib import Path
import statistics
import sys

P = Path(__file__).resolve().parent / "raw"
TERMS = {
 "recall_exact": [["18.4"], ["F-9"], ["B3"]],
 "temporal_conflict": [["green"], ["panel A"], ["09:07"], ["amber"]],
 "shader_packet": [["B174"], ["0.418"], ["0.431"], ["2.0"], ["43.2"]],
 "audio_features": [["0.40"], ["0.90"], ["1.40"], ["440"], ["660"], ["emotion", "mood"]],
 "grounded_abstention": [["0.42"], ["unknown", "does not support", "cannot determine", "not enough evidence"]],
 "vision_fixture": [["B174"], ["blue"]],
}

def main(path):
 rows=[json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
 groups={}
 for r in rows:
  if not r.get("ok"): continue
  answer=r.get("response","").lower()
  required=TERMS[r["case"]]
  matched=[[term for term in choices if term.lower() in answer] for choices in required]
  r["scoring"]={"matched_terms":matched,"required_term_alternatives":required,"term_coverage":sum(bool(group) for group in matched)/len(required)}
  groups.setdefault((r["model_key"],r["device"],r["case"]),[]).append(r)
 result=[]
 for (model,device,case),rs in sorted(groups.items()):
  times=[r["total_duration_ns"]/1e9 for r in rs if r.get("total_duration_ns")]
  tps=[r["eval_count"]/(r["eval_duration_ns"]/1e9) for r in rs if r.get("eval_count") and r.get("eval_duration_ns")]
  result.append({"model_key":model,"device":device,"case":case,"n":len(rs),"successes":sum(r["scoring"]["term_coverage"]==1 for r in rs),"mean_term_coverage":statistics.mean(r["scoring"]["term_coverage"] for r in rs),"median_total_s":statistics.median(times) if times else None,"median_output_tok_s":statistics.median(tps) if tps else None})
 with Path(path).open("w") as f:
  for r in rows: f.write(json.dumps(r,ensure_ascii=False)+"\n")
 out=P/(Path(path).stem.replace("_trials","_summary")+".json")
 out.write_text(json.dumps({"scoring":"lowercase exact required-term coverage; screening metric, not semantic quality","groups":result},indent=2)+"\n")
 print(out)

if __name__=="__main__": main(sys.argv[1])
