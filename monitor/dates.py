"""Universo de pares de fechas (ida, vuelta)."""

from __future__ import annotations

import datetime as dt


def date_pairs(departure_from: dt.date, return_until: dt.date, durations: list[int]) -> list[tuple[dt.date, dt.date]]:
    """Todos los pares con ida >= departure_from, vuelta <= return_until y duración en durations."""
    pairs = []
    depart = departure_from
    while depart <= return_until:
        for days in sorted(durations):
            ret = depart + dt.timedelta(days=days)
            if ret <= return_until:
                pairs.append((depart, ret))
        depart += dt.timedelta(days=1)
    return pairs
