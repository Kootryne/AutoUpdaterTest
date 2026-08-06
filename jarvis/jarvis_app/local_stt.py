from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

import numpy as np

from .paths import DATA_DIR, FRAME_MS, SAMPLE_RATE


_STATE_FILE = DATA_DIR / "stt_backend.json"


class ParakeetLocalSTT:
    """Lazy, persistent NVIDIA Parakeet TDT v3 transcription engine."""

    DEFAULT_MODEL = "nvidia/parakeet-tdt-0.6b-v3"

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.model_id = (
            os.getenv("PARAKEET_MODEL", self.DEFAULT_MODEL).strip()
            or self.DEFAULT_MODEL
        )
        self.device_preference = (
            os.getenv("PARAKEET_DEVICE", "auto").strip().lower() or "auto"
        )
        self._processor: Any | None = None
        self._model: Any | None = None
        self._torch: Any | None = None
        self._device = "unloaded"
        self._dtype = "unloaded"
        self._lock = threading.RLock()
        self.last_metrics: dict[str, Any] = {}

    def _select_device(self, torch: Any) -> tuple[str, Any]:
        preference = self.device_preference
        if preference not in {"auto", "cuda", "cpu"}:
            raise ValueError("PARAKEET_DEVICE must be auto, cuda, or cpu.")

        cuda_available = bool(torch.cuda.is_available())
        if preference == "cuda" and not cuda_available:
            raise RuntimeError(
                "PARAKEET_DEVICE is cuda, but this PyTorch installation cannot use CUDA."
            )
        device = "cuda" if cuda_available and preference != "cpu" else "cpu"
        dtype = torch.float16 if device == "cuda" else torch.float32
        return device, dtype

    def _load_on(self, device: str, dtype: Any) -> tuple[Any, Any, Any]:
        try:
            import torch
            from transformers import AutoModelForTDT, AutoProcessor
        except ImportError as exc:
            raise RuntimeError(
                "Local Parakeet STT dependencies are missing. Run the Jarvis updater again."
            ) from exc

        if device == "cpu":
            requested = int(os.getenv("PARAKEET_CPU_THREADS", "6"))
            torch.set_num_threads(max(1, min(16, requested)))

        self.logger.info(
            "LOCAL STT | loading Parakeet | model=%s device=%s dtype=%s",
            self.model_id,
            device,
            str(dtype).replace("torch.", ""),
        )
        processor = AutoProcessor.from_pretrained(self.model_id)
        kwargs: dict[str, Any] = {"low_cpu_mem_usage": True}
        try:
            model = AutoModelForTDT.from_pretrained(
                self.model_id,
                dtype=dtype,
                **kwargs,
            )
        except TypeError:
            model = AutoModelForTDT.from_pretrained(
                self.model_id,
                torch_dtype=dtype,
                **kwargs,
            )
        model.to(device)
        model.eval()
        return torch, processor, model

    def ensure_ready(self) -> dict[str, Any]:
        with self._lock:
            if self._model is not None and self._processor is not None:
                return self.status()

            started = time.perf_counter()
            try:
                import torch
            except ImportError as exc:
                raise RuntimeError(
                    "PyTorch is not installed. Run the Jarvis updater again."
                ) from exc

            device, dtype = self._select_device(torch)
            try:
                loaded_torch, processor, model = self._load_on(device, dtype)
            except RuntimeError as exc:
                message = str(exc).lower()
                if device != "cuda" or not any(
                    marker in message
                    for marker in ("out of memory", "cuda error", "cublas", "cudnn")
                ):
                    raise
                self.logger.exception(
                    "LOCAL STT | CUDA load failed; retrying Parakeet on CPU"
                )
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass
                device, dtype = "cpu", torch.float32
                loaded_torch, processor, model = self._load_on(device, dtype)

            self._torch = loaded_torch
            self._processor = processor
            self._model = model
            self._device = device
            self._dtype = str(dtype).replace("torch.", "")
            elapsed = time.perf_counter() - started
            self.logger.info(
                "LOCAL STT | Parakeet ready: %.3fs | device=%s dtype=%s",
                elapsed,
                self._device,
                self._dtype,
            )
            self.last_metrics = {
                "event": "load",
                "load_seconds": round(elapsed, 3),
            }
            return self.status()

    def unload(self) -> None:
        with self._lock:
            model = self._model
            self._model = None
            self._processor = None
            self._torch = None
            self._device = "unloaded"
            self._dtype = "unloaded"
            del model
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            self.logger.info("LOCAL STT | Parakeet unloaded")

    @staticmethod
    def _decode(processor: Any, output: Any) -> str:
        try:
            decoded, _timestamps = processor.decode(
                output.sequences,
                durations=output.durations,
                skip_special_tokens=True,
            )
        except Exception:
            decoded = processor.batch_decode(
                output.sequences,
                skip_special_tokens=True,
            )

        if isinstance(decoded, (list, tuple)):
            return str(decoded[0] if decoded else "").strip()
        return str(decoded).strip()

    def transcribe(self, frames: list[bytes]) -> str:
        if not frames:
            return ""

        self.ensure_ready()
        with self._lock:
            assert self._torch is not None
            assert self._processor is not None
            assert self._model is not None
            torch = self._torch
            processor = self._processor
            model = self._model

            audio = (
                np.frombuffer(b"".join(frames), dtype=np.int16)
                .astype(np.float32)
                / 32768.0
            )
            audio_seconds = len(frames) * FRAME_MS / 1000
            started = time.perf_counter()

            inputs = processor(
                [audio],
                sampling_rate=SAMPLE_RATE,
                return_tensors="pt",
            )
            model_dtype = next(model.parameters()).dtype
            inputs = inputs.to(device=self._device, dtype=model_dtype)

            with torch.inference_mode():
                output = model.generate(
                    **inputs,
                    return_dict_in_generate=True,
                )
            transcript = self._decode(processor, output)
            elapsed = time.perf_counter() - started
            metrics = {
                "event": "transcription",
                "seconds": round(elapsed, 3),
                "audio_seconds": round(audio_seconds, 3),
                "real_time_factor": (
                    round(elapsed / audio_seconds, 3) if audio_seconds else None
                ),
                "characters": len(transcript),
            }
            self.last_metrics = metrics
            self.logger.info(
                "TIMING | STT local Parakeet: %.3fs | audio=%.2fs | "
                "realtime_factor=%.2fx | chars=%d | device=%s",
                elapsed,
                audio_seconds,
                elapsed / audio_seconds if audio_seconds else 0.0,
                len(transcript),
                self._device,
            )
            return transcript

    def status(self) -> dict[str, Any]:
        return {
            "engine": "NVIDIA Parakeet TDT v3",
            "model": self.model_id,
            "loaded": self._model is not None,
            "device": self._device,
            "dtype": self._dtype,
            "last_metrics": dict(self.last_metrics),
        }


