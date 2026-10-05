"""Canales de alerta: Telegram y email (Gmail SMTP)."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any, Callable, Optional

import requests

log = logging.getLogger(__name__)

TELEGRAM_MAX_CHARS = 4000


class AlertError(RuntimeError):
    pass


class TelegramChannel:
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, http: Optional[Any] = None, timeout: float = 20):
        self._token = bot_token
        self._chat_id = chat_id
        self._http = http or requests
        self._timeout = timeout

    def send(self, subject: str, text: str) -> None:
        body = f"{subject}\n\n{text}" if subject else text
        for chunk in _chunks(body, TELEGRAM_MAX_CHARS):
            try:
                resp = self._http.post(
                    f"https://api.telegram.org/bot{self._token}/sendMessage",
                    data={"chat_id": self._chat_id, "text": chunk, "disable_web_page_preview": "true"},
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                # El mensaje de requests incluye la URL (con el token): no se propaga.
                raise AlertError(f"Telegram: error de red ({type(exc).__name__})") from None
            if resp.status_code != 200:
                detail = ""
                try:
                    detail = resp.json().get("description", "")
                except ValueError:
                    pass
                raise AlertError(f"Telegram: HTTP {resp.status_code} {detail}".strip())


class EmailChannel:
    name = "email"

    def __init__(
        self,
        address: str,
        app_password: str,
        to: str,
        smtp_factory: Callable[..., Any] = smtplib.SMTP_SSL,
        host: str = "smtp.gmail.com",
        port: int = 465,
    ):
        self._address = address
        self._password = app_password
        self._to = [a.strip() for a in to.split(",") if a.strip()]
        self._smtp_factory = smtp_factory
        self._host = host
        self._port = port

    def send(self, subject: str, text: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self._address
        msg["To"] = ", ".join(self._to)
        msg.set_content(text)
        try:
            with self._smtp_factory(self._host, self._port, timeout=30) as smtp:
                smtp.login(self._address, self._password)
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise AlertError(f"Email: {type(exc).__name__}: {exc}") from None


class Notifier:
    """Envía por todos los canales configurados; si uno falla, sigue con el otro."""

    def __init__(self, channels: list[Any]):
        self.channels = channels

    def send(self, subject: str, text: str, only: Optional[set[str]] = None) -> list[str]:
        delivered = []
        for ch in self.channels:
            if only is not None and ch.name not in only:
                continue
            try:
                ch.send(subject, text)
                delivered.append(ch.name)
                log.info("Mensaje enviado por %s: %s", ch.name, subject)
            except Exception as exc:  # noqa: BLE001 - un canal nunca debe frenar al otro
                log.error("Falló el envío por %s: %s", ch.name, exc)
        return delivered


class LogNotifier(Notifier):
    """Para --dry-run: muestra los mensajes en el log y no envía nada."""

    def __init__(self):
        super().__init__([])

    def send(self, subject: str, text: str, only: Optional[set[str]] = None) -> list[str]:
        log.info("[dry-run] no se envía (%s):\n%s\n%s", ",".join(sorted(only)) if only else "todos", subject, text)
        return []


def _chunks(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    out, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            if current:
                out.append(current)
                current = ""
            out.append(line[:size])
            line = line[size:]
        if len(current) + len(line) > size:
            out.append(current)
            current = ""
        current += line
    if current:
        out.append(current)
    return out
