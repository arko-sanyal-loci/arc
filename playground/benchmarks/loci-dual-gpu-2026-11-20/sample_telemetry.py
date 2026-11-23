#!/usr/bin/env python3
"""Low-rate hardware/service snapshots; inaccessible telemetry stays null."""
import argparse,json,subprocess,time,urllib.request
from datetime import datetime,timezone
from pathlib import Path
import glob

BASES={"arc_b70_vulkan":"http://127.0.0.1:8000","rtx_5060ti_cuda":"http://127.0.0.1:11435"}
def read(path):
 try:return Path(path).read_text().strip()
 except (OSError,PermissionError):return None
def snap():
 energy_path=None
 for hp in glob.glob("/sys/class/drm/card0/device/hwmon/hwmon*/energy1_input"):
  if read(str(Path(hp).parent/"name"))=="xe":energy_path=hp;break
 d={"timestamp_utc":datetime.now(timezone.utc).isoformat(),"arc_b70_energy_uj":read(energy_path) if energy_path else None,"system_mem_available_kib":None}
 try:
  for line in Path("/proc/meminfo").read_text().splitlines():
   if line.startswith("MemAvailable:"):d["system_mem_available_kib"]=int(line.split()[1]);break
 except OSError:pass
 try:
  x=subprocess.check_output(["nvidia-smi","--query-gpu=name,memory.used,utilization.gpu,power.draw,temperature.gpu","--format=csv,noheader,nounits"],text=True,timeout=3).strip()
  d["nvidia_smi"]=x
 except Exception as e:d["nvidia_smi_error"]=type(e).__name__
 d["services"]={}
 for lane,base in BASES.items():
  try:
   with urllib.request.urlopen(base+"/api/ps",timeout=3) as r:d["services"][lane]=json.load(r).get("models",[])
  except Exception as e:d["services"][lane]={"error":type(e).__name__}
 return d
def main():
 p=argparse.ArgumentParser();p.add_argument("--duration-s",type=int,default=900);p.add_argument("--interval-s",type=float,default=5);p.add_argument("--output",type=Path,required=True);a=p.parse_args()
 start=time.monotonic();a.output.parent.mkdir(parents=True,exist_ok=True)
 with a.output.open("x") as f:
  while time.monotonic()-start<a.duration_s:
   t=time.monotonic();f.write(json.dumps(snap())+"\n");f.flush();time.sleep(max(0,a.interval_s-(time.monotonic()-t)))
 print(json.dumps({"samples":sum(1 for _ in a.output.open()),"output":str(a.output)}))
if __name__=="__main__":main()
