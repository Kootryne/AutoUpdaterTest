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
        owner = assistant.get("owner_name", "the user")
        style = assistant.get("reply_style", "")
        location = assistant.get("location", "Stockholm, Sweden")
        return f"""
You are Jarvis, a fast voice assistant on {owner}'s computer.
- Reply in the language the user mainly used. Swedish and English may be mixed.
- Keep ordinary spoken answers short, normally 10 to 35 words.
- {style}
- Use get_current_time for the exact time or date.
- Use web search for recent, changing, political, weather, news, price,
  schedule, software-version, or current office-holder questions.
- When a weather request has no place, use the user's default location:
  {location}.
- Use tools before claiming that you controlled a device or opened an app.
- Never say an action succeeded if its tool returned an error.
- Do not read URLs, citation syntax, markdown, or source lists aloud.
- Interpret "my lamp", "min lampa", and "lampan" as "bedroom".
- Interpret "LED strip", "ljusslingan", and "LED-listen" as "led_strip".
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
        types = [str(getattr(item, "type", type(item).__name__)) for item in response.output]
        return ",".join(types) if types else "none"

    @staticmethod
    def usage_text(response: Any) -> str:
        usage = getattr(response, "usage", None)
        if usage is None:
            return "usage unavailable"
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)
        return (
            f"input={input_tokens}, output={output_tokens}, total={total_tokens}"
        )

    def create_response(self, timing_name: str, **kwargs: Any) -> Any:
        started = time.perf_counter()
        response = self.client.responses.create(**kwargs)
        elapsed = time.perf_counter() - started
        self.logger.info(
            "TIMING | %s: %.3fs | output_types=%s | %s",
            timing_name,
            elapsed,
            self.output_types(response),
            self.usage_text(response),
        )
        return response

    def followup_is_for_jarvis(self, text: str) -> bool:
        total_started = time.perf_counter()
        normalized = text.strip()
        if not normalized:
            self.logger.info("TIMING | follow-up intent: 0.000s | empty transcript")
            return False

        if re.search(r"\b(?:hey\s+)?(?:jarvis|järvis|jervis)\b", normalized, re.I):
            self.logger.info(
                "TIMING | follow-up intent: %.3fs | explicit Jarvis name | YES",
                time.perf_counter() - total_started,
            )
            return True

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
the immediately preceding interaction.

Return exactly YES or NO.

Return YES when it clearly:
- answers a question Jarvis just asked;
- supplies requested information such as a city, date, name, amount, or choice;
- confirms, rejects, corrects, or clarifies the prior request;
- asks a natural follow-up about Jarvis's last answer;
- gives Jarvis another command using context such as "turn it down";
- directly addresses Jarvis.

Return NO when it appears to:
- start or continue a conversation with another person;
- be unrelated background speech, media, or self-talk;
- address someone else by name;
- be an incomplete fragment with no clear connection;
- merely be a statement that does not call for Jarvis.

Speech-to-text may contain mistakes. Judge the likely intent using context rather
than requiring perfect wording. When uncertain, return NO. Do not explain.
""".strip()

        classifier_input = (
            f"Previous user request: {previous_user!r}\n"
            f"Jarvis's reply: {previous_jarvis!r}\n"
            f"New speech: {normalized!r}"
        )

        try:
            response = self.create_response(
                "follow-up intent API",
                model=self.settings.text_model,
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
                normalized,
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

        response = self.create_response(
            "brain API round 1",
            model=self.settings.text_model,
            instructions=self.instructions(),
            input=request_input,
            tools=self.tools.schemas(),
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
                tools=self.tools.schemas(),
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
