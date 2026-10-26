"""Orquestación de una corrida: planificar, consultar, guardar, detectar y alertar."""

from __future__ import annotations

import datetime as dt
import logging
import statistics
import time
from collections import Counter
from typing import Callable, Optional

from .alerts.channels import Notifier
from .alerts.format import (
    AR_TZ,
    SLOT_MORNING,
    SLOT_NIGHT,
    Candidate,
    FollowUp,
    DestinationSummary,
    TopItem,
    alert_message,
    alerts_email,
    budget_skip_message,
    error_message,
    summary_message,
    telegram_alert_chunks,
)
from .config import Config
from .detection import Evaluation, evaluate, passes_antispam
from .planner import Plan, PlannedQuery, plan_night, plan_run
from .sources.base import STATUS_NO_RESULTS, STATUS_OK, PriceSource, SearchOutcome, SearchQuery
from .storage import PairState, Store

log = logging.getLogger(__name__)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


NIGHT_FROM_HOUR_AR = 14   # con --slot auto, una corrida que arranca desde las 14:00 hora argentina es la de la noche


def slot_for(now: dt.datetime) -> str:
    return SLOT_NIGHT if now.astimezone(AR_TZ).hour >= NIGHT_FROM_HOUR_AR else SLOT_MORNING


def ar_day_start(now: dt.datetime) -> dt.datetime:
    """00:00 hora argentina del día de `now`."""
    local = now.astimezone(AR_TZ)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


