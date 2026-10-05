import datetime as dt

import pytest

from monitor.config import ConfigError, Secrets, load_config
from monitor.dates import date_pairs

from .conftest import ROOT, make_config


def test_repo_config_loads():
    cfg = load_config(ROOT / "config.yaml")
    assert cfg.search.origins == ["EZE", "AEP"]
    assert cfg.search.destinations == ["GIG", "GRU", "CFB"]
    assert cfg.search.open_jaw is False
    assert cfg.budget.max_requests_per_month == 2500
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
