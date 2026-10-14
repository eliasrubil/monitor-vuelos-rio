import datetime as dt
import sqlite3
from collections import defaultdict

import pytest

from monitor.alerts.channels import LogNotifier, Notifier
from monitor.planner import plan_run
from monitor.runner import Runner
from monitor.storage import Store

from .conftest import FakeChannel, FakeSource, constant_prices, make_config, repo_raw_config


def run_once(cfg, source, notifier, clock, full_scan=False, dry_run=False):
    """Igual que la CLI: en dry-run los precios van a memoria y el uso a la base real."""
    store = Store.open(cfg.database.path, dry_run=dry_run)
    usage = Store.open(cfg.database.path) if dry_run else None
    try:
        return Runner(cfg, store, source, notifier, dry_run=dry_run, usage=usage, clock=clock).run(
            full_scan=full_scan)
    finally:
        store.close()
        if usage is not None:
            usage.close()


def alerts_sent(channel):
    """Mensajes de alertas (sin el resumen diario, que va por Telegram)."""
    return [(s, t) for s, t in channel.sent if not s.startswith("Resumen vuelos")]


def summaries_sent(channel):
    return [(s, t) for s, t in channel.sent if s.startswith("Resumen vuelos")]


def db(cfg):
    conn = sqlite3.connect(cfg.database.path)
    conn.row_factory = sqlite3.Row
    return conn


def test_first_run_is_full_scan(cfg, notifier, channels, clock):
    src = FakeSource(constant_prices())
    assert run_once(cfg, src, notifier, clock) == 0
    assert len(src.calls) == 39 * 3        # BUE cubre EZE y AEP en una consulta
    conn = db(cfg)
    run = conn.execute("SELECT * FROM runs").fetchone()
    assert run["mode"] == "full" and run["status"] == "ok"
    assert run["executed_queries"] == 117 and run["saved_queries"] == 0
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 117
    assert conn.execute("SELECT COUNT(*) FROM requests WHERE billable = 1").fetchone()[0] == 117
    obs = conn.execute("SELECT * FROM observations LIMIT 1").fetchone()
    assert obs["price_total"] == obs["price_pp"] * 5
    assert obs["duration_days"] == 9 and obs["currency"] == "USD" and obs["airlines"] == "AR,G3"
    # resumen por email, nada por Telegram (no hay historial suficiente para alertar)
    # sin alertas: no hay email; el resumen va por Telegram
    assert channels["email"].sent == []
    (summary,) = summaries_sent(channels["telegram"])
    assert "Top 5" in summary[1]


def test_incremental_run_queries_all_destinations_daily(cfg, notifier, clock):
    run_once(cfg, FakeSource(constant_prices()), notifier, clock)
    clock.advance()
    src = FakeSource(constant_prices())
    run_once(cfg, src, notifier, clock)
    per_dest = defaultdict(set)
    for q in src.calls:
        per_dest[q.destination].add((q.depart, q.ret))
    # Sin modo reducido: GRU se consulta todos los días como GIG y CFB.
    assert set(per_dest) == {"GIG", "GRU", "CFB"}
    assert len(per_dest["GIG"]) == len(per_dest["GRU"]) == 8 + 5
    assert len(src.calls) == 3 * 13
    run = db(cfg).execute("SELECT * FROM runs ORDER BY id DESC").fetchone()
    assert run["mode"] == "incremental"
    assert run["saved_queries"] == 117 - 39


def test_top_k_are_the_cheapest_pairs(cfg, notifier, clock):
    run_once(cfg, FakeSource(constant_prices()), notifier, clock)
    store = Store.open(cfg.database.path)
    clock.advance()
    plan = plan_run(cfg, store, cfg.search.origins, clock.now)
    gig = next(d for d in plan.destinations if d.destination == "GIG")
    top = sorted({(q.depart, q.ret) for q in gig.queries if q.reason == "top"})
    window = sorted(store.window("GIG"), key=lambda p: p.price_pp)[:8]
    assert top == sorted((p.depart, p.ret) for p in window)
    store.close()