class STTBackendManager:
    """Persistent cloud/local selector for Jarvis speech recognition."""

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.engine = ParakeetLocalSTT(logger)
        self._backend = self._load_backend()

    @staticmethod
    def _normalise(value: str) -> str:
        lowered = value.strip().lower()
        aliases = {
            "openai": "cloud",
            "online": "cloud",
            "api": "cloud",
            "parakeet": "local",
            "offline": "local",
        }
        lowered = aliases.get(lowered, lowered)
        if lowered not in {"cloud", "local"}:
            raise ValueError("STT backend must be cloud or local.")
        return lowered

    def _load_backend(self) -> str:
        try:
            payload = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return self._normalise(str(payload.get("backend", "")))
        except Exception:
            pass
        return self._normalise(os.getenv("STT_BACKEND", "cloud"))

    def backend(self) -> str:
        return self._backend

    def set_backend(self, value: str, *, load: bool = True) -> dict[str, Any]:
        backend = self._normalise(value)
        if backend == "local" and load:
            self.engine.ensure_ready()

        self._backend = backend
        os.environ["STT_BACKEND"] = backend
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = _STATE_FILE.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "backend": backend,
                    "updated_at_unix": time.time(),
                    "local_model": self.engine.model_id,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(_STATE_FILE)
        self.logger.info("STT | backend changed to %s", backend)
        return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "backend": self._backend,
            "cloud_engine": os.getenv("STT_MODEL", "gpt-4o-mini-transcribe"),
            "local": self.engine.status(),
        }
