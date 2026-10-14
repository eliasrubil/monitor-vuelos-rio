"""Canales de alerta: Telegram y email (Gmail SMTP)."""

from __future__ import annotations

import html
import logging
import re
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Any, Callable, Optional

import requests

log = logging.getLogger(__name__)

TELEGRAM_MAX_CHARS = 4000
# Imagen que va al pie de cada email, integrada en el cuerpo (no como adjunto para descargar).
EMAIL_FOOTER_IMAGE = Path(__file__).resolve().parent.parent / "assets" / "email_footer.jpg"
_URL = re.compile(r"https?://[^\s<>\"]+")


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
        footer_image: Optional[Path] = EMAIL_FOOTER_IMAGE,
    ):
        self._address = address
        self._password = app_password
        self._to = [a.strip() for a in to.split(",") if a.strip()]
        self._smtp_factory = smtp_factory
        self._host = host
        self._port = port
        self._footer = footer_image.read_bytes() if footer_image and footer_image.exists() else None

    def send(self, subject: str, text: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self._address
        msg["To"] = ", ".join(self._to)
        msg.set_content(text)
        if self._footer:
            # Versión HTML con la imagen embebida al final (cid), como el banner de un mail corporativo:
            # se ve directamente en el cuerpo, sin descargar nada. El texto plano queda como alternativa.
            cid = make_msgid(domain="monitor-vuelos")
            msg.add_alternative(_html_body(text, cid[1:-1]), subtype="html")
            msg.get_payload()[1].add_related(self._footer, "image", "jpeg", cid=cid, filename="pie.jpg",
                                             disposition="inline")
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


def _html_body(text: str, cid: str) -> str:
    """El texto del mensaje tal cual (saltos de línea y links clickeables) y la imagen al pie."""
    body = _URL.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', html.escape(text, quote=False))
    return (
        '<!DOCTYPE html><html><body style="margin:0;padding:0">'
        '<div style="max-width:600px;margin:0 auto;padding:16px;font-family:Arial,Helvetica,sans-serif;'
        f'font-size:14px;line-height:1.45;color:#202124;white-space:pre-wrap">{body}</div>'
        '<div style="max-width:600px;margin:0 auto;padding:8px 16px 24px">'
        f'<img src="cid:{cid}" alt="" width="600" '
        'style="display:block;width:100%;max-width:600px;height:auto;border:0"></div>'
        "</body></html>"
    )


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