def test_staleness_guarantee(cfg, notifier, clock):
    """Ningún par de GIG/CFB queda más de max_staleness_runs corridas sin actualizar."""
    last_seen: dict = {}
    max_gap = 0
    for run in range(1, 25):
        src = FakeSource(constant_prices())
        run_once(cfg, src, notifier, clock)
        for q in src.calls:
            if q.destination != "GIG":
                continue
            key = (q.depart, q.ret)
            if key in last_seen:
                max_gap = max(max_gap, run - last_seen[key])
            last_seen[key] = run
        stale_now = [k for k, v in last_seen.items() if run - v >= cfg.optimization.max_staleness_runs]
        assert not stale_now, f"corrida {run}: pares sin actualizar {stale_now}"
        clock.advance()
    assert len(last_seen) == 39
    assert max_gap <= cfg.optimization.max_staleness_runs


def test_no_service_after_two_empty_runs_and_recheck(raw_config, tmp_path, notifier, clock):
    # Con varios orígenes por separado, no_service se lleva por origen.
    cfg = make_config(raw_config, database={"path": str(tmp_path / "p.db")},
                      search={"origins": ["EZE", "AEP"], "destinations": ["GIG"]},
                      optimization={"top_k": 39, "rotating_k": 0})
    dead = (dt.date(2027, 1, 14), dt.date(2027, 1, 23))
    base = constant_prices()

    def prices(q):
        return None if (q.origin, q.depart, q.ret) == ("AEP", *dead) else base(q)

    run_once(cfg, FakeSource(prices), notifier, clock)            # vacío 1
    clock.advance()
    src = FakeSource(prices)
    run_once(cfg, src, notifier, clock)                          # vacío 2 -> no_service
    assert ("AEP", *dead) in {(q.origin, q.depart, q.ret) for q in src.calls}
    row = db(cfg).execute("SELECT * FROM pair_state WHERE origin='AEP' AND depart_date='2027-01-14' "
                          "AND return_date='2027-01-23'").fetchone()
    assert row["no_service"] == 1 and row["empty_streak"] == 2

    for _ in range(13):
        clock.advance()
        src = FakeSource(prices)
        run_once(cfg, src, notifier, clock)
        assert ("AEP", *dead) not in {(q.origin, q.depart, q.ret) for q in src.calls}
        assert ("EZE", *dead) in {(q.origin, q.depart, q.ret) for q in src.calls}
    clock.advance()   # 14 días desde la última consulta
    store = Store.open(cfg.database.path)
    plan = plan_run(cfg, store, cfg.search.origins, clock.now)
    store.close()
    rechecks = [q for q in plan.queries if q.reason == "recheck"]
    assert [(q.origin, q.depart, q.ret) for q in rechecks] == [("AEP", *dead)]


def test_gru_reduced_cadence_and_promotion(raw_config, tmp_path, notifier, clock):
    # El modo reducido sigue disponible por config (hoy desactivado: reduced_destinations: []).
    cfg = make_config(raw_config, database={"path": str(tmp_path / "p.db")},
                      optimization={"reduced_destinations": ["GRU"]})
    run_once(cfg, FakeSource(constant_prices()), notifier, clock)   # GRU 600 vs GIG 400: reducido
    gru_runs = []
    for day in range(1, 15):
        clock.advance()
        src = FakeSource(constant_prices())
        run_once(cfg, src, notifier, clock)
        if any(q.destination == "GRU" for q in src.calls):
            gru_runs.append(day)
    assert gru_runs == [2, 4, 6, 8, 10, 12, 14]          # reduced_interval_days: 2

    def gru_mode_after(gru_price):
        clock.advance(7)
        prices = constant_prices({"GIG": 400.0, "CFB": 450.0, "GRU": gru_price})
        run_once(cfg, FakeSource(prices), notifier, clock, full_scan=True)
        clock.advance()
        run_once(cfg, FakeSource(prices), notifier, clock)
        return db(cfg).execute("SELECT mode FROM dest_state WHERE destination='GRU'").fetchone()[0]

    # Margen de 50 USD: GRU 40 más barato que GIG sigue reducido; 60 más barato pasa a normal.
    assert gru_mode_after(360.0) == "reduced"
    assert gru_mode_after(340.0) == "normal"


