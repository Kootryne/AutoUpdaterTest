from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any
import wave

from .paths import DATA_DIR

_STATE_FILE = DATA_DIR / "tts_backend.json"
_VOICE_DIR = DATA_DIR / "piper_voices"

DEFAULT_SWEDISH_VOICE = "sv_SE-nst-medium"
DEFAULT_ENGLISH_VOICE = "en_US-lessac-medium"


class LocalTTSManager:
    """Persistent cloud/local TTS selection with lazy Piper voice setup."""

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.voice_dir = Path(
            os.getenv("LOCAL_TTS_VOICE_DIR", str(_VOICE_DIR))
        ).expanduser()
        self.swedish_voice = os.getenv(
            "LOCAL_TTS_SWEDISH_VOICE", DEFAULT_SWEDISH_VOICE
        ).strip()
        self.english_voice = os.getenv(
            "LOCAL_TTS_ENGLISH_VOICE", DEFAULT_ENGLISH_VOICE
        ).strip()
        self.length_scale = float(os.getenv("LOCAL_TTS_LENGTH_SCALE", "0.96"))
        self.volume = float(os.getenv("LOCAL_TTS_VOLUME", "1.0"))
        self._voices: dict[str, Any] = {}
        self._lock = threading.RLock()

    def backend(self) -> str:
        try:
            payload = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            backend = str(payload.get("backend", "")).strip().lower()
            if backend in {"cloud", "local"}:
                return backend
        except Exception:
            pass
        configured = os.getenv("TTS_BACKEND", "cloud").strip().lower()
        return configured if configured in {"cloud", "local"} else "cloud"

    def set_backend(self, backend: str) -> None:
        normalized = backend.strip().lower()
        if normalized not in {"cloud", "local"}:
            raise ValueError(f"Unsupported TTS backend: {backend}")
        os.environ["TTS_BACKEND"] = normalized
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temporary = _STATE_FILE.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"backend": normalized}, indent=2),
            encoding="utf-8",
        )
        temporary.replace(_STATE_FILE)

    def voice_name(self, language: str) -> str:
        return self.swedish_voice if language == "sv" else self.english_voice

    def model_path(self, language: str) -> Path:
        return self.voice_dir / f"{self.voice_name(language)}.onnx"

    def config_path(self, language: str) -> Path:
        return self.voice_dir / f"{self.voice_name(language)}.onnx.json"

    def _download_voice(self, voice_name: str) -> None:
        self.voice_dir.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "piper.download_voices",
            "--data-dir",
            str(self.voice_dir),
            voice_name,
        ]
        self.logger.info("LOCAL TTS | downloading voice %s", voice_name)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if sys.platform == "win32"
                else 0
            ),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                f"Could not download Piper voice {voice_name}: {detail}"
            )

    def ensure_voice(self, language: str) -> Path:
        model_path = self.model_path(language)
        config_path = self.config_path(language)
        if not model_path.is_file() or not config_path.is_file():
            self._download_voice(self.voice_name(language))
        if not model_path.is_file() or not config_path.is_file():
            raise RuntimeError(
                f"Piper voice files are missing for {self.voice_name(language)}."
            )
        return model_path

    def ensure_ready(self) -> None:
        self.ensure_voice("sv")
        self.ensure_voice("en")

    def _load_voice(self, language: str) -> Any:
        with self._lock:
            name = self.voice_name(language)
            voice = self._voices.get(name)
            if voice is not None:
                return voice
            try:
                from piper import PiperVoice
            except ImportError as exc:
                raise RuntimeError(
                    "Piper is not installed. Run update_jarvis.bat again."
                ) from exc
            model_path = self.ensure_voice(language)
            self.logger.info("LOCAL TTS | loading voice %s", name)
            voice = PiperVoice.load(str(model_path))
            self._voices[name] = voice
            return voice

    def synthesize_to_wav(
        self,
        text: str,
        language: str,
        destination: Path,
    ) -> None:
        try:
            from piper import SynthesisConfig
        except ImportError as exc:
            raise RuntimeError(
                "Piper is not installed. Run update_jarvis.bat again."
            ) from exc

        voice = self._load_voice(language)
        config = SynthesisConfig(
            volume=self.volume,
            length_scale=self.length_scale,
        )
        with wave.open(str(destination), "wb") as wav_file:
            voice.synthesize_wav(text, wav_file, syn_config=config)
