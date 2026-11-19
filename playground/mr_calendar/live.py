"""Capture a selected public YouTube video element and measure its focus regions."""

import argparse
import base64
from contextlib import ExitStack, contextmanager
import io
import json
import re
import subprocess
from threading import Thread
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import numpy as np
from PIL import Image


def post(url, payload=None, method=None, timeout=120):
    body = None if payload is None else json.dumps(payload).encode()
    request = Request(url, data=body, method=method, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as reply:
        return json.load(reply)


@contextmanager
def browser():
    driver = "http://127.0.0.1:4447"
    created = post(driver + "/session", {"capabilities": {"alwaysMatch": {
        "browserName": "firefox",
        "moz:firefoxOptions": {"args": ["-headless"],
                               "prefs": {"media.autoplay.default": 0}},
    }}})["value"]
    base = driver + "/session/" + created["sessionId"]
    try:
        yield base
    finally:
        try:
            post(base, method="DELETE")
        except HTTPError as error:
            if error.code != 404:
                raise


class Aperture:
    """Allocate detail by tile priority while always retaining the overview."""

    def __init__(self):
        self.previous = None
        self.heat = None
        self.focus = None

    def measure(self, frame):
        preview = frame.resize((192, 108))
        gray = np.asarray(preview.convert("L"), dtype=float)
        motion = np.zeros_like(gray) if self.previous is None else np.abs(gray - self.previous) / 255
        self.previous = gray
        tiles = motion.reshape(3, 36, 4, 48).mean(axis=(1, 3))
        self.heat = tiles if self.heat is None else .55 * tiles + .45 * self.heat
        texture = gray.reshape(3, 36, 4, 48).std(axis=(1, 3)) / 255
        priority = np.where(texture >= .035, self.heat, 0)
        hottest = np.unravel_index(np.argmax(priority), priority.shape)
        global_motion = float(np.mean(tiles))
        if global_motion >= .09:
            self.focus = None
        elif priority[hottest] >= .025:
            if self.focus is None or priority[hottest] > priority[self.focus] * 1.15:
                self.focus = hottest
        elif self.focus is not None and priority[self.focus] < .012:
            self.focus = None

        crop = None
        rect = None
        if self.focus is not None:
            row, col = self.focus
            rect = [col / 4, row / 3, (col + 1) / 4, (row + 1) / 3]
            crop = frame.crop(tuple(round(coord * size)
                         for coord, size in zip(rect, (frame.width, frame.height) * 2)))
        ranked = sorted(np.ndindex(priority.shape),
                        key=lambda tile: (tile == self.focus, priority[tile]), reverse=True)
        tiers = [["overview"] * 4 for _ in range(3)]
        enhancements = []
        if global_motion >= .09:
            preview = frame.resize((384, 216))
        else:
            for row, col in ranked[:4]:
                if priority[row, col] < .012:
                    break
                scale = 1 if (row, col) == self.focus else .5
                box = (round(col * frame.width / 4), round(row * frame.height / 3),
                       round((col + 1) * frame.width / 4), round((row + 1) * frame.height / 3))
                image = frame.crop(box)
                if scale < 1:
                    image = image.resize((max(1, image.width // 2), max(1, image.height // 2)))
                tiers[row][col] = "native" if scale == 1 else "medium"
                enhancements.append(((int(row), int(col)), image))
        region = frame if crop is None else crop
        evidence = {
            "motion": {"frame_mean": round(global_motion, 4),
                       "heat": self.heat.round(4).tolist()},
            "focus": {"roi_xyxy": rect, "fidelity": "native_tile" if crop else "coarse_scene",
                      "texture": texture.round(4).tolist(), "priority": priority.round(4).tolist(),
                      "tiers": tiers, "overview_size": list(preview.size)},
            "color": {"region_mean_rgb": np.asarray(region).mean(axis=(0, 1)).round().astype(int).tolist()},
        }
        return preview, crop, evidence, enhancements


@contextmanager
def audio_monitor(source):
    windows = []
    process = subprocess.Popen([
        "parec", f"--device={source}", "--format=s16le",
        "--rate=24000", "--channels=1", "--latency-msec=50",
    ], stdout=subprocess.PIPE)

    def collect():
        while raw := process.stdout.read(2400):
            samples = np.frombuffer(raw, dtype="<i2").astype(float) / 32768
            rms = float(np.sqrt(np.mean(samples ** 2)))
            windows.append((time.monotonic(), round(rms, 6)))

    reader = Thread(target=collect, daemon=True)
    reader.start()
    try:
        yield windows
    finally:
        process.terminate()
        process.wait(timeout=5)
        reader.join(timeout=5)
        process.stdout.close()
    if not windows:
        raise RuntimeError(f"Captured no PCM from {source}")


def jpeg(image):
    encoded = io.BytesIO()
    image.save(encoded, format="JPEG", quality=90)
    return base64.b64encode(encoded.getvalue()).decode()


def vision(image, question):
    result = post("http://127.0.0.1:11435/api/chat", {
        "model": "qwen3-vl:8b-instruct", "stream": False,
        "messages": [{"role": "user", "content": question,
                      "images": [jpeg(image)]}],
        "options": {"num_predict": 140, "temperature": 0, "num_ctx": 4096},
    })
    return result["message"]["content"]


def capture(base, video_id, frames, start, audio=False):
    def script(source):
        return post(base + "/execute/sync", {"script": source, "args": []})["value"]

    if video_id == "fixture":
        html = """<video autoplay muted width=768 height=432></video><script>
const cv=document.createElement('canvas'); cv.width=768; cv.height=432;
const ctx=cv.getContext('2d'); const began=performance.now();
function draw() {
  const t=(performance.now()-began)/1000;
  ctx.fillStyle='#20242b'; ctx.fillRect(0,0,768,432);
  const x=Math.floor(t/3)%2 ? 418 : 42;
  ctx.fillStyle='#28aafa'; ctx.fillRect(x+Math.sin(t*14)*29,83,72,40);
  ctx.fillStyle='white'; ctx.font='16px monospace'; ctx.fillText('ORBIT-742',x-20,71);
  requestAnimationFrame(draw);
}
draw(); document.querySelector('video').srcObject=cv.captureStream(30);
</script>"""
        url = "data:text/html," + quote(html)
    else:
        url = f"https://www.youtube.com/watch?v={video_id}&t={int(start)}s"
    post(base + "/url", {"url": url})
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if script("return [...document.querySelectorAll('video')].some(v => v.readyState >= 2)"):
            break
        time.sleep(.5)
    else:
        status = script("return document.querySelector('video')?.error?.code ?? null")
        raise RuntimeError(f"The video element did not decode a frame (media error: {status})")
    video = post(base + "/element", {"using": "css selector", "value": "video"})["value"]
    element_id = next(iter(video.values()))
    script("const v=document.querySelector('video'); "
           f"v.muted={json.dumps(not audio)}; v.play().catch(()=>{{}});")
    time.sleep(2)
    aperture = Aperture()
    previous = -1.0
    for _ in range(frames):
        t0 = script("return document.querySelector('video').currentTime")
        capture = post(base + "/element/" + element_id + "/screenshot")["value"]
        t1 = script("return document.querySelector('video').currentTime")
        frame = Image.open(io.BytesIO(base64.b64decode(capture))).convert("RGB")
        if t1 > t0 and t1 > previous:
            previous = t1
            preview, crop, detail, enhancements = aperture.measure(frame)
            yield (t0 + t1) / 2, round(t1 - t0, 3), frame, preview, crop, detail, enhancements
        time.sleep(.1)
    if previous < 0:
        status = script("const v=document.querySelector('video'); "
                        "return {paused:v.paused, ready:v.readyState, time:v.currentTime};")
        raise RuntimeError(f"Video playback did not advance: {status}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_id", nargs="?")
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--frames", type=int, default=18)
    parser.add_argument("--ask", action="store_true")
    parser.add_argument("--audio", action="store_true",
                        help="Measure RMS on the dedicated loci_media_test PulseAudio sink")
    parser.add_argument("--mic", action="append", default=[], metavar="SOURCE",
                        help="Capture a named microphone as a separate track. Repeat to add inputs.")
    parser.add_argument("--list-mics", action="store_true", help="List the OS microphone sources")
    parser.add_argument("--image-packets", action="store_true",
                        help="Include JPEG data for the scene overview and enhanced tiles")
    args = parser.parse_args()
    if args.list_mics or args.mic:
        names = subprocess.check_output(["pactl", "list", "short", "sources"], text=True)
        microphones = {line.split()[1] for line in names.splitlines()
                       if line.strip() and not line.split()[1].endswith(".monitor")}
        if args.list_mics:
            print("\n".join(sorted(microphones)))
            return
        if set(args.mic) - microphones or len(set(args.mic)) > 4:
            parser.error("Select up to four attached microphones from --list-mics")
    valid_id = args.video_id == "fixture" or re.fullmatch(r"[a-zA-Z0-9_-]{11}", args.video_id or "")
    if not valid_id or not 0 <= args.start <= 86400 or not 1 <= args.frames <= 120:
        parser.error("Provide a YouTube ID, bounded seek time, and 1–120 samples")
    best = None
    anchors = []
    packets = []
    sources = {source: "microphone" for source in args.mic}
    if args.audio:
        sources["loci_media_test.monitor"] = "playback"
    with browser() as session, ExitStack() as stack:
        inputs = {source: stack.enter_context(audio_monitor(source)) for source in sources}
        for t, bracket, native, preview, crop, detail, enhancements in capture(
                session, args.video_id, args.frames, args.start, audio=args.audio):
            anchors.append((time.monotonic(), t))
            packet = {"track": "video", "t": t, "timestamp_bracket_s": bracket, "aperture": detail}
            if args.image_packets:
                packet["overview_jpeg"] = jpeg(preview)
                packet["enhancements"] = [{"tile": tile, "jpeg": jpeg(image)}
                                          for tile, image in enhancements]
            packets.append(packet)
            if crop is not None:
                best = t, native, preview, crop, detail
    if len(anchors) >= 2:
        wall, media = zip(*anchors)
        for source, windows in inputs.items():
            for wall_end, rms in windows:
                if wall[0] <= wall_end <= wall[-1]:
                    packets.append({"track": "audio", "t": float(np.interp(wall_end, wall, media)),
                                    "source": source, "source_kind": sources[source], "rms": rms,
                                    "window_ms": 50, "alignment": "approximate_read_time"})
    for packet in sorted(packets, key=lambda row: row["t"]):
        print(json.dumps(packet), flush=True)
    if args.ask and best:
        t, native, preview, crop, detail = best
        prompt = "Describe visible action and fine details you can actually see. Flag uncertainty. Do not infer dialogue or identities."
        answers = {key: vision(image, prompt) for key, image in
                   (("coarse", preview), ("focused", crop), ("native_reference", native))}
        print(json.dumps({"track": "review", "t": t, "roi_xyxy": detail["focus"]["roi_xyxy"],
                          "answers": answers}), flush=True)


if __name__ == "__main__":
    main()
