"""Persistencia en SQLite."""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .sources.base import Quote

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL,                 -- full | incremental
    status TEXT NOT NULL,               -- running | ok | partial | skipped_budget
    planned_queries INTEGER NOT NULL DEFAULT 0,
    executed_queries INTEGER NOT NULL DEFAULT 0,
    saved_queries INTEGER NOT NULL DEFAULT 0,
    billable_requests INTEGER NOT NULL DEFAULT 0,
    alerts_sent INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);

-- Una fila por request HTTP a la fuente (base del contador mensual).
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    ts_utc TEXT NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- fares | booking_link
    origin TEXT,
    destination TEXT,
    depart_date TEXT,
    return_date TEXT,
    status TEXT NOT NULL,               -- ok | no_results | error
    http_status INTEGER,
    attempts INTEGER NOT NULL DEFAULT 1,
    billable INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts_utc);

-- Mejor precio por consulta (origen + itinerario) y corrida.
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    ts_utc TEXT NOT NULL,
    source TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT NOT NULL,
    duration_days INTEGER NOT NULL,
    stops INTEGER NOT NULL,
    airlines TEXT NOT NULL,
    price_total REAL NOT NULL,
    price_pp REAL NOT NULL,
    currency TEXT NOT NULL,
    booking_url TEXT,
    source_ref TEXT,
    self_transfer INTEGER NOT NULL DEFAULT 0,
    UNIQUE (run_id, origin, destination, depart_date, return_date)
);
CREATE INDEX IF NOT EXISTS idx_obs_itin ON observations(destination, depart_date, return_date);

-- Estado de cada consulta posible (origen + destino + fechas).
CREATE TABLE IF NOT EXISTS pair_state (
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT NOT NULL,
    last_price_pp REAL,
    last_price_total REAL,
    last_price_at TEXT,                 -- timestamp UTC del último precio
    last_queried_at TEXT,               -- timestamp UTC de la última consulta exitosa
    last_queried_seq INTEGER,           -- número de corrida del destino en que se consultó
    empty_streak INTEGER NOT NULL DEFAULT 0,
    no_service INTEGER NOT NULL DEFAULT 0,
    no_service_since TEXT,
    PRIMARY KEY (origin, destination, depart_date, return_date)
);

