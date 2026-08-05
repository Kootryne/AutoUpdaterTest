from __future__ import annotations

import importlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any
import wave

import requests
import soundfile as sf

from .paths import DATA_DIR


_STATE_FILE = DATA_DIR / "tts_backend.json"
_VOICE_DIR = DATA_DIR / "local_tts"

DEFAULT_SWEDISH_VOICE = "sv_SE-lisa-medium"
DEFAULT_KOKORO_VOICE = "bm_george"
KOKORO_MODEL_NAME = "kokoro-v1.0.int8.onnx"
KOKORO_VOICES_NAME = "voices-v1.0.bin"
KOKORO_MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    f"model-files-v1.0/{KOKORO_MODEL_NAME}"
)
KOKORO_VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    f"model-files-v1.0/{KOKORO_VOICES_NAME}"
)
KOKORO_PACKAGE = "kokoro-onnx>=0.4,<1"


class LocalTTSManager:
    """Persistent hybrid local TTS.

    English uses Kokoro ONNX for much better naturalness. Swedish uses Piper,
    because Kokoro does not currently provide a Swedish voice.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.voice_dir = Path(
            os.getenv("LOCAL_TTS_VOICE_DIR", str(_VOICE_DIR))
        ).expanduser()
        self.swedish_voice = os.getenv(
            "LOCAL_TTS_SWEDISH_VOICE", DEFAULT_SWEDISH_VOICE
        ).strip()
        self.kokoro_voice = os.getenv(
            "LOCAL_TTS_KOKORO_VOICE", DEFAULT_KOKORO_VOICE
        ).strip()
        self.kokoro_language = os.getenv(
            "LOCAL_TTS_KOKORO_LANGUAGE", "en-gb"
        ).strip()
        self.kokoro_speed = float(os.getenv("LOCAL_TTS_KOKORO_SPEED", "1.03"))
        self.piper_length_scale = float(
            os.getenv("LOCAL_TTS_LENGTH_SCALE", "0.96")
        )
        self.volume = float(os.getenv("LOCAL_TTS_VOLUME", "1.0"))
        self._piper_voices: dict[str, Any] = {}
        self._kokoro: Any | None = None
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

    def engine_name(self, language: str) -> str:
        return "Kokoro" if language != "sv" else "Piper Lisa"

    def voice_name(self, language: str) -> str:
        return self.swedish_voice if language == "sv" else self.kokoro_voice

    def _download(
        self,
        url: str,
        destination: Path,
        *,
        minimum_bytes: int,
    ) -> None:
        if destination.is_file() and destination.stat().st_size >= minimum_bytes:
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".download")
        temporary.unlink(missing_ok=True)
        self.logger.info("LOCAL TTS | downloading %s", destination.name)
        try:
            with requests.get(
                url,
                stream=True,
                timeout=(8.0, 120.0),
                headers={"User-Agent": "Jarvis-local-tts"},
            ) as response:
                response.raise_for_status()
                with temporary.open("wb") as target:
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            target.write(chunk)
            if temporary.stat().st_size < minimum_bytes:
                raise RuntimeError(
                    f"Downloaded {destination.name} is unexpectedly small."
                )
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _ensure_kokoro_package(self) -> None:
        try:
            import kokoro_onnx  # noqa: F401
            return
        except ImportError:
            pass

        self.logger.info("LOCAL TTS | installing %s", KOKORO_PACKAGE)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                KOKORO_PACKAGE,
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if sys.platform == "win32"
                else 0
            ),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"Could not install Kokoro: {detail}")
        importlib.invalidate_caches()
        try:
            import kokoro_onnx  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("Kokoro installed but could not be imported.") from exc

    @property
    def kokoro_model_path(self) -> Path:
        return self.voice_dir / KOKORO_MODEL_NAME

    @property
    def kokoro_voices_path(self) -> Path:
        return self.voice_dir / KOKORO_VOICES_NAME

    def _ensure_kokoro(self) -> None:
        self._ensure_kokoro_package()
        self._download(
            KOKORO_MODEL_URL,
            self.kokoro_model_path,
            minimum_bytes=50 * 1024 * 1024,
        )
        self._download(
            KOKORO_VOICES_URL,
            self.kokoro_voices_path,
            minimum_bytes=5 * 1024 * 1024,
        )

    def piper_model_path(self) -> Path:
        return self.voice_dir / f"{self.swedish_voice}.onnx"

    def piper_config_path(self) -> Path:
        return self.voice_dir / f"{self.swedish_voice}.onnx.json"

    def _download_piper_voice(self) -> None:
        self.voice_dir.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "piper.download_voices",
            "--data-dir",
            str(self.voice_dir),
            self.swedish_voice,
        ]
        self.logger.info("LOCAL TTS | downloading voice %s", self.swedish_voice)
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
                f"Could not download Piper voice {self.swedish_voice}: {detail}"
            )

    def _ensure_piper(self) -> None:
        if (
            not self.piper_model_path().is_file()
            or not self.piper_config_path().is_file()
        ):
            self._download_piper_voice()
        if (
            not self.piper_model_path().is_file()
            or not self.piper_config_path().is_file()
        ):
            raise RuntimeError(
                f"Piper voice files are missing for {self.swedish_voice}."
            )

    def ensure_ready(self, language: str | None = None) -> None:
        if language == "sv":
            self._ensure_piper()
            return
        if language is not None:
            self._ensure_kokoro()
            return
        self._ensure_kokoro()
        self._ensure_piper()

    def _load_kokoro(self) -> Any:
        with self._lock:
            if self._kokoro is not None:
                return self._kokoro
            self._ensure_kokoro()
            from kokoro_onnx import Kokoro

            self.logger.info(
                "LOCAL TTS | loading Kokoro int8 | voice=%s",
                self.kokoro_voice,
            )
            self._kokoro = Kokoro(
                str(self.kokoro_model_path),
                str(self.kokoro_voices_path),
            )
            return self._kokoro

    def _load_piper(self) -> Any:
        with self._lock:
            voice = self._piper_voices.get(self.swedish_voice)
            if voice is not None:
                return voice
            try:
                from piper import PiperVoice
            except ImportError as exc:
                raise RuntimeError(
                    "Piper is not installed. Run update_jarvis.bat again."
                ) from exc
            self._ensure_piper()
            self.logger.info(
                "LOCAL TTS | loading Piper voice %s", self.swedish_voice
            )
            voice = PiperVoice.load(str(self.piper_model_path()))
            self._piper_voices[self.swedish_voice] = voice
            return voice

    def synthesize_to_wav(
        self,
        text: str,
        language: str,
        destination: Path,
    ) -> None:
        if language != "sv":
            kokoro = self._load_kokoro()
            samples, sample_rate = kokoro.create(
                text,
                voice=self.kokoro_voice,
                speed=self.kokoro_speed,
                lang=self.kokoro_language,
            )
            sf.write(destination, samples * self.volume, sample_rate)
            return

        try:
            from piper import SynthesisConfig
        except ImportError as exc:
            raise RuntimeError(
                "Piper is not installed. Run update_jarvis.bat again."
            ) from exc

        voice = self._load_piper()
        config = SynthesisConfig(
            volume=self.volume,
            length_scale=self.piper_length_scale,
        )
        with wave.open(str(destination), "wb") as wav_file:
            voice.synthesize_wav(text, wav_file, syn_config=config)
