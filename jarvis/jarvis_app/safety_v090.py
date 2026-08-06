from __future__ import annotations

from typing import Any

from .paths import DATA_DIR


_PATCHED = False


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .app import Jarvis

    original_init = Jarvis.__init__

    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        # Power approvals never survive a process restart or an older release.
        for state_file in (
            DATA_DIR / "pending_pc_power.json",
            DATA_DIR / "pending_pc_power_v090.json",
        ):
            state_file.unlink(missing_ok=True)

    # Luna, not the legacy local stop-phrase matcher, decides whether words such
    # as "cancel" confirm or cancel a pending tool-driven action.
    Jarvis.__init__ = patched_init
    Jarvis.is_stop = classmethod(lambda cls, text: False)
    _PATCHED = True
