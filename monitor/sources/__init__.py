"""Fuentes de precios intercambiables."""

from __future__ import annotations

from ..config import Config, Secrets
from .base import PriceSource


def make_source(cfg: Config, secrets: Secrets) -> PriceSource:
    if cfg.source == "ignav":
        from .ignav import IgnavSource

        return IgnavSource(secrets.ignav_api_key, cfg.ignav)
    raise ValueError(f"Fuente desconocida: {cfg.source!r}")