class Runner:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        source: PriceSource,
        notifier: Notifier,
        *,
        dry_run: bool = False,
        usage: Optional[Store] = None,
        clock: Callable[[], dt.datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ):
        """usage: base donde se registran las requests (contador mensual). Por defecto es store; en
        dry-run es la base real, porque la fuente cobra esas requests aunque no se guarden precios."""
        self.cfg = cfg
        self.store = store
        self.usage = usage or store
        self.source = source
        self.notifier = notifier
        self.dry_run = dry_run
        self.clock = clock
        self.sleep = sleep
        self.notes: list[str] = []

    # ------------------------------------------------------------------ run
    def run(self, full_scan: bool = False, slot: str = SLOT_MORNING, once_per_day: bool = False) -> int:
        """slot: morning (top_k + rotación) o night (seguimiento de los hallazgos de la mañana + barrido de
        las fechas que la mañana no consultó).
        once_per_day: si ese turno ya corrió hoy (hora argentina), no se hace nada. Lo usan las corridas
        automáticas, así una corrida manual adelantada reemplaza a la programada del mismo turno."""
        cfg = self.cfg
        now = self.clock()
        stop_after = cfg.schedule.stop_after
        if stop_after and now.date() > stop_after:
            log.info("Hoy (%s) es posterior a stop_after (%s): no se consulta nada.", now.date(), stop_after)
            return 0
        if once_per_day and not self.dry_run:
            done = self.store.last_run(slot, ar_day_start(now))
            if done:
                log.info("El turno %s de hoy ya corrió (corrida %d, %s): no se repite.", slot, done[0],
                         done[1].astimezone(AR_TZ).strftime("%H:%M hora argentina"))
                return 0
        if self.dry_run:
            log.info("Modo dry-run: se consulta la API (las requests se cuentan en el uso mensual), pero no se "
                     "guardan precios ni se envían alertas.")

        origins = cfg.search.origins
        if cfg.search.open_jaw and not self.source.supports_open_jaw:
            log.warning("open_jaw: true, pero la fuente %s no lo soporta; se consultan solo ida y vuelta.",
                        self.source.name)

        followups: list[FollowUp] = []
        morning_pairs: set[tuple[str, dt.date, dt.date]] = set()
        if slot == SLOT_NIGHT:
            day_start = ar_day_start(now)
            morning = self.store.last_morning_run(day_start)
            if morning:
                followups, morning_pairs = self._morning_findings(morning[0])
                log.info("Corrida de la noche: %d hallazgos de la mañana para seguir; se barren las fechas no "
                         "consultadas desde las %s", len(followups), morning[1].astimezone(AR_TZ).strftime("%H:%M"))
            else:
                log.info("Corrida de la noche sin corrida de la mañana hoy: se barren todas las fechas no "
                         "consultadas en el día")
            since = morning[1] if morning else day_start
        if slot == SLOT_NIGHT and not full_scan:
            keys = {(f.origin, f.destination, f.depart, f.ret) for f in followups}
            plan = plan_night(cfg, self.store, origins, now, since, keys)
        else:
            plan = plan_run(cfg, self.store, origins, now, full_scan=full_scan)
        self._log_plan(plan)
        if self.dry_run:
            planned = len(plan.queries)
            plan.limit_queries(cfg.budget.dry_run_max_queries)
            log.info("dry-run: se limitan las consultas a %d de %d planificadas (budget.dry_run_max_queries)",
                     len(plan.queries), planned)

        limit = cfg.budget.max_requests_per_month
        used = self.usage.month_requests(now)
        estimated = len(plan.queries)
        log.info("Requests estimados para esta corrida: %d · acumulado del mes: %d · tope mensual: %d",
                 estimated, used, limit)
        if used + estimated > limit:
            log.warning("La corrida superaría el tope mensual (%d + %d > %d): se saltea.", used, estimated, limit)
            self.notifier.send(*budget_skip_message(cfg, now, used, estimated))
            if not self.dry_run:
                run_id = self.store.start_run(now, "skipped")
                self.store.finish_run(run_id, self.clock(), "skipped_budget", planned_queries=estimated,
                                      notes=f"acumulado {used}, tope {limit}")
                self.store.commit()
            return 0

        mode = "full" if plan.is_full else ("night" if slot == SLOT_NIGHT else "incremental")
        run_id = self.store.start_run(now, mode, slot)
        # En dry-run, la corrida se registra aparte en la base real solo para el contador de uso.
        separate_usage = self.usage is not self.store
        usage_run_id = self.usage.start_run(now, "dry_run") if separate_usage else run_id
        self._usage_run_id = usage_run_id
        seqs = self._advance_destinations(plan, now)

        statuses: Counter[str] = Counter()
        billable = 0
        fatal_error = None
        executed = 0
        followup_outcomes: dict[tuple[str, str, dt.date, dt.date], SearchOutcome] = {}
        follow_keys = {(f.origin, f.destination, f.depart, f.ret) for f in followups}
        retry_wait = cfg.optimization.retry_empty_wait_seconds
        for q in plan.queries:
            outcome = self._query(q, usage_run_id)
            if outcome.status == STATUS_NO_RESULTS and outcome.raw_count == 0 and retry_wait > 0:
                # Respuesta vacía de la fuente: suele ser una búsqueda que no llegó a completarse. Un reintento.
                log.info("%s→%s %s/%s: respuesta vacía, se reintenta en %.0f s", q.origin, q.destination, q.depart,
                         q.ret, retry_wait)
                self.sleep(retry_wait)
                billable += int(outcome.billable)
                outcome = self._query(q, usage_run_id)
            if (q.origin, q.destination, q.depart, q.ret) in follow_keys:
                followup_outcomes[(q.origin, q.destination, q.depart, q.ret)] = outcome
            executed += 1
            statuses[outcome.status] += 1
            billable += int(outcome.billable)
            self._apply_outcome(run_id, q, outcome, seqs[q.destination])
            if not self.dry_run:
                self.store.commit()
            if outcome.fatal:
                fatal_error = f"HTTP {outcome.http_status}: {outcome.error}"
                log.error("Error fatal de %s (%s): se cortan las consultas restantes.", self.source.name, fatal_error)
                break
        log.info("Resultados: %d con precio, %d sin resultados, %d con error",
                 statuses[STATUS_OK], statuses[STATUS_NO_RESULTS], statuses["error"])
        if statuses["error"]:
            self.notes.append(f"{statuses['error']} consultas con error")

        self._resolve_followups(followups, followup_outcomes)
        # Los hallazgos de la mañana se informan como seguimiento; a la noche solo alertan fechas nuevas.
        groups = self._group_same_price(self._detect(run_id, now, skip=morning_pairs))
        billable += self._fetch_booking_links(run_id, [members[0] for members in groups], now)
        sent = self._send_alerts(run_id, groups, slot, followups)

        if fatal_error:
            self.notes.append(f"Error fatal de la fuente: {fatal_error}")
            self.notifier.send(*error_message(now, f"{self.source.name}: {fatal_error}. "
                                                    "Revisá IGNAV_API_KEY y el estado de la cuenta."))

        saved = plan.universe - executed
        month_total = self.usage.month_requests(now)
        if self.cfg.alerts.summary_telegram:
            self._send_summary(now, mode, executed, saved, month_total, sent, plan, slot)

        status = "ok" if not fatal_error and not statuses["error"] else "partial"
        counts = dict(planned_queries=estimated, executed_queries=executed, saved_queries=saved,
                      billable_requests=billable, alerts_sent=sent, notes="; ".join(self.notes) or None)
        self.store.finish_run(run_id, self.clock(), status, **counts)
        if separate_usage:
            self.usage.finish_run(usage_run_id, self.clock(), "dry_run", **counts)
        log.info(
            "Corrida %s%s terminada: %d consultas hechas, %d ahorradas (universo %d), %d alertas. "
            "Acumulado del mes: %d/%d",
            mode, " (dry-run)" if self.dry_run else "", executed, saved, plan.universe, sent, month_total, limit,
        )
        self.usage.commit()
        if not self.dry_run:
            self.store.commit()
        return 1 if fatal_error else 0

    # ------------------------------------------------------------- helpers
    def _log_plan(self, plan: Plan) -> None:
        for dp in plan.destinations:
            if dp.skipped_reason:
                log.info("%s [%s]: sin consultas (%s)", dp.destination, dp.mode, dp.skipped_reason)
                self.notes.append(f"{dp.destination}: {dp.skipped_reason}")
                continue
            reasons = Counter(q.reason for q in dp.queries)
            detail = ", ".join(f"{k}={v}" for k, v in sorted(reasons.items()))
            log.info("%s [%s]: %d consultas (%s), %d ahorradas", dp.destination, dp.mode, len(dp.queries),
                     detail or "-", dp.saved)

    def _advance_destinations(self, plan: Plan, now: dt.datetime) -> dict[str, int]:
        seqs = {}
        for dp in plan.destinations:
            st = self.store.dest_state(dp.destination)
            st.mode = dp.mode
            if dp.queries:
                st.run_seq += 1
                st.last_queried_at = now
            seqs[dp.destination] = st.run_seq
            self.store.save_dest_state(st)
        return seqs

    def _query(self, q: PlannedQuery, usage_run_id: int) -> SearchOutcome:
        """Una consulta a la fuente, registrada en el contador de uso (commit inmediato: si la corrida se corta,
        las requests facturadas quedan contadas)."""
        started = time.monotonic()
        outcome = self.source.search_round_trip(self._search_query(q))
        if outcome.status == STATUS_NO_RESULTS:
            log.info("%s→%s %s/%s: sin resultados (%s itinerarios de la fuente antes de filtrar, %.1f s)", q.origin,
                     q.destination, q.depart, q.ret, "?" if outcome.raw_count is None else outcome.raw_count,
                     time.monotonic() - started)
        self.usage.record_request(
            usage_run_id, self.clock(), self.source.name, "fares", outcome.status, outcome.billable,
            outcome.http_status, outcome.attempts, outcome.error, q.origin, q.destination, q.depart, q.ret,
        )
        self.usage.commit()
        return outcome

    def _search_query(self, q: PlannedQuery) -> SearchQuery:
        s = self.cfg.search
        return SearchQuery(q.origin, q.destination, q.depart, q.ret, s.adults, s.max_stops, s.cabin_class,
                           s.market, s.currency, tuple(s.airlines_exclude), tuple(s.airports_exclude))

    def _apply_outcome(self, run_id: int, q: PlannedQuery, outcome: SearchOutcome, seq: int) -> None:
        if outcome.status not in (STATUS_OK, STATUS_NO_RESULTS):
            log.warning("%s→%s %s/%s: error (%s)", q.origin, q.destination, q.depart, q.ret, outcome.error)
            return
        ts = self.clock()
        st = self.store.get_pair_state(q.origin, q.destination, q.depart, q.ret) or PairState(
            q.origin, q.destination, q.depart, q.ret
        )
        st.last_queried_at = ts
        st.last_queried_seq = seq
        if outcome.status == STATUS_OK:
            quote = outcome.quote
            self.store.add_observation(run_id, ts, self.source.name, q.origin, q.destination, q.depart, q.ret, quote)
            if st.no_service:
                log.info("%s→%s %s/%s: vuelve a tener servicio", q.origin, q.destination, q.depart, q.ret)
            st.last_price_pp, st.last_price_total, st.last_price_at = quote.price_pp, quote.price_total, ts
            st.empty_streak, st.no_service, st.no_service_since = 0, False, None
            log.info("%s→%s %s/%s: %.0f pp (%s, %s→ / →%s, %d escala/s)", q.origin, q.destination, q.depart, q.ret,
                     quote.price_pp, ",".join(quote.airlines) or "s/d", quote.depart_airport or "?",
                     quote.return_airport or "?", quote.stops)
        else:
            # Sin resultados: el último precio deja de ser vigente.
            st.last_price_pp = st.last_price_total = None
            st.empty_streak += 1
            if not st.no_service and st.empty_streak >= self.cfg.optimization.no_service_after_empty_runs:
                st.no_service, st.no_service_since = True, ts
                log.info("%s→%s %s/%s: sin resultados %d corridas seguidas -> no_service (recheck cada %d días)",
                         q.origin, q.destination, q.depart, q.ret, st.empty_streak, self.cfg.optimization.recheck_days)
        self.store.save_pair_state(st)

    def _candidates(self, run_id: int) -> list[Candidate]:
        best: dict[tuple[str, str, str], Candidate] = {}
        for r in self.store.run_observations(run_id):
            key = (r["destination"], r["depart_date"], r["return_date"])
            if key in best and best[key].price_pp <= r["price_pp"]:
                continue
            best[key] = Candidate(
                origin=r["origin"], destination=r["destination"],
                depart=dt.date.fromisoformat(r["depart_date"]), ret=dt.date.fromisoformat(r["return_date"]),
                stops=r["stops"], airlines=r["airlines"].replace(",", ", "), price_pp=r["price_pp"],
                price_total=r["price_total"], currency=r["currency"], observation_id=r["id"],
                source_ref=r["source_ref"], booking_url=r["booking_url"], self_transfer=bool(r["self_transfer"]),
                depart_airport=r["depart_airport"], return_airport=r["return_airport"],
            )
        return [best[k] for k in sorted(best)]

    def _morning_findings(self, run_id: int) -> tuple[list[FollowUp], set[tuple[str, dt.date, dt.date]]]:
        """Hallazgos del mail de la mañana: uno por número (la primera fecha de cada grupo de mismo precio) y
        todas las fechas alertadas, que a la noche no vuelven a alertar como nuevas."""
        followups: dict[int, FollowUp] = {}
        pairs = set()
        for r in self.store.run_alerts(run_id):
            depart, ret = dt.date.fromisoformat(r["depart_date"]), dt.date.fromisoformat(r["return_date"])
            pairs.add((r["destination"], depart, ret))
            if r["label"] is not None and r["label"] not in followups:
                followups[r["label"]] = FollowUp(r["label"], r["origin"], r["destination"], depart, ret,
                                                 float(r["price_pp"]))
        return [followups[k] for k in sorted(followups)], pairs

    @staticmethod
    def _resolve_followups(followups: list[FollowUp], outcomes: dict) -> None:
        for f in followups:
            out = outcomes.get((f.origin, f.destination, f.depart, f.ret))
            if out is None or out.status not in (STATUS_OK, STATUS_NO_RESULTS):
                f.status = "error"
            elif out.status == STATUS_NO_RESULTS:
                f.status = "gone"
            else:
                f.price_pp = out.quote.price_pp
                # Se compara en dólares enteros, como se muestra en los mensajes.
                diff = round(f.price_pp) - round(f.morning_price_pp)
                f.status = "same" if diff == 0 else ("up" if diff > 0 else "down")
            log.info("Seguimiento %s %s/%s: %s (mañana %.0f, ahora %s)", f.destination, f.depart, f.ret, f.status,
                     f.morning_price_pp, "-" if f.price_pp is None else f"{f.price_pp:.0f}")

    def _detect(self, run_id: int, now: dt.datetime, skip: frozenset | set = frozenset(),
                ) -> list[tuple[Candidate, Evaluation]]:
        det = self.cfg.detection
        windows = {}
        alerts = []
        excluded = set(self.cfg.search.airlines_exclude)
        for c in self._candidates(run_id):
            if (c.destination, c.depart, c.ret) in skip:
                continue
            if excluded & {a.strip() for a in c.airlines.split(",")}:
                # Red de seguridad: la fuente ya las filtra, pero nunca se alerta por una aerolínea excluida.
                log.warning("%s %s/%s: aerolínea excluida (%s), no se evalúa", c.destination, c.depart, c.ret,
                            c.airlines)
                continue
            if c.destination not in windows:
                windows[c.destination] = self.store.window(c.destination)
            history = self.store.itinerary_history(c.destination, c.depart, c.ret, exclude_run=run_id)
            ev = evaluate(c.price_pp, history, windows[c.destination], det, now, c.destination)
            if not ev.triggered:
                continue
            memory = det.antispam_memory_days
            last = self.store.last_alert_price(c.destination, c.depart, c.ret,
                                               since=now - dt.timedelta(days=memory) if memory else None)
            if not passes_antispam(c.price_pp, last, det.antispam_min_drop_pct):
                log.info("%s %s/%s: regla %s, pero anti-spam (%.0f vs. última alerta %.0f)",
                         c.destination, c.depart, c.ret, "+".join(ev.rules), c.price_pp, last)
                continue
            log.info("ALERTA %s %s/%s: %.0f pp, reglas %s", c.destination, c.depart, c.ret, c.price_pp,
                     "+".join(ev.rules))
            alerts.append((c, ev))
        return alerts

    @staticmethod
    def _group_same_price(alerts: list[tuple[Candidate, Evaluation]]) -> list[list[tuple[Candidate, Evaluation]]]:
        """Agrupa las alertas del mismo origen y destino con exactamente el mismo precio. Se avisa solo la primera
        (la de ida más temprana) y se menciona cuántas fechas más tienen ese precio."""
        groups: dict[tuple, list[tuple[Candidate, Evaluation]]] = {}
        for c, ev in alerts:
            groups.setdefault((c.destination, c.origin, round(c.price_pp, 2)), []).append((c, ev))
        out = []
        for members in groups.values():
            members.sort(key=lambda a: (a[0].depart, a[0].ret))
            members[0][0].same_price_count = len(members) - 1
            out.append(members)
        # Orden creciente por precio por persona (el más barato primero), en el email y en Telegram.
        out.sort(key=lambda m: (m[0][0].price_pp, m[0][0].destination, m[0][0].depart, m[0][0].ret))
        return out

    def _fetch_booking_links(self, run_id: int, alerts: list[tuple[Candidate, Evaluation]], now: dt.datetime) -> int:
        if not alerts or not self.cfg.alerts.booking_links:
            return 0
        if self.dry_run:
            log.info("dry-run: no se piden links de reserva")
            return 0
        billable = 0
        for c, _ in alerts:
            if c.booking_links or c.booking_url or not c.source_ref:
                continue
            if self.usage.month_requests(now) + 1 > self.cfg.budget.max_requests_per_month:
                log.warning("Sin presupuesto para pedir links de reserva")
                break
            outcome = self.source.booking_link(c.source_ref)
            billable += int(outcome.billable)
            self.usage.record_request(
                self._usage_run_id, self.clock(), self.source.name, "booking_link", "ok" if outcome.ok else "error",
                outcome.billable, outcome.http_status, outcome.attempts, outcome.error,
                c.origin, c.destination, c.depart, c.ret,
            )
            if outcome.ok:
                c.booking_links = outcome.links
                self.store.set_booking_url(c.observation_id, "\n".join(
                    f"{label}: {url}" if label else url for label, url in outcome.links if url))
            else:
                log.warning("No se obtuvo link de reserva para %s %s/%s: %s", c.destination, c.depart, c.ret,
                            outcome.error)
            if outcome.fatal:
                break
        return billable

    def _send_alerts(self, run_id: int, groups: list[list[tuple[Candidate, Evaluation]]],
                     slot: str = SLOT_MORNING, followups: Optional[list[FollowUp]] = None) -> int:
        """Telegram: todas las alertas en un único mensaje (partido si supera el límite). Email: un mensaje con
        todas. Las alertas van numeradas I), II), ... y el número queda registrado para el seguimiento de la
        noche. A la noche, ambos empiezan con el estado de los hallazgos de la mañana. Cada grupo de mismo
        precio se avisa una vez, pero se registra para todas sus fechas (anti-spam)."""
        if not groups and not followups:
            return 0
        leads = [members[0] for members in groups]
        telegram_ok: set[int] = set()
        for text, indices in telegram_alert_chunks(self.cfg, [c for c, _ in leads], slot=slot, followups=followups):
            if self.notifier.send("", text, only={"telegram"}):
                telegram_ok.update(indices)
        messages = [alert_message(self.cfg, c, ev) for c, ev in leads]
        email_ok = self.notifier.send(*alerts_email(self.cfg, messages, slot, followups), only={"email"})
        sent = 0
        for i, members in enumerate(groups):
            channels = (["telegram"] if i in telegram_ok else []) + email_ok
            if not channels:
                continue   # no se registra: se reintenta en la próxima corrida
            sent += 1
            for c, ev in members:
                self.store.record_alert(run_id, self.clock(), c.origin, c.destination, c.depart, c.ret, c.price_pp,
                                        c.price_total, ev.rules, channels, label=i + 1)
        if sent < len(groups) and not self.dry_run:
            log.error("%d alertas no se pudieron enviar por ningún canal", len(groups) - sent)
        return sent

    def _send_summary(self, now: dt.datetime, mode: str, executed: int, saved: int, month_total: int, sent: int,
                      plan: Plan, slot: str = SLOT_MORNING) -> None:
        top: list[TopItem] = []
        dests = []
        for dp in plan.destinations:
            window = self.store.window(dp.destination)
            prices = [p.price_pp for p in window]
            dests.append(DestinationSummary(
                dp.destination, dp.mode, len(prices),
                statistics.fmean(prices) if prices else None, statistics.median(prices) if prices else None,
            ))
            top += [TopItem(dp.destination, p.origin, p.depart, p.ret, p.price_pp, max((now - p.price_at).days, 0))
                    for p in window]
        top.sort(key=lambda t: (t.price_pp, t.destination, t.depart, t.ret))
        top = top[: self.cfg.alerts.summary_top_n]
        self.notifier.send(*summary_message(self.cfg, now, mode, executed, saved, month_total, sent, top, dests,
                                            self.notes, slot), only={"telegram"})
