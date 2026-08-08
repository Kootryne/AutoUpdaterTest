from __future__ import annotations

import re
from typing import Any

from . import self_modification_v098

_PATCHED = False

_EXTRA_CURRENT_RE = re.compile(
    r"\b(?:"
    r"hur\s+(?:varmt|kallt)\s+(?:är\s+det\s+)?(?:ute|utomhus)|"
    r"(?:varmt|kallt)\s+(?:är\s+det\s+)?(?:ute|utomhus)|"
    r"(?:grader|grad)\s+(?:är\s+det\s+)?(?:ute|utomhus)|"
    r"utomhustemperatur(?:en)?|"
    r"outside\s+temperature|"
    r"how\s+(?:hot|cold|warm)\s+is\s+it\s+outside|"
    r"how\s+many\s+degrees\s+is\s+it\s+outside"
    r")\b",
    re.IGNORECASE,
)

_CAPABILITY_DEAD_END_RE = re.compile(
    r"(?:"
    r"\b(?:i|we)\s+(?:can't|cannot|can not|am unable to|are unable to)\s+"
    r"(?:currently\s+|right\s+now\s+|yet\s+)?"
    r"(?:access|see|view|capture|control|open|close|fetch|get|retrieve|read|write|"
    r"send|connect|check|look up|look at|inspect|use|do)\b|"
    r"\b(?:i|we)\s+(?:don't|do not)\s+(?:currently\s+)?have\s+"
    r"(?:access|the ability|a way|permission)\b|"
    r"\bjag\s+kan\s+inte\s+(?:just\s+nu\s+|ännu\s+|för\s+närvarande\s+)?"
    r"(?:hämta|se|visa|ta\s+en\s+skärmbild|styra|öppna|stänga|läsa|skriva|"
    r"skicka|ansluta|kontrollera|kolla|slå\s+upp|komma\s+åt|göra)\b|"
    r"\bjag\s+har\s+inte\s+(?:just\s+nu\s+)?(?:åtkomst|tillgång|möjlighet)\b|"
    r"\bsaknar\s+(?:åtkomst|tillgång|möjlighet)\b"
    r")",
    re.IGNORECASE,
)

_POLICY_REFUSAL_RE = re.compile(
    r"(?:"
    r"\bi\s+(?:can't|cannot|won't)\s+(?:help|assist)\s+(?:with|you\s+with)\b|"
    r"\bi\s+can't\s+provide\b|"
    r"\bi\s+can't\s+help\s+you\b|"
    r"\bjag\s+kan\s+inte\s+hjälpa\s+(?:till\s+med|dig\s+med)\b|"
    r"\bjag\s+kan\s+inte\s+bistå\b|"
    r"\bjag\s+kan\s+inte\s+tillhandahålla\b"
    r")",
    re.IGNORECASE,
)

_META_CAPABILITY_RE = re.compile(
    r"(?:"
    r"\bwhat\s+(?:can(?:'t|not)|can't)\s+you\s+do\b|"
    r"\bwhat\s+are\s+your\s+limitations\b|"
    r"\bvad\s+kan\s+du\s+inte\s+göra\b|"
    r"\bvilka\s+begränsningar\s+har\s+du\b"
    r")",
    re.IGNORECASE,
)


def _looks_like_capability_dead_end(text: str) -> bool:
    value = str(text or "").strip()
    if not value or value.endswith("?"):
        return False
    if _POLICY_REFUSAL_RE.search(value):
        return False
    return bool(_CAPABILITY_DEAD_END_RE.search(value))


def _adaptive_recovery_prompt(user_text: str, failed_answer: str) -> str:
    return (
        "[INTERNAL ADAPTIVE CAPABILITY RECOVERY]\n"
        "The actual user request is quoted below. Your previous draft ended in a "
        "capability dead-end. Do not repeat that dead-end as the final answer.\n\n"
        "Re-evaluate the request under these rules:\n"
        "1. First use an existing capability if one can solve it. This includes web "
        "search for current public information, Home Assistant discovery/control for "
        "home devices, Jarvis settings, and all other currently exposed tools.\n"
        "2. If an existing route needs exactly one missing fact or confirmation, ask "
        "one short natural question and stop there.\n"
        "3. If Jarvis genuinely lacks the capability, call plan_core_change now with "
        "a concrete persistent built-in capability. After planning, briefly explain "
        "what will be added and ask one explicit yes/no question. Do not wait for the "
        "user to explicitly say 'change yourself'.\n"
        "4. Do not create or mention legacy skills.\n"
        "5. Do not use self-modification to bypass a safety or policy refusal. If the "
        "request is disallowed rather than technically unsupported, keep the refusal.\n"
        "6. Do not claim the capability already exists until the relevant tool confirms it.\n\n"
        f"ACTUAL USER REQUEST:\n{user_text}\n\n"
        f"PREVIOUS DEAD-END DRAFT:\n{failed_answer}"
    )


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .brain import Brain

    original_needs_web = Brain.needs_web
    original_ask = Brain.ask
    original_instructions = Brain.instructions

    def needs_web(self: Brain, text: str) -> bool:
        if _EXTRA_CURRENT_RE.search(self.context_text(text)):
            return True
        return original_needs_web(self, text)

    def instructions(self: Brain) -> str:
        return (
            original_instructions(self)
            + "\n\nADAPTIVE DEAD-END RULE\n"
            "- A technical capability limitation is never a satisfactory final answer "
            "for a normal user request. Before saying you cannot access, see, control, "
            "retrieve, inspect, connect to, or otherwise perform something, first try "
            "an existing integration/tool. If one useful fact is missing, ask that one "
            "question. If no current route exists, call plan_core_change and offer to "
            "add the capability. The user does not need to explicitly ask you to modify "
            "yourself. Safety/policy refusals are different and must remain refusals.\n"
            "- Current outdoor temperature/weather questions are current-information "
            "requests. Use web search when it is available rather than claiming weather "
            "is unavailable.\n"
        )

    def ask(self: Brain, text: str) -> str:
        previous_history = list(self.history)
        previous_turn = int(getattr(self, "_v090_turn_id", 0))
        answer = original_ask(self, text)

        if _META_CAPABILITY_RE.search(text):
            return answer
        if self_modification_v098._pending() is not None:
            return answer
        if not _looks_like_capability_dead_end(answer):
            return answer

        self.logger.warning(
            "ADAPTIVE GAP | intercepted capability dead-end | request=%r | draft=%r",
            text,
            answer,
        )

        self.history = previous_history
        recovery = _adaptive_recovery_prompt(text, answer)
        try:
            recovered = original_ask(self, recovery)
        except Exception:
            self.history = previous_history
            self._v090_turn_id = previous_turn + 1
            if hasattr(self, "tools"):
                self.tools._v090_turn_id = self._v090_turn_id
            self.logger.exception("ADAPTIVE GAP | recovery pass failed")
            return answer

        # Hide the internal recovery message from conversational history.
        self.history = [
            *previous_history,
            {"role": "user", "content": text},
            {"role": "assistant", "content": recovered},
        ][-self.settings.max_history :]
        self._v090_turn_id = previous_turn + 1
        if hasattr(self, "tools"):
            self.tools._v090_turn_id = self._v090_turn_id
        self.logger.info(
            "ADAPTIVE GAP | recovery completed | request=%r | answer=%r",
            text,
            recovered,
        )
        return recovered

    Brain.needs_web = needs_web
    Brain.instructions = instructions
    Brain.ask = ask
    _PATCHED = True
