# Jarvis Voice Assistant

Windows-first Jarvis using openWakeWord, OpenAI speech-to-text, OpenAI Responses,
OpenAI text-to-speech, Home Assistant tools, background tasks, self-updates, and
user-created skills.

## Install and start

Jarvis is installed in:

```text
%LOCALAPPDATA%\Jarvis
```

Use `start_jarvis.vbs` for silent background mode. Use `run_jarvis.bat` for a
visible console with live logs. The visible launcher copies its loop to a temporary
batch file before starting Jarvis, so an update cannot replace the batch script
that is currently supervising the process.

## Language behavior

Every transcription is classified locally as English or Swedish. Jarvis is given
an explicit per-turn instruction to answer entirely in the detected language.
Very short replies such as `yes`, `no`, `ja`, and `nej` retain the language of the
current conversation.

## Updates

Jarvis checks for updates at startup and once per hour. It downloads and validates
a new version in the background, but does not install it silently. While idle it
asks:

```text
I received update 0.x.x. Should I install it?
```

or the Swedish equivalent. Installation begins only after an affirmative reply.
Manual voice update commands still install immediately because the user has
already explicitly requested the update.

`run_jarvis.bat` uses `console_host.bat`, copied to `%TEMP%`, so updates and restarts
return to the same visible console. Background launches remain background.

## Shared skills

Generated skills are validated and tested locally, then published under:

```text
jarvis/shared_skills/<skill-id>/
```

`jarvis/shared_skills/index.json` is the shared catalogue. Every Jarvis instance
syncs it at startup and hourly. Shared files are hash-checked and pass the normal
skill manifest and code-safety validation before installation.

A skill can be disabled on one computer without disabling it elsewhere. Voice
examples:

```text
Disable the weather report skill on this computer.
Enable the weather report skill.
Sync shared skills now.
```

Local disabled IDs are stored in `data\disabled_skills.json` and are not uploaded.

Publishing skills and creating developer suggestions require `GITHUB_TOKEN` in
`.env` or the Windows environment. Use a fine-grained token restricted to
`Kootryne/AutoUpdaterTest` with **Contents: read/write** and
**Issues: read/write**. The token stays local.

When Sol decides that a requested capability cannot be implemented safely as a
generated skill, Jarvis creates a GitHub issue for developer review. Failed builds
can also create an issue containing the requested capability, proposed design,
and failure reason.

## Secrets

Never commit `.env`. It may contain the OpenAI key, Home Assistant token, and
GitHub token. `.env.example` contains only empty placeholders.
