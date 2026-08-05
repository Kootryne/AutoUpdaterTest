from __future__ import annotations

from collections import deque
import math
import os
import re
from typing import Any

import numpy as np

from .language_mode import detect_language
from . import reliability_v082

_PATCHED = False

_BASE_WAKE_RE = re.compile(
    r"^\s*(?:(?:hey|hej)[\s,]+)?(?:jarvis|järvis|jervis)\b[\s,.:;!?-]*",
    re.IGNORECASE,
)
_UPDATE_STATUS_RE = re.compile(
    r"(?:"
    r"(?:do|does)\s+(?:you|jarvis)\s+(?:have|got|see)\s+(?:an?\s+|any\s+)?(?:new\s+)?updates?|"
    r"(?:is|are)\s+there\s+(?:an?\s+|any\s+)?(?:new\s+)?updates?|"
    r"(?:are\s+you|is\s+jarvis)\s+up[- ]?to[- ]?date|"
    r"(?:what(?:'s|\s+is)\s+your\s+update\s+status)|"
    r"(?:har\s+du|finns\s+det)\s+(?:en|någon|några)?\s*(?:ny\s+)?uppdatering(?:ar)?|"
    r"(?:är\s+du)\s+uppdaterad|"
    r"(?:finns\s+det)\s+(?:en|någon)?\s*ny\s+version"
    r")",
    re.IGNORECASE,
)

_LAST_WAKE_EVIDENCE: dict[str, Any] | None = None


def _plausible_direct_command(transcript: str) -> bool:
    text = transcript.strip()
    if not text:
        return False
    words = re.findall(r"[A-Za-zÅÄÖåäö']+", text)
    if not words or len(words) > 22:
        return False
    lower = text.lower()
    if len(words) <= 7:
        return True
    return bool(
        re.match(
            r"^(?:can|could|would|will|do|did|are|is|what|why|how|when|where|"
            r"tell|show|open|close|turn|set|check|update|restart|stop|"
            r"kan|kunde|skulle|är|har|vad|varför|hur|när|var|"
            r"berätta|visa|öppna|stäng|sätt|kolla|uppdatera|starta|sluta)\b",
            lower,
            re.IGNORECASE,
        )
    )


class AdaptiveWakeMatcher:
    """Confirm the wake phrase without requiring STT to preserve the name every time."""

    def match(self, transcript: str) -> Any:
        global _LAST_WAKE_EVIDENCE
        direct = _BASE_WAKE_RE.match(transcript)
        evidence = _LAST_WAKE_EVIDENCE or {}
        _LAST_WAKE_EVIDENCE = None
        if direct is not None:
            return direct

        accepted = bool(evidence.get("accepted"))
        peak = float(evidence.get("peak", 0.0))
        hits = int(evidence.get("hits", 0))
        logger = evidence.get("logger")

        fallback = accepted and _plausible_direct_command(transcript)
        if fallback:
            if logger is not None:
                logger.info(
                    "WAKE | accepted without transcribed wake phrase | "
                    "peak=%.3f hits=%d transcript=%r",
                    peak,
                    hits,
                    transcript,
                )
            return object()

        if logger is not None:
            logger.warning(
                "WAKE | rejected after adaptive confirmation | "
                "peak=%.3f hits=%d transcript=%r",
                peak,
                hits,
                transcript,
            )
        return None


