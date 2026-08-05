from __future__ import annotations

import logging
from typing import Any

import requests

from .settings import Settings


class HomeAssistant:
    def __init__(
        self,
        settings: Settings,
        lights: dict[str, str],
        logger: logging.Logger,
    ) -> None:
        self.url = settings.ha_url
        self.token = settings.ha_token
        self.lights = lights
        self.logger = logger

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def check_config(self) -> None:
        if not self.url or not self.token:
            raise RuntimeError("Home Assistant is not configured in .env.")

    def resolve(self, alias: str) -> list[tuple[str, str]]:
        alias = alias.lower().replace(" ", "_")
        if alias == "all":
            return list(self.lights.items())
        if alias not in self.lights:
            raise ValueError(
                f"Unknown light '{alias}'. Known: {', '.join(self.lights)}"
            )
        return [(alias, self.lights[alias])]

    def control(
        self,
        light: str,
        power: str,
        brightness_percent: int | None,
        rgb_color: list[int] | None,
    ) -> dict[str, Any]:
        self.check_config()
        results = []

        for alias, entity in self.resolve(light):
            service = "turn_off" if power == "off" else "turn_on"
            payload: dict[str, Any] = {"entity_id": entity}

            if service == "turn_on":
                if brightness_percent is not None:
                    payload["brightness_pct"] = max(1, min(100, brightness_percent))
                if rgb_color is not None:
                    payload["rgb_color"] = [
                        max(0, min(255, int(value))) for value in rgb_color
                    ]

            response = requests.post(
                f"{self.url}/api/services/light/{service}",
                headers=self.headers(),
                json=payload,
                timeout=8,
            )
            response.raise_for_status()
            results.append({"light": alias, "service": service, "ok": True})

        return {"results": results}

    def state(self, light: str) -> dict[str, Any]:
        self.check_config()
        results = []

        for alias, entity in self.resolve(light):
            response = requests.get(
                f"{self.url}/api/states/{entity}",
                headers=self.headers(),
                timeout=8,
            )
            response.raise_for_status()
            data = response.json()
            attributes = data.get("attributes", {})
            results.append(
                {
                    "light": alias,
                    "state": data.get("state"),
                    "brightness": attributes.get("brightness"),
                    "rgb_color": attributes.get("rgb_color"),
                }
            )

        return {"states": results}
