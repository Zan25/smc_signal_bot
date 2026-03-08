"""
Logger utility - creates named loggers with console and rotating file handlers.
Timestamps are displayed in WIB (Asia/Jakarta) timezone.
"""

import logging
import logging.handlers
from pathlib import Path
import pytz
from datetime import datetime, timezone


class _WIBFormatter(logging.Formatter):
    """Custom formatter that converts log timestamps to WIB timezone."""

    _tz = pytz.timezone("Asia/Jakarta")

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc).astimezone(self._tz)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_initialized_loggers: dict[str, logging.Logger] = {}


def get_logger(name: str) -> logging.Logger:
    """
    Return a configured logger with the given name.
    Creates handlers on first call; subsequent calls return the cached logger.
    """
    if name in _initialized_loggers:
        return _initialized_loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        formatter = _WIBFormatter(_LOG_FORMAT)

        # Console handler — INFO and above
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(formatter)
        logger.addHandler(console)

        # Rotating file handler — DEBUG and above (5 MB × 3 backups)
        log_file = _LOG_DIR / "smc_bot.log"
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    logger.propagate = False
    _initialized_loggers[name] = logger
    return logger
