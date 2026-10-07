import sqlite3

from monitor.storage import RESET_MIGRATION, SCHEMA, Store


def _legacy_db(path):
    """Base como quedó en main antes del reinicio: sin columnas de aeropuertos, con precios y uso del mes."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.replace("    depart_airport TEXT,", "").replace("    return_airport TEXT,", ""))
    conn.executescript("""
        INSERT INTO runs (started_at, mode, status) VALUES ('2026-10-06T11:03:20+00:00', 'full', 'ok');
        INSERT INTO requests (run_id, ts_utc, source, kind, status, billable)
        VALUES (1, '2026-10-06T11:04:00+00:00', 'ignav', 'fares', 'ok', 1),
               (1, '2026-10-06T11:04:01+00:00', 'ignav', 'fares', 'ok', 1);
        INSERT INTO observations (run_id, ts_utc, source, origin, destination, depart_date, return_date,
            duration_days, stops, airlines, price_total, price_pp, currency)
        VALUES (1, '2026-10-06T11:05:00+00:00', 'ignav', 'AEP', 'GIG', '2027-01-14', '2027-01-23', 9, 0,
            'FO', 815, 163, 'USD');
        INSERT INTO pair_state (origin, destination, depart_date, return_date, last_price_pp)
        VALUES ('AEP', 'GIG', '2027-01-14', '2027-01-23', 163);
        INSERT INTO dest_state (destination, mode, run_seq) VALUES ('GIG', 'normal', 2);
        INSERT INTO alerts (run_id, ts_utc, origin, destination, depart_date, return_date, price_pp, price_total,
            rules, channels)
        VALUES (1, '2026-10-06T11:06:00+00:00', 'AEP', 'GIG', '2027-01-14', '2027-01-23', 163, 815, 'B', 'email');
        INSERT INTO migrations (name, applied_at) VALUES ('2026-10-07_price_is_total_x5', 'x');
    """)
    conn.commit()
    conn.close()


def test_reset_clears_prices_keeps_usage_and_runs_once(tmp_path):
    path = tmp_path / "prices.db"
    _legacy_db(path)
    Store.open(path).close()
    conn = sqlite3.connect(path)
    for table in ("observations", "pair_state", "dest_state", "alerts"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert conn.execute("SELECT COUNT(*) FROM requests WHERE billable = 1").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    cols = {r[1] for r in conn.execute("PRAGMA table_info(observations)")}
    assert {"depart_airport", "return_airport"} <= cols

    # Datos nuevos después del reinicio: una segunda apertura no los borra.
    conn.execute("""INSERT INTO observations (run_id, ts_utc, source, origin, destination, depart_date,
        return_date, duration_days, stops, airlines, price_total, price_pp, currency, depart_airport, return_airport)
        VALUES (1, 'x', 'ignav', 'BUE', 'GIG', '2027-01-14', '2027-01-23', 9, 0, 'AR', 2000, 400, 'USD',
        'EZE', 'AEP')""")
    conn.commit()
    conn.close()
    Store.open(path).close()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT depart_airport, return_airport FROM observations").fetchall() == [("EZE", "AEP")]
    assert conn.execute("SELECT COUNT(*) FROM migrations WHERE name = ?", (RESET_MIGRATION,)).fetchone()[0] == 1


def test_new_database_is_created_already_reset(tmp_path):
    path = tmp_path / "prices.db"
    Store.open(path).close()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM migrations WHERE name = ?", (RESET_MIGRATION,)).fetchone()[0] == 1
