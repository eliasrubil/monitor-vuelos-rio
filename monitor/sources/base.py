"""Interfaz común para fuentes de precios."""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

STATUS_OK = "ok"
STATUS_NO_RESULTS = "no_results"
STATUS_ERROR = "error"


@dataclass(frozen=True)
class SearchQuery:
    origin: str
    destination: str
    depart: dt.date
    ret: dt.date
    adults: int
    max_stops: int
    cabin_class: str
    market: str
    currency: str
    airlines_exclude: tuple[str, ...] = ()
    airports_exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class Quote:
    """Mejor tarifa encontrada para una consulta (ya filtrada por escalas y moneda)."""

    price_total: float
    price_pp: float
    currency: str
    stops: int
    airlines: tuple[str, ...]
    source_ref: Optional[str] = None      # id opaco de la fuente para pedir el link de reserva
    booking_url: Optional[str] = None
    self_transfer: bool = False
    depart_airport: Optional[str] = None  # aeropuerto real de salida de la ida (p. ej. EZE cuando se busca BUE)
    return_airport: Optional[str] = None  # aeropuerto real de llegada de la vuelta


@dataclass
class SearchOutcome:
    status: str                           # ok | no_results | error
    quote: Optional[Quote] = None
    http_status: Optional[int] = None
    attempts: int = 0
    billable: bool = False                # la request cuenta para el presupuesto
    error: Optional[str] = None
    fatal: bool = False                   # error que hace inútil seguir consultando (clave, facturación)


@dataclass
class LinkOutcome:
    # (etiqueta, url): etiqueta "" = link para ida y vuelta; "Ida"/"Vuelta" = link de un solo tramo
    # (url None si ese tramo no tiene link).
    links: list[tuple[str, Optional[str]]] = field(default_factory=list)
    http_status: Optional[int] = None
    attempts: int = 0
    billable: bool = False
    error: Optional[str] = None
    fatal: bool = False

    @property
    def ok(self) -> bool:
        return any(url for _, url in self.links)


class PriceSource(ABC):
    """Adaptador de una API de precios. Cada consulta debe ser independiente:
    los errores se devuelven en el resultado, nunca se propagan como excepción."""

    name: str = "base"
    supports_open_jaw: bool = False

    @abstractmethod
    def search_round_trip(self, query: SearchQuery) -> SearchOutcome:
        ...

    def booking_link(self, source_ref: str) -> LinkOutcome:
        return LinkOutcome(error="la fuente no ofrece links de reserva")


@dataclass
class Itinerary:
    """Helper para adaptadores: una opción candidata antes de elegir la mejor."""

    amount: float
    currency: str
    stops: int
    airlines: tuple[str, ...] = field(default_factory=tuple)
    source_ref: Optional[str] = None
    self_transfer: bool = False
    airports: tuple[str, ...] = field(default_factory=tuple)   # todos los aeropuertos de todos los tramos
    # Salida y llegada de la ida y de la vuelta (sin escalas): (ida_desde, ida_hasta, vuelta_desde, vuelta_hasta)
    endpoints: tuple[Optional[str], ...] = field(default_factory=tuple)
    depart_airport: Optional[str] = None
    return_airport: Optional[str] = None
