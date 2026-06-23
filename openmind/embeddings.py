"""Local, CPU-only embeddings.

Primary backend: fastembed (ONNX, CPU) with a small code-capable model.
Fallback: a deterministic hashing embedder, so the app is fully functional
even with no network / no cached model (Invariant 1 — embeddings never
carry project content off-machine; computation is 100% in-process).

The embedding model weights may be fetched once from HuggingFace (weights
only, never project content). Set OPENMIND_EMBED_OFFLINE=1 to forbid that and
force the hashing fallback.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from typing import List, Optional

import numpy as np

from . import config

_lock = threading.Lock()
_model = None  # singleton
_desired_gpu: Optional[bool] = None   # 'smart' mode: set per-ingest by prepare_for_bulk


def _embed_providers() -> Optional[List[str]]:
    """ONNX execution providers for fastembed, selected by OPENMIND_EMBED_DEVICE:

      * ``smart`` (DEFAULT) — use the GPU only when the card is FREE. A bulk ingest
        calls :func:`prepare_for_bulk` first; if the local LLM is resident on the GPU
        it embeds on CPU (so DirectML never fights the model for VRAM and crashes the
        display driver — a whole-machine freeze), and on the fast GPU path when the
        card is free. Outside a bulk pass it defaults to CPU (query embedding is tiny).
      * ``auto`` — always use a local GPU provider IF onnxruntime exposes one
        (DirectML / CUDA / ROCm); otherwise CPU. Ignores LLM residency.
      * ``gpu`` — like ``auto`` but logs a warning when no GPU provider is present.
      * ``cpu`` — force CPU (fastembed's default).

    A GPU provider only exists when a GPU build of onnxruntime was deliberately
    installed. Computation is 100% on-machine for every option (Invariant 1).
    Returns None to use the CPU default."""
    pref = os.environ.get("OPENMIND_EMBED_DEVICE", "smart").strip().lower()
    if pref == "cpu":
        return None
    if pref == "smart" and not _desired_gpu:   # None (idle) or False (LLM resident) -> CPU
        return None
    try:
        import onnxruntime as ort
        avail = set(ort.get_available_providers())
    except Exception:
        return None
    gpu = [p for p in ("CUDAExecutionProvider", "ROCMExecutionProvider",
                       "DmlExecutionProvider") if p in avail]
    if gpu:
        return gpu + ["CPUExecutionProvider"]
    if pref == "gpu":
        import sys
        print("[embeddings] OPENMIND_EMBED_DEVICE=gpu but no GPU ONNX provider is "
              "installed (need onnxruntime-directml for AMD/Intel or onnxruntime-gpu "
              "for NVIDIA); using CPU.", file=sys.stderr)
    return None


def _gpu_total_vram_gb() -> float:
    """Best-effort total VRAM of the first GPU (0.0 if unknown). Local probe."""
    try:
        from . import sysload
        gpus = sysload._gpus()
        if gpus and gpus[0].get("vram_total_gb"):
            return float(gpus[0]["vram_total_gb"])
    except Exception:
        pass
    return 0.0


def _embed_batch_size(providers: Optional[List[str]]) -> int:
    """Per-inference batch size handed to fastembed.

    On a GPU this is the main lever bounding PEAK VRAM: the ONNX/DirectML arena
    grows with the batch, and SATURATING VRAM crashes the display driver. So the
    GPU batch is deliberately conservative and SCALED TO THE CARD — small enough
    to leave generous headroom on ANY GPU, a little larger on a big card for
    throughput. This adapts automatically when the machine/GPU changes; override
    explicitly with OPENMIND_EMBED_BATCH. CPU is memory-cheap, so it uses a larger
    batch where the only thing that matters is per-call overhead."""
    env = (os.environ.get("OPENMIND_EMBED_BATCH") or "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    on_gpu = bool(providers) and any(("Dml" in p or "CUDA" in p or "ROCM" in p)
                                     for p in providers)
    if not on_gpu:
        return 256
    total = _gpu_total_vram_gb()
    if total <= 0:
        return 12                       # unknown GPU -> small safe batch
    # Measured on this stack: the ONNX/DirectML arena already reserves ~half the
    # card at model load, then each chunk adds activation on top. Saturating VRAM
    # crashes the display driver, so the batch is kept SMALL and scaled to the
    # card — ~0.7 chunks/GB, clamped [8, 24] — which holds PEAK to ~60-65% (i.e.
    # ~35-40% headroom) on anything from a 4 GB to a 48 GB GPU. Throughput barely
    # benefits from larger batches here, so we spend the slack on safety.
    return int(max(8, min(24, round(total * 0.7))))


class _FastEmbedBackend:
    name = "fastembed"

    def __init__(self) -> None:
        from fastembed import TextEmbedding
        providers = _embed_providers()
        try:
            self._m = (TextEmbedding(model_name=config.EMBED_MODEL_NAME, providers=providers)
                       if providers else TextEmbedding(model_name=config.EMBED_MODEL_NAME))
        except Exception:
            # older fastembed without a `providers` kwarg, or a GPU provider that
            # fails to initialise — fall back to the library default (CPU) so
            # embeddings ALWAYS work rather than breaking the whole ingest.
            self._m = TextEmbedding(model_name=config.EMBED_MODEL_NAME)
            providers = None
        self.providers = providers
        self.batch_size = _embed_batch_size(providers)
        import sys
        print(f"[embeddings] fastembed ready — providers: {providers or 'CPU (default)'}"
              f"; batch_size={self.batch_size}", file=sys.stderr)
        # probe dim
        v = next(iter(self._m.embed(["probe"])))
        self.dim = int(len(v))

    def embed(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        # batch_size bounds PEAK VRAM (keeps the GPU driver from OOM-ing); fastembed
        # processes a long input in batch_size-sized sub-batches reusing buffers.
        vecs = list(self._m.embed(list(texts), batch_size=self.batch_size))
        arr = np.asarray(vecs, dtype=np.float32)
        return _l2norm(arr)


class _HashingBackend:
    """Deterministic char-n-gram hashing embedder (offline-safe fallback)."""
    name = "hashing"

    def __init__(self, dim: int = config.EMBED_DIM) -> None:
        self.dim = dim

    def embed(self, texts: List[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            out[i] = self._one(t)
        return _l2norm(out)

    def _one(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        toks = re.findall(r"[A-Za-z_][A-Za-z0-9_]+|[{}();=<>.]", text.lower())
        # unigrams + bigrams of identifiers
        grams = list(toks)
        grams += [toks[j] + " " + toks[j + 1] for j in range(len(toks) - 1)]
        for g in grams:
            h = int.from_bytes(hashlib.md5(g.encode("utf-8")).digest()[:8], "little")
            idx = h % self.dim
            sign = 1.0 if (h >> 63) & 1 else -1.0
            v[idx] += sign
        return v


def _l2norm(arr: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (arr / norms).astype(np.float32)


def get_model():
    global _model
    if _model is not None:
        return _model
    with _lock:
        if _model is not None:
            return _model
        if os.environ.get("OPENMIND_EMBED_OFFLINE") == "1":
            _model = _HashingBackend()
            return _model
        try:
            _model = _FastEmbedBackend()
        except Exception as exc:  # offline, no wheel, no cache, etc.
            import sys
            print(f"[embeddings] fastembed unavailable ({exc!r}); "
                  f"falling back to deterministic hashing embedder.", file=sys.stderr)
            _model = _HashingBackend()
        return _model


def backend_name() -> str:
    return get_model().name


def dim() -> int:
    return get_model().dim


def prepare_for_bulk(gpu_free: bool) -> str:
    """Pick the embedding device for a bulk ingest pass (``smart`` mode only).

    The caller passes ``gpu_free=False`` when the local LLM is resident on the GPU,
    so embedding stays on CPU and never contends with the model for VRAM (which can
    crash the display driver → machine freeze); ``gpu_free=True`` when the card is
    free, for the fast DirectML path. The singleton is rebuilt only when the device
    actually changes. An explicit OPENMIND_EMBED_DEVICE (cpu/auto/gpu) overrides and
    is left untouched. Returns a short device label for logging."""
    global _model, _desired_gpu
    mode = os.environ.get("OPENMIND_EMBED_DEVICE", "smart").strip().lower()
    if mode != "smart":
        return f"{backend_name()} (OPENMIND_EMBED_DEVICE={mode})"
    with _lock:
        if _desired_gpu != gpu_free or _model is None:
            _desired_gpu = gpu_free
            _model = None   # force rebuild on next get_model() with the new device
    return "GPU (card free)" if gpu_free else "CPU (LLM resident — VRAM-safe)"


def embed(texts: List[str]) -> np.ndarray:
    return get_model().embed(texts)


def embed_one(text: str) -> List[float]:
    return embed([text])[0].tolist()
