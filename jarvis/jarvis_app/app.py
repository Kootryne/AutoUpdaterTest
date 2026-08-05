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
from .updater import UpdateManager


class Jarvis:
    wake_regex = re.compile(
        r"^\s*(?:hey[\s,]+)?(?:jarvis|järvis|jervis)\b[\s,.:;!?\-]*",
        re.IGNORECASE,
    )
    update_regex = re.compile(
        r"(?:^|\b)(?:"
        r"update(?:\s+now)?\s*$|"
        r"update\s+(?:yourself|jarvis|the\s+assistant)|"
        r"check\s+for\s+(?:an?\s+)?update|"
        r"install\s+(?:the\s+)?update|"
        r"uppdatera(?:\s+nu)?\s*$|"
        r"uppdatera\s+(?:dig|jarvis)|"
        r"sök\s+efter\s+uppdatering(?:ar)?|"
        r"installera\s+uppdatering(?:en)?"
        r")\b",
        re.IGNORECASE,
    )
    stop_phrases = {
        "stop", "cancel", "never mind", "nevermind", "sluta", "avbryt",
        "strunt samma", "det var inget",
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
        self.updater = UpdateManager(settings, logger)
        self.tools = Tools(settings, config, logger, updater=self.updater)
        self.brain = Brain(self.client, settings, config, self.tools, logger)
        self.wake_model = self.load_wake_model()
        self.last_wake = 0.0
        self.exit_requested = False

    def load_wake_model(self) -> WakeWordModel:
        started = time.perf_counter()
        self.logger.info("Checking openWakeWord model files...")
        download_models(["hey_jarvis"])
        self.logger.info("Loading pretrained hey_jarvis model...")
        model = WakeWordModel(
            wakeword_models=["hey_jarvis"],
            inference_framework="onnx",
            vad_threshold=self.settings.wake_vad_threshold,
        )
        self.logger.info("Loaded wake model: %s", ", ".join(model.models))
        self.logger.info(
            "TIMING | wake model setup: %.3fs",
            time.perf_counter() - started,
        )
        return model

    def run(self) -> None:
        self.updater.start()
        audio_started = time.perf_counter()
        self.audio.open()
        self.audio.enable()
        self.logger.info(
            "TIMING | microphone startup: %.3fs",
            time.perf_counter() - audio_started,
        )

        pre_roll_count = max(1, int(self.settings.pre_roll * 1000 / FRAME_MS))
        pre_roll: deque[bytes] = deque(maxlen=pre_roll_count)

        print("\nJARVIS READY | Say 'Hey Jarvis' | Ctrl+C stops it\n")
        self.logger.info(
            "Ready. Wake threshold %.2f | VAD mode=%d | energy=%d | max=%.1fs",
            self.settings.wake_threshold,
            self.settings.vad_aggressiveness,
            self.settings.energy_threshold,
            self.settings.effective_max_utterance,
        )

        try:
            while not self.exit_requested:
                if self.updater.auto_apply_ready():
                    self.logger.info(
                        "UPDATER | applying staged automatic update while idle"
                    )
                    self.audio.disable()
                    if self.updater.launch_apply(reason="automatic"):
                        self.exit_requested = True
                        break

                try:
                    frame = self.audio.get(timeout=0.25)
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
                    interaction_started = time.perf_counter()
                    self.logger.info("Wake detected, score %.3f", score)
                    frames = self.record_after_wake(list(pre_roll))
                    pre_roll.clear()
                    self.audio.disable()
                    self.handle_audio(frames, interaction_started)
                    if self.exit_requested:
                        break
                    self.wake_model.reset()
                    self.audio.enable()
                    self.logger.info("Listening for wake word...")
        except KeyboardInterrupt:
            self.logger.info("Stopped by Ctrl+C.")
        finally:
            self.updater.stop()
            self.audio.disable()
            self.audio.close()

    def _record_until_silence(
        self,
        initial_frames: list[bytes],
        *,
        silence_seconds: float,
        label: str,
    ) -> list[bytes]:
        frames = list(initial_frames)
        started = time.perf_counter()
        last_voice = started
        voice_window: deque[bool] = deque(maxlen=self.settings.vad_window_frames)
        raw_voiced = 0
        vad_positive = 0
        rms_total = 0.0
        rms_max = 0.0
        measured_frames = 0
        stop_reason = "hard-timeout"

        while time.perf_counter() - started < self.settings.effective_max_utterance:
            try:
                frame = self.audio.get(timeout=0.25)
            except queue.Empty:
                continue

            frames.append(frame)
            measurement = self.detector.measure(frame)
            measured_frames += 1
            raw_voiced += int(measurement.voiced)
            vad_positive += int(measurement.vad)
            rms_total += measurement.rms
            rms_max = max(rms_max, measurement.rms)
            voice_window.append(measurement.voiced)

            smoothed_voice = (
                len(voice_window) >= self.settings.vad_min_voiced_frames
                and sum(voice_window) >= self.settings.vad_min_voiced_frames
            )
            if smoothed_voice:
                last_voice = time.perf_counter()

            elapsed = time.perf_counter() - started
            if elapsed >= 0.55 and elapsed - (last_voice - started) >= silence_seconds:
                stop_reason = "silence"
                break

        elapsed = time.perf_counter() - started
        audio_seconds = len(frames) * FRAME_MS / 1000
        self.logger.info(
            "TIMING | %s: %.3fs | audio=%.2fs | stop=%s",
            label,
            elapsed,
            audio_seconds,
            stop_reason,
        )
        self.logger.info(
            "VAD | %s | frames=%d | voiced=%.1f%% | webrtc=%.1f%% | "
            "avg_rms=%.0f | max_rms=%.0f | threshold=%d | window=%d/%d",
            label,
            measured_frames,
            100 * raw_voiced / measured_frames if measured_frames else 0.0,
            100 * vad_positive / measured_frames if measured_frames else 0.0,
            rms_total / measured_frames if measured_frames else 0.0,
            rms_max,
            self.settings.energy_threshold,
            self.settings.vad_min_voiced_frames,
            self.settings.vad_window_frames,
        )
        return frames

    def record_after_wake(self, pre_roll: list[bytes]) -> list[bytes]:
        self.logger.info("Listening for command...")
        return self._record_until_silence(
            pre_roll,
            silence_seconds=self.settings.end_silence,
            label="command capture after wake",
        )

    def record_followup(self) -> list[bytes] | None:
        self.audio.enable()
        wait_started = time.perf_counter()
        start_window: deque[bool] = deque(
            maxlen=self.settings.followup_start_window_frames
        )
        pre_voice: deque[bytes] = deque(maxlen=12)
        start_rms_max = 0.0

        while time.perf_counter() - wait_started < self.settings.followup_timeout:
            try:
                frame = self.audio.get(timeout=0.15)
            except queue.Empty:
                continue

            measurement = self.detector.measure(frame)
            start_rms_max = max(start_rms_max, measurement.rms)
            pre_voice.append(frame)
            start_window.append(measurement.voiced)

            if (
                len(start_window) >= self.settings.followup_start_min_voiced_frames
                and sum(start_window)
                >= self.settings.followup_start_min_voiced_frames
            ):
                wait_seconds = time.perf_counter() - wait_started
                self.logger.info(
                    "TIMING | follow-up speech start: %.3fs | max_rms=%.0f",
                    wait_seconds,
                    start_rms_max,
                )
                frames = self._record_until_silence(
                    list(pre_voice),
                    silence_seconds=self.settings.followup_end_silence,
                    label="follow-up capture",
                )
                self.audio.disable()
                return frames

        self.audio.disable()
        self.logger.info(
            "TIMING | follow-up wait: %.3fs | no speech | max_rms=%.0f",
            time.perf_counter() - wait_started,
            start_rms_max,
        )
        return None

    @staticmethod
    def _looks_swedish(text: str) -> bool:
        return bool(
            re.search(
                r"\b(?:uppdatera|uppdatering|sök|efter|installera|dig|nu|"
                r"är|jag|min|vad|kan)\b|[åäö]",
                text,
                re.IGNORECASE,
            )
        )

    def _handle_local_update_command(
        self,
        command: str,
        turn_started: float | None,
    ) -> bool:
        normalized_command = command.strip().strip(" .,!?:;")
        if not self.update_regex.search(normalized_command):
            return False

        result = self.updater.request_manual_update()
        swedish = self._looks_swedish(command)

        if result.error:
            self.say(
                "Kunde inte kontrollera uppdateringar."
                if swedish
                else "I couldn't check for updates.",
                turn_started,
            )
            return True

        if not result.update_available:
            self.say(
                "Jag är redan uppdaterad."
                if swedish
                else "I'm already up to date.",
                turn_started,
            )
            return True

        if result.staged:
            self.say(
                "Uppdaterar nu." if swedish else "Updating now.",
                turn_started,
            )
            if self.updater.launch_apply(reason="voice"):
                self.exit_requested = True
            return True

        self.say(
            "Uppdateringen kunde inte förberedas."
            if swedish
            else "The update couldn't be prepared.",
            turn_started,
        )
        return True

    def handle_audio(
        self,
        frames: list[bytes],
        interaction_started: float | None = None,
    ) -> None:
        transcript = self.transcribe(frames)
        command = self.strip_wake(transcript)
        print(f"\nYOU: {transcript}")
        self.logger.info("Transcript: %s", transcript)
        if command != transcript.strip():
            self.logger.info("Cleaned command after wake removal: %s", command)

        if not command:
            self.say("Yes?", interaction_started)
            followup = self.record_followup()
            if followup is None:
                return
            command = self.strip_wake(self.transcribe(followup).strip())
            print(f"YOU: {command}")

        first_turn = True
        while command:
            if self.is_stop(command):
                return

            turn_started = interaction_started if first_turn else time.perf_counter()
            first_turn = False

            if self._handle_local_update_command(command, turn_started):
                return

            try:
                answer = self.brain.ask(command)
            except Exception as exc:
                self.logger.exception("Brain request failed")
                answer = self.friendly_error(exc)

            self.say(answer, turn_started)
            if self.updater.manual_apply_ready():
                if self.updater.launch_apply(reason="voice-tool"):
                    self.exit_requested = True
                return

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

        total_started = time.perf_counter()
        audio_seconds = len(frames) * FRAME_MS / 1000
        path = self.temp_path(".wav")
        try:
            encode_started = time.perf_counter()
            with wave.open(str(path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(SAMPLE_RATE)
                wav_file.writeframes(b"".join(frames))
            self.logger.info(
                "TIMING | WAV encode: %.3fs | audio=%.2fs",
                time.perf_counter() - encode_started,
                audio_seconds,
            )

            api_started = time.perf_counter()
            with path.open("rb") as audio_file:
                result = self.client.audio.transcriptions.create(
                    model=self.settings.stt_model,
                    file=audio_file,
                    response_format="json",
                    prompt=(
                        "The speaker may use Swedish, English, or both. "
                        "Expected words include Jarvis, Viktor, Home Assistant, "
                        "Stockholm, Göteborg, Gothenburg, Spotify, Steam, and "
                        "smart-home commands."
                    ),
                )
            api_elapsed = time.perf_counter() - api_started
            transcript = str(result.text).strip()
            self.logger.info(
                "TIMING | STT API: %.3fs | audio=%.2fs | realtime_factor=%.2fx | "
                "chars=%d",
                api_elapsed,
                audio_seconds,
                api_elapsed / audio_seconds if audio_seconds > 0 else 0.0,
                len(transcript),
            )
            self.logger.info(
                "TIMING | STT total: %.3fs",
                time.perf_counter() - total_started,
            )
            return transcript
        finally:
            path.unlink(missing_ok=True)

    def say(self, text: str, turn_started: float | None = None) -> None:
        print(f"JARVIS: {text}\n")
        self.logger.info("Jarvis: %s", text)

        if not self.settings.tts_enabled:
            if turn_started is not None:
                self.logger.info(
                    "TIMING | turn until text ready: %.3fs | TTS disabled",
                    time.perf_counter() - turn_started,
                )
            return

        speech = self.clean_speech(text)[:1800]
        if not speech:
            return

        tts_total_started = time.perf_counter()
        path = self.temp_path(".wav")
        self.audio.speaking.set()
        self.audio.disable()

        try:
            api_started = time.perf_counter()
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
            self.logger.info(
                "TIMING | TTS API + download: %.3fs | chars=%d",
                time.perf_counter() - api_started,
                len(speech),
            )

            decode_started = time.perf_counter()
            data, rate = sf.read(path, dtype="float32")
            duration = len(data) / rate if rate else 0.0
            self.logger.info(
                "TIMING | TTS decode: %.3fs | generated_audio=%.2fs | rate=%d",
                time.perf_counter() - decode_started,
                duration,
                rate,
            )

            if turn_started is not None:
                self.logger.info(
                    "TIMING | turn until playback starts: %.3fs",
                    time.perf_counter() - turn_started,
                )

            playback_started = time.perf_counter()
            sd.play(data, rate, device=self.settings.speaker_device, blocking=True)
            self.logger.info(
                "TIMING | playback: %.3fs | expected_audio=%.2fs",
                time.perf_counter() - playback_started,
                duration,
            )
        except Exception:
            self.logger.exception("TTS or playback failed")
        finally:
            self.logger.info(
                "TIMING | TTS + playback total: %.3fs",
                time.perf_counter() - tts_total_started,
            )
            if turn_started is not None:
                self.logger.info(
                    "TIMING | full turn through playback: %.3fs",
                    time.perf_counter() - turn_started,
                )
            path.unlink(missing_ok=True)
            time.sleep(0.15)
            self.audio.speaking.clear()
            self.audio.flush()

    @staticmethod
    def temp_path(suffix: str) -> Path:
        handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir=APP_DIR)
        path = Path(handle.name)
        handle.close()
        return path

    @classmethod
    def strip_wake(cls, text: str) -> str:
        cleaned = text.strip()
        while True:
            match = cls.wake_regex.match(cleaned)
            if not match:
                return cleaned
            cleaned = cleaned[match.end() :].lstrip(" \t\r\n.,:;!?-")

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
