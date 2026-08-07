from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any


_PATCHED = False
_TEST_FRAME = b"JARVIS_TEST_CAMERA_FRAME"


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return

    from .privileged_skill_api import SkillAPI

    original_capture_camera = SkillAPI.capture_camera
    original_model_text = SkillAPI.model_text

    def capture_camera(
        self: SkillAPI,
        camera_index: int = 0,
        path: str | None = None,
        width: int | None = None,
        height: int | None = None,
        warmup_frames: int = 5,
    ) -> dict[str, Any]:
        if not self.test_mode:
            return original_capture_camera(
                self,
                camera_index=camera_index,
                path=path,
                width=width,
                height=height,
                warmup_frames=warmup_frames,
            )

        self._require("camera")
        requested = Path(path).expanduser().resolve() if path else None
        if requested is not None and (
            requested == self.data_dir or self.data_dir in requested.parents
        ):
            destination = requested
        else:
            filename = requested.name if requested is not None else (
                datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".jpg"
            )
            destination = self.data_dir / "camera_test" / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_TEST_FRAME)
        actual_width = int(width or 1280)
        actual_height = int(height or 720)
        self._audit(
            "capture_camera_simulated",
            {
                "camera_index": int(camera_index),
                "path": str(destination),
                "requested_path": str(requested) if requested is not None else None,
                "width": actual_width,
                "height": actual_height,
            },
        )
        return {
            "path": str(destination),
            "camera_index": int(camera_index),
            "width": actual_width,
            "height": actual_height,
            "simulated": True,
        }

    def model_text(
        self: SkillAPI,
        prompt: str,
        image_paths: list[str] | None = None,
        max_output_tokens: int = 1200,
    ) -> dict[str, Any]:
        if not self.test_mode:
            return original_model_text(
                self,
                prompt,
                image_paths=image_paths,
                max_output_tokens=max_output_tokens,
            )

        self._require("model")
        self._audit(
            "model_text_simulated",
            {
                "prompt_chars": len(str(prompt)),
                "image_count": len(image_paths or []),
            },
        )
        return {
            "model": "jarvis-skill-test",
            "text": "Simulated visual analysis completed successfully.",
            "simulated": True,
        }

    SkillAPI.capture_camera = capture_camera
    SkillAPI.model_text = model_text
    _PATCHED = True
