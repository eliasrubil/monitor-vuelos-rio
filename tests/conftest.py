"""Fakes compartidos. Ningún test llama a la API real ni envía mensajes."""

from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
from typing import Callable, Optional, Union

import pytest
import yaml

from monitor.alerts.channels import Notifier
from monitor.config import config_from_dict
from monitor.sources.base import (
    STATUS_ERROR,
    STATUS_NO_RESULTS,
    STATUS_OK,
    LinkOutcome,
    PriceSource,
    Quote,
    SearchOutcome,
    SearchQuery,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Cualquier intento de red real hace fallar el test."""
    import requests

    def boom(*args, **kwargs):
        raise AssertionError("Los tests no deben hacer requests reales")

    monkeypatch.setattr(requests.Session, "request", boom)
    monkeypatch.setattr(requests, "post", boom)


@pytest.fixture
def raw_config() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def make_config(raw: dict, **sections):
    data = copy.deepcopy(raw)
    for section, values in sections.items():
        if isinstance(values, dict) and isinstance(data.get(section), dict):
            data[section].update(values)
        else:
            data[section] = values
    return config_from_dict(data)


@pytest.fixture
def cfg(raw_config, tmp_path):
    return make_config(raw_config, database={"path": str(tmp_path / "prices.db")})


PriceFn = Callable[[SearchQuery], Union[float, None, str]]


class FakeSource(PriceSource):
    """price_fn devuelve un precio por persona, None (sin resultados), 'error' o 'fatal'."""

    name = "fake"

    def __init__(self, price_fn: PriceFn, adults: int = 5, airlines_fn=None, airports_fn=None):
        self.price_fn = price_fn
        self.adults = adults
        self.airlines_fn = airlines_fn or (lambda q: ("AR", "G3"))
        # (aeropuerto de salida de la ida, aeropuerto de llegada de la vuelta)
        self.airports_fn = airports_fn or (lambda q: (None, None))
        self.calls: list[SearchQuery] = []
        self.link_calls: list[str] = []

    def search_round_trip(self, query: SearchQuery) -> SearchOutcome:
        self.calls.append(query)
        value = self.price_fn(query)
        if value == "error":
            return SearchOutcome(STATUS_ERROR, http_status=424, attempts=4, error="no completado")
        if value == "fatal":
            return SearchOutcome(STATUS_ERROR, http_status=401, attempts=1, error="invalid key", fatal=True)
        if value is None:
            return SearchOutcome(STATUS_NO_RESULTS, http_status=200, attempts=1, billable=True)
        ref = f"{query.origin}-{query.destination}-{query.depart}-{query.ret}"
        quote = Quote(price_total=value * self.adults, price_pp=value, currency="USD", stops=1,
                      airlines=tuple(self.airlines_fn(query)), source_ref=ref,
                      depart_airport=self.airports_fn(query)[0], return_airport=self.airports_fn(query)[1])
        return SearchOutcome(STATUS_OK, quote=quote, http_status=200, attempts=1, billable=True)

    def booking_link(self, source_ref: str) -> LinkOutcome:
        self.link_calls.append(source_ref)
        return LinkOutcome(links=[("", f"https://example.com/book/{source_ref}")], http_status=200, attempts=1,
                           billable=True)


class FakeChannel:
    def __init__(self, name: str, fail: bool = False):
        self.name = name
        self.fail = fail
        self.sent: list[tuple[str, str]] = []

    def send(self, subject: str, text: str) -> None:
        if self.fail:
            raise RuntimeError(f"{self.name} caído")
        self.sent.append((subject, text))


@pytest.fixture
def channels():
    return {"telegram": FakeChannel("telegram"), "email": FakeChannel("email")}


@pytest.fixture
def notifier(channels):
    return Notifier([channels["telegram"], channels["email"]])


class Clock:
    def __init__(self, start: dt.datetime):
        self.now = start

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, days: int = 1) -> None:
        self.now += dt.timedelta(days=days)


@pytest.fixture
def clock():
    return Clock(dt.datetime(2026, 10, 6, 11, 0, tzinfo=dt.timezone.utc))


def constant_prices(base: Optional[dict] = None) -> PriceFn:
    """Precio determinístico por destino y fecha: más barato cuanto antes la ida."""
    base = base or {"GIG": 400.0, "CFB": 450.0, "GRU": 600.0}

    def fn(q: SearchQuery):
        offset = (q.depart - dt.date(2027, 1, 14)).days * 3 + (q.ret - q.depart).days
        extra = 5 if q.origin == "AEP" else 0
        return base[q.destination] + offset + extra

    return fn
