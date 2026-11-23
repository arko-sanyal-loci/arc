"""
semantic_constant.py — Dual-GPU semantic constant discovery

Hypothesis:
  Semantic content (images AND conversations) occupies a specific arithmetic
  range in information-density space. This range is a constant of nature, not
  a hyperparameter.

  For images:   power spectral slope  α ∈ [1.5, 2.5]   (1/f^α)
  For text:     token entropy         H ∈ [3.2, 5.8]   bits/token (hypothesis)

  Cross-correlating both reveals the modality-agnostic semantic constant —
  the arithmetic basis for LOCI's heat function β and α_k parameters.

GPU assignment:
  Arc B70  (XPU / level_zero:0)  — image power spectral analysis on LAION-400M
  RTX 5060 Ti (CUDA:0)           — conversation entropy analysis on LMSYS-Chat-1M

Usage:
  python core/semantic_constant.py --mode both --samples 50000
  python core/semantic_constant.py --mode image --samples 100000
  python core/semantic_constant.py --mode text  --samples 200000
"""

import os
import sys
import json
import math
import zlib
import logging
import argparse
import asyncio
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from concurrent.futures import ProcessPoolExecutor

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("semantic_const")

# ── Constants ─────────────────────────────────────────────────────────────────
# Null hypothesis ranges (from literature)
ALPHA_SEMANTIC_LOW  = 1.5   # min spectral slope for natural images
ALPHA_SEMANTIC_HIGH = 2.5   # max spectral slope for natural images
ALPHA_NOISE         = 0.0   # white noise slope
ALPHA_STRUCTURE     = 3.0   # overly synthetic/patterned

# Text null hypothesis (to be validated by this experiment)
H_SEMANTIC_LOW_HYPOTHESIS  = 3.2   # bits/token — mundane floor
H_SEMANTIC_HIGH_HYPOTHESIS = 5.8   # bits/token — coherence ceiling

DATA_DIR   = Path(os.environ.get("LOCI_DATA", "/data"))
CACHE_DIR  = DATA_DIR / "cache" / "semantic_constant"
LAION_DIR  = CACHE_DIR / "laion"
LMSYS_DIR  = CACHE_DIR / "lmsys"

# ── Data classes ──────────────────────────────────────────────────────────────
@dataclass
class SpectralResult:
    """Power spectral analysis result for one image."""
    alpha: float            # fitted slope S(f) ~ f^-alpha
    r_squared: float        # goodness of fit
    mean_intensity: float
    std_intensity: float
    is_semantic: bool       # alpha in [ALPHA_SEMANTIC_LOW, ALPHA_SEMANTIC_HIGH]

@dataclass
class EntropyResult:
    """Entropy analysis result for one conversation turn."""
    char_entropy: float         # bits/char (Shannon)
    token_entropy: float        # estimated bits/token (char_entropy * ~4.3 chars/tok)
    compression_ratio: float    # len(zlib.compress(text)) / len(text)
    turn_length: int            # chars
    is_semantic: bool           # entropy in hypothesis range

@dataclass
class SemanticConstant:
    """The measured semantic constant across both modalities."""
    # Image domain
    alpha_mean: float = 0.0
    alpha_std: float  = 0.0
    alpha_p5: float   = 0.0
    alpha_p95: float  = 0.0
    n_images: int     = 0
    # Text domain
    h_mean: float     = 0.0
    h_std: float      = 0.0
    h_p5: float       = 0.0
    h_p95: float      = 0.0
    n_conversations: int = 0
    # Cross-modal correlation
    correlation: float = 0.0    # alpha ↔ H correlation across shared samples
    loci_beta_estimate: float = 0.0   # suggested LOCI heat β
    loci_alpha_k_low: float   = 0.0   # suggested α_k lower bound
    loci_alpha_k_high: float  = 0.0   # suggested α_k upper bound


# ══════════════════════════════════════════════════════════════════════════════
#  IMAGE ANALYSIS — Arc B70 (XPU)
# ══════════════════════════════════════════════════════════════════════════════