def test_budget_exceeded_skips_run_and_notifies(raw_config, tmp_path, notifier, channels, clock):
    cfg = make_config(raw_config, database={"path": str(tmp_path / "p.db")}, budget={"max_requests_per_month": 100})
    src = FakeSource(constant_prices())
    assert run_once(cfg, src, notifier, clock) == 0
    assert src.calls == []
    assert "presupuesto" in channels["telegram"].sent[0][0]
    assert "presupuesto" in channels["email"].sent[0][0]
    assert db(cfg).execute("SELECT status FROM runs").fetchone()[0] == "skipped_budget"


PRICE_TABLES = ("observations", "pair_state", "dest_state", "alerts")


def snapshot(cfg, tables=PRICE_TABLES):
    conn = db(cfg)
    try:
        return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}
    finally:
        conn.close()


def test_dry_run_is_limited_and_counts_usage_without_writing_prices(cfg, notifier, clock):
    run_once(cfg, FakeSource(constant_prices()), notifier, clock)
    before = snapshot(cfg)
    used_before = db(cfg).execute("SELECT COUNT(*) FROM requests WHERE billable = 1").fetchone()[0]
    clock.advance()

    src = FakeSource(constant_prices())
    assert run_once(cfg, src, LogNotifier(), clock, dry_run=True) == 0
    assert len(src.calls) == cfg.budget.dry_run_max_queries == 4
    assert snapshot(cfg) == before                      # precios y estado intactos
    conn = db(cfg)
    assert conn.execute("SELECT COUNT(*) FROM requests WHERE billable = 1").fetchone()[0] == used_before + 4
    dry = conn.execute("SELECT * FROM runs WHERE mode = 'dry_run'").fetchone()
    assert dry["status"] == "dry_run" and dry["executed_queries"] == 4 and dry["billable_requests"] == 4


def test_dry_run_requests_count_towards_monthly_budget(raw_config, tmp_path, notifier, clock):
    cfg = make_config(raw_config, database={"path": str(tmp_path / "p.db")}, budget={"max_requests_per_month": 120})
    for _ in range(2):
        run_once(cfg, FakeSource(constant_prices()), LogNotifier(), clock, dry_run=True)
    src = FakeSource(constant_prices())
    run_once(cfg, src, notifier, clock)       # 8 + 117 > 120: la línea base no entra
    assert src.calls == []


def test_dry_run_on_empty_db_only_records_usage(cfg, clock):
    run_once(cfg, FakeSource(constant_prices()), LogNotifier(), clock, dry_run=True)
    assert snapshot(cfg) == {t: [] for t in PRICE_TABLES}
    assert db(cfg).execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 4


@pytest.fixture
def big_cfg(raw_config, tmp_path):
    """Varios barridos completos superan el tope mensual real; para estos tests se sube."""
    return make_config(raw_config, database={"path": str(tmp_path / "p.db")},
                       budget={"max_requests_per_month": 100_000})


def _build_history(cfg, notifier, clock, runs=8):
    for _ in range(runs):
        run_once(cfg, FakeSource(constant_prices()), notifier, clock, full_scan=True)
        clock.advance()


def drop_for(pair, price):
    base = constant_prices()

    def fn(q):
        if q.destination == "GIG" and (q.depart, q.ret) == pair:
            return price
        return base(q)

    return fn


