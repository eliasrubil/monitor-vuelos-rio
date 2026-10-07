"""Textos de alertas y resúmenes (texto plano, sirve para Telegram y email)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from ..config import Config
from ..detection import Evaluation

STATS_TITLE = ">>STATS<<"
TELEGRAM_MAX_CHARS = 4000   # Telegram admite 4096 caracteres por mensaje
WEEKDAYS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


@dataclass
class Candidate:
    """Mejor tarifa de un itinerario (destino + fechas) en la corrida actual."""

    origin: str
    destination: str
    depart: dt.date
    ret: dt.date
    stops: int
    airlines: str
    price_pp: float
    price_total: float
    currency: str
    observation_id: int
    source_ref: Optional[str] = None
    booking_url: Optional[str] = None
    self_transfer: bool = False
    # Links de reserva: [("", url)] para ida y vuelta, o [("Ida", url), ("Vuelta", url)] por tramo.
    booking_links: list[tuple[str, Optional[str]]] = field(default_factory=list)
    # Cuántas otras fechas del mismo origen y destino alertaron en esta corrida con el mismo precio.
    same_price_count: int = 0
    depart_airport: Optional[str] = None   # aeropuerto real de salida (la búsqueda puede ser BUE)
    return_airport: Optional[str] = None   # aeropuerto real de llegada de la vuelta

    @property
    def shown_origin(self) -> str:
        """EZE o AEP si ida y vuelta usan el mismo; "EZE/AEP" (salida/llegada) si son distintos."""
        dep, ret = self.depart_airport, self.return_airport
        if dep and ret and dep != ret:
            return f"{dep}/{ret}"
        return dep or ret or self.origin

    @property
    def duration(self) -> int:
        return (self.ret - self.depart).days


def money(amount: float, currency: str = "USD") -> str:
    return f"{currency} {amount:,.0f}".replace(",", ".")


def pct(value: float) -> str:
    return f"{value * 100:.1f}%".replace(".", ",")


def fdate(d: dt.date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d:%d/%m/%Y}"


def booking_lines(c: Candidate) -> list[str]:
    links = c.booking_links or ([("", c.booking_url)] if c.booking_url else [])
    if not any(url for _, url in links):
        return ["Reserva: sin link disponible"]
    return [f"Reserva{f' {label}' if label else ''}: {url or 'sin link disponible'}" for label, url in links]


def same_price_text(c: Candidate) -> str:
    n = c.same_price_count
    return "1 fecha más con el mismo precio" if n == 1 else f"{n} fechas más con el mismo precio"


def _vs_line(label: str, below: Optional[float], extra: str) -> str:
    if below is None:
        return f"{label}: sin datos ({extra})"
    direction = "debajo" if below >= 0 else "arriba"
    return f"{label}: {pct(abs(below))} {direction} de la media ({extra})"


def alert_message(cfg: Config, c: Candidate, ev: Evaluation) -> tuple[str, str]:
    subject = (
        f"Precio bajo {c.shown_origin}→{c.destination} {c.depart:%d/%m}–{c.ret:%d/%m}: "
        f"{money(c.price_pp, c.currency)} por persona"
    )
    codes = [a.strip() for a in c.airlines.split(",") if a.strip()]
    airlines = ", ".join(cfg.airline(code) for code in codes) or "s/d"
    its = ev.itinerary_stats
    itin_extra = f"{its.n} obs., media {money(its.mean, c.currency)}" if its else "0 obs."
    win = ev.window_stats
    if win:
        rank_txt = f"puesto {ev.window_rank} de {win.n}, " if ev.window_rank else f"{win.n} pares, "
        win_extra = f"{rank_txt}media {money(win.mean, c.currency)}"
    else:
        win_extra = "0 pares"
    details = [
        f"Fechas: {fdate(c.depart)} → {fdate(c.ret)} ({c.duration} días)",
        f"Escalas: {c.stops}",
        airlines,
        f"Precio: {money(c.price_pp, c.currency)} por persona · {money(c.price_total, c.currency)} total "
        f"({cfg.search.adults} adultos, con impuestos)",
    ]
    if c.same_price_count:
        details.append(f"Hay {same_price_text(c)}.")
    details += booking_lines(c)
    if c.self_transfer:
        details.append("Atención: requiere self-transfer (tramos en tickets separados).")
    stats = [
        _vs_line("Vs. itinerario", ev.pct_below_itinerary, itin_extra),
        _vs_line(f"Vs. ventana {c.destination}", ev.pct_below_window, win_extra),
    ]
    if ev.robust_z is not None:
        stats.append(f"z robusto del itinerario: {ev.robust_z:.2f}")
    return subject, "\n".join(details) + "\n\n" + STATS_TITLE + "\n" + "\n".join(stats)


def alerts_email(cfg: Config, messages: list[tuple[str, str]]) -> tuple[str, str]:
    if len(messages) == 1:
        return messages[0]
    subject = f"{len(messages)} precios bajos detectados"
    body = "\n\n".join(f"{s}\n{'-' * min(len(s), 60)}\n{t}" for s, t in messages)
    return subject, body


def telegram_alert_entry(c: Candidate) -> str:
    amount = f"{c.price_pp:,.0f}".replace(",", ".")
    line = (f"{c.shown_origin} --> {c.destination} {c.depart:%d/%m} al {c.ret:%d/%m} ({c.duration} días) - "
            f"{amount}{c.currency} por persona")
    if c.same_price_count:
        line += f" (+{same_price_text(c)})"
    return "\n".join([line] + booking_lines(c))


def telegram_alert_chunks(cands: list[Candidate], max_chars: int = TELEGRAM_MAX_CHARS) -> list[tuple[str, list[int]]]:
    """Todas las alertas en un único mensaje; si supera el límite de Telegram, se parte en varios mensajes sin
    cortar ninguna alerta. Devuelve (texto, índices de las alertas que contiene)."""
    chunks: list[tuple[str, list[int]]] = []
    text, idx = "", []
    for i, c in enumerate(cands):
        entry = telegram_alert_entry(c)
        if text and len(text) + 1 + len(entry) > max_chars:
            chunks.append((text, idx))
            text, idx = "", []
        text = f"{text}\n{entry}" if text else entry
        idx.append(i)
    if text:
        chunks.append((text, idx))
    return chunks


@dataclass
class DestinationSummary:
    destination: str
    mode: str
    n: int
    mean: Optional[float]
    median: Optional[float]


@dataclass
class TopItem:
    destination: str
    origin: str
    depart: dt.date
    ret: dt.date
    price_pp: float
    age_days: int


def summary_message(
    cfg: Config,
    now: dt.datetime,
    mode: str,
    executed: int,
    saved: int,
    month_total: int,
    alerts_sent: int,
    top: list[TopItem],
    destinations: list[DestinationSummary],
    notes: list[str],
) -> tuple[str, str]:
    subject = f"Resumen vuelos {now:%d/%m/%Y}: " + (
        f"mejor {top[0].destination} {money(top[0].price_pp, cfg.search.currency)} pp" if top else "sin precios"
    )
    lines = [
        f"Corrida {now:%Y-%m-%d %H:%M} UTC ({mode})",
        f"Consultas: {executed} hechas, {saved} ahorradas · acumulado del mes: {month_total}"
        f"/{cfg.budget.max_requests_per_month}",
        f"Alertas enviadas: {alerts_sent}",
        "",
        f"Top {cfg.alerts.summary_top_n} itinerarios más baratos (último precio conocido, por persona):",
    ]
    adults = cfg.search.adults
    for i, t in enumerate(top, 1):
        lines.append(
            f"{i}. {t.destination} {fdate(t.depart)} → {fdate(t.ret)} ({(t.ret - t.depart).days} d) · "
            f"{money(t.price_pp, cfg.search.currency)} pp · {money(t.price_pp * adults, cfg.search.currency)} total · "
            f"desde {t.origin} · dato de hace {t.age_days} d"
        )
    if not top:
        lines.append("(sin datos todavía)")
    lines += ["", "Media por persona por destino:"]
    for d in destinations:
        mode_txt = " · modo reducido" if d.mode == "reduced" else ""
        if d.mean is None:
            lines.append(f"- {cfg.airport(d.destination)}: sin datos{mode_txt}")
        else:
            lines.append(
                f"- {cfg.airport(d.destination)}: media {money(d.mean, cfg.search.currency)}, "
                f"mediana {money(d.median, cfg.search.currency)} ({d.n} pares){mode_txt}"
            )
    if notes:
        lines += ["", "Notas:"] + [f"- {n}" for n in notes]
    return subject, "\n".join(lines)


def budget_skip_message(cfg: Config, now: dt.datetime, used: int, estimated: int) -> tuple[str, str]:
    limit = cfg.budget.max_requests_per_month
    subject = "Monitor de vuelos: corrida salteada por presupuesto"
    text = (
        f"La corrida del {now:%Y-%m-%d %H:%M} UTC necesitaba ~{estimated} requests y el acumulado del mes es "
        f"{used}. Con el tope de {limit} (max_requests_per_month) se superaría el límite, así que no se consultó "
        f"nada. Subí budget.max_requests_per_month en config.yaml o bajá top_k/rotating_k si querés seguir."
    )
    return subject, text


def error_message(now: dt.datetime, detail: str, title: str = "error de la fuente") -> tuple[str, str]:
    return f"Monitor de vuelos: {title}", f"Corrida {now:%Y-%m-%d %H:%M} UTC.\n{detail}"


def test_message(now: dt.datetime) -> tuple[str, str]:
    return (
        "Monitor de vuelos: mensaje de prueba",
        f"Si recibís esto, el canal está bien configurado ({now:%Y-%m-%d %H:%M} UTC).",
    )
