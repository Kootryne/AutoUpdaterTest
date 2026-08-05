from __future__ import annotations

from collections import deque
import logging
from pathlib import Path
import queue
import re
import tempfile
import time
from typing import Any
import wave

import numpy as np
from openai import OpenAI
from openwakeword.model import Model as WakeWordModel
from openwakeword.utils import download_models
import sounddevice as sd
import soundfile as sf

from .audio import AudioInput, SpeechDetector
from .brain import Brain
from .paths import APP_DIR, FRAME_MS, SAMPLE_RATE
from .settings import Settings
from .tools import Tools


class Jarvis:
    wake_regex = re.compile(
        r"^\s*(?:hey[\s,]+)?(?:jarvis|järvis|jervis)\b[\s,.:;!?\-]*",
        re.IGNORECASE,
    )
    stop_phrases = {
        "stop",
        "cancel",
        "never mind",
        "nevermind",
        "sluta",
        "avbryt",
        "strunt samma",
        "det var inget",
    }

    def __init__(
        self,
        settings: Settings,
        config: dict[str, Any],
        logger: logging.Logger,
    ) -> None:
        if not settings.api_key:
            raise RuntimeError(
                "OPENAI_API_KEY was not found in .env or in the Windows "
                "Process/User/Machine environment variables."
            )

        self.settings = settings
        self.config = config
        self.logger = logger
        self.client = OpenAI(api_key=settings.api_key)
        self.audio = AudioInput(settings, logger)
        self.detector = SpeechDetector(settings)
        self.tools = Tools(settings, config, logger)
        self.brain = Brain(self.client, settings, config, self.tools, logger)
        self.wake_model = self.load_wake_model()
        self.last_wake = 0.0

    def load_wake_model(self) -> WakeWordModel:
        self.logger.info("Checking openWakeWord model files...")
        download_models(["hey_jarvis"])
        self.logger.info("Loading pretrained hey_jarvis model...")
        model = WakeWordModel(
            wakeword_models=["hey_jarvis"],
            inference_framework="onnx",
            vad_threshold=self.settings.wake_vad_threshold,
        )
        self.logger.info("Loaded wake model: %s", ", ".join(model.models))
        return model

    def run(self) -> None:
        self.audio.open()
        self.audio.enable()
        pre_roll_count = max(1, int(self.settings.pre_roll * 1000 / FRAME_MS))
        pre_roll: deque[bytes] = deque(maxlen=pre_roll_count)

        print("\nJARVIS READY | Say 'Hey Jarvis' | Ctrl+C stops it\n")
        self.logger.info(
            "Ready. Wake threshold %.2f", self.settings.wake_threshold
        )

        try:
            while True:
                try:
                    frame = self.audio.get(timeout=1)
                except queue.Empty:
                    continue

                pre_roll.append(frame)
                samples = np.frombuffer(frame, dtype=np.int16)
                scores = self.wake_model.predict(samples)
                score = float(scores.get("hey_jarvis", 0.0))

                if self.settings.debug and score >= 0.08:
                    self.logger.debug("Wake score %.3f", score)

                now = time.monotonic()
                if (
                    score >= self.settings.wake_threshold
                    and now - self.last_wake >= self.settings.wake_cooldown
                ):
                    self.last_wake = now
                    self.logger.info("Wake detected, score %.3f", score)
                    frames = self.record_after_wake(list(pre_roll))
                    pre_roll.clear()
                    self.audio.disable()
                    self.handle_audio(frames)
                    self.wake_model.reset()
                    self.audio.enable()
                    self.logger.info("Listening for wake word...")
        except KeyboardInterrupt:
            self.logger.info("Stopped by Ctrl+C.")
        finally:
            self.audio.disable()
            self.audio.close()

    def record_after_wake(self, pre_roll: list[bytes]) -> list[bytes]:
        frames = list(pre_roll)
        started = time.monotonic()
        last_voice = started
        self.logger.info("Listening for command...")

        while time.monotonic() - started < self.settings.max_utterance:
            try:
                frame = self.audio.get(timeout=0.25)
            except queue.Empty:
                continue

            frames.append(frame)
            if self.detector.is_speech(frame):
                last_voice = time.monotonic()

            if (
                time.monotonic() - started >= 0.45
                and time.monotonic() - last_voice >= self.settings.end_silence
            ):
                break

        return frames

    def record_followup(self) -> list[bytes] | None:
        self.audio.enable()
        wait_started = time.monotonic()
        pre_voice: deque[bytes] = deque(maxlen=10)
        frames: list[bytes] = []
        started = False
        last_voice = wait_started

        while True:
            now = time.monotonic()
            if not started and now - wait_started >= self.settings.followup_timeout:
                self.audio.disable()
                return None
            if started and now - wait_started >= self.settings.max_utterance:
                break

            try:
                frame = self.audio.get(timeout=0.15)
            except queue.Empty:
                continue

            voice = self.detector.is_speech(frame)
            if not started:
                pre_voice.append(frame)
                if voice:
                    started = True
                    frames.extend(pre_voice)
                    last_voice = time.monotonic()
            else:
                frames.append(frame)
                if voice:
                    last_voice = time.monotonic()
                elif (
                    time.monotonic() - last_voice
                    >= self.settings.followup_end_silence
                ):
                    break

        self.audio.disable()
        return frames or None

    def handle_audio(self, frames: list[bytes]) -> None:
        transcript = self.transcribe(frames)
        command = self.strip_wake(transcript)
        print(f"\nYOU: {transcript}")
        self.logger.info("Transcript: %s", transcript)

        if not command:
            self.say("Yes?")
            followup = self.record_followup()
            if followup is None:
                return
            command = self.strip_wake(self.transcribe(followup).strip())
            print(f"YOU: {command}")

        while command:
            if self.is_stop(command):
                return

            try:
                answer = self.brain.ask(command)
            except Exception as exc:
                self.logger.exception("Brain request failed")
                answer = self.friendly_error(exc)

            self.say(answer)
            followup = self.record_followup()
            if followup is None:
                return

            heard = self.transcribe(followup).strip()
            print(f"HEARD DURING FOLLOW-UP: {heard}")
            self.logger.info("Follow-up transcript: %s", heard)

            if (
                self.settings.followup_require_intent
                and not self.brain.followup_is_for_jarvis(heard)
            ):
                self.logger.info(
                    "Ignoring follow-up because it was probably not addressed "
                    "to Jarvis."
                )
                print("JARVIS: Ignored. It probably was not meant for me.\n")
                return

            command = self.strip_wake(heard)
            print(f"YOU: {command}")

    def transcribe(self, frames: list[bytes]) -> str:
        if not frames:
            return ""

        path = self.temp_path(".wav")
        try:
            with wave.open(str(path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(SAMPLE_RATE)
                wav_file.writeframes(b"".join(frames))

            with path.open("rb") as audio_file:
                result = self.client.audio.transcriptions.create(
                    model=self.settings.stt_model,
                    file=audio_file,
                    response_format="json",
                    prompt=(
                        "The speaker may use Swedish, English, or both. "
                        "Expected words include Jarvis, Viktor, Home Assistant, "
                        "Spotify, Steam, and smart-home commands."
                    ),
                )
            return str(result.text).strip()
        finally:
            path.unlink(missing_ok=True)

    def say(self, text: str) -> None:
        print(f"JARVIS: {text}\n")
        self.logger.info("Jarvis: %s", text)

        if not self.settings.tts_enabled:
            return

        speech = self.clean_speech(text)[:1800]
        if not speech:
            return

        path = self.temp_path(".wav")
        self.audio.speaking.set()
        self.audio.disable()

        try:
            with self.client.audio.speech.with_streaming_response.create(
                model=self.settings.tts_model,
                voice=self.settings.tts_voice,
                input=speech,
                instructions=(
                    "Speak like a calm, precise, understated personal assistant. "
                    "Use natural Swedish for Swedish text and natural English for "
                    "English text. Speak clearly and slightly briskly."
                ),
                response_format="wav",
            ) as response:
                response.stream_to_file(path)

            data, rate = sf.read(path, dtype="float32")
            sd.play(
                data,
                rate,
                device=self.settings.speaker_device,
                blocking=True,
            )
        except Exception:
            self.logger.exception("TTS or playback failed")
        finally:
            path.unlink(missing_ok=True)
            time.sleep(0.15)
            self.audio.speaking.clear()
            self.audio.flush()

    @staticmethod
    def temp_path(suffix: str) -> Path:
        handle = tempfile.NamedTemporaryFile(
            suffix=suffix,
            delete=False,
            dir=APP_DIR,
        )
        path = Path(handle.name)
        handle.close()
        return path

    @classmethod
    def strip_wake(cls, text: str) -> str:
        text = text.strip()
        match = cls.wake_regex.match(text)
        return text[match.end() :].strip() if match else text

    @classmethod
    def is_stop(cls, text: str) -> bool:
        normalized = re.sub(r"[^\wåäö]+", " ", text.lower()).strip()
        return normalized in cls.stop_phrases

    @staticmethod
    def clean_speech(text: str) -> str:
        text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
        text = re.sub(r"`([^`]*)`", r"\1", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"https?://\S+", "", text)
        text = re.sub(r"[*_#>|]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def friendly_error(exc: Exception) -> str:
        message = str(exc).lower()
        if "401" in message or "api key" in message or "authentication" in message:
            return "The OpenAI API key was rejected. Check the .env file."
        if "quota" in message or "billing" in message:
            return "The OpenAI API account has no available credit."
        if "rate" in message and "limit" in message:
            return "The API rate limit was reached. Try again in a moment."
        if "connection" in message or "timeout" in message:
            return "I could not reach the service. Check the internet connection."
        return "Something failed. The exact error is in the Jarvis log."