def test_alert_antispam_and_booking_link(big_cfg, notifier, channels, clock):
    cfg = big_cfg
    _build_history(cfg, notifier, clock)
    pair = (dt.date(2027, 1, 20), dt.date(2027, 1, 30))   # precio normal ~428
    channels["telegram"].sent.clear()
    channels["email"].sent.clear()

    src = FakeSource(drop_for(pair, 300), airports_fn=lambda q: ("EZE", "EZE"))
    run_once(cfg, src, notifier, clock, full_scan=True)
    assert len(alerts_sent(channels["telegram"])) == 1 and len(summaries_sent(channels["telegram"])) == 1
    subject, text = alerts_sent(channels["telegram"])[0]
    assert subject == ""
    assert text.splitlines() == ["I) EZE --> GIG 20/01 al 30/01 (10 días) - 300USD por persona - Aerolíneas Argentinas, GOL",
                                 "Reserva: https://example.com/book/BUE-GIG-2027-01-20-2027-01-30"]
    assert len(src.link_calls) == 1               # link solo para el alertado
    # email: una alerta (formato completo) + resumen
    # un único email por día: solo las alertas
    assert len(channels["email"].sent) == 1
    mail_subject, mail = channels["email"].sent[0]
    assert mail_subject.endswith("by BotRabo - Morning Sunshine! - 1 hallazgo")
    assert "I) Precio bajo EZE→GIG" in mail and "300 por persona" in mail
    assert "Vs. itinerario:" in mail and "USD 1.500 total" in mail and "10 días" in mail

    clock.advance()
    channels["telegram"].sent.clear()
    run_once(cfg, FakeSource(drop_for(pair, 295)), notifier, clock, full_scan=True)   # baja < 3%
    assert alerts_sent(channels["telegram"]) == []

    clock.advance()
    run_once(cfg, FakeSource(drop_for(pair, 290)), notifier, clock, full_scan=True)   # 290 <= 300*0.97
    assert len(alerts_sent(channels["telegram"])) == 1

    alerts = db(cfg).execute("SELECT * FROM alerts ORDER BY id").fetchall()
    assert [a["price_pp"] for a in alerts] == [300, 290]
    assert alerts[0]["rules"] == "A,B" and alerts[0]["channels"] == "telegram,email"


def test_failed_channel_does_not_block_the_other(big_cfg, clock):
    cfg = big_cfg
    tg, mail = FakeChannel("telegram", fail=True), FakeChannel("email")
    notifier = Notifier([tg, mail])
    _build_history(cfg, notifier, clock)
    pair = (dt.date(2027, 1, 20), dt.date(2027, 1, 30))
    run_once(cfg, FakeSource(drop_for(pair, 300)), notifier, clock, full_scan=True)
    alert = db(cfg).execute("SELECT channels FROM alerts").fetchone()
    assert alert["channels"] == "email"


def test_errors_do_not_stop_the_run(cfg, notifier, clock):
    base = constant_prices()
    src = FakeSource(lambda q: "error" if q.destination == "CFB" else base(q))
    assert run_once(cfg, src, notifier, clock) == 0
    assert len(src.calls) == 117
    conn = db(cfg)
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "partial"
    assert conn.execute("SELECT COUNT(*) FROM requests WHERE status='error'").fetchone()[0] == 39
    # los pares con error no se marcan como consultados: van primero en la próxima rotación
    assert conn.execute("SELECT COUNT(*) FROM pair_state WHERE destination='CFB'").fetchone()[0] == 0


def test_fatal_error_stops_and_alerts(cfg, notifier, channels, clock):
    src = FakeSource(lambda q: "fatal")
    assert run_once(cfg, src, notifier, clock) == 1
    assert len(src.calls) == 1
    assert any("error de la fuente" in s for s, _ in channels["telegram"].sent)


def test_stop_after(cfg, notifier, clock):
    clock.now = dt.datetime(2027, 1, 11, 11, tzinfo=dt.timezone.utc)
    src = FakeSource(constant_prices())
    assert run_once(cfg, src, notifier, clock) == 0
    assert src.calls == []


