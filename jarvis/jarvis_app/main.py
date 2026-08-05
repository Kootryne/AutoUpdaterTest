from __future__ import annotations

import argparse
import json
from typing import Any

import sounddevice as sd

from .app import Jarvis
from .logging_utils import setup_logger
from .paths import CONFIG_FILE, LOG_FILE
from .settings import Settings


def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        raise RuntimeError("config.json is missing.")
    data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("config.json must contain a JSON object.")
    data.setdefault("assistant", {})
    data.setdefault("lights", {})
    data.setdefault("apps", {})
    return data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Jarvis voice assistant MVP")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--text", type=str)
    parser.add_argument("--no-tts", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.list_devices:
        print(sd.query_devices())
        return 0

    settings = Settings.load()
    if args.no_tts:
        settings.tts_enabled = False

    logger = setup_logger(settings.debug)

    try:
        config = load_config()
        app = Jarvis(settings, config, logger)
        if args.text:
            answer = app.brain.ask(args.text)
            app.say(answer)
        else:
            app.run()
        return 0
    except Exception as exc:
        logger.exception("Jarvis could not start")
        print(f"\nSTARTUP ERROR: {exc}")
        print(f"Detailed log: {LOG_FILE}\n")
        return 1
