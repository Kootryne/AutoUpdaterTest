from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
import sys

from dotenv import load_dotenv

from .paths import ENV_FILE


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_device(value: str | None) -> int | str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    return int(value) if value.lstrip("-").isdigit() else value


def find_openai_api_key() -> str:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if key and key != "sk-proj-replace_me":
        return key

    if sys.platform == "win32":
        command = (
            "$k=[Environment]::GetEnvironmentVariable("
            "'OPENAI_API_KEY','User');"
            "if(-not $k){$k=[Environment]::GetEnvironmentVariable("
            "'OPENAI_API_KEY','Machine')};"
            "if($k){[Console]::Out.Write($k)}"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            key = result.stdout.strip()
            if key and key != "sk-proj-replace_me":
                return key
        except (OSError, subprocess.SubprocessError):
            pass

    return ""


@dataclass(slots=True)
class Settings:
    api_key: str
    stt_model: str
    text_model: str
    tts_model: str
    tts_voice: str
    tts_enabled: bool
    mic_device: int | str | None
    speaker_device: int | str | None
    wake_threshold: float
    wake_vad_threshold: float
    wake_cooldown: float
    energy_threshold: int
    vad_aggressiveness: int
    end_silence: float
    followup_end_silence: float
    followup_timeout: float
    followup_require_intent: bool
    max_utterance: float
    pre_roll: float
    timezone: str
    max_history: int
    debug: bool
    ha_url: str
    ha_token: str

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(ENV_FILE)
        return cls(
            api_key=find_openai_api_key(),
            stt_model=os.getenv("STT_MODEL", "gpt-4o-mini-transcribe").strip(),
            text_model=os.getenv("TEXT_MODEL", "gpt-5-mini").strip(),
            tts_model=os.getenv("TTS_MODEL", "gpt-4o-mini-tts").strip(),
            tts_voice=os.getenv("TTS_VOICE", "cedar").strip(),
            tts_enabled=env_bool("TTS_ENABLED", True),
            mic_device=parse_device(os.getenv("MIC_DEVICE")),
            speaker_device=parse_device(os.getenv("SPEAKER_DEVICE")),
            wake_threshold=float(os.getenv("WAKE_THRESHOLD", "0.45")),
            wake_vad_threshold=float(os.getenv("WAKE_VAD_THRESHOLD", "0.20")),
            wake_cooldown=float(os.getenv("WAKE_COOLDOWN_SECONDS", "1.5")),
            energy_threshold=int(os.getenv("ENERGY_THRESHOLD", "350")),
            vad_aggressiveness=int(os.getenv("VAD_AGGRESSIVENESS", "2")),
            end_silence=float(os.getenv("END_SILENCE_SECONDS", "1.05")),
            followup_end_silence=float(
                os.getenv("FOLLOWUP_END_SILENCE_SECONDS", "0.90")
            ),
            followup_timeout=float(os.getenv("FOLLOWUP_TIMEOUT_SECONDS", "4.0")),
            followup_require_intent=env_bool("FOLLOWUP_REQUIRE_INTENT", True),
            max_utterance=float(os.getenv("MAX_UTTERANCE_SECONDS", "25")),
            pre_roll=float(os.getenv("PRE_ROLL_SECONDS", "1.5")),
            timezone=os.getenv("TIMEZONE", "Europe/Stockholm").strip(),
            max_history=int(os.getenv("MAX_HISTORY_MESSAGES", "12")),
            debug=env_bool("DEBUG", True),
            ha_url=os.getenv("HOME_ASSISTANT_URL", "").strip().rstrip("/"),
            ha_token=os.getenv("HOME_ASSISTANT_TOKEN", "").strip(),
        )
