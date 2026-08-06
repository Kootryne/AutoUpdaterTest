from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any

import numpy as np
import soundfile as sf

from .paths import DATA_DIR


_SETTINGS_FILE = DATA_DIR / "local_tts" / "supertonic_settings.json"


class SupertonicLocalTTS:
    """Fast multilingual local TTS using Supertonic 3."""

    SAMPLE_RATE = 44_100
    VOICES = ("M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5")
    DEFAULT_VOICE = "M1"
    DEFAULT_STEPS = 8
    MIN_STEPS = 5
    MAX_STEPS = 12

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        saved = self._read_settings()

        voice = str(saved.get("voice") or os.getenv("SUPERTONIC_VOICE", self.DEFAULT_VOICE)).strip().upper()
        self.voice = voice if voice in self.VOICES else self.DEFAULT_VOICE

        raw_steps = saved.get("steps", os.getenv("SUPERTONIC_STEPS", str(self.DEFAULT_STEPS)))
        try:
            requested_steps = int(raw_steps)
        except (TypeError, ValueError):
            requested_steps = self.DEFAULT_STEPS
        if requested_steps < self.MIN_STEPS:
            self.logger.warning(
                "LOCAL TTS | migrated unsupported low quality steps %s -> %d",
                raw_steps,
                self.DEFAULT_STEPS,
            )
            requested_steps = self.DEFAULT_STEPS
        self.steps = max(self.MIN_STEPS, min(self.MAX_STEPS, requested_steps))

        raw_speed = saved.get("speed", os.getenv("SUPERTONIC_SPEED", "1.05"))
        try:
            requested_speed = float(raw_speed)
        except (TypeError, ValueError):
            requested_speed = 1.05
        self.speed = max(0.7, min(2.0, requested_speed))

        self.volume = max(0.0, min(2.0, float(os.getenv("LOCAL_TTS_VOLUME", "1.0"))))
        self.max_chunk_length = max(
            80, min(500, int(os.getenv("SUPERTONIC_MAX_CHUNK_LENGTH", "300")))
        )
        self.silence_duration = max(
            0.0, min(1.0, float(os.getenv("SUPERTONIC_SILENCE_SECONDS", "0.12")))
        )
        self.cache_dir = DATA_DIR / "local_tts" / "supertonic_cache_v091"
        self.cache_limit = max(32, int(os.getenv("SUPERTONIC_CACHE_ITEMS", "256")))
        self._tts: Any | None = None
        self._styles: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._persist_settings()

    @staticmethod
    def _read_settings() -> dict[str, Any]:
        try:
            value = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _persist_settings(self) -> None:
        _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = _SETTINGS_FILE.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "engine": "Supertonic 3",
                    "voice": self.voice,
                    "steps": self.steps,
                    "speed": self.speed,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(_SETTINGS_FILE)

    def settings(self) -> dict[str, Any]:
        return {
            "engine": "Supertonic 3",
            "voice": self.voice,
            "steps": self.steps,
            "speed": self.speed,
            "quality_range": [self.MIN_STEPS, self.MAX_STEPS],
            "available_voices": list(self.VOICES),
        }

    def configure(
        self,
        *,
        voice: str | None = None,
        steps: int | None = None,
        speed: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if voice is not None:
                normalized = voice.strip().upper()
                if normalized not in self.VOICES:
                    raise ValueError(
                        f"Unsupported Supertonic voice: {voice}. "
                        f"Choose one of {', '.join(self.VOICES)}."
                    )
                self.voice = normalized
            if steps is not None:
                if not self.MIN_STEPS <= int(steps) <= self.MAX_STEPS:
                    raise ValueError(
                        f"Supertonic quality steps must be {self.MIN_STEPS}-{self.MAX_STEPS}."
                    )
                self.steps = int(steps)
            if speed is not None:
                if not 0.7 <= float(speed) <= 2.0:
                    raise ValueError("Supertonic speed must be 0.7-2.0.")
                self.speed = float(speed)
            self._persist_settings()
            return self.settings()

    def _load_tts(self) -> Any:
        with self._lock:
            if self._tts is not None:
                return self._tts
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
            self.logger.info(
                "LOCAL TTS | Supertonic ready: %.3fs",
                time.perf_counter() - started,
            )
            return self._tts

    def _style(self, voice: str) -> Any:
        normalized = voice.strip().upper()
        if normalized not in self.VOICES:
            raise ValueError(f"Unsupported Supertonic voice: {voice}")
        with self._lock:
            style = self._styles.get(normalized)
            if style is None:
                style = self._load_tts().get_voice_style(voice_name=normalized)
                self._styles[normalized] = style
            return style

    def ensure_ready(self) -> None:
        self._load_tts()
        self._style(self.voice)

    def _cache_path(
        self,
        text: str,
        language: str,
        voice: str,
        steps: int,
        speed: float,
    ) -> Path:
        identity = (
            f"supertonic3-v091|{voice}|{steps}|{speed}|"
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

    def _synthesize_with(
        self,
        text: str,
        language: str,
        *,
        voice: str,
        steps: int,
        speed: float,
    ) -> tuple[np.ndarray, int, bool, float]:
        normalized_language = "sv" if language == "sv" else "en"
        normalized_voice = voice.strip().upper()
        path = self._cache_path(
            text,
            normalized_language,
            normalized_voice,
            int(steps),
            float(speed),
        )
        if path.is_file():
            audio, rate = sf.read(path, dtype="float32")
            return (
                np.asarray(audio, dtype=np.float32).reshape(-1),
                int(rate),
                True,
                0.0,
            )

        tts = self._load_tts()
        style = self._style(normalized_voice)
        started = time.perf_counter()
        with self._lock:
            wav, duration = tts.synthesize(
                text=text,
                voice_style=style,
                lang=normalized_language,
                total_steps=int(steps),
                speed=float(speed),
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

        _ = float(np.asarray(duration).reshape(-1)[0])
        return audio, self.SAMPLE_RATE, False, elapsed

    def synthesize(
        self,
        text: str,
        language: str,
    ) -> tuple[np.ndarray, int, bool, float]:
        return self._synthesize_with(
            text,
            language,
            voice=self.voice,
            steps=self.steps,
            speed=self.speed,
        )

    def synthesize_preview(
        self,
        text: str,
        language: str,
        *,
        voice: str,
        steps: int | None = None,
        speed: float | None = None,
    ) -> tuple[np.ndarray, int, bool, float]:
        return self._synthesize_with(
            text,
            language,
            voice=voice,
            steps=self.steps if steps is None else int(steps),
            speed=self.speed if speed is None else float(speed),
        )
