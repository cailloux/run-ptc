"""Command line entry point: python -m app.cli {migrate,import,exclusions}."""

import argparse
import logging
import sys

import httpx

from app import db, exclusions
from app.arcgis import fetch_features
from app.config import load_settings
from app.importer import LAYERS, import_layer


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
    with db.connect() as conn, httpx.Client(timeout=120) as client:
        db.migrate(conn)
        for name in names:
            layer = LAYERS[name]
            features = fetch_features(client, layer.url, layer.oid_field)
            report = import_layer(conn, layer, features, settings, excl)
            print("\n".join(report.lines()))
    return 0


def cmd_exclusions(args) -> int:
    excl = exclusions.load()
    with db.connect() as conn:
        warnings = exclusions.apply(conn, excl)
        excluded = conn.execute("SELECT count(*) FROM segment WHERE excluded").fetchone()[0]
    print(f"{excluded} segments excluded, {len(warnings)} warnings")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending migrations").set_defaults(func=cmd_migrate)
    p = sub.add_parser("import", help="import city layers, regenerate nodes, apply exclusions")
    p.add_argument("--layer", choices=list(LAYERS))
    p.set_defaults(func=cmd_import)
    sub.add_parser("exclusions", help="reapply config/exclusions.yaml").set_defaults(
        func=cmd_exclusions)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except exclusions.ExclusionError as e:
        print(f"exclusions.yaml: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
