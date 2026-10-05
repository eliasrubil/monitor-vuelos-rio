import datetime as dt

from monitor.config import DetectionConfig
from monitor.detection import compute_stats, evaluate, passes_antispam
from monitor.storage import WindowPoint

NOW = dt.datetime(2026, 11, 1, 11, tzinfo=dt.timezone.utc)


def window(prices, age_days=0):
    base = dt.date(2027, 1, 14)
    return [
        WindowPoint(base + dt.timedelta(days=i), base + dt.timedelta(days=i + 10), p,
                    NOW - dt.timedelta(days=age_days + i % 3), "EZE")
        for i, p in enumerate(prices)
    ]


def test_stats():
    s = compute_stats([1, 2, 3, 4, 100])
    assert s.n == 5 and s.median == 3 and s.mean == 22 and s.mad == 1


def test_rule_a_by_mean():
    ev = evaluate(80, [100] * 7, [], DetectionConfig(), NOW)
    assert ev.rules == ["A"]
    assert ev.robust_z is None          # MAD = 0: z no definido
    assert round(ev.pct_below_itinerary, 2) == 0.20


def test_rule_a_by_robust_z():
    history = [100, 102, 98, 101, 99, 100, 103, 97]
    ev = evaluate(90, history, [], DetectionConfig(), NOW)
    # 90 no está 15% debajo de la media (100), pero z = (90-100)/(1.4826*1.5) ≈ -4.5
    assert ev.rules == ["A"]
    assert ev.robust_z < -2.5


def test_rule_a_needs_min_observations():
    ev = evaluate(50, [100] * 6, [], DetectionConfig(), NOW)
    assert ev.rules == []
    assert ev.itinerary_stats.n == 6


def test_rule_b_bottom_decile_and_below_mean():
    prices = [100 + i for i in range(20)] + [70]
    ev = evaluate(70, [], window(prices, age_days=2), DetectionConfig(), NOW)
    assert ev.rules == ["B"]
    assert ev.window_rank == 1 and ev.window_cutoff == 3
    assert ev.window_age_days == (2, 4)


def test_rule_b_requires_drop_vs_mean():
    prices = [100 + i for i in range(20)]
    ev = evaluate(100, [], window(prices), DetectionConfig(), NOW)
    assert ev.window_rank == 1
    assert ev.rules == []


def test_rule_b_requires_bottom_percentile():
    # 5 precios muy bajos: el cuarto no está en el 10% (cutoff = ceil(0.1*25) = 3)
    prices = [50, 51, 52, 53, 54] + [200] * 20
    ev = evaluate(53, [], window(prices), DetectionConfig(), NOW)
    assert ev.window_rank == 4 and ev.window_cutoff == 3
    assert ev.rules == []


def test_rule_c_absolute_threshold():
    cfg = DetectionConfig(absolute_threshold_usd_pp=300)
    assert evaluate(299, [], [], cfg, NOW).rules == ["C"]
    assert evaluate(301, [], [], cfg, NOW).rules == []
    assert evaluate(1, [], [], DetectionConfig(), NOW).rules == []   # null = desactivada


def test_a_and_b_together_single_evaluation():
    prices = [100 + i for i in range(20)] + [70]
    ev = evaluate(70, [100] * 8, window(prices), DetectionConfig(), NOW)
    assert ev.rules == ["A", "B"]


def test_antispam():
    assert passes_antispam(100, None, 0.03)
    assert not passes_antispam(98, 100, 0.03)
    assert passes_antispam(97, 100, 0.03)
    assert not passes_antispam(120, 100, 0.03)
