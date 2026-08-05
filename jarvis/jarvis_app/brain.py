from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from openai import OpenAI

from .settings import Settings
from .tools import Tools


class Brain:
    CURRENT_RE = re.compile(
        r"\b(weather|forecast|temperature|väder|temperatur|regn|rain|"
        r"today|tomorrow|tonight|idag|imorgon|ikväll|latest|current|recent|"
        r"senaste|nuvarande|president|prime minister|statsminister|news|nyheter|"
        r"price|cost|pris|score|result|schedule|release date|version|"
        r"open now|öppet nu)\b",
        re.IGNORECASE,
    )
    TIME_RE = re.compile(
        r"\b(what time|what date|what day|time is it|klockan|vilket datum|"
        r"vilken dag|dag är det|datum är det)\b",
        re.IGNORECASE,
    )

    def __init__(
        self,
        client: OpenAI,
        settings: Settings,
        config: dict[str, Any],
        tools: Tools,
        logger: logging.Logger,
    ) -> None:
        self.client = client
        self.settings = settings
        self.config = config
        self.tools = tools
        self.logger = logger
        self.history: list[dict[str, Any]] = []

    def instructions(self) -> str:
        assistant = self.config.get("assistant", {})
        owner = assistant.get("owner_name", "Viktor")
        location = assistant.get("location", "Stockholm, Sweden")
        light_names = ", ".join(sorted(self.tools.lights)) or "none"
        app_names = ", ".join(sorted(self.tools.apps)) or "none"
        current_version = (
            self.tools.updater.current_version
            if self.tools.updater is not None
            else "unknown"
        )
        skill_context = (
            self.tools.skill_system.prompt_context()
            if self.tools.skill_system is not None
            else "The skill system is unavailable."
        )

        return f"""
You are Jarvis, {owner}'s live voice assistant running on a Windows 11 PC.

INPUT CONTEXT
- Every user message comes from automatic speech-to-text, not typing.
- The transcription can contain wrong words, repeated phrases, missing
  punctuation, mixed Swedish and English, and phonetic mistakes.
- Infer the most likely intended meaning from context. Do not mention
  transcription errors unless the request is genuinely impossible to understand.
- You are hearing the user through the microphone via transcription. If asked
  whether you can hear them, answer yes.
- This is a live spoken conversation, so replies will be read aloud immediately.

KNOWN CONTEXT
- User: {owner}.
- Default location: {location}.
- Time zone: Europe/Stockholm.
- Device: Windows 11 PC.
- Jarvis version: {current_version}.
- Configured lights: {light_names}.
- Configured applications: {app_names}.
- "Here", "här", and local weather mean {location} unless another place is given.
- "My lamp", "min lampa", and "lampan" mean the bedroom light.
- "LED strip", "ljusslingan", and "LED-listen" mean the LED-strip light.

RESPONSE RULES
- Reply in the language the user mainly used. Swedish and English may be mixed.
- Default to one short sentence, usually 4 to 18 words.
- Action confirmations should usually be 1 to 6 words.
- Give a longer explanation only when the user asks for detail.
- No greetings, preambles, markdown, URLs, citations, source lists, or tool names.
- Do not repeat the user's question.
- Do not add the date or time unless it is relevant or requested.
- Use get_current_time only for an explicit time or date question.
- Use web search for current weather, news, prices, schedules, releases, versions,
  current public office-holders, and other changing facts.
- Use tools before claiming that an action happened.
- If update_jarvis reports a staged update, reply only "Updating now." or
  "Uppdaterar nu." in the user's language.
- If a tool fails, state the failure briefly and truthfully.

SKILL CREATION AND BACKGROUND WORK
- Installed user-created skills appear below as tools. Use them when relevant.
- If the user requests something that no current tool or installed skill can
  actually do, never pretend. Call suggest_new_skill, then say briefly that
  you cannot do it yet and suggest how the proposed skill would work.
- Only call build_new_skill after the user explicitly says to make, build,
  create, or program the skill. Building runs in the background.
- If asked how background work is going, call get_background_status.
- When a long-running skill starts, say it is running and that the user can
  ask for progress. Do not claim its result exists until the task completes.
- Treat task status and skill results as current system state, not guesses.

CURRENT SKILL AND TASK STATE
{skill_context}
""".strip()

    @staticmethod
    def dump_item(item: Any) -> dict[str, Any]:
        if hasattr(item, "model_dump"):
            return item.model_dump(exclude_none=True)
        if isinstance(item, dict):
            return item
        raise TypeError(f"Unsupported response item: {type(item)}")

    @staticmethod
    def output_types(response: Any) -> str:
        return ",".join(
            str(getattr(item, "type", "unknown")) for item in response.output
        )

    @staticmethod
    def usage_text(response: Any) -> str:
        usage = getattr(response, "usage", None)
        if usage is None:
            return "usage=unavailable"
        return (
            f"input={getattr(usage, 'input_tokens', '?')}, "
            f"output={getattr(usage, 'output_tokens', '?')}, "
            f"total={getattr(usage, 'total_tokens', '?')}"
        )

    def create_response(self, label: str, **kwargs: Any) -> Any:
        model = str(kwargs.get("model", ""))
        if model.startswith("gpt-5") and "reasoning" not in kwargs:
            kwargs["reasoning"] = {"effort": "minimal"}

        started = time.perf_counter()
        response = self.client.responses.create(**kwargs)
        elapsed = time.perf_counter() - started
        self.logger.info(
            "TIMING | %s: %.3fs | model=%s | output_types=%s | %s",
            label,
            elapsed,
            model,
            self.output_types(response),
            self.usage_text(response),
        )
        return response

    def context_text(self, new_text: str) -> str:
        recent_text = " ".join(
            str(item.get("content", "")) for item in self.history[-4:]
        )
        return f"{recent_text} {new_text}".strip()

    def needs_web(self, text: str) -> bool:
        return bool(self.CURRENT_RE.search(self.context_text(text)))

    def needs_time(self, text: str) -> bool:
        return bool(self.TIME_RE.search(text))

    def obvious_followup(self, text: str) -> bool | None:
        normalized = text.strip()
        if not normalized:
            return False

        if re.search(r"\b(?:hey\s+)?(?:jarvis|järvis|jervis)\b", normalized, re.I):
            return True

        previous_jarvis = ""
        for item in reversed(self.history):
            if item.get("role") == "assistant":
                previous_jarvis = str(item.get("content", ""))
                break

        lower = normalized.lower().strip(" .,!?")
        if lower in {
            "yes", "no", "yep", "nope", "ja", "nej", "correct", "right",
            "exactly", "precis", "japp", "nä",
        }:
            return True

        if re.match(r"^(?:in|at|i|på)\s+\S+", lower):
            if re.search(
                r"weather|forecast|väder|city|location|stad|plats|where|var",
                previous_jarvis,
                re.I,
            ):
                return True

        if re.match(
            r"^(?:what about|how about|and what|och|men|make it|turn it|"
            r"gör den|sätt den|varför|why|when|när)\b",
            lower,
        ):
            return True

        return None

    def followup_is_for_jarvis(self, text: str) -> bool:
        total_started = time.perf_counter()
        obvious = self.obvious_followup(text)
        if obvious is not None:
            self.logger.info(
                "TIMING | follow-up intent: %.3fs | local=%s | transcript=%r",
                time.perf_counter() - total_started,
                "YES" if obvious else "NO",
                text,
            )
            return obvious

        recent = self.history[-2:]
        previous_user = ""
        previous_jarvis = ""
        for item in recent:
            if item.get("role") == "user":
                previous_user = str(item.get("content", ""))
            elif item.get("role") == "assistant":
                previous_jarvis = str(item.get("content", ""))

        classifier_instructions = """
You are a strict routing classifier for a voice assistant named Jarvis.
Determine whether the new speech is addressed to Jarvis as a continuation of
the immediately preceding interaction. Speech-to-text may contain mistakes.

Return exactly YES or NO.

YES includes an answer, correction, clarification, requested city/date/name,
natural follow-up question, or contextual command. NO includes speech to another
person, background media, unrelated self-talk, or an unclear fragment. When
uncertain, return NO.
""".strip()
        classifier_input = (
            f"Previous user request: {previous_user!r}\n"
            f"Jarvis's reply: {previous_jarvis!r}\n"
            f"New speech: {text.strip()!r}"
        )

        try:
            response = self.create_response(
                "follow-up intent API",
                model=self.settings.followup_model,
                instructions=classifier_instructions,
                input=classifier_input,
                max_output_tokens=16,
                store=False,
            )
            verdict = (response.output_text or "").strip().upper()
            allowed = verdict.startswith("YES")
            self.logger.info(
                "Follow-up intent verdict: %s | raw=%r | transcript=%r",
                "YES" if allowed else "NO",
                verdict,
                text,
            )
            self.logger.info(
                "TIMING | follow-up intent total: %.3fs",
                time.perf_counter() - total_started,
            )
            return allowed
        except Exception:
            self.logger.exception("Follow-up intent classification failed")
            self.logger.info(
                "TIMING | follow-up intent failed after %.3fs",
                time.perf_counter() - total_started,
            )
            return False

    def ask(self, text: str) -> str:
        total_started = time.perf_counter()
        user_item = {"role": "user", "content": text}
        request_input: list[Any] = [*self.history, user_item]
        include_web = self.needs_web(text)
        include_time = self.needs_time(text)
        schemas = self.tools.schemas(
            include_web=include_web,
            include_time=include_time,
        )
        self.logger.info(
            "ROUTING | web=%s | time=%s | model=%s | followup_model=%s",
            include_web,
            include_time,
            self.settings.text_model,
            self.settings.followup_model,
        )

        response = self.create_response(
            "brain API round 1",
            model=self.settings.text_model,
            instructions=self.instructions(),
            input=request_input,
            tools=schemas,
            max_output_tokens=100,
            store=False,
        )

        rounds = 1
        tool_call_count = 0
        for _ in range(6):
            calls = [
                item
                for item in response.output
                if getattr(item, "type", None) == "function_call"
            ]
            if not calls:
                break

            tool_call_count += len(calls)
            request_input.extend(self.dump_item(item) for item in response.output)
            outputs = []

            for call in calls:
                try:
                    args = json.loads(call.arguments or "{}")
                    result = self.tools.call(call.name, args)
                    payload = {"ok": True, "result": result}
                except Exception as exc:
                    self.logger.exception("Tool failed: %s", call.name)
                    payload = {"ok": False, "error": str(exc)}

                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": json.dumps(payload, ensure_ascii=False),
                    }
                )

            request_input.extend(outputs)
            rounds += 1
            response = self.create_response(
                f"brain API round {rounds}",
                model=self.settings.text_model,
                instructions=self.instructions(),
                input=request_input,
                tools=schemas,
                max_output_tokens=100,
                store=False,
            )
        else:
            raise RuntimeError("Too many tool-call rounds.")

        answer = (response.output_text or "").strip()
        if not answer:
            answer = "The request completed, but no spoken answer was generated."

        self.history.extend(
            [user_item, {"role": "assistant", "content": answer}]
        )
        self.history = self.history[-self.settings.max_history :]

        self.logger.info(
            "TIMING | brain total: %.3fs | rounds=%d | function_calls=%d",
            time.perf_counter() - total_started,
            rounds,
            tool_call_count,
        )
        return answer
