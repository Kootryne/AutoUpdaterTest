from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any

_PATCHED = False


def _discover_token() -> tuple[str, str]:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.getenv(name, "").strip()
        if value:
            return value, name

    if sys.platform == "win32":
        command = (
            "$k=[Environment]::GetEnvironmentVariable('GITHUB_TOKEN','User');"
            "if(-not $k){$k=[Environment]::GetEnvironmentVariable('GH_TOKEN','User')};"
            "if(-not $k){$k=[Environment]::GetEnvironmentVariable('GITHUB_TOKEN','Machine')};"
            "if(-not $k){$k=[Environment]::GetEnvironmentVariable('GH_TOKEN','Machine')};"
            "if($k){[Console]::Out.Write($k)}"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
            value = result.stdout.strip()
            if value:
                return value, "Windows environment"
        except Exception:
            pass

    gh = shutil.which("gh")
    if gh:
        try:
            result = subprocess.run(
                [gh, "auth", "token"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            value = result.stdout.strip()
            if result.returncode == 0 and value:
                return value, "GitHub CLI"
        except Exception:
            pass

    return "", ""


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .github_client import GitHubRepoClient

    original_init = GitHubRepoClient.__init__

    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        if not self.token:
            token, source = _discover_token()
            if token:
                self.token = token
                self.logger.info("GITHUB | write authentication loaded from %s", source)
            else:
                self.logger.info(
                    "GITHUB | write authentication unavailable; "
                    "run setup_github.bat or authenticate GitHub CLI"
                )

    GitHubRepoClient.__init__ = patched_init
    _PATCHED = True
