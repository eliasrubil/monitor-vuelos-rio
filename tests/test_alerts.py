import datetime as dt
import logging
import smtplib

import pytest
import requests

from monitor.alerts import build_notifier
from monitor.alerts.channels import AlertError, EmailChannel, Notifier, TelegramChannel
from monitor.alerts.format import EMAIL_SUBJECT, Candidate, alert_message, alerts_email
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


def test_every_email_has_the_footer_image_inline():
    FakeSMTP.instances.clear()
    EmailChannel("yo@gmail.com", "app-pass", "a@x.com", smtp_factory=FakeSMTP).send(
        "Hola", "Linea 1 <b>\nReserva: https://x.com/a?b=1&c=2")
    msg = FakeSMTP.instances[0].messages[0]
    assert msg.get_body(("plain",)).get_content().startswith("Linea 1 <b>")
    html_part = msg.get_body(("html",))
    page = html_part.get_content()
    (img,) = [p for p in msg.walk() if p.get_content_type() == "image/jpeg"]
    cid = img["Content-ID"].strip("<>")
    assert page.index("Linea 1 &lt;b&gt;") < page.index(f'src="cid:{cid}"')       # al final, después del texto
    assert '<a href="https://x.com/a?b=1&amp;c=2">' in page
    assert img.get_content_disposition() == "inline"
    assert len(img.get_content()) < 100_000                                        # comprimida


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
    assert subject == "Precio bajo EZE→GIG 15/01–25/01: USD 412 por persona"
    details, stats = text.split("\n\n")
    assert stats.splitlines()[0] == ">>STATS<<"
    stats = "\n".join(stats.splitlines()[1:])
    assert details.splitlines() == [
        "Fechas: vie 15/01/2027 → lun 25/01/2027 (10 días)",
        "Escalas: 1",
        "Aerolíneas Argentinas, GOL",
        "Precio: USD 412 por persona · USD 2.060 total (5 adultos, con impuestos)",
        "Reserva: https://example.com/x",
    ]
    assert stats.splitlines() == [
        "Vs. itinerario: 17,6% debajo de la media (8 obs., media USD 500)",
        "Vs. ventana GIG: sin datos (0 pares)",
    ]
    for gone in ("Destino:", "Origen:", "Regla", "máximo", "antigüedad"):
        assert gone not in text


def test_single_unknown_airline_shows_code(cfg):
    c = Candidate("AEP", "CFB", dt.date(2027, 1, 20), dt.date(2027, 1, 31), 0, "ZZ", 834.4, 4172.0, "USD", 1)
    ev = evaluate(834.4, [], [], DetectionConfig(), dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc))
    _, text = alert_message(cfg, c, ev)
    assert "Escalas: 0\nZZ\n" in text


def test_no_emojis_in_any_message(cfg):
    import re
    from monitor.alerts.format import budget_skip_message, error_message, summary_message, test_message
    now = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    c = Candidate("EZE", "GIG", dt.date(2027, 1, 15), dt.date(2027, 1, 25), 1, "AR", 412.0, 2060.0, "USD", 1,
                  self_transfer=True)
    ev = evaluate(412, [500] * 8, [], DetectionConfig(), now)
    messages = [alert_message(cfg, c, ev), alerts_email(cfg, [alert_message(cfg, c, ev)] * 2),
                budget_skip_message(cfg, now, 100, 50), error_message(now, "x"), test_message(now),
                summary_message(cfg, now, "full", 1, 0, 1, 0, [], [], [])]
    emoji = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]")
    brazil_flag = "\U0001F1E7\U0001F1F7"   # única excepción pedida: el título del email diario
    for subject, body in messages:
        assert not emoji.search(subject.replace(brazil_flag, "") + body), subject


def test_daily_email_subject_tip_and_order(cfg):
    now = dt.datetime(2026, 10, 8, 11, tzinfo=dt.timezone.utc)
    def msg(price):
        c = Candidate("BUE", "GIG", dt.date(2027, 1, 25), dt.date(2027, 2, 5), 0, "JA", price, price * 5, "USD", 1)
        return alert_message(cfg, c, evaluate(price, [], [], DetectionConfig(), now))
    subject, body = alerts_email(cfg, [msg(299.8)])
    assert subject == f"{EMAIL_SUBJECT} - Morning Sunshine! - 1 hallazgo"
    assert EMAIL_SUBJECT.startswith("Alerta Diaria de Vuelos \U0001F1E7\U0001F1F7 by ")
    assert body.rstrip().endswith("Muchas veces la tarifa sigue o hay una muy parecida.")
    subject, _ = alerts_email(cfg, [msg(299.8), msg(343.6), msg(384.0)])
    assert subject.endswith(" - 3 hallazgos")


def test_alert_message_with_per_leg_links_and_same_price(cfg):
    c = Candidate("AEP", "CFB", dt.date(2027, 1, 26), dt.date(2027, 2, 5), 1, "AR", 901.6, 4508.0, "USD", 1,
                  booking_links=[("Ida", None), ("Vuelta", "https://v")], same_price_count=1)
    ev = evaluate(901.6, [], [], DetectionConfig(), dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc))
    _, text = alert_message(cfg, c, ev)
    assert "Hay 1 fecha más con el mismo precio." in text
    assert "Reserva Ida: sin link disponible\nReserva Vuelta: https://v" in text


