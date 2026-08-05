from __future__ import annotations

import re
from typing import Any

from .language_mode import detect_language
from . import capability_flow

_PATCHED = False

_GITHUB_SUGGESTION_RE = re.compile(
    r"(?:"
    r"\b(?:i\s+have|i've\s+got)\s+(?:another\s+|a\s+)?(?:suggestion|recommendation)\b|"
    r"\b(?:post|submit|create|send)\b.{0,45}\b(?:github|issue|suggestion|recommendation)\b|"
    r"\bjag\s+har\s+(?:en|ett)?\s*(?:till\s+)?(?:rekommendation|förslag)\b|"
    r"\b(?:lägg\s+upp|posta|skicka|skapa)\b.{0,45}\b(?:github|issue|förslag|rekommendation)\b"
    r")",
    re.IGNORECASE,
)


def _extract_goal(text: str) -> str:
    value = text.strip().strip(" .")
    patterns = [
        r"^.*?\b(?:som\s+är\s+att|vilket\s+är\s+att)\s+",
        r"^.*?\b(?:that|which\s+is\s+that)\s+",
    ]
    for pattern in patterns:
        cleaned = re.sub(pattern, "", value, count=1, flags=re.IGNORECASE).strip()
        if cleaned != value and cleaned:
            return cleaned.rstrip(" .")
    return value


def _proposal_name(goal: str, language: str) -> str:
    lower = goal.lower()
    if re.search(r"\b(kortare|kort|för långa svar|svara mindre)\b", lower):
        return "Kortare Jarvis-svar" if language == "sv" else "Shorter Jarvis responses"
    if re.search(r"\b(shorter|too long|long responses|more concise)\b", lower):
        return "Shorter Jarvis responses"

    words = re.findall(r"[A-Za-zÅÄÖåäö0-9'-]+", goal)
    if not words:
        return "Jarvis core suggestion"
    title = " ".join(words[:8])
    return title[:80]


def _is_local_jarvis_suggestion(text: str) -> bool:
    lower = text.lower()
    if not _GITHUB_SUGGESTION_RE.search(text):
        return False
    if re.search(r"\b(release notes?|changelog|versionsanteckningar)\b", lower):
        return False
    return True


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .app import Jarvis
    from .brain import Brain

    original_skill_handler = Jarvis._handle_local_skill_command
    original_create_response = Brain.create_response
    original_instructions = Brain.instructions

    def patched_skill_handler(
        self: Any,
        command: str,
        turn_started: float | None,
    ) -> bool:
        normalized = command.strip()

        if capability_flow._read_pending() is not None:
            return original_skill_handler(self, command, turn_started)

        if not _is_local_jarvis_suggestion(normalized):
            return original_skill_handler(self, command, turn_started)

        language = detect_language(
            normalized,
            getattr(self, "current_language", "en"),
        )
        goal = _extract_goal(normalized)
        proposal = {
            "kind": "core",
            "goal": goal,
            "name": _proposal_name(goal, language),
            "how_it_would_work": (
                "Implement the requested behavior in Jarvis core and add a regression test."
            ),
            "reason": "The user explicitly requested this as a Jarvis core improvement.",
            "language": language,
        }
        capability_flow._save_pending(proposal)
        self.logger.info(
            "CAPABILITY | saved direct GitHub core proposal | name=%r goal=%r",
            proposal["name"],
            proposal["goal"],
        )
        self.say(
            "Jag kan lägga upp det på GitHub. Ska jag?"
            if language == "sv"
            else "I can post that on GitHub. Should I?",
            turn_started,
        )
        return True

    def patched_create_response(self: Any, label: str, **kwargs: Any) -> Any:
        if kwargs.get("tools"):
            current = int(kwargs.get("max_output_tokens") or 0)
            if current < 512:
                kwargs["max_output_tokens"] = 512
        return original_create_response(self, label, **kwargs)

    def patched_needs_web(self: Any, text: str) -> bool:
        if _is_local_jarvis_suggestion(text):
            return False

        if self.CURRENT_RE.search(text):
            return True

        lower = text.strip().lower()
        if re.match(r"^(?:in|at|i|på)\s+\S+", lower):
            previous = ""
            for item in reversed(self.history):
                if item.get("role") == "assistant":
                    previous = str(item.get("content", ""))
                    break
            return bool(
                re.search(
                    r"weather|forecast|temperature|väder|temperatur|regn|rain|"
                    r"city|location|stad|plats",
                    previous,
                    re.IGNORECASE,
                )
            )

        return False

    def patched_instructions(self: Any) -> str:
        base = original_instructions(self)
        return (
            f"{base}\n\n"
            "TOOL-CALL RELIABILITY\n"
            "- Keep every string tool argument concise, normally one short sentence.\n"
            "- Never repeat the same failed tool call with identical arguments.\n"
            "- If a tool reports malformed arguments, correct them once or report failure.\n\n"
            "SPOKEN BREVITY\n"
            "- Default to one sentence of roughly 3 to 12 words.\n"
            "- Do not add an optional follow-up question after a complete answer."
        )

    Jarvis._handle_local_skill_command = patched_skill_handler
    Brain.create_response = patched_create_response
    Brain.needs_web = patched_needs_web
    Brain.instructions = patched_instructions
    _PATCHED = True
