from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any

import numpy as np
import soundfile as sf

from .paths import DATA_DIR


class SupertonicLocalTTS:
    """Fast multilingual local TTS using Supertonic 3."""

    SAMPLE_RATE = 44_100

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.voice = os.getenv("SUPERTONIC_VOICE", "M1").strip() or "M1"
        self.steps = max(2, min(12, int(os.getenv("SUPERTONIC_STEPS", "3"))))
        self.speed = max(0.7, min(2.0, float(os.getenv("SUPERTONIC_SPEED", "1.08"))))
        self.volume = max(0.0, min(2.0, float(os.getenv("LOCAL_TTS_VOLUME", "1.0"))))
        self.max_chunk_length = max(
            80, min(500, int(os.getenv("SUPERTONIC_MAX_CHUNK_LENGTH", "300")))
        )
        self.silence_duration = max(
            0.0, min(1.0, float(os.getenv("SUPERTONIC_SILENCE_SECONDS", "0.08")))
        )
        self.cache_dir = DATA_DIR / "local_tts" / "supertonic_cache"
        self.cache_limit = max(32, int(os.getenv("SUPERTONIC_CACHE_ITEMS", "256")))
        self._tts: Any | None = None
        self._style: Any | None = None
        self._lock = threading.RLock()

    def _load(self) -> tuple[Any, Any]:
        with self._lock:
            if self._tts is not None and self._style is not None:
                return self._tts, self._style

            started = time.perf_counter()
            try:
                from supertonic import TTS
            except ImportError as exc:
                raise RuntimeError(
                    "Supertonic is not installed. Run update_jarvis.bat again."
                ) from exc

            self.logger.info(
                "LOCAL TTS | loading Supertonic 3 | voice=%s steps=%d",
                self.voice,
                self.steps,
            )
            self._tts = TTS(auto_download=True)
            self._style = self._tts.get_voice_style(voice_name=self.voice)
            self.logger.info(
                "LOCAL TTS | Supertonic ready: %.3fs",
                time.perf_counter() - started,
            )
            return self._tts, self._style

    def ensure_ready(self) -> None:
        self._load()

    def _cache_path(self, text: str, language: str) -> Path:
        identity = (
            f"supertonic3|{self.voice}|{self.steps}|{self.speed}|"
            f"{language}|{self.max_chunk_length}|{self.silence_duration}|{text}"
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.wav"

    def _trim_cache(self) -> None:
        try:
            files = sorted(
                self.cache_dir.glob("*.wav"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for old in files[self.cache_limit :]:
                old.unlink(missing_ok=True)
        except Exception:
            self.logger.debug("LOCAL TTS | cache trim failed", exc_info=True)

    def synthesize(
        self,
        text: str,
        language: str,
    ) -> tuple[np.ndarray, int, bool, float]:
        normalized_language = "sv" if language == "sv" else "en"
        path = self._cache_path(text, normalized_language)
        if path.is_file():
            audio, rate = sf.read(path, dtype="float32")
            return np.asarray(audio, dtype=np.float32).reshape(-1), int(rate), True, 0.0

        tts, style = self._load()
        started = time.perf_counter()
        with self._lock:
            wav, duration = tts.synthesize(
                text=text,
                voice_style=style,
                lang=normalized_language,
                total_steps=self.steps,
                speed=self.speed,
                max_chunk_length=self.max_chunk_length,
                silence_duration=self.silence_duration,
                verbose=False,
            )
        elapsed = time.perf_counter() - started

        audio = np.asarray(wav, dtype=np.float32)
        if audio.ndim > 1:
            audio = audio[0]
        audio = np.ascontiguousarray(audio.reshape(-1) * self.volume)

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp.wav")
        sf.write(temporary, audio, self.SAMPLE_RATE)
        temporary.replace(path)
        self._trim_cache()

        duration_value = float(np.asarray(duration).reshape(-1)[0])
        return audio, self.SAMPLE_RATE, False, elapsed
