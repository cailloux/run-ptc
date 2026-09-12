"""Command line entry point: python -m app.cli <command>."""

import argparse
import logging
import sys
import time
from datetime import UTC, date, datetime

import httpx

from app import db, exclusions, health
from app.arcgis import layer_signature
from app.config import intervals_credentials, load_settings
from app.graph import build_graph
from app.importer import LAYERS, METERS_PER_MILE, import_layer
from app.intervals import IntervalsClient
from app.jobs import JobBusy, JobFn, start_job
from app.matching import metrics, near_miss_lines, near_misses, recompute
from app.refresh import download, refresh, store_signature
from app.sync import EASTERN, sync, sync_window


# Exit codes the nightly User Script acts on: 1 alerts, EXIT_BUSY warns.
EXIT_BUSY = 75   # EX_TEMPFAIL: another job holds the lock; try again later


def _run_job(args, job: str, fn: JobFn) -> int:
    """Run a data job under the shared lock, recording it in job_run."""
    with db.connect() as conn:
        db.migrate(conn)
    try:
        running = start_job(job, args.trigger)
    except JobBusy:
        print("another job is running (sync, import, refresh, or recompute); try again when it finishes",
              file=sys.stderr)
        return EXIT_BUSY
    try:
        lines = running.run(fn)
    except Exception as e:
        # The traceback is already logged above. End with one clean line, the
        # same error job_run records, for alerts and anyone reading the log.
        cause = (str(e).splitlines() or [""])[0]
        print(f"{job} failed (job {running.id}): {type(e).__name__}: {cause}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


def cmd_migrate(args) -> int:
    with db.connect() as conn:
        applied = db.migrate(conn)
    print(f"applied: {', '.join(applied)}" if applied else "schema up to date")
    return 0


def cmd_import(args) -> int:
    settings = load_settings()
    # Validate exclusions before spending time on the download.
    excl = exclusions.load()
    names = [args.layer] if args.layer else list(LAYERS)

    def job(conn) -> list[str]:
        lines = []
        signatures = {}
        with httpx.Client(timeout=120) as client:
            for name in names:
                layer = LAYERS[name]
                # Taken before the download, so an edit in between is caught
                # by the next refresh rather than missed.
                signatures[name] = layer_signature(client, layer.url, layer.oid_field)
                features = download(client, layer, signatures[name])
                lines += import_layer(conn, layer, features, settings, excl).lines()
        # City data changed, so the routing graph is rebuilt from it.
        lines += build_graph(conn, settings).lines()
        for name, sig in signatures.items():
            store_signature(conn, name, sig)
        return lines

    return _run_job(args, "import", job)


def cmd_refresh(args) -> int:
    settings = load_settings()
    excl = exclusions.load()

    def job(conn) -> list[str]:
        with httpx.Client(timeout=120) as client:
            return refresh(conn, client, settings, excl, force=args.force).lines()

    return _run_job(args, "refresh", job)


def cmd_graph(args) -> int:
    settings = load_settings()
    return _run_job(args, "graph", lambda conn: build_graph(conn, settings).lines())


def cmd_exclusions(args) -> int:
    excl = exclusions.load()
    with db.connect() as conn:
        warnings = exclusions.apply(conn, excl)
        excluded = conn.execute("SELECT count(*) FROM segment WHERE excluded").fetchone()[0]
    print(f"{excluded} segments excluded, {len(warnings)} warnings")
    return 0


def cmd_sync(args) -> int:
    settings = load_settings()
    athlete_id, api_key = intervals_credentials()

    def job(conn) -> list[str]:
        start, end = sync_window(conn, settings, args.start, args.end)
        if start > end:
            raise ValueError(f"--from {start} is after --to {end}")
        with IntervalsClient(athlete_id, api_key) as client:
            report = sync(conn, client, start, end, settings,
                          refetch=args.refetch, dry_run=args.dry_run)
        return [report.headline(), *report.lines()]

    return _run_job(args, "sync", job)


def cmd_activities(args) -> int:
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT start_at, intervals_id, status, sport, distance_m, name
            FROM activity WHERE %(status)s::text IS NULL OR status = %(status)s
            ORDER BY start_at
        """, {"status": args.status}).fetchall()
    for start_at, intervals_id, status, sport, distance_m, name in rows:
        miles = f"{(distance_m or 0) / METERS_PER_MILE:5.2f} mi"
        local = start_at.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M")
        print(f"{local}  {intervals_id:>12}  {status:7}  {sport:8}  {miles}  {name or ''}")
    print(f"{len(rows)} activities")
    return 0


def _stats_lines(conn) -> list[str]:
    return metrics(conn).lines() + near_miss_lines(near_misses(conn))


def cmd_recompute(args) -> int:
    settings = load_settings()

    def job(conn) -> list[str]:
        started = time.monotonic()
        hit = recompute(conn, settings)
        return [
            f"recompute: {hit} nodes hit in {time.monotonic() - started:.1f} s "
            f"(radius: cart paths {settings.match_radius_cartpath_m} m, cart path ends "
            f"{settings.match_radius_cartpath_end_m} m, roads {settings.match_radius_road_m} m, "
            f"{', '.join(settings.match_radius_wide_road_classes)} {settings.match_radius_wide_m} m; "
            f"gap split {settings.track_gap_split_m} m)",
            *_stats_lines(conn),
        ]

    return _run_job(args, "recompute", job)


def cmd_alerts(args) -> int:
    """Print the notices the nightly script should send, or record one as sent."""
    with db.connect() as conn:
        db.migrate(conn)
        if args.sent:
            health.mark_sent(conn, args.sent)
            return 0
        with conn.transaction():
            notices = health.alerts(conn, load_settings(), datetime.now(UTC))
    # Records end with RECORD_SEP so bodies can span lines.
    sys.stdout.write("".join(n.record() + health.RECORD_SEP for n in notices))
    return 0


def cmd_stats(args) -> int:
    with db.connect() as conn:
        print("\n".join(_stats_lines(conn)))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # httpx logs every request at INFO; a backfill makes hundreds.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("--trigger", choices=["cli", "schedule"], default="cli",
                        help="recorded in job_run; the nightly script passes schedule")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending migrations").set_defaults(func=cmd_migrate)
    p = sub.add_parser("import", help="import city layers, regenerate nodes, apply exclusions")
    p.add_argument("--layer", choices=list(LAYERS))
    p.set_defaults(func=cmd_import)
    sub.add_parser("exclusions", help="reapply config/exclusions.yaml").set_defaults(
        func=cmd_exclusions)
    p = sub.add_parser("sync", help="sync runs from Intervals.icu (read-only)")
    p.add_argument("--from", dest="start", type=date.fromisoformat, metavar="YYYY-MM-DD",
                   help="default: a week before the newest stored run, or sync_backfill_start")
    p.add_argument("--to", dest="end", type=date.fromisoformat, metavar="YYYY-MM-DD",
                   help="default: today in US Eastern")
    p.add_argument("--refetch", action="store_true", help="refetch streams for runs already stored")
    p.add_argument("--dry-run", action="store_true", help="fetch and classify, but store nothing")
    p.set_defaults(func=cmd_sync)
    p = sub.add_parser("activities", help="list synced activities")
    p.add_argument("--status", choices=["city", "outside", "no_gps"])
    p.set_defaults(func=cmd_activities)
    sub.add_parser("recompute", help="clear all hits and replay every stored run").set_defaults(
        func=cmd_recompute)
    sub.add_parser("stats", help="completion metrics and near misses").set_defaults(func=cmd_stats)
    sub.add_parser("graph", help="rebuild the routing graph and list its islands").set_defaults(
        func=cmd_graph)
    p = sub.add_parser("refresh", help="import city layers the city has changed, with a change report")
    p.add_argument("--force", action="store_true", help="import every layer even if unchanged")
    p.set_defaults(func=cmd_refresh)
    p = sub.add_parser("alerts", help="print data-health notices for the nightly script to send")
    p.add_argument("--sent", metavar="KEY", help="record the notice with this key as sent")
    p.set_defaults(func=cmd_alerts)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except exclusions.ExclusionError as e:
        print(f"exclusions.yaml: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
