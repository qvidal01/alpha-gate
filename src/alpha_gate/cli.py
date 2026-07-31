#!/usr/bin/env python3
"""
alpha-gate CLI.

    alpha-gate seal      --strategy ta_aggregate --symbols XRPUSDT ...
    alpha-gate capture   [--interval 1h]
    alpha-gate status
    alpha-gate evaluate  [--strategy ta_aggregate]
    alpha-gate trials

The verb order is the discipline: you cannot evaluate what you did not seal,
and you cannot seal after the data arrived.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from alpha_gate import __version__
from alpha_gate.capture import (INTERVAL_SECONDS, audit_bars, capture_universe,
                                read_bars)
from alpha_gate.costs import fetch_fee_schedule
from alpha_gate.evaluate import MIN_COVERAGE, Verdict, evaluate
from alpha_gate.prereg import (REGISTRY_DIR, SealError, StrategySpec,
                               count_trials, latest_seal, load_seals, seal)
from alpha_gate.strategies import REGISTRY

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_UNKNOWN = 0, 1, 2, 3


def cmd_seal(args: argparse.Namespace) -> int:
    params = json.loads(args.params) if args.params else {}
    spec = StrategySpec(
        strategy_id=args.strategy,
        hypothesis=args.hypothesis,
        params=params,
        universe=args.symbols,
        bar_interval=args.interval,
        initial_equity=args.equity,
        sizing_fraction=args.fraction,
        min_days=args.min_days,
        model_cutoff=args.model_cutoff,
        fee_mode=args.fee_mode,
        slippage_model=args.slippage,
        notes=args.notes or "",
    )
    try:
        s = seal(spec)
    except SealError as exc:
        print(f"seal refused: {exc}", file=sys.stderr)
        return EXIT_FAIL

    print(f"sealed  trial #{s.trial_index}")
    print(f"  file       {s.path()}")
    print(f"  strategy   {spec.strategy_id}  params={params}")
    print(f"  universe   {', '.join(spec.universe)} @ {spec.bar_interval}")
    print(f"  sealed at  {s.sealed_at_utc}")
    print(f"  spec sha   {s.spec_sha256[:16]}")
    print(f"  code sha   {s.code_sha256[:16]}")
    print()
    print("From this moment, only bars captured LATER count. Start the capture")
    print("cron now; the clock on your 90 days begins with the next bar.")
    return EXIT_OK


def cmd_capture(args: argparse.Namespace) -> int:
    symbols = args.symbols or _symbols_from_registry()
    if not symbols:
        print("no symbols: pass --symbols or seal a strategy first", file=sys.stderr)
        return EXIT_USAGE

    results = capture_universe(symbols, args.interval)
    errors = 0
    for sym, status in sorted(results.items()):
        print(f"  {sym:<12} {status}")
        if status.startswith("error"):
            errors += 1
    return EXIT_FAIL if errors == len(results) else EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    seals = load_seals()
    print(f"alpha-gate {__version__}")
    print(f"registry: {count_trials()} trial(s) sealed\n")

    if not seals:
        print("No seals yet. Nothing is under test.")
        return EXIT_OK

    symbols = args.symbols or _symbols_from_registry()
    for sym in sorted(symbols):
        a = audit_bars(sym, args.interval)
        # Coverage, not just gap COUNT. One gap of 300 hours and one gap of one
        # hour both print "gaps=1"; only the percentage distinguishes them, and
        # only the percentage tells you whether a verdict is still reachable.
        cov = a.get("coverage", 1.0)
        flag = "ok" if a["ok"] else "PROBLEM"
        if cov < MIN_COVERAGE:
            flag = "THIN"          # below this, evaluate() refuses a verdict
        print(f"  {sym:<12} {a['bars']:>5} bars  {a['days']:>6.1f} days  "
              f"gaps={a['gaps']:<3} cov={cov:>6.1%}  {flag}")
        if cov < MIN_COVERAGE:
            print(f"      ! {a.get('missing_bars', 0)} of "
                  f"{a.get('expected_bars', 0)} expected bars missing — "
                  f"below {MIN_COVERAGE:.0%}, verdict will be INCONCLUSIVE")
        for p in a.get("problems", []):
            print(f"      ! {p}")
    return EXIT_OK


def cmd_evaluate(args: argparse.Namespace) -> int:
    seals = load_seals()
    if args.strategy:
        seals = [s for s in seals if s.spec["strategy_id"] == args.strategy]
    if not seals:
        print("no matching seals", file=sys.stderr)
        return EXIT_UNKNOWN

    worst = EXIT_OK
    any_pass = False
    for s in seals:
        spec = StrategySpec(**s.spec)
        for sym in spec.universe:
            bars = read_bars(sym, spec.bar_interval)
            if not bars:
                print(f"\n{spec.strategy_id} on {sym}: no data captured yet\n")
                worst = max(worst, EXIT_UNKNOWN)
                continue
            fees = fetch_fee_schedule(sym) if not args.offline else \
                fetch_fee_schedule(sym)
            report = evaluate(s, sym, fees)
            print()
            print(report.summary())
            print()
            if args.json:
                print(json.dumps(report.to_dict(), indent=2, default=str))
            if report.verdict == Verdict.PASS:
                any_pass = True
            elif report.verdict in (Verdict.INVALID, Verdict.FAIL):
                worst = max(worst, EXIT_FAIL)
            else:
                worst = max(worst, EXIT_UNKNOWN)

    if any_pass and worst == EXIT_OK:
        return EXIT_OK
    return worst


def cmd_trials(args: argparse.Namespace) -> int:
    seals = load_seals()
    if not seals:
        print("no trials recorded")
        return EXIT_OK
    print(f"{count_trials()} trial(s) -- ALL of them count toward deflation\n")
    for s in seals:
        spec = s.spec
        mark = " (superseded)" if s.superseded_by else ""
        print(f"  #{s.trial_index:<3} {spec['strategy_id']:<16} "
              f"{s.sealed_at_utc[:19]}  {spec.get('params')}{mark}")
        print(f"       {spec['hypothesis'][:100]}")
    return EXIT_OK


def _symbols_from_registry() -> list[str]:
    out: set[str] = set()
    for s in load_seals():
        out.update(s.spec.get("universe", []))
    return sorted(out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alpha-gate",
        description="Honestly disprove trading strategies. Cannot place orders.",
    )
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seal", help="freeze a strategy before any data exists")
    s.add_argument("--strategy", required=True, choices=sorted(REGISTRY))
    s.add_argument("--hypothesis", required=True,
                   help="falsifiable claim, >=20 chars")
    s.add_argument("--symbols", nargs="+", required=True)
    s.add_argument("--params", help="JSON dict of strategy params")
    s.add_argument("--interval", default="1h", choices=sorted(INTERVAL_SECONDS))
    s.add_argument("--equity", type=float, default=1000.0)
    s.add_argument("--fraction", type=float, default=0.02)
    s.add_argument("--min-days", type=int, default=90)
    s.add_argument("--model-cutoff", default=None,
                   help="YYYY-MM-DD; REQUIRED for LLM strategies")
    s.add_argument("--fee-mode", default="taker", choices=["taker", "maker"])
    s.add_argument("--slippage", default="book_walk",
                   choices=["book_walk", "spread"])
    s.add_argument("--notes", default="")
    s.set_defaults(func=cmd_seal)

    c = sub.add_parser("capture", help="collect one bar per symbol, forward-only")
    c.add_argument("--symbols", nargs="+")
    c.add_argument("--interval", default="1h", choices=sorted(INTERVAL_SECONDS))
    c.set_defaults(func=cmd_capture)

    st = sub.add_parser("status", help="what is under test and how far along")
    st.add_argument("--symbols", nargs="+")
    st.add_argument("--interval", default="1h", choices=sorted(INTERVAL_SECONDS))
    st.set_defaults(func=cmd_status)

    e = sub.add_parser("evaluate", help="render the verdict")
    e.add_argument("--strategy")
    e.add_argument("--json", action="store_true")
    e.add_argument("--offline", action="store_true",
                   help="use cached fees instead of querying the venue")
    e.set_defaults(func=cmd_evaluate)

    t = sub.add_parser("trials", help="every seal ever written")
    t.set_defaults(func=cmd_trials)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return EXIT_UNKNOWN
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
