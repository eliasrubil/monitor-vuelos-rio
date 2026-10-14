"""Punto de entrada: python -m monitor [--slot auto|morning|night] [--dry-run] [--full-scan] [--test-alert]
[--demo-alert]."""

from __future__ import annotations

import argparse
import logging
import sys

from .alerts import LogNotifier, build_notifier
from .alerts.format import error_message, test_message
from .config import ConfigError, Secrets, load_config
from .logsetup import setup_logging
from .runner import Runner, slot_for, utcnow
from .sources import make_source
from .storage import Store

log = logging.getLogger("monitor")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="monitor", description="Monitor de precios de vuelos")
    p.add_argument("--config", default="config.yaml", help="ruta a config.yaml")
    p.add_argument("--dry-run", action="store_true", help="consulta y calcula, sin escribir la base ni alertar")
    p.add_argument("--full-scan", action="store_true", help="barrido completo de todas las combinaciones")
    p.add_argument("--test-alert", action="store_true", help="envía un mensaje de prueba por ambos canales y termina")
    p.add_argument("--demo-alert", action="store_true",
                   help="envía alertas simuladas (un email y un mensaje de Telegram) para ver el formato y termina")
    p.add_argument("--slot", choices=["auto", "morning", "night"], default="auto",
                   help="corrida de la mañana (top + rotación) o de la noche (seguimiento + fechas nuevas); "
                        "auto = night desde las 14:00 hora argentina")
    p.add_argument("-v", "--verbose", action="store_true", help="logging detallado")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    secrets = Secrets.from_env()
    setup_logging(secrets.values(), verbose=args.verbose)
    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError) as exc:
        log.error("Config inválida: %s", exc)
        return 2

    if args.demo_alert:
        from .demo import send_demo

        notifier = build_notifier(cfg, secrets)
        if not notifier.channels:
            log.error("No hay canales configurados (revisá los secrets).")
            return 1
        delivered = send_demo(cfg, notifier, utcnow())
        log.info("Alertas simuladas enviadas por: %s", ", ".join(delivered) or "ningún canal")
        return 0 if len(delivered) == len(notifier.channels) else 1

    if args.test_alert:
        notifier = build_notifier(cfg, secrets)
        if not notifier.channels:
            log.error("No hay canales configurados (revisá los secrets).")
            return 1
        delivered = notifier.send(*test_message(utcnow()))
        log.info("Prueba enviada por: %s", ", ".join(delivered) or "ningún canal")
        return 0 if len(delivered) == len(notifier.channels) else 1

    if not secrets.ignav_api_key and cfg.source == "ignav":
        log.error("Falta el secret IGNAV_API_KEY.")
        return 2

    store = Store.open(cfg.database.path, dry_run=args.dry_run)
    # En dry-run los precios van a una copia en memoria, pero las requests se registran en la base real.
    usage = Store.open(cfg.database.path) if args.dry_run else None
    notifier = LogNotifier() if args.dry_run else build_notifier(cfg, secrets)
    try:
        runner = Runner(cfg, store, make_source(cfg, secrets), notifier, dry_run=args.dry_run, usage=usage)
        slot = slot_for(utcnow()) if args.slot == "auto" else args.slot
        log.info("Corrida: %s", slot)
        # Con --slot auto (las programadas), un turno que ya corrió hoy no se repite. Elegir morning o night a
        # mano siempre corre: sirve para adelantar un turno, y la corrida programada de ese turno se saltea.
        return runner.run(full_scan=args.full_scan, slot=slot, once_per_day=args.slot == "auto")
    except Exception as exc:  # noqa: BLE001 - avisar siempre ante un fallo inesperado
        log.exception("La corrida falló")
        notifier.send(*error_message(utcnow(), f"{type(exc).__name__}. Ver el log del workflow.",
                                    title="fallo inesperado"))
        return 1
    finally:
        store.close()
        if usage is not None:
            usage.close()


if __name__ == "__main__":
    sys.exit(main())
