"""Construcción del notificador a partir de config y secrets."""

from __future__ import annotations

import logging

from ..config import Config, Secrets
from .channels import EmailChannel, LogNotifier, Notifier, TelegramChannel

log = logging.getLogger(__name__)

__all__ = ["build_notifier", "Notifier", "LogNotifier"]


def build_notifier(cfg: Config, secrets: Secrets) -> Notifier:
    channels = []
    if cfg.alerts.telegram:
        if secrets.telegram_bot_token and secrets.telegram_chat_id:
            channels.append(TelegramChannel(secrets.telegram_bot_token, secrets.telegram_chat_id))
        else:
            log.warning("Telegram habilitado pero faltan TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID: canal desactivado")
    if cfg.alerts.email:
        if secrets.gmail_address and secrets.gmail_app_password and secrets.alert_email_to:
            channels.append(EmailChannel(secrets.gmail_address, secrets.gmail_app_password, secrets.alert_email_to))
        else:
            log.warning("Email habilitado pero faltan GMAIL_ADDRESS/GMAIL_APP_PASSWORD/ALERT_EMAIL_TO: canal desactivado")
    return Notifier(channels)
