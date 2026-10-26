import datetime as dt

import pytest

from monitor.config import ConfigError, Secrets, load_config
from monitor.dates import date_pairs

from .conftest import ROOT, make_config


def test_repo_config_loads():
    cfg = load_config(ROOT / "config.yaml")
    assert cfg.search.origins == ["BUE"]
    assert cfg.search.airports_exclude == ["SDU"] and cfg.search.airlines_exclude == ["FO"]
    assert cfg.optimization.reduced_destinations == []   # GRU diario
    assert cfg.detection.cross.max_rank == 3
    assert cfg.search.destinations == ["GIG", "GRU"]          # CFB dado de baja
    assert (cfg.optimization.top_k, cfg.optimization.rotating_k, cfg.optimization.night_top_k) == (12, 10, 12)
    assert cfg.search.open_jaw is False
    assert cfg.budget.max_requests_per_month == 7000
    assert cfg.search.departure_from == dt.date(2027, 1, 10)
    gig, gru = cfg.optimization.for_destination("GIG"), cfg.optimization.for_destination("GRU")
    assert (gig.top_k, gig.rotating_k, gig.night_top_k) == (60, 60, 60)
    assert (gru.top_k, gru.rotating_k, gru.night_top_k) == (12, 10, 0)
    assert (cfg.optimization.no_service_after_empty_runs, cfg.optimization.recheck_days) == (4, 2)
    assert cfg.optimization.retry_empty_wait_seconds == 20
    assert cfg.detection.absolute_threshold_by_destination == {"GIG": 350.0}
    assert cfg.detection.antispam_memory_days == 7
    assert cfg.budget.dry_run_max_queries == 4
    assert cfg.ignav.price_is_total is True
    assert cfg.detection.absolute_threshold_usd_pp is None
    assert cfg.schedule.stop_after == dt.date(2027, 1, 10)


def test_universe_has_39_date_pairs():
    pairs = date_pairs(dt.date(2027, 1, 14), dt.date(2027, 2, 5), [9, 10, 11])
    assert len(pairs) == 39
    assert pairs[0] == (dt.date(2027, 1, 14), dt.date(2027, 1, 23))
    assert pairs[-1] == (dt.date(2027, 1, 27), dt.date(2027, 2, 5))
    assert all((r - d).days in (9, 10, 11) for d, r in pairs)


def test_unknown_keys_are_rejected(raw_config):
    raw_config["optimization"]["topk"] = 3
    with pytest.raises(ConfigError, match="topk"):
        make_config(raw_config)


def test_adults_limit(raw_config):
    with pytest.raises(ConfigError):
        make_config(raw_config, search={"adults": 10})


def test_secrets_from_env():
    s = Secrets.from_env({"IGNAV_API_KEY": "k" * 10, "TELEGRAM_CHAT_ID": " 123 "})
    assert s.ignav_api_key == "k" * 10
    assert s.telegram_chat_id == "123"
    assert s.values() == ["k" * 10, "123"]


def test_excluded_airport_cannot_be_a_destination(raw_config):
    raw_config["search"]["destinations"] = ["GIG", "SDU"]
    with pytest.raises(ConfigError, match="airports_exclude"):
        make_config(raw_config)


def test_unknown_destination_override_is_rejected(raw_config):
    raw_config["optimization"]["destination_overrides"] = {"GIG": {"topk": 3}}
    with pytest.raises(ConfigError, match="topk"):
        make_config(raw_config)
