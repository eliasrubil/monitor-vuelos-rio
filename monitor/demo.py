"""Alertas simuladas para ver el formato real en Telegram y email, sin consultar la API ni tocar la base."""

from __future__ import annotations

import datetime as dt

from .alerts.channels import Notifier
from .alerts.format import Candidate, alert_message, alerts_email, telegram_alert_chunks
from .config import Config
from .detection import evaluate
from .storage import WindowPoint

DEMO_PREFIX = "[PRUEBA - valores simulados]"


def _window(now: dt.datetime, cheapest: float, mean_target: float, n: int = 39) -> list[WindowPoint]:
    base = dt.date(2027, 1, 14)
    others = [mean_target + 5 * (i - (n - 2) / 2) for i in range(n - 1)]   # todos más caros que el simulado
    return [WindowPoint(base + dt.timedelta(days=i % 14), base + dt.timedelta(days=i % 14 + 10), p, now, "BUE")
            for i, p in enumerate([cheapest] + others)]


def demo_alerts(cfg: Config, now: dt.datetime) -> list[tuple[Candidate, object]]:
    d = dt.date
    specs = [
        # (candidato, historial del itinerario, media de la ventana)
        (Candidate("BUE", "GIG", d(2027, 1, 15), d(2027, 1, 25), 0, "JA", 349.0, 1745.0, "USD", 0,
                   depart_airport="EZE", return_airport="EZE", same_price_count=2,
                   booking_links=[("", "https://jetsmart.com/ar/es/")]),
         [420, 415, 430, 410, 425, 418, 422, 428], 452.0),
        (Candidate("BUE", "CFB", d(2027, 1, 20), d(2027, 1, 31), 1, "AR", 834.4, 4172.0, "USD", 0,
                   depart_airport="AEP", return_airport="EZE",
                   booking_links=[("Ida", "https://www.aerolineas.com.ar/"), ("Vuelta", "https://www.aerolineas.com.ar/")]),
         [948.0], 1189.0),
        (Candidate("BUE", "GRU", d(2027, 1, 22), d(2027, 2, 1), 0, "LA", 387.0, 1935.0, "USD", 0,
                   depart_airport="EZE", return_airport="EZE"),
         [470, 465, 480, 455, 475, 468, 472], 505.0),
    ]
    out = []
    for c, history, win_mean in specs:
        ev = evaluate(c.price_pp, history, _window(now, c.price_pp, win_mean), cfg.detection, now)
        out.append((c, ev))
    return out


def send_demo(cfg: Config, notifier: Notifier, now: dt.datetime) -> list[str]:
    """Un único mensaje de Telegram y un único email, con el mismo formato que una corrida real."""
    alerts = demo_alerts(cfg, now)
    delivered = []
    for text, _ in telegram_alert_chunks(cfg, [c for c, _ in alerts]):
        delivered += notifier.send("", f"{DEMO_PREFIX}\n{text}", only={"telegram"})
    subject, body = alerts_email(cfg, [alert_message(cfg, c, ev) for c, ev in alerts])
    delivered += notifier.send(f"{DEMO_PREFIX} {subject}", body, only={"email"})
    return delivered