-- Estado por destino: contador de corridas y modo (normal | reduced).
CREATE TABLE IF NOT EXISTS dest_state (
    destination TEXT PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'normal',
    run_seq INTEGER NOT NULL DEFAULT 0,
    last_queried_at TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    ts_utc TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT NOT NULL,
    price_pp REAL NOT NULL,
    price_total REAL NOT NULL,
    rules TEXT NOT NULL,
    channels TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_itin ON alerts(destination, depart_date, return_date);
"""


def iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


@dataclass
class PairState:
    origin: str
    destination: str
    depart: dt.date
    ret: dt.date
    last_price_pp: Optional[float] = None
    last_price_total: Optional[float] = None
    last_price_at: Optional[dt.datetime] = None
    last_queried_at: Optional[dt.datetime] = None
    last_queried_seq: Optional[int] = None
    empty_streak: int = 0
    no_service: bool = False
    no_service_since: Optional[dt.datetime] = None


@dataclass
class DestState:
    destination: str
    mode: str = "normal"
    run_seq: int = 0
    last_queried_at: Optional[dt.datetime] = None


@dataclass
class WindowPoint:
    depart: dt.date
    ret: dt.date
    price_pp: float
    price_at: dt.datetime
    origin: str


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    # ----------------------------------------------------------- apertura
    @classmethod
    def open(cls, path: str | Path, dry_run: bool = False) -> "Store":
        """En dry_run se trabaja sobre una copia en memoria que nunca se escribe a disco."""
        path = Path(path)
        if dry_run:
            mem = sqlite3.connect(":memory:")
            if path.exists():
                src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                src.backup(mem)
                src.close()
            return cls(mem)
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(sqlite3.connect(path))

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- runs
    def start_run(self, now: dt.datetime, mode: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, mode, status) VALUES (?, ?, 'running')", (iso(now), mode)
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, now: dt.datetime, status: str, **counts) -> None:
        allowed = {"planned_queries", "executed_queries", "saved_queries", "billable_requests", "alerts_sent", "notes"}
        sets = ["finished_at = ?", "status = ?"]
        values: list = [iso(now), status]
        for key, value in counts.items():
            if key not in allowed:
                raise KeyError(key)
            sets.append(f"{key} = ?")
            values.append(value)
        values.append(run_id)
        self.conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", values)

    def has_any_observation_attempt(self, destination: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM pair_state WHERE destination = ? AND last_queried_at IS NOT NULL LIMIT 1", (destination,)
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------ requests
    def record_request(
        self,
        run_id: int,
        now: dt.datetime,
        source: str,
        kind: str,
        status: str,
        billable: bool,
        http_status: Optional[int] = None,
        attempts: int = 1,
        error: Optional[str] = None,
        origin: Optional[str] = None,
        destination: Optional[str] = None,
        depart: Optional[dt.date] = None,
        ret: Optional[dt.date] = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO requests (run_id, ts_utc, source, kind, origin, destination, depart_date,
                   return_date, status, http_status, attempts, billable, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, iso(now), source, kind, origin, destination,
                depart.isoformat() if depart else None, ret.isoformat() if ret else None,
                status, http_status, attempts, int(billable), error,
            ),
        )

    def month_requests(self, now: dt.datetime) -> int:
        month = now.astimezone(dt.timezone.utc).strftime("%Y-%m")
        row = self.conn.execute(
            "SELECT COUNT(*) FROM requests WHERE billable = 1 AND substr(ts_utc, 1, 7) = ?", (month,)
        ).fetchone()
        return int(row[0])

    # -------------------------------------------------------- observations
    def add_observation(
        self, run_id: int, now: dt.datetime, source: str, origin: str, destination: str,
        depart: dt.date, ret: dt.date, quote: Quote,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO observations (run_id, ts_utc, source, origin, destination, depart_date, return_date,
                   duration_days, stops, airlines, price_total, price_pp, currency, booking_url, source_ref,
                   self_transfer)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, iso(now), source, origin, destination, depart.isoformat(), ret.isoformat(),
                (ret - depart).days, quote.stops, ",".join(quote.airlines), quote.price_total, quote.price_pp,
                quote.currency, quote.booking_url, quote.source_ref, int(quote.self_transfer),
            ),
        )
        return int(cur.lastrowid)

    def set_booking_url(self, observation_id: int, url: str) -> None:
        self.conn.execute("UPDATE observations SET booking_url = ? WHERE id = ?", (url, observation_id))

    def itinerary_history(self, destination: str, depart: dt.date, ret: dt.date, exclude_run: int) -> list[float]:
        """Mejor precio por persona de cada corrida anterior (mínimo entre orígenes)."""
        rows = self.conn.execute(
            """SELECT MIN(price_pp) FROM observations
               WHERE destination = ? AND depart_date = ? AND return_date = ? AND run_id != ?
               GROUP BY run_id ORDER BY run_id""",
            (destination, depart.isoformat(), ret.isoformat(), exclude_run),
        ).fetchall()
        return [float(r[0]) for r in rows]

    def run_observations(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM observations WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()

    # ---------------------------------------------------------- pair_state
    def pair_states(self, destination: str) -> dict[tuple[str, dt.date, dt.date], PairState]:
        rows = self.conn.execute("SELECT * FROM pair_state WHERE destination = ?", (destination,)).fetchall()
        out = {}
        for r in rows:
            st = _row_to_pair_state(r)
            out[(st.origin, st.depart, st.ret)] = st
        return out

    def get_pair_state(self, origin: str, destination: str, depart: dt.date, ret: dt.date) -> Optional[PairState]:
        r = self.conn.execute(
            """SELECT * FROM pair_state WHERE origin = ? AND destination = ? AND depart_date = ? AND return_date = ?""",
            (origin, destination, depart.isoformat(), ret.isoformat()),
        ).fetchone()
        return _row_to_pair_state(r) if r else None

    def save_pair_state(self, st: PairState) -> None:
        self.conn.execute(
            """INSERT INTO pair_state (origin, destination, depart_date, return_date, last_price_pp, last_price_total,
                   last_price_at, last_queried_at, last_queried_seq, empty_streak, no_service, no_service_since)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (origin, destination, depart_date, return_date) DO UPDATE SET
                   last_price_pp = excluded.last_price_pp,
                   last_price_total = excluded.last_price_total,
                   last_price_at = excluded.last_price_at,
                   last_queried_at = excluded.last_queried_at,
                   last_queried_seq = excluded.last_queried_seq,
                   empty_streak = excluded.empty_streak,
                   no_service = excluded.no_service,
                   no_service_since = excluded.no_service_since""",
            (
                st.origin, st.destination, st.depart.isoformat(), st.ret.isoformat(), st.last_price_pp,
                st.last_price_total, iso(st.last_price_at) if st.last_price_at else None,
                iso(st.last_queried_at) if st.last_queried_at else None, st.last_queried_seq, st.empty_streak,
                int(st.no_service), iso(st.no_service_since) if st.no_service_since else None,
            ),
        )

    def window(self, destination: str) -> list[WindowPoint]:
        """Último precio conocido de cada par de fechas del destino (mínimo entre orígenes con servicio)."""
        rows = self.conn.execute(
            """SELECT origin, depart_date, return_date, last_price_pp, last_price_at FROM pair_state
               WHERE destination = ? AND no_service = 0 AND last_price_pp IS NOT NULL""",
            (destination,),
        ).fetchall()
        best: dict[tuple[str, str], WindowPoint] = {}
        for r in rows:
            key = (r["depart_date"], r["return_date"])
            point = WindowPoint(
                depart=dt.date.fromisoformat(r["depart_date"]),
                ret=dt.date.fromisoformat(r["return_date"]),
                price_pp=float(r["last_price_pp"]),
                price_at=parse_ts(r["last_price_at"]),
                origin=r["origin"],
            )
            if key not in best or point.price_pp < best[key].price_pp:
                best[key] = point
        return sorted(best.values(), key=lambda p: (p.depart, p.ret))

    # ---------------------------------------------------------- dest_state
    def dest_state(self, destination: str) -> DestState:
        r = self.conn.execute("SELECT * FROM dest_state WHERE destination = ?", (destination,)).fetchone()
        if r is None:
            return DestState(destination)
        return DestState(
            destination=r["destination"],
            mode=r["mode"],
            run_seq=int(r["run_seq"]),
            last_queried_at=parse_ts(r["last_queried_at"]) if r["last_queried_at"] else None,
        )

    def save_dest_state(self, st: DestState) -> None:
        self.conn.execute(
            """INSERT INTO dest_state (destination, mode, run_seq, last_queried_at) VALUES (?, ?, ?, ?)
               ON CONFLICT (destination) DO UPDATE SET mode = excluded.mode, run_seq = excluded.run_seq,
                   last_queried_at = excluded.last_queried_at""",
            (st.destination, st.mode, st.run_seq, iso(st.last_queried_at) if st.last_queried_at else None),
        )

    # -------------------------------------------------------------- alerts
    def last_alert_price(self, destination: str, depart: dt.date, ret: dt.date) -> Optional[float]:
        r = self.conn.execute(
            """SELECT price_pp FROM alerts WHERE destination = ? AND depart_date = ? AND return_date = ?
               ORDER BY id DESC LIMIT 1""",
            (destination, depart.isoformat(), ret.isoformat()),
        ).fetchone()
        return float(r[0]) if r else None

    def record_alert(
        self, run_id: int, now: dt.datetime, origin: str, destination: str, depart: dt.date, ret: dt.date,
        price_pp: float, price_total: float, rules: Iterable[str], channels: Iterable[str],
    ) -> None:
        self.conn.execute(
            """INSERT INTO alerts (run_id, ts_utc, origin, destination, depart_date, return_date, price_pp,
                   price_total, rules, channels) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, iso(now), origin, destination, depart.isoformat(), ret.isoformat(), price_pp, price_total,
             ",".join(rules), ",".join(channels)),
        )


def _row_to_pair_state(r: sqlite3.Row) -> PairState:
    return PairState(
        origin=r["origin"],
        destination=r["destination"],
        depart=dt.date.fromisoformat(r["depart_date"]),
        ret=dt.date.fromisoformat(r["return_date"]),
        last_price_pp=r["last_price_pp"],
        last_price_total=r["last_price_total"],
        last_price_at=parse_ts(r["last_price_at"]) if r["last_price_at"] else None,
        last_queried_at=parse_ts(r["last_queried_at"]) if r["last_queried_at"] else None,
        last_queried_seq=r["last_queried_seq"],
        empty_streak=int(r["empty_streak"]),
        no_service=bool(r["no_service"]),
        no_service_since=parse_ts(r["no_service_since"]) if r["no_service_since"] else None,
    )
