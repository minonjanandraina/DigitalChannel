import os
from pathlib import Path

from django.conf import settings


class ReportAlreadyExists(Exception):
    pass


def store_report(filename, content):
    destination = Path(settings.MVOLA_INPUT_DIR)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / filename

    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0),
            0o600,
        )
    except FileExistsError as exc:
        raise ReportAlreadyExists(filename) from exc

    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(content)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target