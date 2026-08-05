from __future__ import annotations

import json
import logging
import re
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
        return f"""
You are Jarvis, a fast voice assistant on {owner}'s computer.
- Reply in the language the user mainly used. Swedish and English may be mixed.
- Keep ordinary spoken answers short, normally 10 to 35 words.
- {style}
- Use get_current_time for the exact time or date.
- Use web search for recent, changing, political, weather, news, price,
  schedule, software-version, or current office-holder questions.
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

    def followup_is_for_jarvis(self, text: str) -> bool:
        normalized = text.strip()
        if not normalized:
            return False

        if re.search(r"\b(?:hey\s+)?(?:jarvis|järvis|jervis)\b", normalized, re.I):
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

When uncertain, return NO. Do not explain.
""".strip()

        classifier_input = (
            f"Previous user request: {previous_user!r}\n"
            f"Jarvis's reply: {previous_jarvis!r}\n"
            f"New speech: {normalized!r}"
        )

        try:
            response = self.client.responses.create(
                model=self.settings.text_model,
                instructions=classifier_instructions,
                input=classifier_input,
                max_output_tokens=5,
                store=False,
            )
            verdict = (response.output_text or "").strip().upper()
            allowed = verdict.startswith("YES")
            self.logger.info(
                "Follow-up intent verdict: %s | transcript=%r",
                "YES" if allowed else "NO",
                normalized,
            )
            return allowed
        except Exception:
            self.logger.exception("Follow-up intent classification failed")
            return False

    def ask(self, text: str) -> str:
        user_item = {"role": "user", "content": text}
        request_input: list[Any] = [*self.history, user_item]

        response = self.client.responses.create(
            model=self.settings.text_model,
            instructions=self.instructions(),
            input=request_input,
            tools=self.tools.schemas(),
            store=False,
        )

        for _ in range(6):
            calls = [
                item
                for item in response.output
                if getattr(item, "type", None) == "function_call"
            ]
            if not calls:
                break

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
            response = self.client.responses.create(
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
        return answer
