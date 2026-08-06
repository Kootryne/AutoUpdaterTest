from jarvis_app.process_controls import apply_patches as apply_process_patches
from jarvis_app.enhancements_v08 import apply_patches as apply_v08_patches
from jarvis_app.enhancements_v081 import apply_patches as apply_v081_patches
from jarvis_app.reliability_v082 import apply_patches as apply_v082_patches
from jarvis_app.enhancements_v083 import apply_patches as apply_v083_patches
from jarvis_app.reliability_v084 import apply_patches as apply_v084_patches
from jarvis_app.reliability_v085 import apply_patches as apply_v085_patches
from jarvis_app.reliability_v086 import apply_patches as apply_v086_patches
from jarvis_app.conversation_reliability_v086 import apply_patches as apply_conversation_v086_patches
from jarvis_app.reliability_v087 import apply_patches as apply_v087_patches
from jarvis_app.reliability_v088 import apply_patches as apply_v088_patches
from jarvis_app.reliability_v089 import apply_patches as apply_v089_patches
from jarvis_app.reliability_v090 import apply_patches as apply_v090_patches
from jarvis_app.safety_v090 import apply_patches as apply_safety_v090_patches
from jarvis_app.reliability_v091 import apply_patches as apply_v091_patches
from jarvis_app.privileged_skills_v092 import apply_patches as apply_privileged_v092_patches
from jarvis_app.voice_settings_v092 import apply_patches as apply_settings_v092_patches

apply_process_patches()
apply_v08_patches()
apply_v081_patches()
apply_v082_patches()
apply_v083_patches()
apply_v084_patches()
apply_v085_patches()
apply_v086_patches()
apply_conversation_v086_patches()
apply_v087_patches()
apply_v088_patches()
apply_v089_patches()
apply_v090_patches()
apply_safety_v090_patches()
apply_v091_patches()
apply_privileged_v092_patches()
apply_settings_v092_patches()

from jarvis_app.main import main

if __name__ == "__main__":
    raise SystemExit(main())
