from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any

from .language_mode import detect_language
from .paths import DATA_DIR
from . import process_controls

_PATCHED = False
_PENDING_FILE = DATA_DIR / "pending_capability.json"

_CONFIRM_RE = re.compile(
    r"^(?:yes(?: please)?|yeah|yep|sure|do it|go ahead|make it|build it|"
    r"post it|submit it|create the issue|ja|japp|absolut|gör det|bygg den|"
    r"skapa den|posta det|skicka det|lägg upp det)(?: now| nu)?[.!?]*$",
    re.IGNORECASE,
)
_DECLINE_RE = re.compile(
    r"^(?:no|nope|not now|cancel|never mind|nej|nä|inte nu|avbryt|"
    r"strunt samma)[.!?]*$",
    re.IGNORECASE,
)


def _save_pending(value: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(value)
    payload["created_at"] = datetime.now(timezone.utc).isoformat()
    temporary = _PENDING_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(_PENDING_FILE)


def _read_pending() -> dict[str, Any] | None:
    try:
        value = json.loads(_PENDING_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _clear_pending() -> None:
    _PENDING_FILE.unlink(missing_ok=True)


def _github_setup_reply(language: str) -> str:
    if language == "sv":
        return (
            "GitHub är inte anslutet på den här datorn. "
            "Kör setup_github.bat en gång och be mig posta förslaget igen."
        )
    return (
        "GitHub isn't connected on this PC. "
        "Run setup_github.bat once, then ask me to post it again."
    )


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .app import Jarvis
    from .brain import Brain
    from .skills import SkillSystem
    from .skill_builder import SkillBuilder
    from .shared_skill_manager import SharedSkillManager

    original_instructions = Brain.instructions
    original_update_handler = Jarvis._handle_local_update_command
    original_skill_handler = Jarvis._handle_local_skill_command
    original_schemas = SkillSystem.schemas
    original_call = SkillSystem.call
    original_post = SharedSkillManager.post_feature_request
    original_worker = SkillBuilder._build_worker

    def patched_instructions(self: Any) -> str:
        base = original_instructions(self)
        return (
            f"{base}\n\n"
            "RELEASE-NOTE QUESTIONS\n"
            "- For every question about Jarvis release notes, changelog, or what changed, "
            "call get_jarvis_release_notes first.\n"
            "- Answer the user's exact question. If they ask whether a note says something, "
            "answer yes or no and explain only the relevant note in one short sentence.\n"
            "- Do not recite the whole changelog unless the user explicitly asks for the list.\n\n"
            "CAPABILITY-GAP PROTOCOL\n"
            "- Never end with only 'I cannot do that'.\n"
            "- If the missing capability can be implemented safely as a generated workflow "
            "or restricted Python skill, call propose_capability_extension with kind='skill'.\n"
            "- If it needs changes to Jarvis core, unrestricted Windows access, credentials, "
            "new permissions, or a new built-in integration, call propose_capability_extension "
            "with kind='core'.\n"
            "- After proposing, ask one short yes/no question. Do not build or post yet.\n"
            "- Call submit_jarvis_feature_request only when the user explicitly asked you to "
            "post/create/submit the GitHub issue, or explicitly approved a pending core proposal.\n"
            "- If GitHub is not connected, say only that setup_github.bat must be run once. "
            "Do not offer to repeat, reformat, save, or copy request text."
        )

    def patched_update_handler(
        self: Any,
        command: str,
        turn_started: float | None,
    ) -> bool:
        if process_controls.RELEASE_NOTES_RE.search(command.strip()):
            self.logger.info("ROUTING | release-note question sent to GPT tool flow")
            return False
        return original_update_handler(self, command, turn_started)

    def patched_schemas(self: Any) -> list[dict[str, Any]]:
        schemas = [
            schema
            for schema in original_schemas(self)
            if schema.get("name") != "suggest_new_skill"
        ]
        for schema in schemas:
            if schema.get("name") == "submit_jarvis_feature_request":
                schema["description"] = (
                    "Post a Jarvis core-feature suggestion to GitHub only after the "
                    "user explicitly asked to post it or approved a pending proposal."
                )
        schemas.append(
            {
                "type": "function",
                "name": "propose_capability_extension",
                "description": (
                    "Save a proposed solution when Jarvis cannot perform a request. "
                    "Use kind=skill for a safe generated skill, or kind=core for a "
                    "Jarvis core change. This only proposes; it does not build or post."
                ),
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["skill", "core"]},
                        "goal": {"type": "string"},
                        "name": {"type": "string"},
                        "how_it_would_work": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "kind", "goal", "name", "how_it_would_work", "reason"
                    ],
                    "additionalProperties": False,
                },
            }
        )
        return schemas

    def patched_call(
        self: Any,
        name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if name == "propose_capability_extension":
            proposal = {
                "kind": str(args["kind"]),
                "goal": str(args["goal"]).strip(),
                "name": str(args["name"]).strip() or "New capability",
                "how_it_would_work": str(args["how_it_would_work"]).strip(),
                "reason": str(args["reason"]).strip(),
                "language": detect_language(str(args["goal"]), "en"),
            }
            _save_pending(proposal)
            return {
                "saved": True,
                "proposal_kind": proposal["kind"],
                "followup_required": True,
                "instruction": (
                    "Briefly explain the proposal and ask for yes/no approval. "
                    "Do not perform it yet."
                ),
            }

        if name == "submit_jarvis_feature_request":
            manager = getattr(self, "shared_manager", None)
            if manager is not None:
                manager._explicit_post_approval = True
                try:
                    result = original_call(self, name, args)
                finally:
                    manager._explicit_post_approval = False
                if result.get("posted"):
                    _clear_pending()
                return result

        return original_call(self, name, args)

    def guarded_post(
        self: Any,
        *,
        goal: str,
        suggested_name: str,
        reason: str,
        design: str = "",
    ) -> dict[str, Any]:
        if not getattr(self, "_explicit_post_approval", False):
            proposal = {
                "kind": "core",
                "goal": goal,
                "name": suggested_name or "Jarvis core feature",
                "how_it_would_work": design,
                "reason": reason,
                "language": detect_language(goal, "en"),
            }
            _save_pending(proposal)
            return {
                "posted": False,
                "approval_required": True,
                "proposal": proposal,
            }
        return original_post(
            self,
            goal=goal,
            suggested_name=suggested_name,
            reason=reason,
            design=design,
        )

    def patched_skill_handler(
        self: Any,
        command: str,
        turn_started: float | None,
    ) -> bool:
        normalized = command.strip()
        pending = _read_pending()

        if pending is not None and _DECLINE_RE.match(normalized):
            language = detect_language(
                normalized,
                str(pending.get("language") or getattr(self, "current_language", "en")),
            )
            _clear_pending()
            self.say("Okej." if language == "sv" else "Okay.", turn_started)
            return True

        if pending is not None and _CONFIRM_RE.match(normalized):
            language = detect_language(
                normalized,
                str(pending.get("language") or getattr(self, "current_language", "en")),
            )
            if str(pending.get("kind")) == "skill":
                result = self.skill_system.start_build(
                    requested_capability=pending.get("goal"),
                    suggested_name=pending.get("name"),
                    how_it_would_work=pending.get("how_it_would_work"),
                )
                if result.get("started"):
                    _clear_pending()
                    self.say(
                        "Jag bygger den i bakgrunden."
                        if language == "sv"
                        else "I'm building it in the background.",
                        turn_started,
                    )
                else:
                    self.say(
                        "Jag kunde inte starta bygget."
                        if language == "sv"
                        else "I couldn't start the build.",
                        turn_started,
                    )
                return True

            manager = getattr(self.skill_system, "shared_manager", None)
            if manager is None:
                self.say(
                    "GitHub-funktionen är inte tillgänglig."
                    if language == "sv"
                    else "The GitHub feature is unavailable.",
                    turn_started,
                )
                return True

            manager._explicit_post_approval = True
            try:
                result = manager.post_feature_request(
                    goal=str(pending.get("goal") or ""),
                    suggested_name=str(pending.get("name") or "Jarvis core feature"),
                    reason=str(pending.get("reason") or "Requires a Jarvis core change."),
                    design=str(pending.get("how_it_would_work") or ""),
                )
            finally:
                manager._explicit_post_approval = False

            if result.get("posted"):
                _clear_pending()
                self.say(
                    "Jag lade upp förslaget på GitHub."
                    if language == "sv"
                    else "I posted the suggestion on GitHub.",
                    turn_started,
                )
            elif "token" in str(result.get("error", "")).lower():
                self.say(_github_setup_reply(language), turn_started)
            else:
                self.say(
                    "Jag kunde inte lägga upp förslaget."
                    if language == "sv"
                    else "I couldn't post the suggestion.",
                    turn_started,
                )
            return True

        return original_skill_handler(self, command, turn_started)

    def patched_worker(
        self: Any,
        reporter: Any,
        goal: str,
        name: str,
        design: str,
    ) -> dict[str, Any]:
        try:
            result = original_worker(self, reporter, goal, name, design)
        except Exception as exc:
            pending = _read_pending()
            if pending is not None and str(pending.get("kind")) == "core":
                language = detect_language(goal, str(pending.get("language") or "en"))
                message = (
                    "Skillbygget misslyckades. Det verkar kräva en ändring i "
                    "Jarvis-kärnan. Jag kan lägga upp ett GitHub-förslag. Ska jag göra det?"
                    if language == "sv"
                    else
                    "The skill build failed and appears to need a Jarvis core change. "
                    "I can post a GitHub suggestion. Should I?"
                )
                return {
                    "installed": False,
                    "summary": str(exc),
                    "spoken_summary": message,
                    "error": str(exc),
                    "approval_required": True,
                }
            raise

        pending = _read_pending()
        if (
            not result.get("installed")
            and pending is not None
            and str(pending.get("kind")) == "core"
        ):
            language = detect_language(goal, str(pending.get("language") or "en"))
            result["approval_required"] = True
            result["spoken_summary"] = (
                "Det gick inte att bygga den som en säker skill. "
                "Jag kan lägga upp ett GitHub-förslag för Jarvis-kärnan. Ska jag göra det?"
                if language == "sv"
                else
                "I couldn't build it as a safe skill. "
                "I can post a Jarvis core suggestion on GitHub. Should I?"
            )
        return result

    Brain.instructions = patched_instructions
    Jarvis._handle_local_update_command = patched_update_handler
    Jarvis._handle_local_skill_command = patched_skill_handler
    SkillSystem.schemas = patched_schemas
    SkillSystem.call = patched_call
    SharedSkillManager.post_feature_request = guarded_post
    SkillBuilder._build_worker = patched_worker
    _PATCHED = True
