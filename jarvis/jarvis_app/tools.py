from __future__ import annotations

from datetime import datetime
import logging
import os
import subprocess
import sys
import time
from typing import Any
from zoneinfo import ZoneInfo

from .home_assistant import HomeAssistant
from .settings import Settings


class Tools:
    def __init__(
        self,
        settings: Settings,
        config: dict[str, Any],
        logger: logging.Logger,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.lights = {
            str(key).lower().replace(" ", "_"): str(value)
            for key, value in config.get("lights", {}).items()
        }
        self.apps = {
            str(key).lower().replace(" ", "_"): str(value)
            for key, value in config.get("apps", {}).items()
        }
        self.ha = HomeAssistant(settings, self.lights, logger)

    def schemas(self) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = [
            {"type": "web_search"},
            {
                "type": "function",
                "name": "get_current_time",
                "description": "Get the exact current local date and time.",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
        ]

        if self.lights:
            aliases = sorted(self.lights) + ["all"]
            schemas.extend(
                [
                    {
                        "type": "function",
                        "name": "control_light",
                        "description": (
                            "Control a configured light. For brightness or color "
                            "without an explicit power request, use power 'unchanged'."
                        ),
                        "strict": True,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "light": {"type": "string", "enum": aliases},
                                "power": {
                                    "type": "string",
                                    "enum": ["on", "off", "unchanged"],
                                },
                                "brightness_percent": {
                                    "type": ["integer", "null"],
                                    "minimum": 1,
                                    "maximum": 100,
                                },
                                "rgb_color": {
                                    "type": ["array", "null"],
                                    "items": {
                                        "type": "integer",
                                        "minimum": 0,
                                        "maximum": 255,
                                    },
                                    "minItems": 3,
                                    "maxItems": 3,
                                },
                            },
                            "required": [
                                "light",
                                "power",
                                "brightness_percent",
                                "rgb_color",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "type": "function",
                        "name": "get_light_state",
                        "description": "Get the current state of a configured light.",
                        "strict": True,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "light": {"type": "string", "enum": aliases}
                            },
                            "required": ["light"],
                            "additionalProperties": False,
                        },
                    },
                ]
            )

        if self.apps:
            schemas.append(
                {
                    "type": "function",
                    "name": "open_application",
                    "description": "Open a configured application.",
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "application": {
                                "type": "string",
                                "enum": sorted(self.apps),
                            }
                        },
                        "required": ["application"],
                        "additionalProperties": False,
                    },
                }
            )

        return schemas

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        self.logger.info("Tool: %s %s", name, args)

        try:
            if name == "get_current_time":
                try:
                    timezone = ZoneInfo(self.settings.timezone)
                except Exception:
                    timezone = datetime.now().astimezone().tzinfo
                now = datetime.now(timezone)
                return {
                    "iso": now.isoformat(),
                    "time": now.strftime("%H:%M:%S"),
                    "date": now.strftime("%Y-%m-%d"),
                    "weekday": now.strftime("%A"),
                    "timezone": str(timezone),
                }

            if name == "control_light":
                return self.ha.control(
                    str(args["light"]),
                    str(args["power"]),
                    args.get("brightness_percent"),
                    args.get("rgb_color"),
                )

            if name == "get_light_state":
                return self.ha.state(str(args["light"]))

            if name == "open_application":
                alias = str(args["application"])
                target = self.apps[alias]
                if sys.platform == "win32":
                    os.startfile(target)  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", target])
                else:
                    subprocess.Popen(["xdg-open", target])
                return {"opened": True, "application": alias}

            raise ValueError(f"Unknown tool: {name}")
        finally:
            self.logger.info(
                "TIMING | tool %s: %.3fs",
                name,
                time.perf_counter() - started,
            )
