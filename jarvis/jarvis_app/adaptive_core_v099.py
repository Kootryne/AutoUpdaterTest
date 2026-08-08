from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
import json
import logging
from pathlib import Path
import re
from typing import Any

import requests

from .paths import DATA_DIR, SKILLS_DIR
from . import self_modification_v098

_PATCHED = False
_HA_MAPPINGS = DATA_DIR / "adaptive_home_assistant_entities.json"
_RETIRED_ROOT = DATA_DIR / "retired_skills"

_RETIRED_TOOL_NAMES = {
    "suggest_new_skill", "build_new_skill", "approve_skill_build",
    "cancel_skill_build", "get_pending_skill_approval", "get_background_status",
    "list_installed_skills", "set_local_skill_enabled", "sync_shared_skills",
    "submit_jarvis_feature_request", "propose_capability_extension",
    "edit_installed_skill", "approve_skill_edit", "cancel_skill_edit",
    "get_pending_skill_edit", "list_skill_revisions", "rollback_skill_edit",
}

_HA_ACTIONS = (
    "turn_on", "turn_off", "toggle", "open", "close", "stop", "lock",
    "unlock", "press", "set_position", "set_brightness", "set_temperature",
)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _normalise_alias(value: str) -> str:
    value = str(value).strip().lower()
    value = re.sub(r"[\s\-]+", "_", value)
    value = re.sub(r"[^a-z0-9_åäöéü]+", "", value)
    return value.strip("_")


def _mapping_state() -> dict[str, dict[str, Any]]:
    raw = _read_json(_HA_MAPPINGS, {})
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): dict(value)
        for key, value in raw.items()
        if isinstance(value, dict) and value.get("entity_id")
    }


def _retire_existing_skills(logger: logging.Logger) -> str | None:
    try:
        if not SKILLS_DIR.exists():
            SKILLS_DIR.mkdir(parents=True, exist_ok=True)
            return None
        entries = list(SKILLS_DIR.iterdir())
        if not entries:
            return None
        _RETIRED_ROOT.mkdir(parents=True, exist_ok=True)
        destination = _RETIRED_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        destination.mkdir(parents=True, exist_ok=False)
        for item in entries:
            item.replace(destination / item.name)
        logger.info(
            "ADAPTIVE CORE | retired %d legacy skill item(s) to %s",
            len(entries), destination,
        )
        return str(destination)
    except Exception:
        logger.exception("ADAPTIVE CORE | could not retire legacy skills")
        return None


