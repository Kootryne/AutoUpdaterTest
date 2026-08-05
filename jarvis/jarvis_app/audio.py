from __future__ import annotations

import logging
import math
import queue
import threading
from typing import Any

import numpy as np
import sounddevice as sd
import webrtcvad

from .paths import FRAME_BYTES, FRAME_SAMPLES, SAMPLE_RATE
from .settings import Settings


class AudioInput:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self.frames: queue.Queue[bytes] = queue.Queue(maxsize=500)
        self.capture_enabled = threading.Event()
        self.speaking = threading.Event()
        self.stream: sd.RawInputStream | None = None

    def callback(self, indata: Any, frames: int, info: Any, status: Any) -> None:
        if status:
            self.logger.debug("Microphone status: %s", status)
        if not self.capture_enabled.is_set() or self.speaking.is_set():
            return

        raw = bytes(indata)
        if len(raw) != FRAME_BYTES:
            return

        try:
            self.frames.put_nowait(raw)
        except queue.Full:
            try:
                self.frames.get_nowait()
                self.frames.put_nowait(raw)
            except queue.Empty:
                pass

    def open(self) -> None:
        self.stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=FRAME_SAMPLES,
            channels=1,
            dtype="int16",
            device=self.settings.mic_device,
            callback=self.callback,
        )
        self.stream.start()

    def close(self) -> None:
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def enable(self) -> None:
        self.flush()
        self.capture_enabled.set()

    def disable(self) -> None:
        self.capture_enabled.clear()
        self.flush()

    def flush(self) -> None:
        while True:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                break

    def get(self, timeout: float = 1.0) -> bytes:
        return self.frames.get(timeout=timeout)


class SpeechDetector:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.vad = webrtcvad.Vad(settings.vad_aggressiveness)

    def is_speech(self, frame: bytes) -> bool:
        try:
            vad_result = self.vad.is_speech(frame, SAMPLE_RATE)
        except Exception:
            vad_result = False

        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        rms = math.sqrt(float(np.mean(samples * samples))) if samples.size else 0.0
        return vad_result or rms >= self.settings.energy_threshold
