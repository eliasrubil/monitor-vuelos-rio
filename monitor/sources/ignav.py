"""Adaptador para la API REST de Ignav (https://ignav.com/docs).

Solo usa endpoints y campos documentados:
- POST /fares/round-trip: origin, destination, departure_date, return_date, adults,
  max_stops, cabin_class, market, allow_self_transfer.
- Respuesta: itineraries[] con price{amount, currency}, outbound/inbound{segments[]},
  requires_self_transfer e ignav_id; segments[] con marketing_carrier_code.
- POST /fares/booking-links: {"ignav_id"} -> booking_options[].links[].url
- Auth: header X-Api-Key. Solo las respuestas 200 se facturan.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

import requests

from ..config import IgnavConfig
from .base import (
    STATUS_ERROR,
    STATUS_NO_RESULTS,
    STATUS_OK,
    Itinerary,
    LinkOutcome,
    PriceSource,
    Quote,
    SearchOutcome,
    SearchQuery,
)

log = logging.getLogger(__name__)

# 424: "request could not be completed; safe to retry". 429/5xx: transitorios.
RETRYABLE_STATUSES = {424, 429, 500, 502, 503, 504}
# 401: clave inválida, 402: facturación, 403: email sin verificar -> no tiene sentido seguir.
FATAL_STATUSES = {401, 402, 403}


class IgnavSource(PriceSource):
    name = "ignav"
    supports_open_jaw = False     # POST /fares/search existe pero su esquema no está verificado

    def __init__(
        self,
        api_key: str,
        cfg: IgnavConfig,
        http: Optional[Any] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_key:
            raise ValueError("Falta IGNAV_API_KEY")
        self._api_key = api_key
        self._cfg = cfg
        self._http = http or requests.Session()
        self._sleep = sleep
        self._currency_warned: set[str] = set()

    # ------------------------------------------------------------------ HTTP
    def _post(self, path: str, payload: dict) -> tuple[Optional[int], Optional[dict], int, Optional[str]]:
        """POST con reintentos. Devuelve (status, json, intentos, error)."""
        url = f"{self._cfg.base_url.rstrip('/')}{path}"
        headers = {"X-Api-Key": self._api_key, "Content-Type": "application/json"}
        status: Optional[int] = None
        error: Optional[str] = None
        attempts = 0
        for attempt in range(self._cfg.max_retries + 1):
            attempts = attempt + 1
            if attempt:
                self._sleep(self._cfg.backoff_seconds * (2 ** (attempt - 1)))
            try:
                resp = self._http.post(url, json=payload, headers=headers, timeout=self._cfg.timeout_seconds)
            except (requests.Timeout, requests.ConnectionError) as exc:
                status, error = None, f"{type(exc).__name__}"
                log.warning("Ignav %s: %s (intento %d)", path, error, attempts)
                continue
            except requests.RequestException as exc:
                return None, None, attempts, type(exc).__name__
            status = resp.status_code
            if status == 200:
                try:
                    return status, resp.json(), attempts, None
                except ValueError:
                    return status, None, attempts, "respuesta 200 sin JSON válido"
            error = _error_message(resp)
            if status in RETRYABLE_STATUSES:
                log.warning("Ignav %s: HTTP %s %s (intento %d)", path, status, error, attempts)
                continue
            break
        return status, None, attempts, error

    # --------------------------------------------------------------- búsqueda
    def search_round_trip(self, query: SearchQuery) -> SearchOutcome:
        payload = {
            "origin": query.origin,
            "destination": query.destination,
            "departure_date": query.depart.isoformat(),
            "return_date": query.ret.isoformat(),
            "adults": query.adults,
            "max_stops": query.max_stops,
            "cabin_class": query.cabin_class,
            "market": query.market,
            "allow_self_transfer": self._cfg.allow_self_transfer,
        }
        if query.airlines_exclude:
            payload["airlines_exclude"] = list(query.airlines_exclude)
        status, data, attempts, error = self._post("/fares/round-trip", payload)
        billable = status == 200
        if status != 200 or data is None:
            return SearchOutcome(
                status=STATUS_ERROR,
                http_status=status,
                attempts=attempts,
                billable=billable,
                error=error or f"HTTP {status}",
                fatal=status in FATAL_STATUSES,
            )
        try:
            candidates = [c for c in (self._parse_itinerary(it) for it in data.get("itineraries") or []) if c]
        except (TypeError, AttributeError) as exc:
            return SearchOutcome(STATUS_ERROR, http_status=status, attempts=attempts, billable=True,
                                 error=f"respuesta con formato inesperado: {type(exc).__name__}")
        valid = []
        for c in candidates:
            if c.stops > query.max_stops:
                continue
            if set(c.airlines) & set(query.airlines_exclude):
                continue
            if set(c.airports) & set(query.airports_exclude):
                continue
            if c.currency != query.currency:
                if c.currency not in self._currency_warned:
                    log.warning("Ignav devolvió moneda %s (se esperaba %s); se descarta", c.currency, query.currency)
                    self._currency_warned.add(c.currency)
                continue
            valid.append(c)
        if not valid:
            return SearchOutcome(STATUS_NO_RESULTS, http_status=status, attempts=attempts, billable=True)
        best = min(valid, key=lambda c: c.amount)
        if self._cfg.price_is_total:
            total, pp = best.amount, best.amount / query.adults
        else:
            total, pp = best.amount * query.adults, best.amount
        quote = Quote(
            price_total=round(total, 2),
            price_pp=round(pp, 2),
            currency=best.currency,
            stops=best.stops,
            airlines=best.airlines,
            source_ref=best.source_ref,
            self_transfer=best.self_transfer,
            depart_airport=best.depart_airport,
            return_airport=best.return_airport,
        )
        return SearchOutcome(STATUS_OK, quote=quote, http_status=status, attempts=attempts, billable=True)

    @staticmethod
    def _parse_itinerary(it: dict) -> Optional[Itinerary]:
        price = it.get("price") or {}
        amount = price.get("amount")
        if amount is None:
            return None
        legs = [leg for leg in (it.get("outbound"), it.get("inbound")) if leg]
        if not legs:
            return None
        stops = 0
        airlines: list[str] = []
        airports: list[str] = []
        for leg in legs:
            segments = leg.get("segments") or []
            stops = max(stops, max(len(segments) - 1, 0))
            for seg in segments:
                code = seg.get("marketing_carrier_code")
                if code and code not in airlines:
                    airlines.append(code)
                for key in ("departure_airport", "arrival_airport"):
                    if seg.get(key) and seg[key] not in airports:
                        airports.append(seg[key])
            if not segments and leg.get("carrier") and leg["carrier"] not in airlines:
                airlines.append(leg["carrier"])
        return Itinerary(
            amount=float(amount),
            currency=str(price.get("currency") or ""),
            stops=stops,
            airlines=tuple(airlines),
            source_ref=it.get("ignav_id"),
            self_transfer=bool(it.get("requires_self_transfer", False)),
            airports=tuple(airports),
            depart_airport=_first_segment(it.get("outbound"), "departure_airport", first=True),
            return_airport=_first_segment(it.get("inbound"), "arrival_airport", first=False),
        )

    # ---------------------------------------------------------------- reserva
    def booking_link(self, source_ref: str) -> LinkOutcome:
        status, data, attempts, error = self._post("/fares/booking-links", {"ignav_id": source_ref})
        if status != 200 or data is None:
            return LinkOutcome(http_status=status, attempts=attempts, billable=status == 200,
                               error=error or f"HTTP {status}", fatal=status in FATAL_STATUSES)
        links = pick_booking_links(data.get("booking_options") or [])
        return LinkOutcome(links=links, http_status=status, attempts=attempts, billable=True,
                           error=None if links else "sin links de reserva")


def _first_segment(leg: Optional[dict], key: str, first: bool) -> Optional[str]:
    """Aeropuerto de salida del primer tramo (first=True) o de llegada del último (first=False)."""
    segments = (leg or {}).get("segments") or []
    if not segments:
        return None
    return segments[0 if first else -1].get(key)


def pick_booking_links(options: list[dict]) -> list[tuple[str, Optional[str]]]:
    """Prefiere una opción que cubra ida y vuelta (legs = outbound + inbound). Si no hay, devuelve un link por
    tramo, etiquetado "Ida" / "Vuelta". Una opción sin legs se usa solo si no hay nada mejor."""
    full = unknown = None
    per_leg: dict[str, str] = {}
    for option in options:
        url = next((link["url"] for link in option.get("links") or [] if link.get("url")), None)
        if not url:
            continue
        legs = {leg for leg in option.get("legs") or [] if isinstance(leg, str)}
        if {"outbound", "inbound"} <= legs:
            full = full or url
        elif legs:
            for leg in legs:
                per_leg.setdefault(leg, url)
        else:
            unknown = unknown or url
    if full:
        return [("", full)]
    if per_leg:
        return [("Ida", per_leg.get("outbound")), ("Vuelta", per_leg.get("inbound"))]
    return [("", unknown)] if unknown else []


def _error_message(resp: Any) -> str:
    try:
        err = resp.json().get("error") or {}
        parts = [err.get("code"), err.get("message")]
        msg = ": ".join(str(p) for p in parts if p)
        return msg or f"HTTP {resp.status_code}"
    except (ValueError, AttributeError):
        return f"HTTP {resp.status_code}"
