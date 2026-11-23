"""Multiplex shader or tracked-object packets and MusicSympRev on media time."""

import argparse
import heapq
import json
import math
from pathlib import Path
import wave

import numpy as np
from voice_spectrum import analyze_frame


def event(track, start, data, end=None):
    return {"track": track, "pts_us": start, "end_us": end, "data": data}


def video_events(path):
    last = -1
    with path.open(encoding="utf-8") as source:
        for line in source:
            packet = json.loads(line)
            timestamp = round(packet["t"] * 1_000_000)
            if timestamp < 0 or timestamp <= last:
                raise ValueError("Video packet times must be nonnegative and strictly increasing")
            last = timestamp
            yield event("video", timestamp, packet)


def audio_events(path, offset_us):
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2 or source.getnchannels() != 1:
            raise ValueError("Expected mono 16-bit PCM")
        rate = source.getframerate()
        block = round(rate * .05)
        start = 0
        while raw := source.readframes(block):
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768
            end = start + len(samples)
            spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
            frequencies = np.fft.rfftfreq(len(samples), 1 / rate)
            total = spectrum.sum()
            yield event("audio", offset_us + round(start * 1_000_000 / rate), {
                "rms": round(float(np.sqrt(np.mean(samples ** 2))), 6),
                "spectral_centroid_hz": round(float(spectrum @ frequencies / total), 1) if total else None,
                "voice_spectrum": analyze_frame(samples, rate),
                "source": "measured_pcm",
            }, offset_us + round(end * 1_000_000 / rate))
            start = end


def score_events(variant, offset_us):
    def at(seconds):
        return offset_us + round(seconds * 1_000_000)

    cue = variant["cue"]
    scene = variant["scene"]
    score = variant["known_score"]
    yield event("score", offset_us, {"source": "authored", **score})
    yield from sorted([
        event("score", at(cue["score_dip_start_seconds"]), {
            "source": "authored", "cue": "score_dip_start",
        }),
        event("caption", at(scene["line_at_seconds"]), {
            "source": "authored", "text": scene["line"],
            "written_stress_pattern": scene["written_stress_pattern"],
        }),
    ], key=lambda packet: packet["pts_us"])


def multiplex(tracks):
    yield from heapq.merge(*tracks, key=lambda packet: packet["pts_us"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path, help="Video JSON Lines with source timestamp t in seconds")
    parser.add_argument("output", type=Path)
    parser.add_argument("--features", type=Path,
                        default=Path(__file__).with_name("musicsymprev") / "features.json")
    parser.add_argument("--variant", default="minor-early")
    parser.add_argument("--audio-offset-ms", type=float, default=0,
                        help="Position of the score and its authored caption on the video timeline")
    args = parser.parse_args()
    if not math.isfinite(args.audio_offset_ms):
        parser.error("audio offset must be finite")
    variants = json.loads(args.features.read_text(encoding="utf-8"))["qSympRev"]
    if args.variant not in variants:
        parser.error(f"unknown variant, available: {', '.join(sorted(variants))}")
    variant = variants[args.variant]
    offset = round(args.audio_offset_ms * 1000)
    tracks = (video_events(args.video), audio_events(args.features.parent / variant["audio"], offset),
              score_events(variant, offset))
    with args.output.open("x", encoding="utf-8") as output:
        for packet in multiplex(tracks):
            output.write(json.dumps(packet, ensure_ascii=False, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
