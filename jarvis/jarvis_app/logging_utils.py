import logging
from logging.handlers import RotatingFileHandler

from .paths import LOG_DIR, LOG_FILE
from .session_logging import SessionLogManager


def setup_logger(debug: bool) -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger("jarvis")
    for handler in list(logger.handlers):
        try:
            handler.close()
        except Exception:
            pass
        logger.removeHandler(handler)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    session_logs = SessionLogManager.from_environment()
    logger.session_logs = session_logs  # type: ignore[attr-defined]
    if session_logs.enabled:
        logger.addHandler(session_logs.handler())
        logger.info(
            "SESSION | run started | id=%s | path=%s",
            session_logs.run_id,
            session_logs.run_dir,
        )
    else:
        logger.info("SESSION | per-session logging disabled")
    return logger
