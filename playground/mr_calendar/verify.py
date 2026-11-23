"""Run the reproducible Mr Calendar workstation checks and record evidence."""

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw

from live import Aperture


def require(condition, description):
    if not condition:
        raise RuntimeError(description)


def aperture():
    def frame(levels):
        image = Image.new("RGB", (768, 432), "#242424")
        draw = ImageDraw.Draw(image)
        for index, level in enumerate(levels):
            draw.rectangle((192 * index + 40, 25, 192 * index + 165, 105),
                           fill=(level,) * 3)
        return image

    focus = Aperture()
    focus.measure(frame([80, 80, 80, 80]))
    overview, _, evidence, tiles = focus.measure(frame([220, 170, 120, 80]))
    require(evidence["focus"]["tiers"][0] == ["native", "medium", "medium", "overview"],
            "Expected tier ordering in the authored motion fixture")
    require([image.size for _, image in tiles] == [(192, 144), (96, 72), (96, 72)],
            "Incorrect tile sizes")
    require(overview.size == (192, 108), "Missing overview")
    focus = Aperture()
    focus.measure(Image.new("RGB", (768, 432), "black"))
    overview, _, _, tiles = focus.measure(Image.new("RGB", (768, 432), "white"))
    require(overview.size == (384, 216) and not tiles, "Global cut should widen overview")
    return {"nominal_overview": [192, 108], "cut_overview": [384, 216],
            "fixture_tiers": evidence["focus"]["tiers"][0]}


def command(args):
    subprocess.run(args, check=True, timeout=300,
                   env={**os.environ, "OPENBLAS_NUM_THREADS": "1"})


def gpu_report(path):
    data = json.loads(path.read_text())
    runs = data["runs"]
    require(len(runs) == 45 and sum(row["frames"] for row in runs) == 10800,
            "Incomplete GPU matrix")
    require(all(row["text_candidates_correct"] == row["packets"] for row in runs),
            "Closed-set label mismatch")
    require(data["audio"]["cue_seconds"] == [.4, .9, 1.4], "Unexpected PCM cue times")
    observed = [r for r in runs if r["resolution"] == [1920, 1080] and r["mode"] == "observed"]
    require(len(observed) == 3, "Missing 1080p observed runs")
    full = [r for r in runs if r["resolution"] == [1920, 1080] and r["mode"] == "full"]
    hybrid = [r for r in runs if r["resolution"] == [1920, 1080] and r["mode"] == "hybrid"]
    return {"renderer": data["renderer"], "matrix_frames": 10800,
            "1080p_observed_10ms_budget_held": all(not r["frames_over_10ms"] for r in observed),
            "1080p_observed_over_10ms": sum(r["frames_over_10ms"] for r in observed),
            "1080p_full_over_10ms": sum(r["frames_over_10ms"] for r in full),
            "1080p_observed_max_p95_ms": max(r["frame_ms_p95"] for r in observed),
            "1080p_packet_byte_reduction": 1 - sum(r["packet_json_bytes"] for r in observed)
            / sum(r["packet_json_bytes"] for r in hybrid)}


def read_stream(path, clock):
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    require(rows and all(a[clock] <= b[clock] for a, b in zip(rows, rows[1:])),
            "Empty or unordered timeline")
    return rows


def multiplex_report(path, cue_delta, offset):
    rows = read_stream(path, "pts_us")
    audio = [r for r in rows if r["track"] == "audio"]
    require(len(audio) == 240 and all(a["end_us"] == b["pts_us"] for a, b in zip(audio, audio[1:])),
            "Audio did not keep 240 contiguous 50 ms windows")
    caption = next(r["pts_us"] for r in rows if r["track"] == "caption")
    cue = next(r["pts_us"] for r in rows if r["data"].get("cue") == "score_dip_start")
    require(caption == 4800000 + offset and cue - caption == cue_delta,
            "Cue/caption alignment changed")
    require(sum(r["track"] == "video" for r in rows) > 0, "Missing video")
    return {"tracks": dict(Counter(r["track"] for r in rows)), "cue_minus_caption_us": cue_delta}


def live_report(path, minimum_sources):
    rows = read_stream(path, "t")
    video = [row for row in rows if row["track"] == "video"]
    counts = Counter(row["source_kind"] for row in rows if row["track"] == "audio")
    sources = Counter(row["source"] for row in rows if row["track"] == "audio")
    require(len(video) >= 5 and len(sources) == minimum_sources and min(sources.values()) >= 5,
            "Missing live video samples or audio inputs")
    return {"video": len(video), "audio_by_kind": counts,
            "audio_windows_per_source": sorted(sources.values()),
            "tiers": sorted({tier for row in video for line in row["aperture"]["focus"]["tiers"]
                             for tier in line})}


