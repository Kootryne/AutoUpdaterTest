from __future__ import annotations

import re
from typing import Any

from .language_mode import detect_language
from .paths import DATA_DIR


_PATCHED = False
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-zÅÄÖåäö]")


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .app import Jarvis

    original_init = Jarvis.__init__
    original_transcribe = Jarvis.transcribe

    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        # Power approvals never survive a process restart or an older release.
        for state_file in (
            DATA_DIR / "pending_pc_power.json",
            DATA_DIR / "pending_pc_power_v090.json",
        ):
            state_file.unlink(missing_ok=True)

    def patched_transcribe(self: Any, frames: list[bytes]) -> str:
        transcript = original_transcribe(self, frames)
        previous = getattr(self, "current_language", "en")
        if (
            transcript
            and _CJK_RE.search(transcript)
            and not _LATIN_RE.search(transcript)
            and previous in {"en", "sv"}
        ):
            self.logger.warning(
                "STT | rejected unexpected non-Latin transcript in "
                "Swedish/English mode: %r",
                transcript,
            )
            return ""

        self.current_language = detect_language(transcript, previous)
        if hasattr(self, "brain"):
            self.brain.response_language = self.current_language
        self.logger.info(
            "LANGUAGE | detected=%s | transcript=%r",
            self.current_language,
            transcript,
        )
        return transcript

    # Luna, not the legacy local stop-phrase matcher, decides whether words such
    # as "cancel" confirm or cancel a pending tool-driven action.
    Jarvis.__init__ = patched_init
    Jarvis.transcribe = patched_transcribe
    Jarvis.is_stop = classmethod(lambda cls, text: False)
    _PATCHED = True
