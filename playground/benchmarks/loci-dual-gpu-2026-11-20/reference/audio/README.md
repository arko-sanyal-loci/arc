# Audio reference module

This directory contains the reusable audio side of the media benchmark:

- `voice_spectrum.py` measures offline PCM features without inferring speaker identity.
- `media_timeline.py` multiplexes video packets, measured audio features, and authored score cues.
- `musicsymprev.py` generates the synthetic score fixture and `features.json`.

The module accepts only local 16-bit PCM WAV input. It does not record from a
microphone, identify a speaker, or claim emotion recognition. Install NumPy
before running it:

```bash
python3 -m pip install numpy
python3 musicsymprev.py fixtures
```

The video reference remains in `../vision_hearing/`; the dual-GPU runners and
raw evidence remain in the parent benchmark directory.
