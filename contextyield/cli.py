"""Local operator commands; compare never contacts upstreams or claims measured energy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

from .calibration import run_profile, run_quality_eval
from .config import load_settings
from .replay import compare, write_report
from .repo_scan import scan_repository
from .store import ReceiptStore
from .trajectory import compare_cohorts, load_events, summarize


def _emit_json(report: dict, out: Path) -> None:
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if str(out) == "-":
        print(rendered, end="")
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="contextyield")
    sub = parser.add_subparsers(dest="command", required=True)
    compare_cmd = sub.add_parser("compare", help="compare policies on a synthetic full-information fixture")
    compare_cmd.add_argument("--fixture", type=Path, default=Path("fixtures/synthetic-v1.json"))
    compare_cmd.add_argument("--out", type=Path, default=Path("artifacts/compare-v1"))
    reconcile_cmd = sub.add_parser("reconcile", help="mark old pending receipts unknown")
    reconcile_cmd.add_argument("--older-than-s", type=int, default=300)
    profile_cmd = sub.add_parser("profile", help="measure an isolated local NVIDIA inference route")
    profile_cmd.add_argument("--target", required=True)
    profile_cmd.add_argument("--fixture", type=Path, required=True)
    profile_cmd.add_argument("--out", type=Path, required=True)
    evaluate_cmd = sub.add_parser("evaluate", help="run a private exact-substring chat evaluation")
    evaluate_cmd.add_argument("--target", required=True)
    evaluate_cmd.add_argument("--fixture", type=Path, required=True)
    evaluate_cmd.add_argument("--out", type=Path, required=True)
    evaluate_cmd.add_argument("--confirm-live-requests", action="store_true")
    scan_cmd = sub.add_parser("scan-repo", help="inventory AI integration points without executing code")
    scan_cmd.add_argument("--path", type=Path, default=Path("."))
    scan_cmd.add_argument("--out", type=Path, default=Path("-"))
    trace_cmd = sub.add_parser("analyze-trace", help="summarize metadata-only task events")
    trace_cmd.add_argument("--trace", type=Path, required=True)
    trace_cmd.add_argument("--out", type=Path, default=Path("-"))
    paired_cmd = sub.add_parser("compare-traces", help="paired descriptive task comparison")
    paired_cmd.add_argument("--baseline", type=Path, required=True)
    paired_cmd.add_argument("--candidate", type=Path, required=True)
    paired_cmd.add_argument("--out", type=Path, default=Path("-"))
    args = parser.parse_args(argv)
    if args.command == "scan-repo":
        _emit_json(scan_repository(args.path), args.out)
        return
    if args.command == "analyze-trace":
        _emit_json(summarize(load_events(args.trace)), args.out)
        return
    if args.command == "compare-traces":
        report = compare_cohorts(summarize(load_events(args.baseline)),
                                 summarize(load_events(args.candidate)))
        _emit_json(report, args.out)
        return
    settings = load_settings()
    if args.command == "compare":
        result = compare(settings, args.fixture)
        write_report(result, args.out)
        print(f"Synthetic report: {args.out / 'report.md'}")
    elif args.command == "reconcile":
        if args.older_than_s < 60:
            parser.error("--older-than-s must be >= 60")
        store = ReceiptStore(settings.database_url)
        store.open()
        try:
            count = store.reconcile_pending(args.older_than_s)
        finally:
            store.close()
        print(f"Marked {count} old pending receipts unknown")
    elif args.command == "profile":
        route = settings.routes.get(args.target)
        if route is None:
            parser.error(f"unknown target: {args.target}")
        result = run_profile(route, args.fixture, args.out)
        print(f"Physical GPU-device profile: {args.out} ({result['profile']['sample_count']} samples)")
    elif args.command == "evaluate":
        if not args.confirm_live_requests:
            parser.error("evaluate sends live backend requests; pass --confirm-live-requests")
        route = settings.routes.get(args.target)
        if route is None:
            parser.error(f"unknown target: {args.target}")
        with httpx.Client() as client:
            result = run_quality_eval(route, args.fixture,
                                      None if str(args.out) == "-" else args.out, client)
        if str(args.out) == "-":
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(f"Exact-substring evaluation: {args.out} "
                  f"({result['evaluation']['sample_count']} samples; no energy measured)")


if __name__ == "__main__":
    main()
