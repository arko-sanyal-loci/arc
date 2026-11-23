"""Generate an original MusicSympRev timing and harmony experiment."""

import argparse
import json
from pathlib import Path
import wave

import numpy as np

RATE = 24000
BPM = 100
BEAT = 60 / BPM
LINE_TIME = 4.8
LINE = "You look like hell. I'm glad you made it back."
PARTS = {
    "strings": "Slow attack, sustained low-mid tones, light vibrato",
    "woodwind": "Soft, rounded lead with a quieter upper harmonic",
    "brass": "Short, bright harmonic accents",
    "percussion": "Sharp beat attacks with descending low-frequency tails",
}


def tone(midi: int, time: np.ndarray, harmonics: tuple, vibrato: bool = False) -> np.ndarray:
    frequency = 440 * 2 ** ((midi - 69) / 12)
    phase = 2 * np.pi * frequency * time
    if vibrato:
        phase += 0.32 * np.sin(2 * np.pi * 5 * time)
    return sum(level * np.sin(phase * h) for h, level in enumerate(harmonics, 1))


def render(mode: str, dip: float) -> tuple[np.ndarray, dict]:
    time = np.arange(12 * RATE) / RATE
    third = 64 if mode == "major" else 63
    chord = (48, third, 67)
    strings = sum(tone(note, time, (1, .31, .13), True) for note in chord) / 3
    strings *= np.minimum(time / .35, 1)
    music = .33 * strings
    for index in range(20):
        start = round(index * BEAT * RATE)
        active = time[:min(round(BEAT * RATE), len(time) - start)]
        beat = np.exp(-22 * active) * np.sin(2 * np.pi * (65 * active - 22 * active**2))
        wind = tone((72, third + 12, 67, 72)[index % 4], active, (1, .13))
        wind *= np.minimum(active / .08, 1) * np.minimum((BEAT - active) / .12, 1)
        brass = tone(55, active, (1, .6, .28, .15)) * np.exp(-9 * active)
        music[start:start + len(active)] += (
            (.30 if index % 4 == 0 else .18) * beat
            + .10 * wind + (.11 if index % 4 == 0 else 0) * brass
        )
    # Apply a score dip without moving the scene, notes, beat grid, or instruments.
    amount = np.clip((time - dip) / .04, 0, 1) * np.clip((dip + 1.2 - time) / .04, 0, 1)
    music *= (1 - .88 * amount) * np.minimum((12 - time) / .15, 1)
    before = music[round((LINE_TIME - .15) * RATE):round(LINE_TIME * RATE)]
    rms = float(np.sqrt(np.mean(before ** 2)))
    record = {
        "known_score": {"tempo_bpm": BPM, "meter": "4/4", "mode": mode,
                        "chord_midi": list(chord), "orchestral_synth_parts": PARTS},
        "scene": {"line_at_seconds": LINE_TIME, "line": LINE,
                  "written_stress_pattern": "you LOOK | like HELL | i'm GLAD | you MADE | it BACK"},
        "cue": {"score_dip_start_seconds": dip, "cue_minus_line_seconds": round(dip - LINE_TIME, 2),
                "mixed_score_rms_in_final_150ms_before_line": round(rms, 5)},
    }
    return music, record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    variants = {}
    for name, mode, dip in [
        ("minor-early", "minor", 4.2),
        ("minor-late", "minor", 5.4),
        ("major-early", "major", 4.2),
    ]:
        music, variants[name] = render(mode, dip)
        with wave.open(str(args.output / f"{name}.wav"), "wb") as output:
            output.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
            output.writeframes(np.rint(np.clip(music, -1, 1) * 32767).astype("<i2").tobytes())
        variants[name]["audio"] = f"{name}.wav"
    data = {
        "schema": "musicsymprev.v1",
        "scope": "Original synthesized score, fixed scene. No spoken audio, measured listener ratings, or claim of real-time recognition.",
        "qSympRev": variants,
        "iSympRev": {
            "status": "hypothesis",
            "timing": "An early dip may make space for concern. A late dip may let the same line read as banter before the mood settles.",
            "mode": "Changing the third with the timing fixed can change the shade of the moment. Test which description listeners hear."
        },
    }
    (args.output / "features.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
