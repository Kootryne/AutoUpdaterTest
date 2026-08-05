from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from typing import Any
from urllib.parse import quote

import requests

from .paths import DATA_DIR

INDEX_PATH = "jarvis/shared_skills/index.json"
_SUGGESTIONS_FILE = DATA_DIR / "published_suggestions.json"


def redact_public_text(value: str) -> str:
    value = value[:12000]
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{10,}\b", "[redacted OpenAI key]", value)
    value = re.sub(r"\b(?:github_pat_|ghp_)[A-Za-z0-9_]{10,}\b", "[redacted GitHub token]", value)
    value = re.sub(
        r"(?im)^(.*?(?:password|token|api[ _-]?key)\s*[:=]\s*)\S+",
        r"\1[redacted]",
        value,
    )
    value = re.sub(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "[redacted email]",
        value,
        flags=re.I,
    )
    return value


def read_suggestion_cache() -> dict[str, str]:
    try:
        value = json.loads(_SUGGESTIONS_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def write_suggestion_cache(value: dict[str, str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = _SUGGESTIONS_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temp.replace(_SUGGESTIONS_FILE)


def _write_disabled(values: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = _DISABLED_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(sorted(values), indent=2), encoding="utf-8")
    temp.replace(_DISABLED_FILE)


class GitHubRepoClient:
    def __init__(self, logger: Any) -> None:
        self.logger = logger
        self.token = os.getenv("GITHUB_TOKEN", "").strip()
        self.repository = os.getenv(
            "JARVIS_GITHUB_REPOSITORY", "Kootryne/AutoUpdaterTest"
        ).strip()
        self.branch = os.getenv("JARVIS_GITHUB_BRANCH", "main").strip()
        self.api = "https://api.github.com"

    @property
    def write_configured(self) -> bool:
        return bool(self.token and self.repository)

    def _headers(self, *, write: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Viktor-Jarvis",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if write and not self.token:
            raise RuntimeError(
                "GITHUB_TOKEN is missing. It needs Contents and Issues write access."
            )
        return headers

    def _content_url(self, path: str) -> str:
        return f"{self.api}/repos/{self.repository}/contents/{quote(path)}"

    def get_text(self, path: str) -> tuple[str | None, str | None]:
        response = requests.get(
            self._content_url(path),
            params={"ref": self.branch},
            headers=self._headers(),
            timeout=(4, 12),
        )
        if response.status_code == 404:
            return None, None
        response.raise_for_status()
        payload = response.json()
        content = base64.b64decode(payload["content"]).decode("utf-8")
        return content, str(payload.get("sha") or "")

    def put_text(
        self,
        path: str,
        content: str,
        message: str,
        *,
        expected_sha: str | None = None,
        use_expected_sha: bool = False,
    ) -> None:
        sha = expected_sha if use_expected_sha else self.get_text(path)[1]
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if sha:
            payload["sha"] = sha
        response = requests.put(
            self._content_url(path),
            headers=self._headers(write=True),
            json=payload,
            timeout=(5, 20),
        )
        response.raise_for_status()

    def raw_text(self, path: str) -> str:
        url = (
            f"https://raw.githubusercontent.com/{self.repository}/{self.branch}/"
            f"{quote(path)}"
        )
        response = requests.get(
            url,
            params={"_": int(__import__("time").time())},
            headers={"Cache-Control": "no-cache", "User-Agent": "Viktor-Jarvis"},
            timeout=(4, 15),
        )
        response.raise_for_status()
        return response.text

    def find_open_issue(self, title: str) -> str | None:
        if not self.write_configured:
            return None
        response = requests.get(
            f"{self.api}/repos/{self.repository}/issues",
            params={"state": "open", "per_page": 100},
            headers=self._headers(),
            timeout=(5, 20),
        )
        response.raise_for_status()
        for item in response.json():
            if "pull_request" not in item and str(item.get("title")) == title:
                return str(item.get("html_url") or "")
        return None

    def create_issue(self, title: str, body: str) -> str:
        if not self.write_configured:
            raise RuntimeError(
                "GITHUB_TOKEN is missing. It needs Issues write access."
            )
        response = requests.post(
            f"{self.api}/repos/{self.repository}/issues",
            headers=self._headers(write=True),
            json={"title": title[:240], "body": body},
            timeout=(5, 20),
        )
        response.raise_for_status()
        return str(response.json().get("html_url") or "")


