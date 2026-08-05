from __future__ import annotations

import re
from typing import Any

_PATCHED = False
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-zÅÄÖåäö]")


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .app import Jarvis
    from .brain import Brain

    original_transcribe = Jarvis.transcribe
    original_obvious_followup = Brain.obvious_followup

    def patched_transcribe(self: Any, frames: list[bytes]) -> str:
        transcript = original_transcribe(self, frames)
        if (
            transcript
            and _CJK_RE.search(transcript)
            and not _LATIN_RE.search(transcript)
            and getattr(self, "current_language", "en") in {"en", "sv"}
        ):
            self.logger.warning(
                "STT | rejected unexpected non-Latin transcript in Swedish/English mode: %r",
                transcript,
            )
            return ""
        return transcript

    def patched_obvious_followup(self: Any, text: str) -> bool | None:
        result = original_obvious_followup(self, text)
        if result is not None:
            return result
        normalized = text.lower().strip(" .,!?:;")
        if re.search(
            r"\b(?:skill\w*|kamera\w*|skärm\w*|camera\w*|screen\w*|webcam\w*)\b",
            normalized,
            re.IGNORECASE,
        ):
            return True
        if re.match(
            r"^(?:okej|okay|but|men|try|försök|check|kolla|do|gör|build|bygg)\b",
            normalized,
            re.IGNORECASE,
        ):
            return True
        return None

    Jarvis.transcribe = patched_transcribe
    Brain.obvious_followup = patched_obvious_followup
    _PATCHED = True