def test_messages_use_argentina_time(cfg):
    from monitor.alerts.format import error_message, summary_message, test_message
    now = dt.datetime(2026, 10, 7, 13, 15, tzinfo=dt.timezone.utc)
    for _, body in (test_message(now), error_message(now, "x"), summary_message(cfg, now, "full", 1, 0, 1, 0, [], [], [])):
        assert "07/10/2026 10:15 hora argentina" in body and "UTC" not in body


def test_demo_sends_morning_and_night(cfg):
    from monitor.demo import send_demo

    from .conftest import FakeChannel
    tg, mail = FakeChannel("telegram"), FakeChannel("email")
    delivered = send_demo(cfg, Notifier([tg, mail]), dt.datetime(2026, 10, 7, 14, tzinfo=dt.timezone.utc))
    assert delivered == ["telegram", "email"]
    assert len(tg.sent) == 2 and len(mail.sent) == 2      # mañana y noche
    text = tg.sent[0][1]
    assert text.startswith("[PRUEBA - valores simulados]\nI) EZE --> GIG 15/01 al 25/01 (10 días) - 349USD por persona "
                           "(+2 fechas más con el mismo precio) - JetSMART\n")
    assert "III) AEP/EZE --> CFB" in text and "Reserva Ida:" in text and "II) EZE --> GRU" in text
    assert mail.sent[0][0] == f"[PRUEBA - valores simulados] {EMAIL_SUBJECT} - Morning Sunshine! - 3 hallazgos"
    assert mail.sent[1][0] == f"[PRUEBA - valores simulados] {EMAIL_SUBJECT} - Night Owl! - 1 hallazgo nuevo"
    night = mail.sent[1][1]
    assert "El hallazgo I) de la mañana mantiene su precio" in night
    assert "El hallazgo II) de la mañana subió a USD 405 por persona" in night
    assert "El hallazgo III) de la mañana ya no aparece" in night
    assert "I) Precio bajo AEP→GRU 24/01–03/02" in night


def test_roman_numerals():
    from monitor.alerts.format import roman
    assert [roman(n) for n in (1, 2, 3, 4, 5, 9, 12, 40)] == ["I", "II", "III", "IV", "V", "IX", "XII", "XL"]


def _followups():
    from monitor.alerts.format import FollowUp
    d = dt.date
    return [FollowUp(1, "BUE", "GRU", d(2027, 1, 16), d(2027, 1, 26), 317.0, "same", 317.2),
            FollowUp(2, "BUE", "GIG", d(2027, 1, 20), d(2027, 1, 30), 340.0, "up", 352.4),
            FollowUp(3, "BUE", "CFB", d(2027, 1, 21), d(2027, 1, 31), 640.0, "down", 601.0),
            FollowUp(4, "BUE", "GIG", d(2027, 1, 22), d(2027, 2, 1), 345.0, "gone"),
            FollowUp(5, "BUE", "GIG", d(2027, 1, 23), d(2027, 2, 2), 350.0, "error")]


def test_night_email_lists_morning_followups_then_new_findings(cfg):
    now = dt.datetime(2026, 10, 8, 23, tzinfo=dt.timezone.utc)
    c = Candidate("BUE", "GIG", dt.date(2027, 1, 25), dt.date(2027, 2, 5), 0, "JA", 299.8, 1499.0, "USD", 1)
    msg = alert_message(cfg, c, evaluate(299.8, [], [], DetectionConfig(), now))
    subject, body = alerts_email(cfg, [msg], "night", _followups())
    assert subject == f"{EMAIL_SUBJECT} - Night Owl! - 1 hallazgo nuevo"
    morning, new, tip = body.split("\n\n" + "=" * 60 + "\n")
    assert morning.splitlines() == [
        "Hallazgos de la mañana",
        "El hallazgo I) de la mañana mantiene su precio",
        "El hallazgo II) de la mañana subió a USD 352 por persona",
        "El hallazgo III) de la mañana bajó a USD 601 por persona",
        "El hallazgo IV) de la mañana ya no aparece",
        "El hallazgo V) de la mañana no se pudo verificar",
    ]
    assert new.startswith("Hallazgos nuevos\n\nI) Precio bajo BUE→GIG 25/01–05/02")
    assert tip.startswith("Qué hacer si el link")
    subject, body = alerts_email(cfg, [], "night", _followups())
    assert subject.endswith("Night Owl! - sin hallazgos nuevos") and "Sin hallazgos nuevos." in body


def test_night_telegram_lists_followups_first(cfg):
    from monitor.alerts.format import telegram_alert_chunks
    c = Candidate("BUE", "GIG", dt.date(2027, 1, 25), dt.date(2027, 2, 5), 0, "JA", 299.8, 1499.0, "USD", 1,
                  depart_airport="EZE", return_airport="EZE")
    (text, idx), = telegram_alert_chunks(cfg, [c], slot="night", followups=_followups()[:2])
    assert idx == [0]
    assert text.splitlines() == [
        "Hallazgos de la mañana:",
        "El hallazgo I) de la mañana mantiene su precio",
        "El hallazgo II) de la mañana subió a USD 352 por persona",
        "",
        "Hallazgos nuevos:",
        "I) EZE --> GIG 25/01 al 05/02 (11 días) - 300USD por persona - JetSMART",
        "Reserva: sin link disponible",
    ]
