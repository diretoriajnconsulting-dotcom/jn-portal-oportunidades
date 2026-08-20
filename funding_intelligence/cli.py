from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

from funding_intelligence.adapters import ADAPTERS
from funding_intelligence.models import utc_now
from funding_intelligence.pipeline import aggregate, validate_catalog


def read_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path: str, value: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def collect(args: argparse.Namespace) -> int:
    adapter_class = ADAPTERS[args.source]
    fixture = args.input_v1 if args.source == "transferegov" else args.fixture
    adapter = adapter_class(fixture=fixture)
    try:
        snapshot = adapter.snapshot()
    except Exception as exc:
        snapshot = {
            "source": {"id": adapter.id, "name": adapter.name, "url": adapter.url},
            "checked_at": adapter.checked_at,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "opportunities": [],
        }
        print(f"::warning::{adapter.name} collection failed: {exc}", file=sys.stderr)
        if args.strict:
            write_json(args.output, snapshot)
            return 1
    write_json(args.output, snapshot)
    print(f"{adapter.id}: {len(snapshot['opportunities'])} opportunities; status={snapshot['status']}")
    return 0


def aggregate_command(args: argparse.Namespace) -> int:
    paths = []
    for pattern in args.inputs:
        paths.extend(glob.glob(pattern))
    if not paths:
        raise ValueError("No source snapshots matched --inputs")
    snapshots = [read_json(path) for path in sorted(set(paths))]
    checked_at = args.generated_at or utc_now()
    present = {snapshot["source"]["id"] for snapshot in snapshots}
    for source in read_json(args.sources)["sources"]:
        if source["id"] in present:
            continue
        snapshots.append({
            "source": {"id": source["id"], "name": source["name"], "url": source["url"]},
            "checked_at": checked_at,
            "status": "error",
            "error": "source snapshot missing from workflow artifacts",
            "opportunities": [],
        })
    previous = read_json(args.previous) if args.previous and Path(args.previous).exists() else None
    portfolios = read_json(args.portfolios)["portfolios"]
    catalog = aggregate(snapshots, previous, portfolios, generated_at=checked_at)
    validate_catalog(catalog, args.schema)
    write_json(args.output, catalog)
    print(f"{args.output}: {catalog['summary']['open']} open / {catalog['summary']['monitored']} monitored; "
          f"{catalog['summary']['stale_sources']} stale sources")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="PONTE Funding Intelligence v2")
    commands = root.add_subparsers(dest="command", required=True)

    collect_parser = commands.add_parser("collect", help="Collect one isolated source snapshot")
    collect_parser.add_argument("--source", required=True, choices=sorted(ADAPTERS))
    collect_parser.add_argument("--output", required=True)
    collect_parser.add_argument("--fixture", help="HTML fixture (tests/development only)")
    collect_parser.add_argument("--input-v1", help="Validated TransfereGov v1 catalog")
    collect_parser.add_argument("--strict", action="store_true", help="Exit nonzero if this source fails")
    collect_parser.set_defaults(handler=collect)

    aggregate_parser = commands.add_parser("aggregate", help="Normalize, diff, match and validate snapshots")
    aggregate_parser.add_argument("--inputs", nargs="+", required=True)
    aggregate_parser.add_argument("--previous")
    aggregate_parser.add_argument("--portfolios", default="config/portfolios.json")
    aggregate_parser.add_argument("--sources", default="config/sources.json")
    aggregate_parser.add_argument("--schema", default="schema/funding-opportunity-2.0.schema.json")
    aggregate_parser.add_argument("--output", required=True)
    aggregate_parser.add_argument("--generated-at")
    aggregate_parser.set_defaults(handler=aggregate_command)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return args.handler(args)
    except Exception as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
