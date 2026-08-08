from __future__ import annotations

import importlib.metadata
import re
import subprocess
import sys
from typing import Any

from . import self_modification_v098

_PATCHED = False

_PREFIXES = (
    r"^(?:pypi|package|dependency)\s*:\s*",
    r"^(?:python\s+-m\s+)?pip\s+install\s+",
)


def _normalise_dependency(value: Any) -> str:
    """Convert harmless planner formatting into one safe pip requirement."""
    text = str(value).strip()
    text = re.sub(r"^\s*[-*]\s+", "", text)
    text = text.strip().strip("`").strip()

    changed = True
    while changed:
        changed = False
        for pattern in _PREFIXES:
            cleaned = re.sub(pattern, "", text, count=1, flags=re.IGNORECASE).strip()
            if cleaned != text:
                text = cleaned
                changed = True

    text = text.strip().strip("`").strip().strip('"').strip("'").strip()
    text = re.sub(
        r"\s*(===|==|!=|~=|>=|<=|>|<)\s*",
        r"\1",
        text,
    )

    if not text:
        raise ValueError(f"Unsupported dependency: {value!r}")

    # Keep dependencies to ordinary package-index requirement strings. URLs,
    # editable installs, shell fragments, and command-line options are not
    # accepted from model output.
    if any(token in text for token in ("@", "://", ";", "\n", "\r")):
        raise ValueError(f"Unsupported dependency: {value!r}")
    if text.startswith("-") or " --" in text:
        raise ValueError(f"Unsupported dependency: {value!r}")
    if " " in text or "\t" in text:
        raise ValueError(f"Invalid dependency: {value}")

    if not re.fullmatch(
        r"[A-Za-z0-9_.-]+"
        r"(?:\[[A-Za-z0-9_,.-]+\])?"
        r"(?:(?:===|==|!=|~=|>=|<=|>|<)[A-Za-z0-9*+!_.-]+"
        r"(?:,(?:===|==|!=|~=|>=|<=|>|<)[A-Za-z0-9*+!_.-]+)*)?",
        text,
    ):
        raise ValueError(f"Invalid dependency: {value}")
    return text


def _plain_distribution_installed(requirement: str) -> bool:
    # Only skip pip for an unconstrained, no-extras distribution. Constraints
    # and extras still go through pip so requested versions/extras are enforced.
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", requirement):
        return False
    try:
        importlib.metadata.version(requirement)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


def _install_dependencies(items: list[str]) -> None:
    dependencies = list(
        dict.fromkeys(_normalise_dependency(value) for value in items)
    )
    if not dependencies:
        return

    pending: list[str] = []
    for dependency in dependencies:
        if _plain_distribution_installed(dependency):
            self_modification_v098._log(
                f"dependency already installed; skipping pip: {dependency}"
            )
        else:
            pending.append(dependency)

    if not pending:
        return

    self_modification_v098._log("pip install: " + ", ".join(pending))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--upgrade-strategy",
            "only-if-needed",
            *pending,
        ],
        cwd=str(self_modification_v098.APP_DIR),
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if sys.platform == "win32"
            else 0
        ),
    )
    for output in (result.stdout, result.stderr):
        if output and output.strip():
            for line in output.strip().splitlines():
                self_modification_v098._log("pip | " + line)
    if result.returncode:
        raise RuntimeError(
            f"Dependency installation failed with exit code {result.returncode}."
        )


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    # plan_core_change and _build in v0.9.8 resolve these module globals at
    # runtime, so replacing them fixes both planning and installation without
    # duplicating the self-modification engine.
    self_modification_v098._dependency = _normalise_dependency
    self_modification_v098._install_dependencies = _install_dependencies
    _PATCHED = True