def test_same_price_dates_alert_once_and_are_all_recorded(big_cfg, notifier, channels, clock):
    cfg = big_cfg
    _build_history(cfg, notifier, clock)
    channels["telegram"].sent.clear()
    channels["email"].sent.clear()
    base = constant_prices()
    tied = {(dt.date(2027, 1, d), dt.date(2027, 1, d + 10)) for d in (16, 18, 20)}

    def prices(q):
        if q.destination == "GIG" and (q.depart, q.ret) in tied:
            return 250
        return base(q)

    src = FakeSource(prices, airports_fn=lambda q: ("EZE", "AEP"))   # ida desde EZE, vuelta a AEP
    run_once(cfg, src, notifier, clock, full_scan=True)
    (_, text), = alerts_sent(channels["telegram"])
    assert text.splitlines()[0] == ("I) EZE/AEP --> GIG 16/01 al 26/01 (10 días) - 250USD por persona "
                                    "(+2 fechas más con el mismo precio) - Aerolíneas Argentinas, GOL")
    assert "18/01" not in text and "20/01" not in text
    assert len(src.link_calls) == 1                       # link solo para la primera
    assert "Hay 2 fechas más con el mismo precio." in channels["email"].sent[0][1]
    recorded = db(cfg).execute("SELECT depart_date FROM alerts ORDER BY depart_date").fetchall()
    assert [r[0] for r in recorded] == ["2027-01-16", "2027-01-18", "2027-01-20"]   # anti-spam para todas

    clock.advance()
    channels["telegram"].sent.clear()
    run_once(cfg, FakeSource(prices), notifier, clock, full_scan=True)
    assert alerts_sent(channels["telegram"]) == []        # al día siguiente no se repite ninguna


def test_excluded_airline_never_alerts(big_cfg, notifier, channels, clock):
    cfg = big_cfg
    _build_history(cfg, notifier, clock)
    channels["telegram"].sent.clear()
    pair = (dt.date(2027, 1, 20), dt.date(2027, 1, 30))
    src = FakeSource(drop_for(pair, 100),
                     airlines_fn=lambda q: ("FO",) if (q.depart, q.ret) == pair else ("AR",))
    run_once(cfg, src, notifier, clock, full_scan=True)
    assert all(q.airlines_exclude == ("FO",) for q in src.calls)
    assert alerts_sent(channels["telegram"]) == []
    assert db(cfg).execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


def test_telegram_list_is_split_without_cutting_alerts():
    from monitor.alerts.format import Candidate, telegram_alert_chunks
    cands = [Candidate("AEP", "GRU", dt.date(2027, 1, 18), dt.date(2027, 1, 29), 1, "AR", 390.0, 1950.0, "USD", i,
                       booking_links=[("", "https://example.com/" + "x" * 300)]) for i in range(30)]
    chunks = telegram_alert_chunks(make_config(repo_raw_config()), cands)
    assert len(chunks) > 1
    assert all(len(text) <= 4000 for text, _ in chunks)
    assert sorted(i for _, idx in chunks for i in idx) == list(range(30))
    for text, idx in chunks:
        assert text.count(" --> ") == len(idx) == text.count("Reserva: ")


def test_alerts_are_sorted_by_price_per_person(big_cfg, notifier, channels, clock):
    """El email y Telegram listan las alertas de menor a mayor precio por persona."""
    cfg = big_cfg
    _build_history(cfg, notifier, clock)
    channels["telegram"].sent.clear()
    channels["email"].sent.clear()
    base = constant_prices()
    drops = {("CFB", dt.date(2027, 1, 14), dt.date(2027, 1, 23)): 250,      # destino "antes" alfabéticamente
             ("GIG", dt.date(2027, 1, 25), dt.date(2027, 2, 5)): 200,       # el más barato, fecha más tardía
             ("GRU", dt.date(2027, 1, 16), dt.date(2027, 1, 26)): 300}
    src = FakeSource(lambda q: drops.get((q.destination, q.depart, q.ret), base(q)))
    run_once(cfg, src, notifier, clock, full_scan=True)
    (_, tg), = alerts_sent(channels["telegram"])
    assert [l.split(" ")[3] for l in tg.splitlines() if " --> " in l] == ["GIG", "CFB", "GRU"]
    assert [l.split(" ")[0] for l in tg.splitlines() if " --> " in l] == ["I)", "II)", "III)"]
    (subject, mail), = channels["email"].sent
    assert subject.endswith("- 3 hallazgos")
    assert mail.index("I) Precio bajo BUE→GIG") < mail.index("II) Precio bajo BUE→CFB") < mail.index(
        "III) Precio bajo BUE→GRU")
    assert "buscá las mismas fechas directo en la web de la aerolínea" in mail


