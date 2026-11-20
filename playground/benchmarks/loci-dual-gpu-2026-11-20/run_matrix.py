#!/usr/bin/env python3
"""Reproducible synthetic LOCI model × GPU matrix. All prompts are fictional."""
import base64
import itertools
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request
import uuid
from datetime import datetime, timezone

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
ENDPOINTS = {"arc_b70_vulkan": "http://127.0.0.1:8000", "rtx_5060ti_cuda": "http://127.0.0.1:11435"}
MODELS = {"4b": "qwen3.5:4b-q4_K_M", "8b": "qwen3-vl:8b-instruct",
          "26b_moe": "gemma4:26b-a4b-it-q4_K_M", "27b": "qwen3.6:27b-q4_K_M"}

def api(base, path, obj=None, timeout=240):
    data = None if obj is None else json.dumps(obj).encode()
    req = urllib.request.Request(base + path, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)

def gpu_snapshot():
    try:
        out = subprocess.check_output(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu", "--format=csv,noheader"], text=True, timeout=3)
        return {"nvidia_smi": out.strip()}
    except Exception as exc:
        return {"nvidia_smi_error": type(exc).__name__}

def make_image():
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (640, 360), (23, 31, 45))
    d = ImageDraw.Draw(img)
    d.ellipse((210, 108, 326, 224), fill=(25, 95, 242), outline=(100, 170, 255), width=4)
    d.rectangle((440, 34, 610, 88), fill=(5, 8, 14))
    fpath = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
    font = ImageFont.truetype(fpath, 29)
    d.text((450, 43), "B174", font=font, fill=(255,255,255))
    d.text((24, 300), "SYNTHETIC FIXTURE | object blue-disc", fill=(220,220,220))
    out = HERE / "raw" / "synthetic_shader_like_fixture.png"
    img.save(out)
    return base64.b64encode(out.read_bytes()).decode(), str(out.name)

def case_suite(image):
    return [
      ("recall_exact", "Fictional event log: 07:10 parcel R-42 arrived at dock C. 07:18 sensor read 18.4 C. 07:25 technician changed filter F-9. 07:40 parcel R-42 moved to shelf B3. What exact sensor value, filter, and final location are recorded? Return JSON with sensor_c, filter, location."),
      ("temporal_conflict", "Fictional versioned notes: at 09:00 status=amber, source=panel A; at 09:07 status=green, source=panel A; at 09:12 panel B reports amber but is marked lower priority. State current value, source, time, and preserve the superseded value. Return compact JSON."),
      ("shader_packet", "Interpret this synthetic shader packet. Separate measured from authored fields and report the packet's time, XY, RGB, label, authored Z and incidence. Do not infer physical depth. Packet: {\"t\":1.25,\"object\":\"blue-disc\",\"xy\":[0.418,0.431],\"z_authored\":2.0,\"incidence_authored\":43.2,\"blur_authored\":0.7,\"color_rgb\":[24,91,235],\"text\":\"B174\",\"moving\":true}."),
      ("audio_features", "Synthetic 24 kHz mono feature report: 10 ms RMS onset detector found events at 0.40, 0.90, and 1.40 seconds; dominant spectral centroid by event was 440, 660, and 440 Hz. Report measured facts separately from any mood interpretation, and state whether emotion can be established from these measurements alone."),
      ("grounded_abstention", "Fictional evidence contains only: frame at 00:01.2 shows a blue disc at normalized x=0.42; soundtrack feature has a 440 Hz onset at 0.40 s. Does the person feel afraid? Answer only what the evidence supports and identify what is unknown."),
      ("vision_fixture", "Read the synthetic image. Return JSON with visible label, object color, approximate normalized center x/y, and list what cannot be inferred. Do not guess emotion.", image),
    ]

def main():
    RAW.mkdir(parents=True, exist_ok=True)
    run_id = "matrix-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    image, image_file = make_image()
    cases = case_suite(image)
    # Rotate order across rounds; one model/device at a time to avoid VRAM contention.
    selected_keys = os.environ.get("MODEL_KEYS")
    selected_models = {k:v for k,v in MODELS.items() if not selected_keys or k in selected_keys.split(",")}
    matrix = list(itertools.product(selected_models.items(), ENDPOINTS.items()))
    schedule = []
    for rep in range(1, 3):
        round_items = matrix if rep == 1 else list(reversed(matrix))
        for (model_key, model), (device, base) in round_items:
            for case in cases:
                schedule.append((rep, model_key, model, device, base, case))
    meta = {"run_id":run_id,"created_utc":datetime.now(timezone.utc).isoformat(),"purpose":"synthetic model×GPU paired comparison","models":selected_models,"model_inventory":MODELS,"endpoints":ENDPOINTS,"rounds":2,"cases":[c[0] for c in cases],"context_tokens":4096,"max_output_tokens":160,"reasoning_mode":False,"image_fixture":image_file,"privacy":"fictional fixtures only; no user or private account content"}
    (RAW / f"{run_id}_manifest.json").write_text(json.dumps(meta,indent=2)+"\n")
    (RAW / f"{run_id}_prompts.json").write_text(json.dumps([{"case":c[0],"prompt":c[1],"image":image_file if len(c)>2 else None} for c in cases],indent=2)+"\n")
    outpath = RAW / f"{run_id}_trials.jsonl"
    with outpath.open("x") as out:
        for ix,(rep,mkey,model,device,base,(case,prompt,*extra)) in enumerate(schedule,1):
            user = {"role":"user","content":prompt}
            if extra: user["images"]=[extra[0]]
            payload={"model":model,"messages":[{"role":"system","content":"Answer only from supplied fictional evidence. Be concise. Separate observation from inference. Return requested JSON where applicable."},user],"stream":False,"think":False,"options":{"num_ctx":4096,"num_predict":160,"temperature":0,"seed":7100+ix},"keep_alive":"2m"}
            before=time.time_ns()
            record={"run_id":run_id,"trial":ix,"round":rep,"model_key":mkey,"model":model,"device":device,"case":case,"started_utc":datetime.now(timezone.utc).isoformat(),"gpu_before":gpu_snapshot()}
            try:
                result=api(base,"/api/chat",payload,timeout=300)
                record.update({"ok":True,"response":result.get("message",{}).get("content",""),"done":result.get("done"),"done_reason":result.get("done_reason"),"total_duration_ns":result.get("total_duration"),"load_duration_ns":result.get("load_duration"),"prompt_eval_count":result.get("prompt_eval_count"),"prompt_eval_duration_ns":result.get("prompt_eval_duration"),"eval_count":result.get("eval_count"),"eval_duration_ns":result.get("eval_duration"),"wall_duration_ns":time.time_ns()-before})
            except Exception as exc:
                record.update({"ok":False,"error_type":type(exc).__name__,"error":str(exc)[:500],"wall_duration_ns":time.time_ns()-before})
            record["gpu_after"]=gpu_snapshot()
            out.write(json.dumps(record,ensure_ascii=False)+"\n"); out.flush()
            print(f"{ix}/{len(schedule)} {mkey} {device} {case} ok={record['ok']} wall={record['wall_duration_ns']/1e9:.1f}s",flush=True)
    print(json.dumps({"manifest":str(RAW / f"{run_id}_manifest.json"),"trials":str(outpath),"count":len(schedule)}))

if __name__ == "__main__": main()
