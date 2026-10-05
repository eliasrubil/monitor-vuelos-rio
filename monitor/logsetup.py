"""Logging con redacción de secrets."""

from __future__ import annotations

import logging
import sys


class RedactingFilter(logging.Filter):
    """Reemplaza cualquier valor secreto que aparezca en un mensaje de log."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        # Los valores muy cortos generarían falsos positivos; ninguno de los secrets reales lo es.
        self._secrets = sorted({s for s in secrets if len(s) >= 6}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            message = record.getMessage()
            redacted = self.redact(message)
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = self.redact(record.exc_text)
            if redacted != message:
                record.msg, record.args = redacted, None
        return True


def setup_logging(secrets: list[str], verbose: bool = False) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    handler.addFilter(RedactingFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # urllib3 en DEBUG loguea URLs (la de Telegram lleva el token).
    logging.getLogger("urllib3").setLevel(logging.WARNING)
