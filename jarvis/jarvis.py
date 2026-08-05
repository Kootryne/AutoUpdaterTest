from jarvis_app.process_controls import apply_patches as apply_process_patches
from jarvis_app.enhancements_v08 import apply_patches as apply_v08_patches
from jarvis_app.enhancements_v081 import apply_patches as apply_v081_patches
from jarvis_app.reliability_v082 import apply_patches as apply_v082_patches
from jarvis_app.enhancements_v083 import apply_patches as apply_v083_patches
from jarvis_app.reliability_v084 import apply_patches as apply_v084_patches
from jarvis_app.reliability_v085 import apply_patches as apply_v085_patches
from jarvis_app.reliability_v086 import apply_patches as apply_v086_patches
from jarvis_app.conversation_reliability_v086 import apply_patches as apply_conversation_v086_patches

apply_process_patches()
apply_v08_patches()
apply_v081_patches()
apply_v082_patches()
apply_v083_patches()
apply_v084_patches()
apply_v085_patches()
apply_v086_patches()
apply_conversation_v086_patches()

from jarvis_app.main import main

if __name__ == "__main__":
    raise SystemExit(main())
