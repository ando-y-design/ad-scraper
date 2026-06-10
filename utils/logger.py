import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


LOG_DIR = Path(__file__).parent.parent / 'logs'


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)

    formatter = logging.Formatter(
        '%(asctime)s [%(threadName)s] %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    if root.handlers:
        return

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    fh = RotatingFileHandler(
        str(LOG_DIR / 'main.log'), maxBytes=10 * 1024 * 1024, backupCount=5,
        encoding='utf-8'
    )
    fh.setFormatter(formatter)
    root.addHandler(fh)

    eh = RotatingFileHandler(
        str(LOG_DIR / 'error.log'), maxBytes=10 * 1024 * 1024, backupCount=3,
        encoding='utf-8'
    )
    eh.setLevel(logging.ERROR)
    eh.setFormatter(formatter)
    root.addHandler(eh)
