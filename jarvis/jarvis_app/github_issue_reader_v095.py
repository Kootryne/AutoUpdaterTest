from __future__ import annotations

from typing import Any

import requests


_PATCHED = False


def _compact_issue(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": int(item.get("number", 0)),
        "title": str(item.get("title") or ""),
        "body": str(item.get("body") or "")[:16000],
        "state": str(item.get("state") or ""),
        "url": str(item.get("html_url") or ""),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "comments": int(item.get("comments", 0)),
    }


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .brain import Brain
    from .github_client import GitHubRepoClient
    from .skills import SkillSystem

    original_schemas = SkillSystem.schemas
    original_call = SkillSystem.call
    original_handles = SkillSystem.handles_tool
    original_instructions = Brain.instructions

    def get_issue(self: GitHubRepoClient, issue_number: int) -> dict[str, Any]:
        if not self.repository:
            raise RuntimeError("JARVIS_GITHUB_REPOSITORY is not configured.")
        response = requests.get(
            f"{self.api}/repos/{self.repository}/issues/{int(issue_number)}",
            headers=self._headers(),
            timeout=(5, 20),
        )
        response.raise_for_status()
        item = response.json()
        if "pull_request" in item:
            raise ValueError(f"#{issue_number} is a pull request, not an issue.")
        return _compact_issue(item)

    def latest_issue(
        self: GitHubRepoClient,
        *,
        state: str = "open",
    ) -> dict[str, Any] | None:
        if not self.repository:
            raise RuntimeError("JARVIS_GITHUB_REPOSITORY is not configured.")
        wanted_state = state if state in {"open", "closed", "all"} else "open"
        response = requests.get(
            f"{self.api}/repos/{self.repository}/issues",
            params={
                "state": wanted_state,
                "sort": "created",
                "direction": "desc",
                "per_page": 30,
            },
            headers=self._headers(),
            timeout=(5, 20),
        )
        response.raise_for_status()
        for item in response.json():
            if "pull_request" not in item:
                return _compact_issue(item)
        return None

    def schemas(self: SkillSystem) -> list[dict[str, Any]]:
        result = [
            schema
            for schema in original_schemas(self)
            if schema.get("name") != "get_github_issue"
        ]
        result.append(
            {
                "type": "function",
                "name": "get_github_issue",
                "description": (
                    "Read the newest GitHub issue or a specific issue from the "
                    "configured Jarvis repository. Use the returned issue body as "
                    "context when repairing an installed skill."
                ),
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["latest", "get"],
                        },
                        "issue_number": {"type": ["integer", "null"]},
                        "state": {
                            "type": "string",
                            "enum": ["open", "closed", "all"],
                        },
                    },
                    "required": ["action", "issue_number", "state"],
                    "additionalProperties": False,
                },
            }
        )
        return result

    def call(
        self: SkillSystem,
        name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if name != "get_github_issue":
            return original_call(self, name, args)
        manager = getattr(self, "shared_manager", None)
        client = getattr(manager, "client", None) if manager is not None else None
        if client is None:
            raise RuntimeError("The Jarvis GitHub client is unavailable.")

        action = str(args["action"])
        if action == "latest":
            issue = client.latest_issue(state=str(args.get("state") or "open"))
            return {
                "found": issue is not None,
                "repository": client.repository,
                "issue": issue,
            }

        number = args.get("issue_number")
        if number is None:
            raise ValueError("issue_number is required when action is get.")
        return {
            "found": True,
            "repository": client.repository,
            "issue": client.get_issue(int(number)),
        }

    def handles_tool(self: SkillSystem, name: str) -> bool:
        return name == "get_github_issue" or original_handles(self, name)

    def instructions(self: Brain) -> str:
        return (
            f"{original_instructions(self)}\n\n"
            "READING GITHUB ISSUES\n"
            "- Use get_github_issue whenever the user asks about the newest, latest, "
            "or a numbered issue in the configured Jarvis repository.\n"
            "- When asked to fix or edit a skill from an issue, read the issue first, "
            "then pass its requested capability, design, failure details, and tests "
            "into edit_installed_skill.issue_or_context.\n"
            "- Do not claim an issue was fixed merely because editing support exists. "
            "Only close it after the local skill edit has passed and the user confirms "
            "the real behavior.\n"
        )

    GitHubRepoClient.get_issue = get_issue
    GitHubRepoClient.latest_issue = latest_issue
    SkillSystem.schemas = schemas
    SkillSystem.call = call
    SkillSystem.handles_tool = handles_tool
    Brain.instructions = instructions
    _PATCHED = True
