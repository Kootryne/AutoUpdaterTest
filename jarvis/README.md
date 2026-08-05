# Jarvis MVP

This Windows-first version contains the full basic loop:

`openWakeWord -> record -> transcribe -> GPT/tools/web -> TTS -> follow-up`

## Stack

- openWakeWord pretrained `hey_jarvis`, local and free
- `gpt-4o-mini-transcribe`
- `gpt-5-mini`
- OpenAI web search and function tools
- `gpt-4o-mini-tts`
- Home Assistant REST API for lights

## Install

1. Use 64-bit Python 3.12.
2. Double-click `install.bat`.
3. The installer checks for `OPENAI_API_KEY` in the Windows Process, User, and Machine environment scopes.
4. When no key is found, open `.env` and add one there.
5. Optionally add your Home Assistant URL and long-lived token.
6. Double-click `run_jarvis.bat`.
7. Say **Hey Jarvis**, followed by the command.

The first launch downloads the pretrained wake-word files.

## Test typed requests first

Double-click `text_test.bat`, or run:

```bat
.venv\Scripts\python.exe jarvis.py --text "Vad är klockan?"
```

## Select microphone or speaker

Run `list_devices.bat`, then put numeric IDs in `.env`:

```text
MIC_DEVICE=4
SPEAKER_DEVICE=7
```

Leave them blank to use Windows defaults.

## Configure Home Assistant and applications

Edit `config.json`. Tokens remain in `.env`.

The default light aliases are:

- `bedroom` -> `light.viktor`
- `led_strip` -> `light.shellyrgbw2_49f5b9`

## Wake-word tuning

The official included model is mainly trained for **Hey Jarvis**. Saying only
**Jarvis** may work, but is less reliable.

- It misses you: lower `WAKE_THRESHOLD`, for example `0.42`
- Random sounds wake it: raise it, for example `0.55`

## Current limits

- No speech interruption while Jarvis is talking yet.
- Several nearby devices are not coordinated yet.
- It is not packaged as an EXE yet.
- The first version uses console status and logs rather than a graphical overlay.
- Log file: `logs\jarvis.log`


## Follow-up conversation filter

After Jarvis answers, it still listens for a few seconds. The transcript is sent
through a strict intent check before Jarvis responds again.

It normally accepts:

- a direct follow-up question;
- a correction or clarification;
- `yes`, `no`, or another answer when Jarvis asked you something;
- contextual commands such as `make it darker`;
- speech that directly says `Jarvis`.

It ignores speech that appears to be aimed at another person or unrelated
background conversation. When uncertain, it stays silent.

Disable this behavior only for debugging:

```text
FOLLOWUP_REQUIRE_INTENT=false
```


## GitHub installer and updates

The public source is stored in:

```text
Kootryne/AutoUpdaterTest/jarvis
```

For a fresh installation, download and run:

```text
install_jarvis.bat
```

The bootstrap file downloads the current `installer.ps1` directly from GitHub.
The installer then downloads the latest repository snapshot and installs Jarvis
to:

```text
%LOCALAPPDATA%\Jarvis
```

Run `update_jarvis.bat` from the installed folder whenever you want to check for
updates. It compares `version.json` and only updates when a newer project version
is available.

Updates preserve:

- `.env`
- `config.json`
- `.venv`
- `logs`

The OpenAI API key and Home Assistant token are never uploaded to GitHub.