def test_slot_for_uses_argentina_time():
    from monitor.runner import slot_for
    utc = dt.timezone.utc
    assert slot_for(dt.datetime(2026, 10, 14, 11, 5, tzinfo=utc)) == "morning"    # 08:05
    assert slot_for(dt.datetime(2026, 10, 14, 23, 10, tzinfo=utc)) == "night"     # 20:10
    assert slot_for(dt.datetime(2026, 10, 15, 2, 30, tzinfo=utc)) == "night"      # 23:30 del 14
    assert slot_for(dt.datetime(2026, 10, 14, 16, 59, tzinfo=utc)) == "morning"   # 13:59


def _run_slot(cfg, source, notifier, clock, slot):
    store = Store.open(cfg.database.path)
    try:
        return Runner(cfg, store, source, notifier, clock=clock).run(slot=slot)
    finally:
        store.close()


def _pairs(calls, dest="GIG"):
    return {(q.depart, q.ret) for q in calls if q.destination == dest}


def test_night_run_follows_morning_findings_and_sweeps_unseen_dates(big_cfg, notifier, channels, clock):
    cfg = big_cfg
    _build_history(cfg, notifier, clock)                    # 8 barridos completos, 08:00 hora argentina
    channels["telegram"].sent.clear()
    channels["email"].sent.clear()
    cheap = (dt.date(2027, 1, 14), dt.date(2027, 1, 23))    # el par más barato de GIG: siempre en el top
    morning_src = FakeSource(drop_for(cheap, 300))
    _run_slot(cfg, morning_src, notifier, clock, "morning")
    (m_subject, m_mail), = channels["email"].sent
    assert m_subject.endswith("Morning Sunshine! - 1 hallazgo") and "I) Precio bajo BUE→GIG 14/01–23/01" in m_mail

    morning_gig = _pairs(morning_src.calls)
    new_pair = next(p for p in sorted(_all_gig_pairs(cfg), reverse=True) if p not in morning_gig)
    base = constant_prices()

    def night_prices(q):
        if q.destination == "GIG" and (q.depart, q.ret) == cheap:
            return 300.0                                    # el hallazgo de la mañana mantiene su precio
        if q.destination == "GIG" and (q.depart, q.ret) == new_pair:
            return 280.0                                    # fecha que la mañana no miró
        return base(q)

    clock.now += dt.timedelta(hours=12)                     # 20:00 hora argentina
    night_src = FakeSource(night_prices)
    _run_slot(cfg, night_src, notifier, clock, "night")

    night_gig = _pairs(night_src.calls)
    assert night_gig & morning_gig == {cheap}               # de lo que miró la mañana, solo el seguimiento
    assert night_gig | morning_gig == _all_gig_pairs(cfg)   # entre las dos corridas, todas las fechas
    assert len(night_src.calls) == 1 + 3 * (39 - 13)

    _, (n_subject, n_mail) = channels["email"].sent
    assert n_subject.endswith("Night Owl! - 1 hallazgo nuevo")
    assert n_mail.startswith("Hallazgos de la mañana\nEl hallazgo I) de la mañana mantiene su precio\n")
    assert f"I) Precio bajo BUE→GIG {new_pair[0]:%d/%m}–{new_pair[1]:%d/%m}" in n_mail
    assert "14/01–23/01" not in n_mail.split("Hallazgos nuevos")[1]   # no se repite como nuevo

    summaries = [s for s, _ in summaries_sent(channels["telegram"])]
    assert len(summaries) == 2 and "Morning Sunshine!" in summaries[0] and "Night Owl!" in summaries[1]
    (_, tg_night), = alerts_sent(channels["telegram"])[1:]
    assert tg_night.splitlines()[:2] == ["Hallazgos de la mañana:", "El hallazgo I) de la mañana mantiene su precio"]

    conn = db(cfg)
    runs = conn.execute("SELECT mode, slot FROM runs ORDER BY id DESC LIMIT 2").fetchall()
    assert [tuple(r) for r in runs] == [("night", "night"), ("incremental", "morning")]
    labels = conn.execute("SELECT run_id, label, depart_date FROM alerts ORDER BY id").fetchall()
    assert [r["label"] for r in labels] == [1, 1] and labels[0]["run_id"] != labels[1]["run_id"]


