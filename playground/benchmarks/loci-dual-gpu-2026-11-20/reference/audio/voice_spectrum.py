"""Offline voice-spectrum measurements; this does not identify a speaker."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import wave

import numpy as np


BAND_EDGES_HZ = (80, 250, 500, 1000, 2000, 4000, 8000)


def analyze_frame(samples: np.ndarray, sample_rate_hz: int) -> dict:
    """Return pitch, periodicity and coarse spectral-band power for one PCM frame."""
    if sample_rate_hz < 8000:
        raise ValueError("sample rate must be at least 8000 Hz")
    values = np.asarray(samples, dtype=np.float64)
    if values.ndim == 2:
        values = values.mean(axis=1)
    if values.ndim != 1 or not len(values):
        raise ValueError("samples must be a non-empty mono or multichannel array")
    if not np.isfinite(values).all():
        raise ValueError("samples must be finite")
    values = np.clip(values, -1.0, 1.0)
    rms = float(np.sqrt(np.mean(values * values)))
    spectrum_input = values * np.hanning(len(values)) if len(values) > 1 else values
    magnitude = np.abs(np.fft.rfft(spectrum_input))
    frequencies = np.fft.rfftfreq(len(spectrum_input), 1 / sample_rate_hz)
    power = magnitude * magnitude
    total_power = float(power.sum())
    centroid = (float(magnitude @ frequencies / magnitude.sum())
                if float(magnitude.sum()) > 0 else None)
    cumulative = np.cumsum(power)
    roll_index = int(np.searchsorted(cumulative, total_power * 0.85)) if total_power else 0
    rolloff = float(frequencies[min(roll_index, len(frequencies) - 1)]) if total_power else None
    bands = []
    nyquist = sample_rate_hz / 2
    for low, high in zip(BAND_EDGES_HZ, BAND_EDGES_HZ[1:]):
        clipped_high = min(high, nyquist)
        if clipped_high <= low:
            continue
        mask = (frequencies >= low) & (frequencies < clipped_high)
        band_power = float(power[mask].sum())
        bands.append({"low_hz": low, "high_hz": clipped_high,
                      "relative_power": round(band_power / total_power, 6)
                      if total_power else 0.0})

    pitch_hz = None
    periodicity = 0.0
    centered = values - float(values.mean())
    if rms >= 1e-5 and len(centered) >= max(3, int(sample_rate_hz * 0.01)):
        centered *= np.hanning(len(centered))
        fft_size = 1 << (2 * len(centered) - 1).bit_length()
        transformed = np.fft.rfft(centered, n=fft_size)
        autocorrelation = np.fft.irfft(transformed * transformed.conjugate(), n=fft_size)
        autocorrelation = autocorrelation[:len(centered)]
        if autocorrelation[0] > 0:
            min_lag = max(1, int(sample_rate_hz / 350))
            max_lag = min(len(autocorrelation) - 1, int(sample_rate_hz / 60))
            if max_lag >= min_lag:
                segment = autocorrelation[min_lag:max_lag + 1] / autocorrelation[0]
                peak_index = int(np.argmax(segment))
                periodicity = max(0.0, min(1.0, float(segment[peak_index])))
                lag = min_lag + peak_index
                if 0 < peak_index < len(segment) - 1:
                    left, center, right = segment[peak_index - 1:peak_index + 2]
                    denominator = left - 2 * center + right
                    if denominator:
                        lag += float(0.5 * (left - right) / denominator)
                if periodicity >= 0.30 and lag > 0:
                    pitch_hz = round(sample_rate_hz / lag, 2)

    return {
        "rms": round(rms, 6),
        "rms_dbfs": round(20 * math.log10(max(rms, 1e-8)), 2),
        "spectral_centroid_hz": round(centroid, 1) if centroid is not None else None,
        "spectral_rolloff_85_hz": round(rolloff, 1) if rolloff is not None else None,
        "periodic_pitch_hz": pitch_hz,
        "periodicity_score": round(periodicity, 4),
        "relative_band_power": bands,
    }


def analyze_audio(samples: np.ndarray, sample_rate_hz: int, *,
                  frame_ms: float = 40, hop_ms: float = 20) -> dict:
    """Measure overlapping windows and summarize the periodic-pitch range."""
    if not math.isfinite(frame_ms) or not math.isfinite(hop_ms) or frame_ms <= 0 or hop_ms <= 0:
        raise ValueError("frame_ms and hop_ms must be positive and finite")
    values = np.asarray(samples, dtype=np.float64)
    if values.ndim == 2:
        values = values.mean(axis=1)
    if values.ndim != 1 or not len(values):
        raise ValueError("samples must be a non-empty mono or multichannel array")
    frame_size = max(1, round(sample_rate_hz * frame_ms / 1000))
    hop_size = max(1, round(sample_rate_hz * hop_ms / 1000))
    frames = []
    for start in range(0, len(values), hop_size):
        chunk = values[start:start + frame_size]
        if len(chunk) < sample_rate_hz * 0.01:
            break
        frames.append({"start_seconds": round(start / sample_rate_hz, 6),
                       "end_seconds": round((start + len(chunk)) / sample_rate_hz, 6),
                       **analyze_frame(chunk, sample_rate_hz)})
    pitches = [frame["periodic_pitch_hz"] for frame in frames
               if frame["periodic_pitch_hz"] is not None]
    return {
        "sample_rate_hz": sample_rate_hz,
        "frame_ms": frame_ms,
        "hop_ms": hop_ms,
        "speaker_identity_inferred": False,
        "frames": frames,
        "periodic_pitch_summary_hz": {
            "min": round(float(np.percentile(pitches, 10)), 2) if pitches else None,
            "median": round(float(np.median(pitches)), 2) if pitches else None,
            "max": round(float(np.percentile(pitches, 90)), 2) if pitches else None,
        },
    }


def read_wave(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2 or source.getcomptype() != "NONE":
            raise ValueError("expected uncompressed 16-bit PCM WAV")
        rate = source.getframerate()
        channels = source.getnchannels()
        raw = source.readframes(source.getnframes())
    if channels < 1:
        raise ValueError("WAV must have at least one channel")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768
    if channels > 1:
        samples = samples.reshape(-1, channels)
    return samples, rate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path, help="local 16-bit PCM WAV file")
    parser.add_argument("--frame-ms", type=float, default=40)
    parser.add_argument("--hop-ms", type=float, default=20)
    parser.add_argument("--output", type=Path, help="write JSON to a new file; default prints it")
    args = parser.parse_args()
    try:
        samples, rate = read_wave(args.wav)
        result = analyze_audio(samples, rate, frame_ms=args.frame_ms, hop_ms=args.hop_ms)
        encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as output:
                output.write(encoded)
        else:
            print(encoded, end="")
    except (OSError, ValueError, wave.Error) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