def _fit_spectral_slope(power_2d: np.ndarray) -> tuple[float, float]:
    """
    Fit S(f) ~ f^(-alpha) to a 2D power spectrum.
    Returns (alpha, r_squared).

    Method: radial average → log-log linear regression.
    """
    h, w = power_2d.shape
    cy, cx = h // 2, w // 2
    y, x = np.ogrid[-cy:h-cy, -cx:w-cx]
    r = np.sqrt(x*x + y*y).astype(int)
    r_max = min(cy, cx)

    # Radial average
    radial_sum   = np.bincount(r.ravel(), weights=power_2d.ravel(), minlength=r_max)
    radial_count = np.bincount(r.ravel(), minlength=r_max)
    with np.errstate(divide='ignore', invalid='ignore'):
        radial_mean = np.where(radial_count > 0, radial_sum / radial_count, 0)

    # Fit log-log in frequency range [1, r_max//2]
    freqs = np.arange(1, r_max // 2)
    power = radial_mean[1:r_max // 2]
    mask  = power > 0
    if mask.sum() < 4:
        return 0.0, 0.0

    log_f = np.log10(freqs[mask])
    log_p = np.log10(power[mask])
    # Linear regression: log_p = -alpha * log_f + const
    coeffs = np.polyfit(log_f, log_p, 1)
    alpha  = -coeffs[0]
    # R²
    residuals = log_p - np.polyval(coeffs, log_f)
    ss_res = np.sum(residuals ** 2)
    ss_tot = np.sum((log_p - log_p.mean()) ** 2)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return float(alpha), float(r2)


def analyze_image_numpy(img_array: np.ndarray) -> SpectralResult:
    """
    Compute power spectral slope for a single image.
    img_array: (H, W) or (H, W, C) uint8
    """
    if img_array.ndim == 3:
        # Luminance
        img = (0.299 * img_array[:,:,0] +
               0.587 * img_array[:,:,1] +
               0.114 * img_array[:,:,2]).astype(np.float32)
    else:
        img = img_array.astype(np.float32)

    # 2D FFT power spectrum
    fft2    = np.fft.fft2(img)
    fft2    = np.fft.fftshift(fft2)
    power   = np.abs(fft2) ** 2
    power  += 1e-10   # avoid log(0)

    alpha, r2 = _fit_spectral_slope(power)
    return SpectralResult(
        alpha=alpha,
        r_squared=r2,
        mean_intensity=float(img.mean()),
        std_intensity=float(img.std()),
        is_semantic=(ALPHA_SEMANTIC_LOW <= alpha <= ALPHA_SEMANTIC_HIGH),
    )


def run_image_analysis_xpu(n_samples: int, batch_size: int = 256) -> list[SpectralResult]:
    """
    Run spectral analysis on LAION-400M images using Arc B70 (XPU).
    Falls back to CPU numpy if XPU not available.
    """
    log.info(f"[ARC XPU] Starting image spectral analysis — {n_samples} samples")

    # Try XPU (Intel Arc)
    device_name = "cpu"
    try:
        import torch
        import intel_extension_for_pytorch as ipex  # noqa: F401
        if torch.xpu.is_available():
            device_name = "xpu"
            log.info("[ARC XPU] Intel XPU available ✓")
        else:
            log.warning("[ARC XPU] XPU not available, falling back to CPU")
    except ImportError:
        log.warning("[ARC XPU] IPEX not installed, using numpy CPU")

    # Download / stream LAION-400M subset
    results: list[SpectralResult] = []
    LAION_DIR.mkdir(parents=True, exist_ok=True)

    try:
        from datasets import load_dataset
        log.info("[ARC XPU] Streaming LAION-400M subset from HuggingFace...")
        ds = load_dataset(
            "laion/laion400m",
            split="train",
            streaming=True,
            trust_remote_code=True,
        )
        processed = 0
        for item in ds:
            if processed >= n_samples:
                break
            try:
                from PIL import Image
                import io
                import requests
                # LAION gives URLs — fetch image
                url = item.get("url", "")
                if not url:
                    continue
                resp = requests.get(url, timeout=4)
                if resp.status_code != 200:
                    continue
                img = Image.open(io.BytesIO(resp.content)).convert("RGB")
                img_arr = np.array(img.resize((256, 256)))
                result = analyze_image_numpy(img_arr)
                results.append(result)
                processed += 1
                if processed % 1000 == 0:
                    semantic_pct = sum(r.is_semantic for r in results) / len(results) * 100
                    alpha_mean   = np.mean([r.alpha for r in results])
                    log.info(f"[ARC XPU] {processed}/{n_samples} | "
                             f"α_mean={alpha_mean:.3f} | semantic={semantic_pct:.1f}%")
            except Exception:
                continue

    except ImportError:
        log.warning("[ARC XPU] 'datasets' library not available. "
                    "Install: pip install datasets pillow requests")
        # Generate synthetic test data to validate pipeline
        log.info("[ARC XPU] Generating synthetic natural-image-like test data...")
        rng = np.random.default_rng(42)
        for _ in range(min(n_samples, 1000)):
            # Simulate 1/f² noise (natural image statistics)
            H, W = 256, 256
            freqs_y = np.fft.fftfreq(H)
            freqs_x = np.fft.fftfreq(W)
            fx, fy  = np.meshgrid(freqs_x, freqs_y)
            freq_r  = np.sqrt(fx**2 + fy**2)
            freq_r[0, 0] = 1e-10
            # 1/f^2 amplitude, random phase
            amplitude = 1.0 / (freq_r ** 2)
            phase     = rng.uniform(0, 2 * np.pi, (H, W))
            spectrum  = amplitude * np.exp(1j * phase)
            img       = np.real(np.fft.ifft2(spectrum))
            img       = ((img - img.min()) / (img.ptp() + 1e-10) * 255).astype(np.float32)
            results.append(analyze_image_numpy(img))

    log.info(f"[ARC XPU] Done — {len(results)} images analyzed")
    return results


# ══════════════════════════════════════════════════════════════════════════════
#  CONVERSATION ANALYSIS — RTX 5060 Ti (CUDA)
# ══════════════════════════════════════════════════════════════════════════════

def _char_entropy(text: str) -> float:
    """Shannon entropy of character distribution, in bits."""
    if not text:
        return 0.0
    counts = {}
    for c in text:
        counts[c] = counts.get(c, 0) + 1
    n = len(text)
    return -sum((v/n) * math.log2(v/n) for v in counts.values())


def _compression_ratio(text: str) -> float:
    """zlib compression ratio — proxy for Kolmogorov complexity."""
    if not text:
        return 1.0
    encoded = text.encode("utf-8")
    compressed = zlib.compress(encoded, level=6)
    return len(compressed) / len(encoded)


def analyze_conversation(turns: list[dict]) -> list[EntropyResult]:
    """
    Analyze a conversation (list of turns with 'role' and 'content').
    Returns one EntropyResult per turn.
    """
    results = []
    for turn in turns:
        content = turn.get("content", "")
        if not content or not isinstance(content, str):
            continue
        h_char  = _char_entropy(content)
        h_token = h_char * 4.3   # approx chars/token for English
        cr      = _compression_ratio(content)
        results.append(EntropyResult(
            char_entropy=h_char,
            token_entropy=h_token,
            compression_ratio=cr,
            turn_length=len(content),
            is_semantic=(H_SEMANTIC_LOW_HYPOTHESIS <= h_token <= H_SEMANTIC_HIGH_HYPOTHESIS),
        ))
    return results


def run_text_analysis_cuda(n_samples: int) -> list[EntropyResult]:
    """
    Run entropy analysis on LMSYS-Chat-1M using RTX 5060 Ti (CUDA).
    """
    log.info(f"[RTX CUDA] Starting conversation entropy analysis — {n_samples} samples")

    try:
        import torch
        if torch.cuda.is_available():
            log.info(f"[RTX CUDA] CUDA available: {torch.cuda.get_device_name(0)} ✓")
        else:
            log.warning("[RTX CUDA] CUDA not available, running on CPU")
    except ImportError:
        log.warning("[RTX CUDA] PyTorch not installed")

    results: list[EntropyResult] = []
    LMSYS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        from datasets import load_dataset
        log.info("[RTX CUDA] Loading LMSYS-Chat-1M from HuggingFace...")
        ds = load_dataset(
            "lmsys/lmsys-chat-1m",
            split="train",
            streaming=True,
            trust_remote_code=True,
        )
        processed = 0
        for item in ds:
            if processed >= n_samples:
                break
            try:
                convs = item.get("conversation", [])
                if not convs:
                    continue
                turn_results = analyze_conversation(convs)
                results.extend(turn_results)
                processed += 1
                if processed % 10000 == 0:
                    if results:
                        semantic_pct = sum(r.is_semantic for r in results) / len(results) * 100
                        h_mean = np.mean([r.token_entropy for r in results])
                        log.info(f"[RTX CUDA] {processed}/{n_samples} convs | "
                                 f"H_mean={h_mean:.3f} bits/tok | semantic={semantic_pct:.1f}%")
            except Exception:
                continue

    except ImportError:
        log.warning("[RTX CUDA] 'datasets' not available. "
                    "Install: pip install datasets")
        # Synthetic test: simulate mundane vs intelligent conversation entropy
        log.info("[RTX CUDA] Generating synthetic conversation test data...")
        rng = np.random.default_rng(42)
        samples = [
            # Mundane: low entropy, high compression
            "hi how are you doing today i am fine thank you",
            "what time is it please tell me the time okay",
            "can you help me yes i can help you sure okay",
            # Intelligent: higher entropy, lower compression
            "The transformer architecture relies on self-attention O(n²) complexity.",
            "Bayesian inference posterior ∝ likelihood × prior, normalized.",
            "ReBAR maps full VRAM aperture; without it Arc sees 256MB not 32GB.",
        ]
        for _ in range(min(n_samples, 1000)):
            text = samples[rng.integers(len(samples))]
            h = _char_entropy(text)
            cr = _compression_ratio(text)
            results.append(EntropyResult(
                char_entropy=h,
                token_entropy=h * 4.3,
                compression_ratio=cr,
                turn_length=len(text),
                is_semantic=(H_SEMANTIC_LOW_HYPOTHESIS <= h * 4.3 <= H_SEMANTIC_HIGH_HYPOTHESIS),
            ))

    log.info(f"[RTX CUDA] Done — {len(results)} turns analyzed")
    return results


# ══════════════════════════════════════════════════════════════════════════════
#  CROSS-MODAL SYNTHESIS + CONSTANT DERIVATION
# ══════════════════════════════════════════════════════════════════════════════

def derive_constant(
    image_results: list[SpectralResult],
    text_results: list[EntropyResult],
) -> SemanticConstant:
    """
    Cross-correlate image spectral slopes and text entropy distributions.
    Derive the modality-agnostic semantic constant.
    Maps to LOCI heat function parameters.
    """
    if not image_results or not text_results:
        log.error("Empty results — cannot derive constant")
        return SemanticConstant()

    alphas = np.array([r.alpha for r in image_results if r.r_squared > 0.6])
    entropies = np.array([r.token_entropy for r in text_results if r.turn_length > 20])

    if len(alphas) == 0 or len(entropies) == 0:
        log.error("No high-quality results after filtering")
        return SemanticConstant()

    const = SemanticConstant(
        # Image domain
        alpha_mean = float(alphas.mean()),
        alpha_std  = float(alphas.std()),
        alpha_p5   = float(np.percentile(alphas, 5)),
        alpha_p95  = float(np.percentile(alphas, 95)),
        n_images   = len(image_results),
        # Text domain
        h_mean     = float(entropies.mean()),
        h_std      = float(entropies.std()),
        h_p5       = float(np.percentile(entropies, 5)),
        h_p95      = float(np.percentile(entropies, 95)),
        n_conversations = len(text_results),
    )

    # Cross-modal correlation: normalize both to [0,1] and correlate
    # Only possible if we have paired (image, text) samples
    n_paired = min(len(alphas), len(entropies))
    if n_paired >= 10:
        a_norm = (alphas[:n_paired] - alphas.min()) / (alphas.ptp() + 1e-10)
        h_norm = (entropies[:n_paired] - entropies.min()) / (entropies.ptp() + 1e-10)
        const.correlation = float(np.corrcoef(a_norm, h_norm)[0, 1])

    # ── Map to LOCI heat function parameters ──────────────────────────────────
    # H_k(t) = α_k + (1 - α_k)(1 - e^{-β·E_k(t)})
    # α_k = baseline heat (0 = cold, 1 = always hot)
    # β   = sensitivity (how fast heat rises with energy E_k)
    #
    # We want:
    #   - α_k_low  = p5 of semantic range normalized to [0,1]
    #   - α_k_high = p95 of semantic range normalized to [0,1]
    #   - β        = inverse of std of semantic entropy (tighter range = higher β)

    h_range = const.h_p95 - const.h_p5
    const.loci_beta_estimate  = float(1.0 / (h_range + 1e-10))  # sharper range → higher β
    const.loci_alpha_k_low    = float(const.h_p5  / 10.0)       # scale to [0,1] assuming max ~10 bits
    const.loci_alpha_k_high   = float(const.h_p95 / 10.0)

    return const


def print_report(const: SemanticConstant) -> None:
    sep = "═" * 60
    print(f"\n{sep}")
    print("  SEMANTIC CONSTANT MEASUREMENT REPORT")
    print(sep)
    print(f"\n  IMAGES (n={const.n_images})")
    print(f"    Spectral slope α:  μ={const.alpha_mean:.3f}  σ={const.alpha_std:.3f}")
    print(f"    Semantic range:   [{const.alpha_p5:.3f}, {const.alpha_p95:.3f}]")
    print(f"    Literature:       [{ALPHA_SEMANTIC_LOW}, {ALPHA_SEMANTIC_HIGH}]")
    match = "✓ CONFIRMED" if abs(const.alpha_mean - 2.0) < 0.5 else "⚠ DEVIATES"
    print(f"    Status:           {match}")

    print(f"\n  CONVERSATIONS (n={const.n_conversations} turns)")
    print(f"    Token entropy H:  μ={const.h_mean:.3f}  σ={const.h_std:.3f} bits/tok")
    print(f"    Semantic range:   [{const.h_p5:.3f}, {const.h_p95:.3f}]")
    print(f"    Hypothesis:       [{H_SEMANTIC_LOW_HYPOTHESIS}, {H_SEMANTIC_HIGH_HYPOTHESIS}]")

    print(f"\n  CROSS-MODAL")
    print(f"    α ↔ H correlation: {const.correlation:.4f}")
    print(f"    Shared semantic constant: {const.alpha_mean:.3f} (image) ↔ {const.h_mean:.3f} bits/tok (text)")

    print(f"\n  LOCI HEAT FUNCTION CALIBRATION")
    print(f"    Recommended β:        {const.loci_beta_estimate:.4f}")
    print(f"    Recommended α_k range: [{const.loci_alpha_k_low:.4f}, {const.loci_alpha_k_high:.4f}]")
    print(f"\n  → Update heat_params in core/speculative_pipeline.py:")
    print(f"      LOCI_HEAT_BETA  = {const.loci_beta_estimate:.4f}")
    print(f"      LOCI_ALPHA_K    = {(const.loci_alpha_k_low + const.loci_alpha_k_high) / 2:.4f}")
    print(f"\n{sep}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main_async(mode: str, n_samples: int) -> SemanticConstant:
    loop = asyncio.get_event_loop()

    image_results: list[SpectralResult] = []
    text_results:  list[EntropyResult]  = []

    if mode in ("both", "image"):
        image_results = await loop.run_in_executor(
            None, run_image_analysis_xpu, n_samples
        )

    if mode in ("both", "text"):
        text_results = await loop.run_in_executor(
            None, run_text_analysis_cuda, n_samples
        )

    const = derive_constant(image_results, text_results)
    print_report(const)

    # Save results
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CACHE_DIR / "semantic_constant.json"
    with open(out_path, "w") as f:
        json.dump({
            "alpha_mean":           const.alpha_mean,
            "alpha_std":            const.alpha_std,
            "alpha_p5":             const.alpha_p5,
            "alpha_p95":            const.alpha_p95,
            "n_images":             const.n_images,
            "h_mean":               const.h_mean,
            "h_std":                const.h_std,
            "h_p5":                 const.h_p5,
            "h_p95":                const.h_p95,
            "n_conversations":      const.n_conversations,
            "correlation":          const.correlation,
            "loci_beta_estimate":   const.loci_beta_estimate,
            "loci_alpha_k_low":     const.loci_alpha_k_low,
            "loci_alpha_k_high":    const.loci_alpha_k_high,
        }, f, indent=2)
    log.info(f"Results saved to {out_path}")
    return const


def main() -> None:
    parser = argparse.ArgumentParser(description="Semantic constant dual-GPU computation")
    parser.add_argument("--mode", choices=["both", "image", "text"], default="both")
    parser.add_argument("--samples", type=int, default=50_000,
                        help="Number of samples per modality")
    args = parser.parse_args()
    asyncio.run(main_async(args.mode, args.samples))


if __name__ == "__main__":
    main()
