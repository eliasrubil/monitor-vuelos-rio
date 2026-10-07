"""Detección de precios bajos (reglas A, B y C) y estadísticas."""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, field
from typing import Optional

from .config import DetectionConfig
from .storage import WindowPoint

RULE_TEMPORAL = "A"
RULE_CROSS = "B"
RULE_ABSOLUTE = "C"


@dataclass(frozen=True)
class Stats:
    n: int
    mean: float
    median: float
    stdev: float
    mad: float


def compute_stats(values: list[float]) -> Optional[Stats]:
    if not values:
        return None
    med = statistics.median(values)
    return Stats(
        n=len(values),
        mean=statistics.fmean(values),
        median=med,
        stdev=statistics.stdev(values) if len(values) > 1 else 0.0,
        mad=statistics.median(abs(v - med) for v in values),
    )


def pct_below(price: float, reference: float) -> float:
    """Fracción por debajo de la referencia (0.2 = 20% más barato)."""
    return (reference - price) / reference if reference else 0.0


@dataclass
class Evaluation:
    price_pp: float
    rules: list[str] = field(default_factory=list)
    itinerary_stats: Optional[Stats] = None
    window_stats: Optional[Stats] = None
    robust_z: Optional[float] = None
    window_rank: Optional[int] = None
    window_cutoff: Optional[int] = None
    window_age_days: Optional[tuple[int, int]] = None   # (mínima, máxima) antigüedad de la comparación

    @property
    def triggered(self) -> bool:
        return bool(self.rules)

    @property
    def pct_below_itinerary(self) -> Optional[float]:
        return pct_below(self.price_pp, self.itinerary_stats.mean) if self.itinerary_stats else None

    @property
    def pct_below_window(self) -> Optional[float]:
        return pct_below(self.price_pp, self.window_stats.mean) if self.window_stats else None


def evaluate(
    price_pp: float,
    history: list[float],
    window: list[WindowPoint],
    cfg: DetectionConfig,
    now: dt.datetime,
) -> Evaluation:
    """history: precios por persona anteriores del itinerario (sin la corrida actual).
    window: último precio conocido de cada par del destino (incluye el itinerario evaluado)."""
    ev = Evaluation(price_pp=price_pp)

    # Regla A: temporal
    t = cfg.temporal
    ev.itinerary_stats = compute_stats(history)
    if ev.itinerary_stats and ev.itinerary_stats.n >= t.min_observations:
        s = ev.itinerary_stats
        if s.mad > 0:
            ev.robust_z = (price_pp - s.median) / (t.mad_scale * s.mad)
        below_mean = price_pp < s.mean * (1 - t.drop_pct)
        below_z = ev.robust_z is not None and ev.robust_z < t.robust_z
        if below_mean or below_z:
            ev.rules.append(RULE_TEMPORAL)

    # Regla B: transversal
    c = cfg.cross
    prices = [p.price_pp for p in window]
    ev.window_stats = compute_stats(prices)
    if window:
        ages = [max((now - p.price_at).days, 0) for p in window]
        ev.window_age_days = (min(ages), max(ages))
    if ev.window_stats and ev.window_stats.n >= c.min_pairs:
        n = ev.window_stats.n
        ev.window_rank = 1 + sum(1 for p in prices if p < price_pp)
        ev.window_cutoff = max(1, math.ceil(round(c.percentile * n, 9)))
        in_bottom = ev.window_rank <= ev.window_cutoff
        if in_bottom and pct_below(price_pp, ev.window_stats.mean) >= c.drop_pct:
            ev.rules.append(RULE_CROSS)

    # Regla C: umbral absoluto
    if cfg.absolute_threshold_usd_pp is not None and price_pp <= cfg.absolute_threshold_usd_pp:
        ev.rules.append(RULE_ABSOLUTE)

    return ev


def passes_antispam(price_pp: float, last_alerted_pp: Optional[float], min_drop: float) -> bool:
    """Se vuelve a alertar un itinerario solo si bajó al menos min_drop respecto de la última alerta."""
    if last_alerted_pp is None:
        return True
    return price_pp <= last_alerted_pp * (1 - min_drop) + 1e-9
