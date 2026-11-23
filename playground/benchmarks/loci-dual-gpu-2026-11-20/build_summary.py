#!/usr/bin/env python3
"""Derive one aggregate report from the preserved raw benchmark records."""
import glob,json,statistics,datetime
from pathlib import Path
HERE=Path(__file__).resolve().parent;RAW=HERE/"raw"
def read_jsonl(paths):return [json.loads(l) for p in paths for l in Path(p).read_text().splitlines() if l]
def p90(v):return statistics.quantiles(v,n=10,method="inclusive")[8] if len(v)>1 else (v[0] if v else None)
def main():
 trials=read_jsonl(glob.glob(str(RAW/"matrix-*_trials.jsonl"))); models={}
 for model in ("4b","8b","26b_moe","27b"):
  models[model]={}
  for device in ("arc_b70_vulkan","rtx_5060ti_cuda"):
   rows=[r for r in trials if r["model_key"]==model and r["device"]==device]
   t=[r["total_duration_ns"]/1e9 for r in rows if r.get("total_duration_ns")]
   rate=[r["eval_count"]/(r["eval_duration_ns"]/1e9) for r in rows if r.get("eval_count") and r.get("eval_duration_ns")]
   cov=[r.get("scoring",{}).get("term_coverage",0) for r in rows]
   models[model][device]={"n":len(rows),"successes":sum(bool(r.get("ok")) for r in rows),"blank_final":sum(not r.get("response","").strip() for r in rows),"rubric_passes":sum(v==1 for v in cov),"rubric_n":len(cov),"mean_required_term_coverage":statistics.mean(cov) if cov else None,"median_total_s":statistics.median(t) if t else None,"p90_total_s":p90(t),"median_output_tokens_per_second":statistics.median(rate) if rate else None}
 shaders={}
 for card in ("rtx","arc"):
  files=glob.glob(str(RAW/f"shader-{card}-20261120.json"))
  if not files:continue
  d=json.loads(Path(files[0]).read_text());shaders[card]={"renderer":d["renderer"],"gl_version":d["gl_version"],"runs":len(d["runs"]),"1080p":{}}
  for mode in ("full","coarse","hybrid","adaptive","observed"):
   rs=[r for r in d["runs"] if r["resolution"]==[1920,1080] and r["mode"]==mode]
   shaders[card]["1080p"][mode]={"median_p95_ms":statistics.median(r["frame_ms_p95"] for r in rs),"frames_over_10ms_total":sum(r["frames_over_10ms"] for r in rs),"measured_frames_total":sum(r["frames"] for r in rs),"median_gpu_readback_bytes":statistics.median(r["gpu_readback_bytes"] for r in rs),"median_packet_json_bytes":statistics.median(r["packet_json_bytes"] for r in rs)}
 ctx_files=glob.glob(str(RAW/"context-*_manifest.json"));context=None
 if ctx_files:
  m=json.loads(Path(ctx_files[-1]).read_text());t=read_jsonl([str(Path(ctx_files[-1]).with_name(Path(ctx_files[-1]).name.replace("_manifest.json","_trials.jsonl")))])
  context={"manifest":Path(ctx_files[-1]).name,"trial_count":len(t),"all_calls_ok":all(r.get("ok") for r in t),"all_trials_retain_exact_terms":all(all(r["exact_fact_terms"].values()) for r in t),"original_chars":m["original_char_count"],"compacted_chars":m["compacted_char_count"],"original_messages":m["original_messages"],"compacted_messages":m["compacted_messages"],"prompt_eval_tokens_full":next(r["prompt_eval_count"] for r in t if r["model_key"]=="8b" and r["device"]=="arc_b70_vulkan" and r["context_mode"]=="full"),"prompt_eval_tokens_compacted":next(r["prompt_eval_count"] for r in t if r["model_key"]=="8b" and r["device"]=="arc_b70_vulkan" and r["context_mode"]=="loci_compacted"),"nomic_top1_id":m["retrieved_top1_id"],"nomic_expected_top1":m["retrieval_expected_top1"],"nomic_rankings":m["retrieval_top_k"]}
 ret_files=glob.glob(str(RAW/"retrieval-*_scoring.json"));retrieval=json.loads(Path(ret_files[-1]).read_text()) if ret_files else None
 rs=read_jsonl(glob.glob(str(RAW/"reasoning-*_trials.jsonl")));reasoning={"trials":len(rs),"final_nonempty":sum(r.get("response_nonempty",False) for r in rs),"ended_at_length":sum(r.get("done_reason")=="length" for r in rs),"eval_tokens":sorted(set(r.get("eval_count") for r in rs)),"thinking_char_count_only_min":min((r.get("thinking_chars_not_saved") or 0 for r in rs),default=None),"thinking_char_count_only_max":max((r.get("thinking_chars_not_saved") or 0 for r in rs),default=None)} if rs else None
 tele=[]
 tpath=RAW/"telemetry-15min.jsonl"
 if tpath.exists():tele=read_jsonl([str(tpath)])
 nmem=[];nutil=[];npower=[];ntemp=[];ram=[]
 for x in tele:
  ram.append(x.get("system_mem_available_kib"))
  try:
   z=[a.strip() for a in x["nvidia_smi"].split(",")];nmem.append(float(z[1]));nutil.append(float(z[2]));npower.append(float(z[3]));ntemp.append(float(z[4]))
  except Exception:pass
 duration=(datetime.datetime.fromisoformat(tele[-1]["timestamp_utc"])-datetime.datetime.fromisoformat(tele[0]["timestamp_utc"])).total_seconds() if len(tele)>1 else 0
 energy=(int(tele[-1]["arc_b70_energy_uj"])-int(tele[0]["arc_b70_energy_uj"])) if len(tele)>1 and tele[0].get("arc_b70_energy_uj") and tele[-1].get("arc_b70_energy_uj") else None
 out={"built_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),"model_matrix":{"trials":len(trials),"http_successes":sum(bool(r.get("ok")) for r in trials),"blank_final_answers":sum(not r.get("response","").strip() for r in trials),"models":models},"shader":shaders,"context_compaction_and_retrieval":context,"retrieval_ablation":retrieval,"reasoning_mode":reasoning,"resource_sampling":{"samples":len(tele),"duration_seconds":duration,"start_utc":tele[0]["timestamp_utc"] if tele else None,"end_utc":tele[-1]["timestamp_utc"] if tele else None,"nvidia_memory_mib_min_max":[min(nmem),max(nmem)] if nmem else None,"nvidia_util_percent_min_max":[min(nutil),max(nutil)] if nutil else None,"nvidia_power_w_max":max(npower) if npower else None,"nvidia_temperature_c_min_max":[min(ntemp),max(ntemp)] if ntemp else None,"host_available_ram_gib_min":min(ram)/1024/1024 if ram else None,"arc_energy_counter_delta_uj":energy,"arc_utilization":None,"arc_utilization_note":"unavailable: intel_gpu_top denied PMU access without CAP_PERFMON"}}
 path=HERE/"raw"/"aggregate_summary.json";path.write_text(json.dumps(out,indent=2)+"\n");print(path)
if __name__=="__main__":main()
