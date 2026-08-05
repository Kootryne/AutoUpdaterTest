from __future__ import annotations

import re
from typing import Any

_PATCHED = False

_SWEDISH_WORDS = {
    "och", "att", "det", "den", "de", "är", "var", "vad", "vem", "hur",
    "varför", "när", "kan", "kunde", "ska", "skulle", "vill", "jag", "du",
    "mig", "dig", "min", "mitt", "mina", "här", "där", "inte", "också",
    "med", "för", "från", "till", "på", "av", "om", "en", "ett", "nu",
    "sen", "gör", "göra", "starta", "stäng", "uppdatera", "skill", "skillen",
    "väder", "klockan", "idag", "imorgon", "ja", "nej", "tack",
}
_ENGLISH_WORDS = {
    "and", "that", "this", "it", "is", "was", "what", "who", "how", "why",
    "when", "can", "could", "should", "would", "will", "want", "i", "you",
    "me", "my", "your", "here", "there", "not", "also", "with", "for",
    "from", "to", "on", "off", "about", "a", "an", "the", "now", "later",
    "make", "start", "stop", "restart", "update", "weather", "time", "today",
    "tomorrow", "yes", "no", "thanks", "please",
}


def detect_language(text: str, previous: str = "en") -> str:
    """Return sv or en, preserving prior language for tiny ambiguous replies."""
    normalized = text.lower().strip()
    if not normalized:
        return previous if previous in {"sv", "en"} else "en"

    words = re.findall(r"[a-zåäö']+", normalized)
    sv_score = sum(1 for word in words if word in _SWEDISH_WORDS)
    en_score = sum(1 for word in words if word in _ENGLISH_WORDS)
    sv_score += 2 * sum(normalized.count(char) for char in "åäö")

    if normalized in {"ja", "japp", "nej", "nä", "gärna", "okej då"}:
        return "sv"
    if normalized in {"yes", "yep", "no", "nope", "sure", "okay then"}:
        return "en"

    if sv_score > en_score:
        return "sv"
    if en_score > sv_score:
        return "en"
    if len(words) <= 3:
        return previous if previous in {"sv", "en"} else "en"
    return "sv" if any(char in normalized for char in "åäö") else "en"


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .app import Jarvis
    from .brain import Brain
    from . import process_controls

    original_brain_init = Brain.__init__
    original_brain_ask = Brain.ask
    original_instructions = Brain.instructions
    original_transcribe = Jarvis.transcribe

    def patched_brain_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_brain_init(self, *args, **kwargs)
        self.response_language = "en"

    def patched_brain_ask(self: Any, text: str) -> str:
        self.response_language = detect_language(
            text, getattr(self, "response_language", "en")
        )
        return original_brain_ask(self, text)

    def patched_instructions(self: Any) -> str:
        base = original_instructions(self)
        language = getattr(self, "response_language", "en")
        if language == "sv":
            rule = (
                "CURRENT LANGUAGE: Swedish. Reply entirely in natural Swedish. "
                "Do not switch to English unless the user explicitly asks you to."
            )
        else:
            rule = (
                "CURRENT LANGUAGE: English. Reply entirely in natural English. "
                "Do not switch to Swedish unless the user explicitly asks you to."
            )
        capability_rule = (
            "If Jarvis cannot perform a capability, first consider a generated skill. "
            "If it cannot be implemented safely as a generated skill, use "
            "submit_jarvis_feature_request to send a concise developer suggestion to GitHub."
        )
        return f"{base}\n\n{rule}\n{capability_rule}"

    def patched_transcribe(self: Any, frames: list[bytes]) -> str:
        transcript = original_transcribe(self, frames)
        previous = getattr(self, "current_language", "en")
        self.current_language = detect_language(transcript, previous)
        if hasattr(self, "brain"):
            self.brain.response_language = self.current_language
        self.logger.info(
            "LANGUAGE | detected=%s | transcript=%r",
            self.current_language,
            transcript,
        )
        return transcript

    Brain.__init__ = patched_brain_init
    Brain.ask = patched_brain_ask
    Brain.instructions = patched_instructions
    Jarvis.transcribe = patched_transcribe
    process_controls._is_swedish = lambda text: detect_language(text, "en") == "sv"
    _PATCHED = True
