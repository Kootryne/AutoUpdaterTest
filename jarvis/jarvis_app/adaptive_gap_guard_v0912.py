from __future__ import annotations

import logging
import re
from typing import Any

from . import adaptive_gap_guard_v0911
from . import self_modification_v098

_PATCHED = False

_WEATHER_REQUEST_RE = re.compile(
    r"(?:"
    r"\bväder(?:et)?\b|"
    r"\btemperatur(?:en)?\b|"
    r"\butomhustemperatur(?:en)?\b|"
    r"\bhur\s+(?:varmt|kallt)\s+(?:är\s+det\s+)?(?:ute|utomhus)\b|"
    r"\b(?:varmt|kallt)\s+(?:är\s+det\s+)?(?:ute|utomhus)\b|"
    r"\bweather\b|"
    r"\bforecast\b|"
    r"\boutside\s+temperature\b|"
    r"\bhow\s+(?:hot|warm|cold)\s+is\s+it\s+outside\b"
    r")",
    re.IGNORECASE,
)

_POLICY_RE = re.compile(
    r"(?:"
    r"\bi\s+(?:can't|cannot|won't)\s+(?:help|assist)\b|"
    r"\bi\s+can't\s+provide\b|"
    r"\bjag\s+kan\s+inte\s+hjälpa\b|"
    r"\bjag\s+kan\s+inte\s+bistå\b|"
    r"\bjag\s+kan\s+inte\s+tillhandahålla\b"
    r")",
    re.IGNORECASE,
)

_TECHNICAL_DEAD_END_RE = re.compile(
    r"(?:"
    r"\bi\s+(?:can't|cannot|can\s+not|am\s+unable\s+to)\s+"
    r"(?:currently\s+|right\s+now\s+|yet\s+)?"
    r"(?:check|access|see|view|capture|control|fetch|get|retrieve|read|write|"
    r"send|connect|inspect|use|open|close|do|look\s+up|look\s+at)\b|"
    r"\bi\s+(?:don't|do\s+not)\s+(?:currently\s+)?have\s+"
    r"(?:access|the\s+ability|a\s+way|permission)\b|"
    r"\bjag\s+kan\s+inte\s+(?:just\s+nu\s+|ännu\s+|för\s+närvarande\s+)?"
    r"(?:kontrollera|hämta|se|visa|styra|öppna|stänga|läsa|skriva|skicka|"
    r"ansluta|kolla|slå\s+upp|komma\s+åt|inspektera|använda|göra)\b|"
    r"\bjag\s+har\s+inte\s+(?:just\s+nu\s+)?(?:åtkomst|tillgång|möjlighet)\b|"
    r"\bsaknar\s+(?:åtkomst|tillgång|möjlighet)\b"
    r")",
    re.IGNORECASE,
)


def _normalise_apostrophes(value: str) -> str:
    return str(value).replace("’", "'").replace("‘", "'").replace("`", "'")


def _is_technical_dead_end(value: str) -> bool:
    text = _normalise_apostrophes(str(value or "").strip())
    if not text or text.endswith("?"):
        return False
    if _POLICY_RE.search(text):
        return False
    if adaptive_gap_guard_v0911._POLICY_REFUSAL_RE.search(text):
        return False
    return bool(
        _TECHNICAL_DEAD_END_RE.search(text)
        or adaptive_gap_guard_v0911._looks_like_capability_dead_end(text)
    )


def _recovery_prompt(user_text: str, failed_answer: str) -> str:
    weather_test = bool(_WEATHER_REQUEST_RE.search(user_text))
    special = (
        "This is a deliberate adaptive-learning test for weather. Web search is "
        "intentionally unavailable for this request even if Jarvis normally has a "
        "generic web-search capability. Do not try to answer the weather from web "
        "search and do not stop at 'I cannot'. Call plan_core_change for a dedicated "
        "persistent built-in weather capability, then briefly explain what Jarvis "
        "would add and ask the user one explicit yes/no question."
        if weather_test
        else
        "Use an existing integration if one can genuinely perform the request. If "
        "one short missing fact would unlock it, ask only that question. Otherwise "
        "call plan_core_change for a concrete persistent built-in capability and ask "
        "the user one explicit yes/no question after the plan."
    )
    return (
        "[INTERNAL ADAPTIVE GAP RECOVERY V0.9.12]\n"
        "The previous answer was a technical dead-end and must not be spoken as the "
        "final answer. The user does not have to explicitly ask Jarvis to change itself.\n\n"
        + special
        + "\n\nNever use legacy skills. Never self-modify to bypass a safety or policy refusal. "
        "Do not claim a capability exists until a tool confirms it.\n\n"
        f"ACTUAL USER REQUEST:\n{user_text}\n\n"
        f"FAILED DRAFT:\n{failed_answer}"
    )


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .brain import Brain

    current_needs_web = Brain.needs_web
    current_ask = Brain.ask
    current_instructions = Brain.instructions

    # Strengthen the v0.9.11 detector too, because its Brain.ask wrapper resolves
    # this module global at runtime.
    adaptive_gap_guard_v0911._looks_like_capability_dead_end = _is_technical_dead_end

    def needs_web(self: Brain, text: str) -> bool:
        # Deliberately leave weather without generic web search so it exercises the
        # adaptive core path and can become a persistent dedicated capability.
        if _WEATHER_REQUEST_RE.search(str(text)):
            return False
        return current_needs_web(self, text)

    def instructions(self: Brain) -> str:
        return (
            current_instructions(self)
            + "\n\nADAPTIVE CORE V0.9.12\n"
            "- A normal technical capability dead-end is not an acceptable final "
            "answer. Use/discover/configure an existing route, ask one useful missing "
            "question, or plan a persistent core capability.\n"
            "- For weather and outdoor-temperature requests, generic web search is "
            "deliberately disabled as an adaptive-learning test. If no dedicated "
            "weather capability already exists, call plan_core_change and offer to "
            "add one. Do not merely say current weather is unavailable.\n"
            "- Safety and policy refusals are not capability gaps and must never be "
            "converted into self-modification.\n"
        )

    def ask(self: Brain, text: str) -> str:
        before_history = list(self.history)
        before_turn = int(getattr(self, "_v090_turn_id", 0))
        answer = current_ask(self, text)

        if self_modification_v098._pending() is not None:
            return answer
        if not _is_technical_dead_end(answer):
            return answer

        self.logger.warning(
            "ADAPTIVE GAP V0912 | intercepted technical dead-end | request=%r | draft=%r",
            text,
            answer,
        )

        self.history = before_history
        try:
            recovered = current_ask(self, _recovery_prompt(text, answer))
        except Exception:
            self.history = before_history
            self._v090_turn_id = before_turn + 1
            if hasattr(self, "tools"):
                self.tools._v090_turn_id = self._v090_turn_id
            self.logger.exception("ADAPTIVE GAP V0912 | recovery failed")
            return answer

        self.history = [
            *before_history,
            {"role": "user", "content": text},
            {"role": "assistant", "content": recovered},
        ][-self.settings.max_history :]
        self._v090_turn_id = before_turn + 1
        if hasattr(self, "tools"):
            self.tools._v090_turn_id = self._v090_turn_id
        self.logger.info(
            "ADAPTIVE GAP V0912 | recovery completed | request=%r | answer=%r",
            text,
            recovered,
        )
        return recovered

    Brain.needs_web = needs_web
    Brain.instructions = instructions
    Brain.ask = ask

    logging.getLogger("jarvis").info(
        "ADAPTIVE GAP V0912 | active | weather_generic_web=false | outer_guard=true"
    )
    _PATCHED = True
