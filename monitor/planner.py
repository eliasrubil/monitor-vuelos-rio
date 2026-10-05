"""Decide qué consultas hacer en cada corrida.

Unidad de selección: el par de fechas de un destino. Cada par elegido se consulta desde
todos los orígenes con servicio. Reglas (por destino):
- Primera vez (o --full-scan): barrido completo.
- Si no: top_k pares más baratos (último precio conocido) + rotating_k del resto, el dato
  más viejo primero, + los pares que superarían max_staleness_runs corridas sin actualizar.
- Consultas no_service: solo se repiten cada recheck_days.
- Destinos en modo reducido: solo se consultan cada reduced_interval_days.
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
from dataclasses import dataclass, field
from typing import Optional

from .config import Config
from .dates import date_pairs
from .storage import PairState, Store

log = logging.getLogger(__name__)

MODE_NORMAL = "normal"
MODE_REDUCED = "reduced"


@dataclass(frozen=True)
class PlannedQuery:
    origin: str
    destination: str
    depart: dt.date
    ret: dt.date
    reason: str            # full | top | rotating | stale | recheck


@dataclass
class DestinationPlan:
    destination: str
    mode: str
    universe: int
    queries: list[PlannedQuery] = field(default_factory=list)
    full: bool = False
    skipped_reason: Optional[str] = None

    @property
    def saved(self) -> int:
        return self.universe - len(self.queries)


@dataclass
class Plan:
    destinations: list[DestinationPlan]

    @property
    def queries(self) -> list[PlannedQuery]:
        return [q for d in self.destinations for q in d.queries]

    @property
    def universe(self) -> int:
        return sum(d.universe for d in self.destinations)

    @property
    def saved(self) -> int:
        return sum(d.saved for d in self.destinations)

    @property
    def is_full(self) -> bool:
        return all(d.full for d in self.destinations if d.queries) and bool(self.queries)


def _days_since(ts: Optional[dt.datetime], today: dt.date) -> Optional[int]:
    return None if ts is None else (today - ts.date()).days


def destination_medians(store: Store, destinations: list[str]) -> dict[str, Optional[float]]:
    out = {}
    for dest in destinations:
        prices = [p.price_pp for p in store.window(dest)]
        out[dest] = statistics.median(prices) if prices else None
    return out


def decide_mode(cfg: Config, store: Store, destination: str) -> str:
    """GRU (o los destinos configurados) quedan en modo reducido salvo que su mediana por persona
    sea <= mediana del destino de referencia - margen."""
    opt = cfg.optimization
    if destination not in opt.reduced_destinations:
        return MODE_NORMAL
    ref = opt.reduced_reference_destination
    med = destination_medians(store, [destination, ref])
    if med[destination] is None or med[ref] is None:
        log.info("%s: modo reducido (sin datos suficientes para comparar con %s)", destination, ref)
        return MODE_REDUCED
    threshold = med[ref] - opt.gru_margin_usd
    mode = MODE_NORMAL if med[destination] <= threshold else MODE_REDUCED
    log.info(
        "%s: mediana pp %.0f vs %s %.0f - margen %.0f = %.0f -> modo %s",
        destination, med[destination], ref, med[ref], opt.gru_margin_usd, threshold, mode,
    )
    return mode


def plan_run(cfg: Config, store: Store, origins: list[str], now: dt.datetime, full_scan: bool = False) -> Plan:
    today = now.date()
    pairs = date_pairs(cfg.search.departure_from, cfg.search.return_until, cfg.search.durations)
    plans = []
    for dest in cfg.search.destinations:
        dstate = store.dest_state(dest)
        mode = decide_mode(cfg, store, dest)
        dplan = DestinationPlan(destination=dest, mode=mode, universe=len(pairs) * len(origins))
        states = store.pair_states(dest)
        first_time = not store.has_any_observation_attempt(dest)

        if full_scan or first_time:
            dplan.full = True
            dplan.queries = [PlannedQuery(o, dest, d, r, "full") for (d, r) in pairs for o in origins]
            plans.append(dplan)
            continue

        if mode == MODE_REDUCED:
            since = _days_since(dstate.last_queried_at, today)
            if since is not None and since < cfg.optimization.reduced_interval_days:
                dplan.skipped_reason = (
                    f"modo reducido: última consulta hace {since} días "
                    f"(cada {cfg.optimization.reduced_interval_days})"
                )
                plans.append(dplan)
                continue

        dplan.queries = _select(cfg, pairs, origins, dest, states, dstate.run_seq + 1, today)
        plans.append(dplan)
    return Plan(plans)


def _select(
    cfg: Config,
    pairs: list[tuple[dt.date, dt.date]],
    origins: list[str],
    dest: str,
    states: dict[tuple[str, dt.date, dt.date], PairState],
    new_seq: int,
    today: dt.date,
) -> list[PlannedQuery]:
    opt = cfg.optimization

    def state(origin: str, pair: tuple[dt.date, dt.date]) -> Optional[PairState]:
        return states.get((origin, pair[0], pair[1]))

    def active_origins(pair) -> list[str]:
        return [o for o in origins if not (state(o, pair) and state(o, pair).no_service)]

    def recheck_origins(pair) -> list[str]:
        due = []
        for o in origins:
            st = state(o, pair)
            if st and st.no_service:
                since = _days_since(st.last_queried_at, today)
                if since is None or since >= opt.recheck_days:
                    due.append(o)
        return due

    def price(pair) -> Optional[float]:
        values = [state(o, pair).last_price_pp for o in active_origins(pair)
                  if state(o, pair) and state(o, pair).last_price_pp is not None]
        return min(values) if values else None

    def seq(pair) -> int:
        # Antigüedad del dato: la consulta más vieja entre los orígenes con servicio (-1 = nunca).
        values = [(state(o, pair).last_queried_seq if state(o, pair) and state(o, pair).last_queried_seq
                   is not None else -1) for o in active_origins(pair)]
        return min(values)

    active = [p for p in pairs if active_origins(p)]
    priced = sorted((p for p in active if price(p) is not None), key=lambda p: (price(p), p))
    top = priced[: opt.top_k]
    top_set = set(top)
    rest = sorted((p for p in active if p not in top_set), key=lambda p: (seq(p), p))
    rotating = rest[: opt.rotating_k]
    stale = [p for p in rest[opt.rotating_k:] if seq(p) < 0 or new_seq - seq(p) >= opt.max_staleness_runs]

    chosen: dict[tuple[dt.date, dt.date], str] = {}
    for reason, group in (("top", top), ("rotating", rotating), ("stale", stale)):
        for p in group:
            chosen.setdefault(p, reason)

    queries = []
    for p in pairs:
        reason = chosen.get(p)
        recheck = recheck_origins(p)
        for o in origins:
            if reason and o in active_origins(p):
                queries.append(PlannedQuery(o, dest, p[0], p[1], reason))
            elif o in recheck:
                queries.append(PlannedQuery(o, dest, p[0], p[1], "recheck"))
    return queries