class AdaptiveWakeModel:
    """Reject isolated media spikes while preserving ordinary spoken wakes."""

    def __init__(self, inner: Any, settings: Any, logger: Any) -> None:
        self.inner = inner
        self.models = inner.models
        self.logger = logger
        self.debug = bool(getattr(settings, "debug", False))
        self.base_trigger = float(getattr(settings, "wake_threshold", 0.45))
        self.threshold = float(os.getenv("WAKE_CONFIRM_THRESHOLD", "0.44"))
        self.strong_threshold = float(os.getenv("WAKE_STRONG_THRESHOLD", "0.78"))
        self.required_hits = max(1, int(os.getenv("WAKE_CONFIRM_HITS", "2")))
        self.window_frames = max(
            self.required_hits,
            int(os.getenv("WAKE_CONFIRM_WINDOW_FRAMES", "10")),
        )
        self.min_rms = float(os.getenv("WAKE_MIN_RMS", "100"))
        self._scores: deque[float] = deque(maxlen=self.window_frames)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def reset(self) -> None:
        self._scores.clear()
        self.inner.reset()

    def predict(self, samples: np.ndarray) -> dict[str, float]:
        global _LAST_WAKE_EVIDENCE
        scores = dict(self.inner.predict(samples))
        raw = float(scores.get("hey_jarvis", 0.0))
        if samples.size:
            values = samples.astype(np.float32, copy=False)
            rms = float(math.sqrt(float(np.mean(values * values))))
        else:
            rms = 0.0

        eligible = raw if rms >= self.min_rms else 0.0
        self._scores.append(eligible)
        hits = sum(score >= self.threshold for score in self._scores)
        peak = max(self._scores, default=0.0)
        accepted = bool(
            rms >= self.min_rms
            and (
                raw >= self.strong_threshold
                or (raw >= self.threshold and hits >= self.required_hits)
            )
        )

        if self.debug and raw >= 0.08:
            self.logger.debug(
                "Wake gate v083 raw=%.3f rms=%.0f peak=%.3f hits=%d/%d accepted=%s",
                raw,
                rms,
                peak,
                hits,
                self.required_hits,
                accepted,
            )

        if accepted:
            _LAST_WAKE_EVIDENCE = {
                "accepted": True,
                "peak": peak,
                "hits": hits,
                "rms": rms,
                "logger": self.logger,
            }
            scores["hey_jarvis"] = max(raw, self.base_trigger)
            self._scores.clear()
        else:
            scores["hey_jarvis"] = 0.0
        return scores


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .app import Jarvis
    from .brain import Brain
    from .tools import Tools

    original_jarvis_init = Jarvis.__init__
    original_update_handler = Jarvis._handle_local_update_command
    original_instructions = Brain.instructions
    original_tools_call = Tools.call

    reliability_v082.WAKE_RE = AdaptiveWakeMatcher()

    def patched_jarvis_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_jarvis_init(self, *args, **kwargs)
        current = self.wake_model
        inner = getattr(current, "inner", current)
        self.wake_model = AdaptiveWakeModel(inner, self.settings, self.logger)

    def patched_update_handler(
        self: Any,
        command: str,
        turn_started: float | None,
    ) -> bool:
        normalized = command.strip()
        if not _UPDATE_STATUS_RE.search(normalized):
            return original_update_handler(self, command, turn_started)

        language = detect_language(
            normalized,
            getattr(self, "current_language", "en"),
        )
        self.logger.info("UPDATER | fresh voice status check")
        result = self.updater.check_and_stage(
            source="voice-status",
            request_auto_apply=False,
            request_manual_apply=False,
        )

        if result.error:
            self.say(
                "Jag kunde inte kontrollera GitHub just nu."
                if language == "sv"
                else "I couldn't check GitHub right now.",
                turn_started,
            )
            return True

        if result.update_available:
            version = result.remote_version or "en ny version"
            self.say(
                f"Ja. Version {version} finns och är redo att installeras."
                if language == "sv"
                else f"Yes. Version {version} is available and ready to install.",
                turn_started,
            )
            return True

        latest = result.remote_version or result.current_version
        self.say(
            f"Nej. Jag kontrollerade GitHub; version {latest} är den senaste."
            if language == "sv"
            else f"No. I checked GitHub; version {latest} is the latest.",
            turn_started,
        )
        return True

    def patched_instructions(self: Any) -> str:
        base = original_instructions(self)
        return (
            f"{base}\n\n"
            "AUTHORITATIVE UPDATE STATUS\n"
            "- Never answer whether an update exists from memory, conversation history, "
            "the installed version string, or release notes.\n"
            "- For every update-availability or up-to-date question, use update_jarvis.\n"
            "- A status answer is authoritative only after a fresh remote check.\n"
            "- If the check fails, say it could not be verified. Never convert a network "
            "or manifest error into 'no update'.\n"
        )

    def patched_tools_call(
        self: Any,
        name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if name == "update_jarvis" and str(args.get("action")) == "status":
            if self.updater is None:
                raise RuntimeError("The update manager is unavailable.")
            result = self.updater.check_and_stage(
                source="tool-status",
                request_auto_apply=False,
                request_manual_apply=False,
            )
            payload = result.as_dict()
            payload["fresh_remote_check"] = True
            return payload
        return original_tools_call(self, name, args)

    Jarvis.__init__ = patched_jarvis_init
    Jarvis._handle_local_update_command = patched_update_handler
    Brain.instructions = patched_instructions
    Tools.call = patched_tools_call
    _PATCHED = True
