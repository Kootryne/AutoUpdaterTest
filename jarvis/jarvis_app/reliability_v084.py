from __future__ import annotations

from collections import deque
import math
import os
import re
from typing import Any

import numpy as np

from . import reliability_v083

_PATCHED = False

_BASE_WAKE_RE = re.compile(
    r"^\s*(?:(?:hey|hej)[\s,]+)?(?:jarvis|järvis|jervis)\b[\s,.:;!?-]*",
    re.IGNORECASE,
)


def _plausible_direct_command(transcript: str) -> bool:
    text = transcript.strip()
    if not text:
        return False
    words = re.findall(r"[A-Za-zÅÄÖåäö']+", text)
    if not words or len(words) > 18:
        return False
    if len(words) <= 6:
        return True
    return bool(
        re.match(
            r"^(?:can|could|would|will|do|did|are|is|what|why|how|when|where|"
            r"tell|show|open|close|turn|set|check|update|restart|stop|"
            r"kan|kunde|skulle|är|har|vad|varför|hur|när|var|"
            r"berätta|visa|öppna|stäng|sätt|kolla|uppdatera|starta|sluta)\b",
            text.lower(),
            re.IGNORECASE,
        )
    )


class DelayedEnergyWakeMatcher:
    """Use transcript confirmation, with a strict fallback for clipped wake words."""

    def match(self, transcript: str) -> Any:
        evidence = reliability_v083._LAST_WAKE_EVIDENCE or {}
        reliability_v083._LAST_WAKE_EVIDENCE = None

        direct = _BASE_WAKE_RE.match(transcript)
        if direct is not None:
            logger = evidence.get("logger")
            if logger is not None:
                logger.info(
                    "WAKE | transcript wake phrase confirmed | peak=%.3f recent_rms=%.0f",
                    float(evidence.get("peak", 0.0)),
                    float(evidence.get("recent_rms", 0.0)),
                )
            return direct

        peak = float(evidence.get("peak", 0.0))
        distinct_hits = int(evidence.get("distinct_hits", 0))
        recent_rms = float(evidence.get("recent_rms", 0.0))
        logger = evidence.get("logger")

        fallback = bool(
            evidence.get("accepted")
            and peak >= float(os.getenv("WAKE_CLIPPED_MIN_PEAK", "0.90"))
            and distinct_hits >= int(os.getenv("WAKE_CLIPPED_MIN_HITS", "2"))
            and recent_rms >= float(os.getenv("WAKE_RECENT_RMS_THRESHOLD", "25"))
            and _plausible_direct_command(transcript)
        )

        if fallback:
            if logger is not None:
                logger.info(
                    "WAKE | accepted clipped wake phrase | peak=%.3f "
                    "distinct_hits=%d recent_rms=%.0f transcript=%r",
                    peak,
                    distinct_hits,
                    recent_rms,
                    transcript,
                )
            return object()

        if logger is not None:
            logger.warning(
                "WAKE | rejected after delayed-energy confirmation | peak=%.3f "
                "distinct_hits=%d recent_rms=%.0f transcript=%r",
                peak,
                distinct_hits,
                recent_rms,
                transcript,
            )
        return None


class DelayedEnergyWakeModel:
    """Align energy evidence with openWakeWord's delayed prediction scores."""

    def __init__(self, inner: Any, settings: Any, logger: Any) -> None:
        self.inner = inner
        self.models = inner.models
        self.logger = logger
        self.debug = bool(getattr(settings, "debug", False))
        self.base_trigger = float(getattr(settings, "wake_threshold", 0.45))
        self.threshold = float(os.getenv("WAKE_CONFIRM_THRESHOLD", "0.44"))
        self.required_hits = max(1, int(os.getenv("WAKE_CONFIRM_HITS", "2")))
        self.score_window = max(
            self.required_hits,
            int(os.getenv("WAKE_CONFIRM_WINDOW_FRAMES", "12")),
        )
        self.energy_window = max(
            8,
            int(os.getenv("WAKE_ENERGY_WINDOW_FRAMES", "50")),
        )
        self.min_recent_rms = float(
            os.getenv("WAKE_RECENT_RMS_THRESHOLD", "25")
        )
        self._scores: deque[float] = deque(maxlen=self.score_window)
        self._recent_rms: deque[float] = deque(maxlen=self.energy_window)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def reset(self) -> None:
        self._scores.clear()
        self._recent_rms.clear()
        self.inner.reset()

    def predict(self, samples: np.ndarray) -> dict[str, float]:
        scores = dict(self.inner.predict(samples))
        raw = float(scores.get("hey_jarvis", 0.0))

        if samples.size:
            values = samples.astype(np.float32, copy=False)
            rms = float(math.sqrt(float(np.mean(values * values))))
        else:
            rms = 0.0

        self._recent_rms.append(rms)
        recent_rms = max(self._recent_rms, default=0.0)

        self._scores.append(raw)
        score_hits = sum(score >= self.threshold for score in self._scores)

        compressed: list[float] = []
        for value in self._scores:
            if not compressed or abs(value - compressed[-1]) >= 0.005:
                compressed.append(value)
        distinct_hits = sum(value >= self.threshold for value in compressed)
        peak = max(self._scores, default=0.0)

        accepted = bool(
            recent_rms >= self.min_recent_rms
            and (
                (
                    raw >= self.threshold
                    and distinct_hits >= self.required_hits
                )
                or (
                    raw >= 0.995
                    and score_hits >= self.required_hits
                )
            )
        )

        if self.debug and raw >= 0.08:
            self.logger.debug(
                "Wake gate v084 raw=%.3f frame_rms=%.0f recent_rms=%.0f "
                "peak=%.3f score_hits=%d distinct_hits=%d accepted=%s",
                raw,
                rms,
                recent_rms,
                peak,
                score_hits,
                distinct_hits,
                accepted,
            )

        if accepted:
            reliability_v083._LAST_WAKE_EVIDENCE = {
                "accepted": True,
                "peak": peak,
                "score_hits": score_hits,
                "distinct_hits": distinct_hits,
                "recent_rms": recent_rms,
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

    original_jarvis_init = Jarvis.__init__

    reliability_v083.reliability_v082.WAKE_RE = DelayedEnergyWakeMatcher()

    def patched_jarvis_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_jarvis_init(self, *args, **kwargs)
        current = self.wake_model
        inner = getattr(current, "inner", current)
        self.wake_model = DelayedEnergyWakeModel(
            inner,
            self.settings,
            self.logger,
        )

    Jarvis.__init__ = patched_jarvis_init
    _PATCHED = True
