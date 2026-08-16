"""Step 1 — audio denoising.

A distress call recorded in a shelter carries crowd noise, alarms, rain and
phone-line crackle.  Cleaning it up measurably improves Whisper's word error
rate, but it is *not* essential: if the denoiser is unavailable the pipeline
carries on with the raw audio and says so, rather than failing the call.

Backends (``ARIA_DENOISER``):

``noisereduce``
    Stationary spectral subtraction.  Fast, CPU-only, the default.
``facebook``
    Facebook's DNS64 deep model.  Better on non-stationary noise, much slower.
``none``
    Pass the audio through untouched.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from aria.config import settings
from aria.core.logging import get_logger

log = get_logger("agents.denoise")


@dataclass(frozen=True)
class DenoiseResult:
    path: str
    backend: str
    applied: bool
    note: str = ""


def denoise(input_path: str | Path, output_path: str | Path) -> DenoiseResult:
    """Clean *input_path* into *output_path*, degrading to a copy on failure."""
    source = Path(input_path)
    target = Path(output_path)
    backend = settings.audio.denoiser.lower()

    if backend == "none":
        return _passthrough(source, target, "none", "denoising disabled")

    try:
        if backend == "facebook":
            _denoise_facebook(source, target)
        else:
            _denoise_noisereduce(source, target)
        return DenoiseResult(path=str(target), backend=backend, applied=True)
    except ImportError as exc:
        return _passthrough(source, target, backend, f"{exc.name or backend} not installed")
    except Exception as exc:  # noqa: BLE001 - malformed wav, mono/stereo oddities…
        log.warning("Denoiser '%s' failed (%s) — using the raw audio", backend, exc)
        return _passthrough(source, target, backend, str(exc))


def _passthrough(source: Path, target: Path, backend: str, note: str) -> DenoiseResult:
    try:
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
    except OSError as exc:  # pragma: no cover - disk full / permissions
        log.error("Could not copy raw audio: %s", exc)
        return DenoiseResult(path=str(source), backend=backend, applied=False, note=str(exc))
    return DenoiseResult(path=str(target), backend=backend, applied=False, note=note)


def _denoise_noisereduce(source: Path, target: Path) -> None:
    import numpy as np  # noqa: PLC0415 - heavy imports stay out of module scope
    import noisereduce as nr
    import scipy.io.wavfile as wav

    rate, data = wav.read(str(source))
    if data.ndim == 2:  # stereo → mono, Whisper wants a single channel
        data = data.mean(axis=1)

    original_dtype = data.dtype
    samples = data.astype(np.float32)
    cleaned = nr.reduce_noise(
        y=samples,
        sr=rate,
        stationary=True,
        prop_decrease=settings.audio.denoise_strength,
    )
    # Clip before casting back: reduce_noise can overshoot the int16 range and
    # wrap around, which turns a quiet voice into a burst of static.
    if np.issubdtype(original_dtype, np.integer):
        info = np.iinfo(original_dtype)
        cleaned = np.clip(cleaned, info.min, info.max)
    wav.write(str(target), rate, cleaned.astype(original_dtype))


def _denoise_facebook(source: Path, target: Path) -> None:
    import torch  # noqa: PLC0415
    import torchaudio
    from denoiser import pretrained
    from denoiser.dsp import convert_audio

    model = pretrained.dns64()
    model.eval()
    waveform, sample_rate = torchaudio.load(str(source))
    waveform = convert_audio(waveform, sample_rate, model.sample_rate, model.chin)
    with torch.no_grad():
        denoised = model(waveform[None])[0]
    torchaudio.save(str(target), denoised, model.sample_rate)
