"""Carga y validación de config.yaml y de los secrets (variables de entorno)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

SECRET_NAMES = (
    "IGNAV_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "GMAIL_ADDRESS",
    "GMAIL_APP_PASSWORD",
    "ALERT_EMAIL_TO",
)


class ConfigError(ValueError):
    pass


# Parámetros de optimization que un destino puede tener propios (destination_overrides).
OVERRIDABLE = {"top_k", "rotating_k", "night_top_k", "max_staleness_runs"}


@dataclass
class SearchConfig:
    origins: list[str]
    destinations: list[str]
    departure_from: dt.date
    return_until: dt.date
    durations: list[int]
    open_jaw: bool = False
    adults: int = 5
    max_stops: int = 1
    cabin_class: str = "economy"
    market: str = "US"
    currency: str = "USD"
    airlines_exclude: list[str] = field(default_factory=list)
    airports_exclude: list[str] = field(default_factory=list)


@dataclass
class IgnavConfig:
    base_url: str = "https://ignav.com/api"
    timeout_seconds: float = 60
    max_retries: int = 3
    backoff_seconds: float = 2
    price_is_total: bool = True
    allow_self_transfer: bool = True


@dataclass
class BudgetConfig:
    max_requests_per_month: int = 1500
    dry_run_max_queries: int = 4


@dataclass
class OptimizationConfig:
    top_k: int = 8
    rotating_k: int = 5
    night_top_k: int = 0             # corrida de la noche: también vuelve a consultar los N pares más baratos
    max_staleness_runs: int = 6
    no_service_after_empty_runs: int = 2
    recheck_days: int = 14
    reduced_destinations: list[str] = field(default_factory=lambda: ["GRU"])
    reduced_interval_days: int = 7
    reduced_reference_destination: str = "GIG"
    gru_margin_usd: float = 80
    # Si la fuente devuelve una respuesta vacía (cero itinerarios), se reintenta una vez tras N segundos (0 = no).
    retry_empty_wait_seconds: float = 0
    # Valores propios de un destino, p. ej. {GIG: {top_k: 60, night_top_k: 60}}.
    destination_overrides: dict = field(default_factory=dict)

    def for_destination(self, destination: str) -> "OptimizationConfig":
        """La configuración efectiva de un destino (con sus valores propios, si tiene)."""
        overrides = self.destination_overrides.get(destination) or {}
        return dataclasses.replace(self, **overrides) if overrides else self


@dataclass
class TemporalRuleConfig:
    drop_pct: float = 0.15
    robust_z: float = -2.5
    min_observations: int = 7
    mad_scale: float = 1.4826


@dataclass
class CrossRuleConfig:
    max_rank: int = 3                # solo las N fechas más baratas del destino pueden alertar
    drop_pct: float = 0.15
    min_pairs: int = 10


@dataclass
class DetectionConfig:
    temporal: TemporalRuleConfig = field(default_factory=TemporalRuleConfig)
    cross: CrossRuleConfig = field(default_factory=CrossRuleConfig)
    absolute_threshold_usd_pp: Optional[float] = None
    # Regla C por destino, p. ej. {GIG: 350}: alerta si el precio por persona es menor a ese valor.
    absolute_threshold_by_destination: dict = field(default_factory=dict)
    antispam_min_drop_pct: float = 0.03
    # El anti-spam solo recuerda alertas de los últimos N días (None = todas).
    antispam_memory_days: Optional[int] = None


@dataclass
class AlertsConfig:
    telegram: bool = True
    email: bool = True
    summary_telegram: bool = True      # resumen de la corrida por Telegram (el email lleva solo alertas)
    summary_top_n: int = 5
    booking_links: bool = True


@dataclass
class ScheduleConfig:
    stop_after: Optional[dt.date] = None


@dataclass
class DatabaseConfig:
    path: str = "data/prices.db"


@dataclass
class Config:
    search: SearchConfig
    source: str = "ignav"
    ignav: IgnavConfig = field(default_factory=IgnavConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    optimization: OptimizationConfig = field(default_factory=OptimizationConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    airport_names: dict[str, str] = field(default_factory=dict)
    airline_names: dict[str, str] = field(default_factory=dict)

    def airport(self, code: str) -> str:
        name = self.airport_names.get(code)
        return f"{name} ({code})" if name else code

    def airline(self, code: str) -> str:
        """Nombre de la aerolínea a partir de su código IATA; si no está cargado, el código."""
        return self.airline_names.get(code, code)


def _build(cls: type, data: Any, path: str) -> Any:
    """Construye un dataclass a partir de un dict, rechazando claves desconocidas."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path or 'config'}: se esperaba un mapa")
    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(data) - set(fields)
    if unknown:
        raise ConfigError(f"{path or 'config'}: claves desconocidas {sorted(unknown)}")
    kwargs = {}
    for name, value in data.items():
        ftype = fields[name].type
        nested = _NESTED.get(ftype)
        kwargs[name] = _build(nested, value, f"{path}.{name}".strip(".")) if nested else value
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"{path or 'config'}: {exc}") from exc


