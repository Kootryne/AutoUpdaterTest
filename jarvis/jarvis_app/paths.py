from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = APP_DIR / ".env"
CONFIG_FILE = APP_DIR / "config.json"
LOG_DIR = APP_DIR / "logs"
LOG_FILE = LOG_DIR / "jarvis.log"
DATA_DIR = APP_DIR / "data"
TASK_DB_FILE = DATA_DIR / "tasks.sqlite3"
SKILLS_DIR = APP_DIR / "skills"
SKILL_STAGING_DIR = DATA_DIR / "skill_staging"

SAMPLE_RATE = 16_000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2
