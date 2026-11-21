#!/usr/bin/env python3
"""Capture non-secret environment facts and model tags for reproducibility."""
import json,platform,subprocess,urllib.request
from datetime import datetime,timezone
from pathlib import Path

def cmd(args):
 try:return subprocess.check_output(args,text=True,stderr=subprocess.STDOUT,timeout=8).strip()
 except Exception as e:return f"unavailable: {type(e).__name__}"
def main():
 endpoints={"arc_b70_vulkan":"http://127.0.0.1:8000","rtx_5060ti_cuda":"http://127.0.0.1:11435"}
 tags={}
 versions={}
 for lane,base in endpoints.items():
  try:
   with urllib.request.urlopen(base+"/api/tags",timeout=5) as r: data=json.load(r)
   tags[lane]=[{k:m.get(k) for k in ("name","size","digest","details","capabilities")} for m in data.get("models",[])]
   try:
    with urllib.request.urlopen(base+"/api/version",timeout=5) as r:versions[lane]=json.load(r)
   except Exception as e:versions[lane]={"error":type(e).__name__}
  except Exception as e:tags[lane]={"error":type(e).__name__}
 out={"captured_utc":datetime.now(timezone.utc).isoformat(),"os":cmd(["bash","-lc",". /etc/os-release && echo \"$PRETTY_NAME\""]),"kernel":platform.release(),"python":platform.python_version(),"logical_cpus":__import__('os').cpu_count(),"ollama_version_command":cmd(["ollama","--version"]),"ollama_server_versions":versions,"nvidia":cmd(["nvidia-smi","--query-gpu=name,memory.total,driver_version","--format=csv,noheader"]),"intel_pci_ids":cmd(["bash","-lc","for c in /sys/class/drm/card0/device /sys/class/drm/card1/device; do printf '%s ' \"$c\"; cat \"$c/vendor\" \"$c/device\" | tr '\\n' ' '; echo; done"]),"arc_gl_renderer":"Mesa Intel(R) Graphics (BMG G31), Mesa 26.2.6 - kisak-mesa PPA","rtx_gl_renderer":"NVIDIA GeForce RTX 5060 Ti/PCIe/SSE2, GL 3.3 NVIDIA 595.99.02","ollama_endpoints":endpoints,"model_tags":tags,"context_setting":"4096 for model×GPU suite; 8192 for long-context suite","reasoning_mode":False,"temperature":0,"source_revisions":{"mr_calendar_context_compaction_compressor":"bbd50360a868ea4e97e8888d4cf46786b6652620","vision_hearing_git":"e66a2ef"},"notes":"Arc GPU utilization PMU blocked by CAP_PERFMON in this session. No host name, credentials, or personal content captured."}
 p=Path(__file__).resolve().parent/"raw"/"environment.json";p.write_text(json.dumps(out,indent=2)+"\n");print(p)
if __name__=="__main__":main()