def _all_gig_pairs(cfg):
    from monitor.dates import date_pairs
    return set(date_pairs(cfg.search.departure_from, cfg.search.return_until, cfg.search.durations))


def test_night_followup_reports_price_changes(big_cfg, notifier, channels, clock):
    cfg = big_cfg
    _build_history(cfg, notifier, clock)
    channels["email"].sent.clear()
    cheap = (dt.date(2027, 1, 14), dt.date(2027, 1, 23))
    _run_slot(cfg, FakeSource(drop_for(cheap, 300)), notifier, clock, "morning")
    clock.now += dt.timedelta(hours=12)
    _run_slot(cfg, FakeSource(drop_for(cheap, 315)), notifier, clock, "night")
    _, (subject, mail) = channels["email"].sent
    assert subject.endswith("Night Owl! - sin hallazgos nuevos")
    assert "El hallazgo I) de la mañana subió a USD 315 por persona" in mail and "Sin hallazgos nuevos." in mail

    clock.now += dt.timedelta(hours=12)
    channels["email"].sent.clear()
    _run_slot(cfg, FakeSource(drop_for(cheap, 300)), notifier, clock, "morning")   # 300: anti-spam, sin alerta
    assert channels["email"].sent == []
    clock.now += dt.timedelta(hours=12)
    _run_slot(cfg, FakeSource(constant_prices()), notifier, clock, "night")         # sin hallazgos ni seguimiento
    assert channels["email"].sent == []


def test_night_followup_gone(big_cfg, notifier, channels, clock):
    cfg = big_cfg
    _build_history(cfg, notifier, clock)
    channels["email"].sent.clear()
    cheap = (dt.date(2027, 1, 14), dt.date(2027, 1, 23))
    _run_slot(cfg, FakeSource(drop_for(cheap, 300)), notifier, clock, "morning")
    clock.now += dt.timedelta(hours=12)
    _run_slot(cfg, FakeSource(drop_for(cheap, None)), notifier, clock, "night")
    assert "El hallazgo I) de la mañana ya no aparece" in channels["email"].sent[-1][1]


def test_night_without_morning_run_sweeps_dates_not_seen_today(big_cfg, notifier, clock):
    cfg = big_cfg
    _build_history(cfg, notifier, clock, runs=2)
    clock.now += dt.timedelta(hours=12)
    src = FakeSource(constant_prices())
    _run_slot(cfg, src, notifier, clock, "night")
    assert len(src.calls) == 117
    clock.now += dt.timedelta(hours=1)
    src = FakeSource(constant_prices())
    _run_slot(cfg, src, notifier, clock, "night")            # todo ya se consultó hoy
    assert src.calls == []


def test_manual_night_run_replaces_the_scheduled_one(cfg, notifier, channels, clock):
    run_once(cfg, FakeSource(constant_prices()), notifier, clock)              # línea base (barrido completo)
    clock.advance()
    run_once(cfg, FakeSource(constant_prices()), notifier, clock)              # 08:00, corrida normal
    clock.now += dt.timedelta(hours=5, minutes=20)                             # 13:20: noche adelantada a mano
    store = Store.open(cfg.database.path)
    src = FakeSource(constant_prices())
    Runner(cfg, store, src, notifier, clock=clock).run(slot="night")
    assert src.calls
    clock.now += dt.timedelta(hours=6, minutes=40)                             # 20:00: la programada no se repite
    src = FakeSource(constant_prices())
    sent_before = len(channels["telegram"].sent)
    assert Runner(cfg, store, src, notifier, clock=clock).run(slot="night", once_per_day=True) == 0
    assert src.calls == [] and len(channels["telegram"].sent) == sent_before
    clock.now += dt.timedelta(hours=12)                                        # 08:00 del día siguiente: normal
    src = FakeSource(constant_prices())
    Runner(cfg, store, src, notifier, clock=clock).run(slot="morning", once_per_day=True)
    assert src.calls
    clock.now += dt.timedelta(hours=12)                                        # 20:00 del día siguiente: normal
    src = FakeSource(constant_prices())
    Runner(cfg, store, src, notifier, clock=clock).run(slot="night", once_per_day=True)
    assert src.calls
    store.close()
