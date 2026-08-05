# Jarvis Voice Assistant

Windows-first Jarvis using:

- openWakeWord's pretrained `hey_jarvis` model locally
- `gpt-4o-mini-transcribe` for Swedish/English speech-to-text
- `gpt-4.1-mini` for answers and tools
- `gpt-4.1-nano` for uncertain follow-up routing
- `gpt-4o-mini-tts` for speech
- Home Assistant REST tools

## Install and start

The GitHub installer places Jarvis in:

```text
%LOCALAPPDATA%\Jarvis
```

Normal startup is silent through `start_jarvis.vbs`. A desktop shortcut and a
Windows Startup shortcut are created automatically. For visible debug logs, run:

```text
%LOCALAPPDATA%\Jarvis\run_jarvis.bat
```

Useful controls:

```text
stop_jarvis.bat
restart_jarvis.bat
enable_startup.bat
disable_startup.bat
```

## Automatic updates

Version 0.5.0 checks GitHub immediately at startup and every hour afterward. A
new version downloads and validates in the background while Jarvis keeps
listening. Jarvis applies it only while idle, then a separate helper swaps files
and restarts the assistant.

Voice examples:

```text
Hey Jarvis, update.
Jarvis, check for an update.
Jarvis, uppdatera dig.
```

The update system preserves `.env`, `config.json`, `.venv`, and logs. It backs up
managed files first and restores them if copying or dependency installation
fails. Normal code-only updates should interrupt Jarvis only for the short
restart; dependency changes may take longer.

Settings:

```text
AUTO_UPDATE_ENABLED=true
UPDATE_CHECK_INTERVAL_SECONDS=3600
```

## Voice behavior

Every user message is treated as microphone speech transcribed into text. Jarvis
expects phonetic mistakes, repeated phrases, missing punctuation, and mixed
Swedish/English. It infers likely intent from context and does not pretend it only
receives typed messages.

Replies default to one short spoken sentence, usually 4 to 18 words. Actions are
confirmed in only a few words. Web search and exact-time tools are exposed only
when the request needs them.

## Audio tuning

The official wake model is strongest for **Hey Jarvis**. Plain **Jarvis** may also
work.

```text
WAKE_THRESHOLD=0.45
VAD_AGGRESSIVENESS=3
ENERGY_THRESHOLD=350
END_SILENCE_SECONDS=0.75
MAX_UTTERANCE_SECONDS=12
```

VAD requires both WebRTC speech detection and sufficient microphone energy, then
smooths decisions over several frames. The hard recording limit prevents room
noise from producing 25-second recordings.

## Configuration

Secrets stay in `.env`, never GitHub. `OPENAI_API_KEY` may also be stored in a
Windows Process, User, or Machine environment variable.

Edit `config.json` for application aliases and Home Assistant lights. The default
light aliases are:

```text
bedroom  -> light.viktor
led_strip -> light.shellyrgbw2_49f5b9
```

Logs:

```text
%LOCALAPPDATA%\Jarvis\logs\jarvis.log
%LOCALAPPDATA%\Jarvis\logs\update.log
```
