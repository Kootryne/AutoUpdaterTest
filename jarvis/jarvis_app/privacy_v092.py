from __future__ import annotations

import re

_PATCHED = False


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from . import session_logging

    # Redact secrets spoken naturally, including "API key to ..." and
    # "token is ...", not only KEY=value syntax.
    session_logging._SECRET_RE = re.compile(
        r"(?i)\b(api[_ -]?key|token|authorization|password|secret)\b"
        r"(\s*(?::|=|\bis\b|\bto\b)\s*)([^\s,;]+)"
    )
    _PATCHED = True
