import datetime as dt
import logging
import smtplib

import pytest
import requests

from monitor.alerts import build_notifier
from monitor.alerts.channels import AlertError, EmailChannel, Notifier, TelegramChannel
from monitor.alerts.format import Candidate, alert_message
from monitor.config import Secrets
from monitor.detection import DetectionConfig, evaluate
from monitor.logsetup import RedactingFilter

from .conftest import FakeChannel

TOKEN = "123456:ABC-secret-token"


class FakeHTTP:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.calls = response, exc, []

    def post(self, url, data=None, timeout=None):
        self.calls.append((url, data))
        if self.exc:
            raise self.exc
        return self.response


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body


def test_telegram_sends_to_bot_api():
    http = FakeHTTP(Resp(200, {"ok": True}))
    TelegramChannel(TOKEN, "999", http=http).send("Asunto", "Texto")
    url, data = http.calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert data["chat_id"] == "999" and data["text"] == "Asunto\n\nTexto"


def test_telegram_network_error_does_not_leak_token():
    exc = requests.ConnectionError(f"Max retries exceeded with url: /bot{TOKEN}/sendMessage")
    with pytest.raises(AlertError) as info:
        TelegramChannel(TOKEN, "999", http=FakeHTTP(exc=exc)).send("a", "b")
    assert TOKEN not in str(info.value)


def test_telegram_http_error():
    with pytest.raises(AlertError, match="401 Unauthorized"):
        TelegramChannel(TOKEN, "1", http=FakeHTTP(Resp(401, {"description": "Unauthorized"}))).send("a", "b")


def test_long_telegram_messages_are_split():
    http = FakeHTTP(Resp(200))
    TelegramChannel(TOKEN, "1", http=http).send("", "linea\n" * 1500)
    assert len(http.calls) == 3
    assert all(len(d["text"]) <= 4000 for _, d in http.calls)


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.logged, self.messages = host, port, None, []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        if password == "bad":
            raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
        self.logged = user

    def send_message(self, msg):
        self.messages.append(msg)


def test_email_via_gmail_smtp():
    FakeSMTP.instances.clear()
    EmailChannel("yo@gmail.com", "app-pass", "a@x.com, b@x.com", smtp_factory=FakeSMTP).send("Hola", "Cuerpo")
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port, smtp.logged) == ("smtp.gmail.com", 465, "yo@gmail.com")
    msg = smtp.messages[0]
    assert msg["To"] == "a@x.com, b@x.com" and msg["Subject"] == "Hola"


def test_email_auth_error_is_alert_error():
    with pytest.raises(AlertError, match="SMTPAuthenticationError"):
        EmailChannel("yo@gmail.com", "bad", "a@x.com", smtp_factory=FakeSMTP).send("a", "b")


def test_notifier_keeps_going_when_a_channel_fails(caplog):
    tg, mail = FakeChannel("telegram", fail=True), FakeChannel("email")
    with caplog.at_level(logging.ERROR):
        delivered = Notifier([tg, mail]).send("s", "t")
    assert delivered == ["email"]
    assert mail.sent == [("s", "t")]
    assert "telegram" in caplog.text


def test_build_notifier_skips_channels_without_secrets(cfg):
    n = build_notifier(cfg, Secrets(telegram_bot_token=TOKEN, telegram_chat_id="1"))
    assert [c.name for c in n.channels] == ["telegram"]


def test_redacting_filter():
    f = RedactingFilter([TOKEN, "app-password-xyz"])
    record = logging.LogRecord("x", logging.ERROR, "", 0, "falló %s con %s", (f"/bot{TOKEN}/send", "app-password-xyz"), None)
    f.filter(record)
    assert TOKEN not in record.getMessage() and "app-password-xyz" not in record.getMessage()
    assert "***" in record.getMessage()


def test_alert_message_contents(cfg):
    c = Candidate("EZE", "GIG", dt.date(2027, 1, 15), dt.date(2027, 1, 25), 1, "AR, G3", 412.0, 2060.0, "USD", 1,
                  booking_url="https://example.com/x")
    ev = evaluate(412, [500] * 8, [], DetectionConfig(), dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc))
    subject, text = alert_message(cfg, c, ev)
    assert "EZE→GIG" in subject and "USD 412 por persona" in subject
    for expected in ["Río de Janeiro (Galeão) (GIG)", "vie 15/01/2027 → lun 25/01/2027 (10 días)", "Escalas: 1",
                     "USD 2.060 total (5 adultos", "17,6% debajo de la media", "Vs. ventana GIG: sin datos",
                     "Regla: A (vs. historial del itinerario)", "Reserva: https://example.com/x"]:
        assert expected in text, expected
