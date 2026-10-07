import sqlite3

from monitor.storage import PRICE_SCALE_FIX, Store


def _legacy_db(path):
    """Base como quedó en main antes del arreglo: sin tabla migrations y precios x5."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
            mode TEXT NOT NULL, status TEXT NOT NULL, planned_queries INTEGER NOT NULL DEFAULT 0,
            executed_queries INTEGER NOT NULL DEFAULT 0, saved_queries INTEGER NOT NULL DEFAULT 0,
            billable_requests INTEGER NOT NULL DEFAULT 0, alerts_sent INTEGER NOT NULL DEFAULT 0, notes TEXT);
        INSERT INTO runs (started_at, mode, status) VALUES ('2026-10-06T11:03:20+00:00', 'full', 'ok');
    """)
    conn.close()
    store = Store.__new__(Store)            # crea el resto del esquema sin migrar
    store.conn = sqlite3.connect(path)
    from monitor.storage import SCHEMA
    store.conn.executescript(SCHEMA.split("-- Migraciones")[0])
    store.conn.executescript("""
        INSERT INTO observations (run_id, ts_utc, source, origin, destination, depart_date, return_date,
            duration_days, stops, airlines, price_total, price_pp, currency)
        VALUES (1, '2026-10-06T11:05:00+00:00', 'ignav', 'EZE', 'GRU', '2027-01-20', '2027-01-30', 10, 0,
            'LX', 9665, 1933, 'USD');
        INSERT INTO pair_state (origin, destination, depart_date, return_date, last_price_pp, last_price_total)
        VALUES ('EZE', 'GRU', '2027-01-20', '2027-01-30', 1933, 9665),
               ('AEP', 'GRU', '2027-01-20', '2027-01-30', NULL, NULL);
        INSERT INTO alerts (run_id, ts_utc, origin, destination, depart_date, return_date, price_pp, price_total,
            rules, channels)
        VALUES (1, '2026-10-06T11:06:00+00:00', 'EZE', 'GRU', '2027-01-20', '2027-01-30', 1933, 9665, 'B', 'email');
    """)
    store.conn.commit()
    store.conn.close()


def test_legacy_prices_are_divided_by_five_once(tmp_path):
    path = tmp_path / "prices.db"
    _legacy_db(path)
    for _ in range(2):   # la segunda apertura no debe volver a dividir
        Store.open(path).close()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT price_pp, price_total FROM observations").fetchone() == (386.6, 1933)
    assert conn.execute("SELECT price_pp, price_total FROM alerts").fetchone() == (386.6, 1933)
    assert conn.execute("SELECT last_price_pp, last_price_total FROM pair_state ORDER BY origin").fetchall() == [
        (None, None), (386.6, 1933)]
    assert conn.execute("SELECT COUNT(*) FROM migrations WHERE name = ?", (PRICE_SCALE_FIX,)).fetchone()[0] == 1


def test_new_database_is_not_rescaled(tmp_path):
    path = tmp_path / "prices.db"
    Store.open(path).close()          # base nueva: la migración queda marcada sin tocar nada
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO runs (started_at, mode, status) VALUES ('x', 'full', 'ok')")
    conn.execute("""INSERT INTO observations (run_id, ts_utc, source, origin, destination, depart_date, return_date,
        duration_days, stops, airlines, price_total, price_pp, currency)
        VALUES (1, 'x', 'ignav', 'EZE', 'GIG', '2027-01-14', '2027-01-23', 9, 0, 'G3', 2000, 400, 'USD')""")
    conn.commit()
    conn.close()
    Store.open(path).close()
    assert sqlite3.connect(path).execute("SELECT price_pp FROM observations").fetchone()[0] == 400


def test_flybondi_observations_are_dropped_and_pairs_reset(tmp_path):
    path = tmp_path / "prices.db"
    store = Store.open(path)
    c = store.conn
    c.execute("INSERT INTO runs (started_at, mode, status) VALUES ('t0', 'full', 'ok'), ('t1', 'full', 'ok')")
    rows = [("AEP", "2027-01-14", "FO", 163, "t1"), ("EZE", "2027-01-14", "AR", 300, "t1"),
            ("AEP", "2027-01-15", "AR", 310, "t0"), ("AEP", "2027-01-15", "FO", 170, "t1")]
    for origin, dep, airline, price, ts in rows:
        c.execute("""INSERT INTO observations (run_id, ts_utc, source, origin, destination, depart_date, return_date,
            duration_days, stops, airlines, price_total, price_pp, currency) VALUES (?, ?, 'ignav', ?, 'GIG', ?,
            '2027-01-24', 10, 0, ?, ?, ?, 'USD')""", (1 if ts == "t0" else 2, ts, origin, dep, airline, price * 5,
                                                       price))
        c.execute("""INSERT OR REPLACE INTO pair_state (origin, destination, depart_date, return_date, last_price_pp,
            last_price_total, last_price_at, last_queried_seq) VALUES (?, 'GIG', ?, '2027-01-24', ?, ?, ?, 3)""",
                  (origin, dep, price, price * 5, ts))
    c.execute("DELETE FROM migrations WHERE name = '2026-10-08_exclude_flybondi'")
    c.commit()
    store.close()

    Store.open(path).close()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT origin, depart_date, airlines FROM observations ORDER BY id").fetchall() == [
        ("EZE", "2027-01-14", "AR"), ("AEP", "2027-01-15", "AR")]
    state = dict(((o, d), (p, s)) for o, d, p, s in conn.execute(
        "SELECT origin, depart_date, last_price_pp, last_queried_seq FROM pair_state"))
    assert state[("AEP", "2027-01-14")] == (None, None)       # el último precio era Flybondi
    assert state[("AEP", "2027-01-15")] == (None, None)
    assert state[("EZE", "2027-01-14")] == (300, 3)           # no se toca
