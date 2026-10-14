"""Alertas simuladas para ver el formato real en Telegram y email, sin consultar la API ni tocar la base."""

from __future__ import annotations

import datetime as dt

from .alerts.channels import Notifier
from .alerts.format import (
    SLOT_MORNING,
    SLOT_NIGHT,
    Candidate,
    FollowUp,
    alert_message,
    alerts_email,
    telegram_alert_chunks,
)
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
        (Candidate("BUE", "GIG", d(2027, 1, 20), d(2027, 1, 31), 1, "AR", 434.4, 2172.0, "USD", 0,
                   depart_airport="AEP", return_airport="EZE",
                   booking_links=[("Ida", "https://www.aerolineas.com.ar/"), ("Vuelta", "https://www.aerolineas.com.ar/")]),
         [548.0], 589.0),
        (Candidate("BUE", "GRU", d(2027, 1, 22), d(2027, 2, 1), 0, "LA", 387.0, 1935.0, "USD", 0,
                   depart_airport="EZE", return_airport="EZE"),
         [470, 465, 480, 455, 475, 468, 472], 505.0),
    ]
    out = []
    for c, history, win_mean in specs:
        ev = evaluate(c.price_pp, history, _window(now, c.price_pp, win_mean), cfg.detection, now)
        out.append((c, ev))
    out.sort(key=lambda a: a[0].price_pp)   # igual que una corrida real: el más barato primero
    return out


def demo_followups(alerts: list[tuple[Candidate, object]]) -> list[FollowUp]:
    """Seguimiento nocturno simulado de los hallazgos de la mañana: uno de cada caso."""
    out = []
    for label, ((c, _), (status, delta)) in enumerate(
            zip(alerts, [("same", 0), ("up", 18), ("gone", None)]), 1):
        f = FollowUp(label, c.origin, c.destination, c.depart, c.ret, c.price_pp, status)
        f.price_pp = None if delta is None else c.price_pp + delta
        out.append(f)
    return out


def send_demo(cfg: Config, notifier: Notifier, now: dt.datetime) -> list[str]:
    """La corrida de la mañana y la de la noche, cada una con un mensaje de Telegram y un email, con el mismo
    formato que una corrida real. Devuelve los canales por los que salió la de la mañana."""
    alerts = demo_alerts(cfg, now)
    # A la noche aparece una fecha distinta de GRU, más barata que la de la mañana.
    gru, gru_ev = next((c, ev) for c, ev in alerts if c.destination == "GRU")
    price = gru.price_pp - 12
    night = [(Candidate(gru.origin, "GRU", dt.date(2027, 1, 24), dt.date(2027, 2, 3), gru.stops, gru.airlines, price,
                        price * cfg.search.adults, gru.currency, 0, depart_airport="AEP", return_airport="AEP"),
              gru_ev)]
    runs = [(SLOT_MORNING, alerts, None), (SLOT_NIGHT, night, demo_followups(alerts))]
    delivered_by_slot = []
    for slot, items, followups in runs:
        delivered = []
        for text, _ in telegram_alert_chunks(cfg, [c for c, _ in items], slot=slot, followups=followups):
            delivered += notifier.send("", f"{DEMO_PREFIX}\n{text}", only={"telegram"})
        subject, body = alerts_email(cfg, [alert_message(cfg, c, ev) for c, ev in items], slot, followups)
        delivered += notifier.send(f"{DEMO_PREFIX} {subject}", body, only={"email"})
        delivered_by_slot.append(delivered)
    return delivered_by_slot[0]