def gateway():
    prompt = {"model": "auto", "stream": False, "messages": [
        {"role": "user", "content": "Reply with the single word READY."}]}
    payload = json.dumps(prompt).encode()
    results = {}
    for path in ("/api/chat", "/v1/chat/completions"):
        request = Request("http://127.0.0.1:11437" + path, data=payload,
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=120) as response:
            data = json.load(response)
            message = data["message"] if path == "/api/chat" else data["choices"][0]["message"]
            require(message["content"].strip() == "READY", f"Unexpected reply on {path}")
            require(response.headers.get("X-LOCI-Execution") == "single", "Missing route trace")
            results[path] = {"model": response.headers.get("X-LOCI-Model"),
                             "lane": response.headers.get("X-LOCI-Lane")}
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rtx-device", type=int, default=0)
    parser.add_argument("--arc-device", type=int, default=2)
    parser.add_argument("--live", action="store_true", help="Exercise local Firefox and the YouTube playback sink")
    parser.add_argument("--mic", action="append", default=[], help="Opt in to a named microphone for the live fixture")
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    workspace = args.workspace.resolve()
    bench = workspace / "playground/vision-hearing/benchmark.py"
    timeline = workspace / "mr-calendar/media_timeline.py"
    live = Path(__file__).with_name("live.py")
    output = args.output.resolve()
    results = {}

    def record(name, check):
        began = time.monotonic()
        try:
            results[name] = {"status": "pass", **check()}
        except Exception as error:
            results[name] = {"status": "fail", "error": str(error)}
        results[name]["elapsed_s"] = round(time.monotonic() - began, 3)
        print(name, results[name]["status"], flush=True)

    record("aperture", aperture)
    record("text_gateway", gateway)
    for name, index, expect in (("rtx", args.rtx_device, "NVIDIA"), ("arc", args.arc_device, "BMG")):
        def check_gpu():
            artifact = output / f"gpu-{name}.json"
            command([sys.executable, str(bench), "--device", str(index), "--expect", expect,
                     "--output", str(artifact)])
            return gpu_report(artifact)
        record(name, check_gpu)

    def check_mux():
        video = output / "shader-packets.jsonl"
        command([sys.executable, str(bench), "--device", str(args.rtx_device), "--expect", "NVIDIA",
                 "--stream", "--frames", "1440", "--output", str(video)])
        reports = {}
        for variant, delta, offset in (("minor-early", -600000, 0), ("minor-late", 600000, 0),
                                       ("major-early", -600000, 125000)):
            path = output / f"{variant}.jsonl"
            command([sys.executable, str(timeline), str(video), str(path), "--variant", variant,
                     "--audio-offset-ms", str(offset / 1000)])
            reports[variant] = multiplex_report(path, delta, offset)
        return reports

    record("offline_timeline", check_mux)

    def check_live(name, options, source, count):
        attempts = []
        for number in (1, 2):
            path = output / f"{name}-{number}.jsonl"
            log = output / f"{name}-{number}.stderr"
            try:
                with path.open("x") as stream, log.open("x") as errors:
                    subprocess.run([sys.executable, str(live), "--frames", "32", *options,
                                    "--", source], stdout=stream, stderr=errors,
                                   check=True, timeout=100)
                metrics = live_report(path, count)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                attempts.append({"attempt": number, "status": "fail", "log": log.name,
                                 "error": str(error)})
                continue
            attempts.append({"attempt": number, "status": "pass"})
            return {"attempts": attempts, **metrics}
        raise RuntimeError(f"Both capture attempts failed: {attempts}")

    if args.live:
        record("live_playback", lambda: check_live("playback", ["--audio", "--start", "95"],
                                                   "-nxb3VCH7Vw", 1))
    else:
        results["live_playback"] = {"status": "skipped", "reason": "Enable with --live"}
    if args.mic:
        options = [field for mic in args.mic for field in ("--mic", mic)]
        record("live_microphones", lambda: check_live("microphones", options, "fixture",
                                                      len(set(args.mic))))
    else:
        results["live_microphones"] = {"status": "skipped", "reason": "Select via --mic"}
    results["voice"] = {"status": "unverified", "cargo_on_path": bool(shutil.which("cargo")),
                        "nvcc_on_path": bool(shutil.which("nvcc")),
                        "reason": "No native full-duplex checkpoint was run"}
    results["semantic_forecast"] = {"status": "unverified",
                                    "reason": "No held-out benchmark or causal scheduler yet"}
    results["independent_reproduction"] = {"status": "unverified",
                                           "reason": "Single workstation and operator"}
    files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(output.iterdir()) if path.is_file()}
    sources = [Path(__file__), live, bench, bench.with_name("scene.frag"), timeline]
    data = {"recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "code_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources},
            "checks": results, "sha256": files}
    (output / "report.json").write_text(json.dumps(data, indent=2) + "\n")
    print(output / "report.json")
    return int(any(result["status"] == "fail" for result in results.values()))


if __name__ == "__main__":
    sys.exit(main())