_NESTED = {
    "SearchConfig": SearchConfig,
    "IgnavConfig": IgnavConfig,
    "BudgetConfig": BudgetConfig,
    "OptimizationConfig": OptimizationConfig,
    "TemporalRuleConfig": TemporalRuleConfig,
    "CrossRuleConfig": CrossRuleConfig,
    "DetectionConfig": DetectionConfig,
    "AlertsConfig": AlertsConfig,
    "ScheduleConfig": ScheduleConfig,
    "DatabaseConfig": DatabaseConfig,
}


def _as_date(value: Any, name: str) -> dt.date:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise ConfigError(f"{name}: fecha inválida {value!r} (usar YYYY-MM-DD)") from exc


def _validate(cfg: Config) -> Config:
    s = cfg.search
    s.departure_from = _as_date(s.departure_from, "search.departure_from")
    s.return_until = _as_date(s.return_until, "search.return_until")
    if cfg.schedule.stop_after is not None:
        cfg.schedule.stop_after = _as_date(cfg.schedule.stop_after, "schedule.stop_after")
    s.airlines_exclude = [c.upper() for c in s.airlines_exclude]
    if any(len(c) != 2 for c in s.airlines_exclude):
        raise ConfigError("search.airlines_exclude: usar códigos IATA de aerolínea de 2 caracteres")
    s.airports_exclude = [c.upper() for c in s.airports_exclude]
    s.origins = [c.upper() for c in s.origins]
    s.destinations = [c.upper() for c in s.destinations]
    for code in [*s.origins, *s.destinations]:
        if len(code) != 3 or not code.isalpha():
            raise ConfigError(f"Código IATA inválido: {code!r}")
    if set(s.airports_exclude) & set(s.origins + s.destinations):
        raise ConfigError("search.airports_exclude no puede incluir orígenes ni destinos de la búsqueda")
    if not s.origins or not s.destinations:
        raise ConfigError("search.origins y search.destinations no pueden estar vacíos")
    if s.return_until < s.departure_from:
        raise ConfigError("search.return_until es anterior a search.departure_from")
    if not s.durations or any(int(d) < 0 for d in s.durations):
        raise ConfigError("search.durations debe tener valores >= 0")
    s.durations = sorted({int(d) for d in s.durations})
    if not 1 <= s.adults <= 9:
        raise ConfigError("search.adults debe estar entre 1 y 9 (límite de la API)")
    if s.max_stops not in (0, 1, 2):
        raise ConfigError("search.max_stops debe ser 0, 1 o 2")
    o = cfg.optimization
    o.reduced_destinations = [c.upper() for c in o.reduced_destinations]
    o.reduced_reference_destination = o.reduced_reference_destination.upper()
    if o.top_k < 0 or o.rotating_k < 0 or o.max_staleness_runs < 1:
        raise ConfigError("optimization: top_k/rotating_k >= 0 y max_staleness_runs >= 1")
    o.destination_overrides = {str(k).upper(): v or {} for k, v in (o.destination_overrides or {}).items()}
    for dest, values in o.destination_overrides.items():
        unknown = set(values) - OVERRIDABLE
        if unknown:
            raise ConfigError(f"optimization.destination_overrides.{dest}: claves no permitidas {sorted(unknown)} "
                              f"(se puede usar {sorted(OVERRIDABLE)})")
        if any(int(v) < 0 for v in values.values()):
            raise ConfigError(f"optimization.destination_overrides.{dest}: los valores deben ser >= 0")
    d = cfg.detection
    d.absolute_threshold_by_destination = {
        str(k).upper(): float(v) for k, v in (d.absolute_threshold_by_destination or {}).items()}
    if cfg.detection.cross.max_rank < 1:
        raise ConfigError("detection.cross.max_rank debe ser >= 1")
    if cfg.budget.max_requests_per_month < 0 or cfg.budget.dry_run_max_queries < 0:
        raise ConfigError("budget.max_requests_per_month y budget.dry_run_max_queries deben ser >= 0")
    return cfg


def load_config(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if "search" not in raw:
        raise ConfigError("config: falta la sección 'search'")
    return _validate(_build(Config, raw, ""))


def config_from_dict(raw: dict) -> Config:
    """Útil para tests."""
    return _validate(_build(Config, raw, ""))


@dataclass(frozen=True)
class Secrets:
    ignav_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    gmail_address: str = ""
    gmail_app_password: str = ""
    alert_email_to: str = ""

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "Secrets":
        env = os.environ if env is None else env
        return cls(*(env.get(name, "").strip() for name in SECRET_NAMES))

    def values(self) -> list[str]:
        """Valores no vacíos, para redactarlos de los logs."""
        return [v for v in dataclasses.astuple(self) if v]