def _ha_states(ha: Any) -> list[dict[str, Any]]:
    ha.check_config()
    response = requests.get(
        f"{ha.url}/api/states", headers=ha.headers(), timeout=10
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("Home Assistant returned an invalid states payload.")
    return [item for item in payload if isinstance(item, dict)]


def _candidate_score(query: str, entity_id: str, friendly_name: str) -> float:
    query = query.strip().lower()
    entity = entity_id.lower()
    friendly = friendly_name.lower()
    if not query:
        return 0.0
    query_words = set(re.findall(r"[a-z0-9åäöéü]+", query))
    candidate_words = set(
        re.findall(r"[a-z0-9åäöéü]+", f"{entity.replace('_', ' ')} {friendly}")
    )
    overlap = len(query_words & candidate_words)
    containment = 0.0
    if query in friendly:
        containment += 90.0
    if query in entity:
        containment += 75.0
    if any(word and word in friendly for word in query_words):
        containment += 18.0
    ratio = max(
        SequenceMatcher(None, query, friendly).ratio(),
        SequenceMatcher(None, query, entity).ratio(),
    )
    return containment + overlap * 28.0 + ratio * 45.0


def _discover(ha: Any, query: str, domain: str | None, limit: int) -> dict[str, Any]:
    wanted_domain = str(domain or "").strip().lower()
    scored: list[tuple[float, dict[str, Any]]] = []
    for item in _ha_states(ha):
        entity_id = str(item.get("entity_id") or "")
        if "." not in entity_id:
            continue
        current_domain = entity_id.split(".", 1)[0]
        if wanted_domain and current_domain != wanted_domain:
            continue
        attributes = item.get("attributes") or {}
        if not isinstance(attributes, dict):
            attributes = {}
        friendly = str(attributes.get("friendly_name") or entity_id)
        score = _candidate_score(query, entity_id, friendly)
        if score <= 0:
            continue
        scored.append((score, {
            "entity_id": entity_id,
            "friendly_name": friendly,
            "domain": current_domain,
            "state": item.get("state"),
            "device_class": attributes.get("device_class"),
            "supported_features": attributes.get("supported_features"),
        }))
    scored.sort(key=lambda item: item[0], reverse=True)
    maximum = max(1, min(int(limit), 12))
    return {
        "configured": True,
        "query": query,
        "domain": wanted_domain or None,
        "matches": [
            {**payload, "match_score": round(score, 1)}
            for score, payload in scored[:maximum]
        ],
        "known_aliases": _mapping_state(),
    }


def _entity_state(ha: Any, entity_id: str) -> dict[str, Any]:
    ha.check_config()
    response = requests.get(
        f"{ha.url}/api/states/{entity_id}", headers=ha.headers(), timeout=8
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Home Assistant returned an invalid entity state.")
    attributes = data.get("attributes") or {}
    if not isinstance(attributes, dict):
        attributes = {}
    return {
        "entity_id": entity_id,
        "state": data.get("state"),
        "friendly_name": attributes.get("friendly_name"),
        "domain": entity_id.split(".", 1)[0] if "." in entity_id else None,
        "attributes": {
            key: attributes.get(key)
            for key in (
                "device_class", "supported_features", "current_position",
                "brightness", "temperature", "current_temperature",
            )
            if key in attributes
        },
    }


def _remember(ha: Any, alias: str, entity_id: str) -> dict[str, Any]:
    normal = _normalise_alias(alias)
    if not normal:
        raise ValueError("The Home Assistant alias is empty.")
    entity = str(entity_id).strip()
    if "." not in entity:
        raise ValueError("A Home Assistant entity ID must include its domain.")
    state = _entity_state(ha, entity)
    mappings = _mapping_state()
    mappings[normal] = {
        "entity_id": entity,
        "friendly_name": state.get("friendly_name"),
        "domain": state.get("domain"),
        "remembered_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(_HA_MAPPINGS, mappings)
    return {"remembered": True, "alias": normal, "entity": mappings[normal]}


def _forget(alias: str) -> dict[str, Any]:
    normal = _normalise_alias(alias)
    mappings = _mapping_state()
    existed = normal in mappings
    removed = mappings.pop(normal, None)
    _write_json(_HA_MAPPINGS, mappings)
    return {"forgotten": existed, "alias": normal, "removed": removed}


def _resolve_entity(ha: Any, target: str) -> tuple[str, str]:
    target = str(target).strip()
    if "." in target and " " not in target:
        return target, target
    normal = _normalise_alias(target)
    mappings = _mapping_state()
    if normal in mappings:
        return normal, str(mappings[normal]["entity_id"])
    lights = getattr(ha, "lights", {})
    if normal in lights:
        return normal, str(lights[normal])
    raise KeyError(
        f"No learned Home Assistant entity matches '{target}'. "
        "Use discover_home_assistant_entities first."
    )


def _service_for(
    domain: str, action: str, value: float | int | None
) -> tuple[str, dict[str, Any]]:
    action = str(action).strip().lower()
    if action not in _HA_ACTIONS:
        raise ValueError(f"Unsupported Home Assistant action: {action}")
    if domain == "cover":
        if action in {"open", "turn_on"}:
            return "open_cover", {}
        if action in {"close", "turn_off"}:
            return "close_cover", {}
        if action == "stop":
            return "stop_cover", {}
        if action == "set_position":
            if value is None:
                raise ValueError("set_position requires value from 0 to 100.")
            return "set_cover_position", {
                "position": max(0, min(100, int(float(value))))
            }
    if domain == "lock":
        if action in {"lock", "turn_off"}:
            return "lock", {}
        if action in {"unlock", "turn_on"}:
            return "unlock", {}
    if domain == "button" and action in {"press", "turn_on"}:
        return "press", {}
    if domain == "light" and action == "set_brightness":
        if value is None:
            raise ValueError("set_brightness requires value from 0 to 100.")
        return "turn_on", {
            "brightness_pct": max(0, min(100, int(float(value))))
        }
    if domain == "climate" and action == "set_temperature":
        if value is None:
            raise ValueError("set_temperature requires a temperature value.")
        return "set_temperature", {"temperature": float(value)}
    if action in {"turn_on", "turn_off", "toggle"}:
        return action, {}
    raise ValueError(
        f"Action '{action}' is not mapped for Home Assistant domain '{domain}'. "
        "Jarvis can add a core mapping if this device needs a different service."
    )


def _control(
    ha: Any, target: str, action: str, value: float | int | None
) -> dict[str, Any]:
    ha.check_config()
    alias, entity_id = _resolve_entity(ha, target)
    domain = entity_id.split(".", 1)[0]
    service, data = _service_for(domain, action, value)
    response = requests.post(
        f"{ha.url}/api/services/{domain}/{service}",
        headers=ha.headers(),
        json={"entity_id": entity_id, **data},
        timeout=10,
    )
    response.raise_for_status()
    return {
        "controlled": True, "alias": alias, "entity_id": entity_id,
        "domain": domain, "service": service, "value": value,
    }


def _known_ha_context(ha: Any) -> dict[str, Any]:
    return {
        "configured": bool(getattr(ha, "url", "") and getattr(ha, "token", "")),
        "learned_entities": _mapping_state(),
        "configured_lights": dict(getattr(ha, "lights", {}) or {}),
    }


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .brain import Brain
    from .home_assistant import HomeAssistant
    from .shared_skill_manager import SharedSkillManager
    from .skills import SkillSystem
    from .tools import Tools
    from . import voice_settings_v092

    def retired_shared_loop(self: SharedSkillManager) -> None:
        return

    def retired_sync(self: SharedSkillManager) -> dict[str, Any]:
        return {
            "discontinued": True,
            "synced": [],
            "errors": [],
            "message": "Legacy Jarvis skills are discontinued.",
        }

    SharedSkillManager._loop = retired_shared_loop
    SharedSkillManager.sync = retired_sync

    original_skill_init = SkillSystem.__init__
    original_skill_schemas = SkillSystem.schemas
    original_skill_call = SkillSystem.call
    original_skill_handles = SkillSystem.handles_tool
    original_brain_instructions = Brain.instructions
    original_tools_schemas = Tools.schemas
    original_tools_call = Tools.call

    def skill_init(
        self: SkillSystem,
        settings: Any,
        logger: logging.Logger,
    ) -> None:
        _retire_existing_skills(logger)
        original_skill_init(self, settings, logger)
        manager = getattr(self, "shared_manager", None)
        if manager is not None:
            try:
                manager.stop()
            except Exception:
                logger.exception("ADAPTIVE CORE | could not stop legacy skill sync")

    def _dynamic_legacy_names(self: SkillSystem) -> set[str]:
        try:
            return {
                str(schema.get("name"))
                for schema in self.registry.tool_schemas()
                if schema.get("name")
            }
        except Exception:
            return set()

    def skill_schemas(self: SkillSystem) -> list[dict[str, Any]]:
        dynamic = _dynamic_legacy_names(self)
        result: list[dict[str, Any]] = []
        for schema in original_skill_schemas(self):
            name = str(schema.get("name") or "")
            if name in _RETIRED_TOOL_NAMES or name in dynamic:
                continue
            if name == "plan_core_change":
                schema = {
                    **schema,
                    "description": (
                        "Plan a persistent change to Jarvis itself. Use this both "
                        "when the user explicitly asks Jarvis to change and when a "
                        "normal request reveals a missing built-in capability. "
                        "Planning does not install anything."
                    ),
                }
            if name == "approve_core_change":
                schema = {
                    **schema,
                    "description": (
                        "Build, test, install, and restart for the pending Jarvis "
                        "core change only after the user explicitly approves the "
                        "capability/change in a later turn."
                    ),
                }
            result.append(schema)
        return result

    def skill_handles(self: SkillSystem, name: str) -> bool:
        if name in _RETIRED_TOOL_NAMES or name in _dynamic_legacy_names(self):
            return False
        return original_skill_handles(self, name)

    def skill_call(
        self: SkillSystem,
        name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if name in _RETIRED_TOOL_NAMES or name in _dynamic_legacy_names(self):
            raise RuntimeError(
                "Legacy Jarvis skills are discontinued. Use Jarvis core "
                "self-modification or an existing integration instead."
            )
        return original_skill_call(self, name, args)

    def prompt_context(self: SkillSystem) -> str:
        pending = self_modification_v098._pending()
        plan = pending.get("plan") if isinstance(pending, dict) else None
        core_meta = self_modification_v098._read_json(
            self_modification_v098._META, {}
        )
        return (
            "ADAPTIVE JARVIS CORE:\n"
            "- Legacy generated skills are discontinued and unavailable.\n"
            "- Missing capabilities are learned as persistent Jarvis core changes.\n"
            f"- Current core self-modification: {json.dumps(core_meta, ensure_ascii=False)}\n"
            f"- Pending core change: {json.dumps(plan, ensure_ascii=False) if plan else 'none'}\n"
            f"- Learned Home Assistant entities: {json.dumps(_mapping_state(), ensure_ascii=False)}"
        )

    HomeAssistant.all_states = _ha_states
    HomeAssistant.discover_entities = _discover
    HomeAssistant.remember_entity = _remember
    HomeAssistant.forget_entity = lambda self, alias: _forget(alias)
    HomeAssistant.resolve_adaptive = _resolve_entity
    HomeAssistant.control_adaptive = _control
    HomeAssistant.adaptive_state = (
        lambda self, target: _entity_state(self, _resolve_entity(self, target)[1])
    )

    def tools_schemas(
        self: Tools,
        *args: Any,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        result = [
            schema
            for schema in original_tools_schemas(self, *args, **kwargs)
            if str(schema.get("name") or "") not in _RETIRED_TOOL_NAMES
        ]
        names = {str(schema.get("name") or "") for schema in result}
        additions = [
            {
                "type": "function",
                "name": "discover_home_assistant_entities",
                "description": (
                    "Search the user's actual Home Assistant entities by spoken "
                    "name or description. Use after the user says a missing device "
                    "is controllable through Home Assistant."
                ),
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "domain": {"type": ["string", "null"]},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 12},
                    },
                    "required": ["query", "domain", "limit"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "remember_home_assistant_entity",
                "description": (
                    "Persist a user-confirmed Home Assistant entity under a natural "
                    "alias so Jarvis can control it directly in future conversations."
                ),
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string"},
                        "entity_id": {"type": "string"},
                    },
                    "required": ["alias", "entity_id"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "forget_home_assistant_entity",
                "description": "Forget a learned Home Assistant entity alias.",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {"alias": {"type": "string"}},
                    "required": ["alias"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "control_home_assistant_entity",
                "description": (
                    "Control a learned Home Assistant entity or an exact entity ID. "
                    "For covers use open/close/stop/set_position; for ordinary "
                    "switch-like devices use turn_on/turn_off/toggle."
                ),
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string"},
                        "action": {"type": "string", "enum": list(_HA_ACTIONS)},
                        "value": {"type": ["number", "null"]},
                    },
                    "required": ["target", "action", "value"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "get_home_assistant_entity_state",
                "description": (
                    "Read the state of a learned Home Assistant entity or exact entity ID."
                ),
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {"target": {"type": "string"}},
                    "required": ["target"],
                    "additionalProperties": False,
                },
            },
        ]
        for schema in additions:
            if schema["name"] not in names:
                result.append(schema)
        return result

    def tools_call(
        self: Tools,
        name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if name == "discover_home_assistant_entities":
            if not self.ha.url or not self.ha.token:
                return {
                    "configured": False,
                    "error": (
                        "Home Assistant is not configured yet. Ask the user for "
                        "the Home Assistant URL/token through Jarvis settings."
                    ),
                }
            return self.ha.discover_entities(
                str(args["query"]),
                str(args["domain"]) if args.get("domain") else None,
                int(args.get("limit", 6)),
            )
        if name == "remember_home_assistant_entity":
            return self.ha.remember_entity(
                str(args["alias"]), str(args["entity_id"])
            )
        if name == "forget_home_assistant_entity":
            return self.ha.forget_entity(str(args["alias"]))
        if name == "control_home_assistant_entity":
            return self.ha.control_adaptive(
                str(args["target"]), str(args["action"]), args.get("value")
            )
        if name == "get_home_assistant_entity_state":
            return self.ha.adaptive_state(str(args["target"]))
        return original_tools_call(self, name, args)

    def brain_instructions(self: Brain) -> str:
        base = original_brain_instructions(self)
        ha_context = _known_ha_context(self.tools.ha)
        return (
            base
            + """

ADAPTIVE CORE CAPABILITIES — THIS OVERRIDES ALL EARLIER SKILL INSTRUCTIONS
- Generated/user skills are discontinued. Never propose, create, build, edit,
  sync, enable, invoke, or discuss a Jarvis skill as the solution to a capability
  gap. Existing legacy skill tools are intentionally unavailable.
- Jarvis is allowed to notice its own missing capabilities during ordinary
  conversation. The user does NOT have to explicitly say "change yourself".
- When a request cannot be completed, first determine whether an existing
  integration can solve it with one missing piece of information. Ask only that
  short question instead of dumping setup steps on the user.
- For a physical/home device Jarvis does not know, if Home Assistant is a
  plausible route and the user has not said whether the device is there, ask
  whether it is controllable through Home Assistant.
- After the user says yes, call discover_home_assistant_entities using the
  natural device name. For awnings/blinds/shutters/garage doors, prefer the
  Home Assistant cover domain first. If one result is clearly best, ask a short confirmation
  such as "Is it cover.viktor_markis?" Do not invent entity IDs.
- After the user confirms the entity, call remember_home_assistant_entity. Then
  complete the original requested action immediately when it is still clear and
  safe. Future requests should use control_home_assistant_entity directly without
  asking again.
- If no existing integration can provide the missing capability, call
  plan_core_change yourself. Planning is non-mutating and may be initiated just
  because the current request exposed a limitation.
- Explain the proposed capability and meaningful risk briefly and ask one
  explicit yes/no question. On the user's later yes, call approve_core_change.
  That one approval is the approval to create the capability; do not ask a
  second redundant permission question.
- Example: if asked "what's on my screen?" and there is no screenshot tool,
  plan a built-in screenshot capability that lets Luna request a screenshot and
  inspect it. Say that Jarvis cannot currently see the screen, explain that the
  new capability can capture visible screen contents, and ask whether to add it.
- Core changes should create ordinary first-class Jarvis tools/hooks and use the
  persistent core override. They may add ordinary PyPI dependencies when needed.
- Jarvis may proactively suggest a core improvement after a repeated tool failure
  or obvious capability gap, but persistent changes are installed only after the
  user's explicit yes.
- Do not tell the user to edit Jarvis code/configuration manually when Jarvis can
  discover, remember, configure, or modify itself conversationally.
- GitHub issues are not the normal capability path. Use GitHub only when the user
  specifically asks about GitHub or a local core plan reports that the change is
  genuinely impossible to implement.
"""
            + "\n\nADAPTIVE HOME ASSISTANT STATE:\n"
            + json.dumps(ha_context, ensure_ascii=False)
        )

    voice_settings_v092.ALIASES.update(
        {
            "core planner model": "SKILL_PLANNER_MODEL",
            "core planner reasoning": "SKILL_PLANNER_REASONING",
            "core implementer model": "SKILL_BUILDER_MODEL",
            "core change retries": "SKILL_BUILD_RETRIES",
        }
    )

    SkillSystem.__init__ = skill_init
    SkillSystem.schemas = skill_schemas
    SkillSystem.handles_tool = skill_handles
    SkillSystem.call = skill_call
    SkillSystem.prompt_context = prompt_context
    Tools.schemas = tools_schemas
    Tools.call = tools_call
    Brain.instructions = brain_instructions

    _PATCHED = True
