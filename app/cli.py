"""Command line entry point: python -m app.cli <command>."""

import argparse
import logging
import sys
import time
from datetime import date

import httpx

from app import db, exclusions
from app.arcgis import fetch_features
from app.config import intervals_credentials, load_settings
from app.importer import LAYERS, METERS_PER_MILE, import_layer
from app.intervals import IntervalsClient
from app.jobs import JobBusy, JobFn, start_job
from app.matching import metrics, near_miss_lines, near_misses, recompute
from app.sync import EASTERN, sync, sync_window


def _run_job(job: str, fn: JobFn) -> int:
    """Run a data job under the shared lock, recording it in job_run."""
    with db.connect() as conn:
        db.migrate(conn)
    try:
        lines = start_job(job, "cli").run(fn)
    except JobBusy:
        print("another job is running (sync, import, or recompute); try again when it finishes",
              file=sys.stderr)
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
        with httpx.Client(timeout=120) as client:
            for name in names:
                layer = LAYERS[name]
                features = fetch_features(client, layer.url, layer.oid_field)
                lines += import_layer(conn, layer, features, settings, excl).lines()
        return lines

    return _run_job("import", job)


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

    return _run_job("sync", job)


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

    return _run_job("recompute", job)


def cmd_stats(args) -> int:
    with db.connect() as conn:
        print("\n".join(_stats_lines(conn)))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # httpx logs every request at INFO; a backfill makes hundreds.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="python -m app.cli")
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
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except exclusions.ExclusionError as e:
        print(f"exclusions.yaml: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
