#!/usr/bin/env python3
"""
The paper-trading engine.

Replays captured bars through a sealed strategy, prices every resulting trade
against the order book that was really there at that moment, and writes an
append-only ledger.

Two structural guarantees, both enforced here rather than trusted:

  1. NO LOOKAHEAD. The strategy is handed bars[:i+1] -- a slice ending at the
     bar being decided on. It is physically not given the array, so it cannot
     index forward even by accident. This is the single most common bug in
     hand-rolled backtesters and the reason so many of them print
     extraordinary Sharpes.

  2. DECIDE ON CLOSE, FILL ON NEXT OPEN. A decision made from bar i's close
     cannot be executed at bar i's close -- that price is already history by
     the time you have computed anything. The fill happens against bar i+1's
     book. Filling at the deciding bar's own close is a one-bar lookahead that
     silently hands the strategy the best price in the window, and it is worth
     several percent a year of pure fiction.

This module contains NO order-placement code and never will. There is no API
key path, no signing, no private endpoint. check.sh asserts that mechanically,
so "just flip it live for a bit" is not a thing that can be done here by
accident -- it would require adding the capability first, in a diff, in review.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alpha_gate.costs import FeeSchedule, Fill, simulate_fill, CostError
from alpha_gate.prereg import Seal, StrategySpec
from alpha_gate.strategies import get_strategy

LEDGER_DIR = Path(__file__).resolve().parents[2] / "data" / "ledgers"

# Don't churn the book for dust. Rebalancing on a 0.4% drift generates fees
# without changing exposure -- a real and expensive bug in naive engines.
MIN_REBALANCE_DELTA = 0.05


class EngineError(RuntimeError):
    """Raised when a run cannot proceed honestly."""


@dataclass
class Position:
    qty: float = 0.0
    avg_cost: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.qty > 1e-12


@dataclass
class LedgerEntry:
    """One decision. Written whether or not it produced a trade -- the flat
    days are part of the record, and a ledger of only the trades would hide
    how often the strategy sat out."""
    seq: int
    symbol: str
    close_time_unix: float
    close_time_utc: str
    price: float
    target_weight: float
    prior_weight: float
    confidence: float
    reasons: list[str]
    traded: bool
    side: str | None = None
    fill: dict[str, Any] | None = None
    equity_before: float = 0.0
    equity_after: float = 0.0
    position_qty: float = 0.0
    cash: float = 0.0
    note: str = ""


@dataclass
class RunResult:
    strategy_id: str
    symbol: str
    seal_path: str
    entries: list[LedgerEntry] = field(default_factory=list)
    initial_equity: float = 0.0
    final_equity: float = 0.0
    total_fees: float = 0.0
    total_slippage: float = 0.0
    trades: int = 0
    bars: int = 0
    skipped_bars: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def net_return(self) -> float:
        if self.initial_equity <= 0:
            return 0.0
        return (self.final_equity - self.initial_equity) / self.initial_equity

    @property
    def total_costs(self) -> float:
        return self.total_fees + self.total_slippage


def _mark_to_market(cash: float, pos: Position, price: float) -> float:
    return cash + pos.qty * price


def run(
    spec: StrategySpec,
    symbol: str,
    bars: list[dict[str, Any]],
    fees: FeeSchedule,
    seal_obj: Seal | None = None,
    seal_path: str = "",
) -> RunResult:
    """
    Replay `bars` through the sealed strategy for one symbol.

    `bars` must come from capture.read_bars() -- i.e. forward-only captured
    data whose seal has already been verified by the caller. This function
    does not re-verify; evaluate.py owns that, and doing it in both places
    would let a caller skip it in one and assume it happened in the other.
    """
    strategy = get_strategy(spec.strategy_id, spec.params)

    cash = float(spec.initial_equity)
    pos = Position()
    prior_weight = 0.0
    result = RunResult(
        strategy_id=spec.strategy_id,
        symbol=symbol,
        seal_path=seal_path,
        initial_equity=cash,
        bars=len(bars),
    )

    if len(bars) < 2:
        result.final_equity = cash
        result.warnings.append("fewer than 2 bars: nothing can be executed")
        return result

    seq = 0
    # Stop at len(bars)-1: the final bar has no NEXT bar to fill against, and
    # inventing a fill for it would be exactly the lookahead this avoids.
    for i in range(len(bars) - 1):
        decide_bar = bars[i]
        fill_bar = bars[i + 1]

        # The strategy sees only what had closed at decision time.
        decision = strategy.decide(bars[: i + 1])

        price = float(fill_bar["c"])
        equity_before = _mark_to_market(cash, pos, price)
        delta = decision.target_weight - prior_weight

        entry = LedgerEntry(
            seq=seq,
            symbol=symbol,
            close_time_unix=fill_bar["close_time_unix"],
            close_time_utc=fill_bar.get("close_time_utc", ""),
            price=price,
            target_weight=decision.target_weight,
            prior_weight=prior_weight,
            confidence=decision.confidence,
            reasons=decision.reasons[:6],
            traded=False,
            equity_before=equity_before,
            equity_after=equity_before,
            position_qty=pos.qty,
            cash=cash,
        )

        if abs(delta) < MIN_REBALANCE_DELTA:
            entry.note = "no material change"
            entry.equity_after = equity_before
            result.entries.append(entry)
            seq += 1
            continue

        book = fill_bar.get("book")
        if not book or not book.get("bids") or not book.get("asks"):
            entry.note = "no order book captured for this bar -- trade skipped"
            result.skipped_bars += 1
            result.entries.append(entry)
            seq += 1
            continue

        # Notional is a fixed fraction of CURRENT equity, per the sealed spec.
        budget = equity_before * spec.sizing_fraction
        side = "buy" if delta > 0 else "sell"
        notional = abs(delta) * budget

        if side == "sell":
            # Cannot sell more than held.
            notional = min(notional, pos.qty * price)
        else:
            # Cannot spend more than cash on hand. No leverage, no margin.
            notional = min(notional, cash)

        if notional <= 1e-9:
            entry.note = f"{side} skipped: nothing available to trade"
            entry.equity_after = equity_before
            result.entries.append(entry)
            seq += 1
            continue

        try:
            fill: Fill = simulate_fill(
                side=side, notional=notional, book=book, fees=fees,
                fee_mode=spec.fee_mode, model=spec.slippage_model,
            )
        except CostError as exc:
            entry.note = f"fill failed: {exc}"
            result.skipped_bars += 1
            result.entries.append(entry)
            seq += 1
            continue

        if side == "buy":
            spend = fill.filled_qty * fill.avg_price + fill.fee_paid
            if spend > cash:
                # Scale back rather than go negative. Silently allowing an
                # overdraft is how paper engines fake leverage they never
                # declared.
                scale = cash / spend if spend > 0 else 0.0
                fill = simulate_fill(
                    side=side, notional=notional * scale, book=book, fees=fees,
                    fee_mode=spec.fee_mode, model=spec.slippage_model,
                )
                spend = fill.filled_qty * fill.avg_price + fill.fee_paid
            new_qty = pos.qty + fill.filled_qty
            pos.avg_cost = (
                (pos.avg_cost * pos.qty + fill.avg_price * fill.filled_qty) / new_qty
                if new_qty > 0 else 0.0
            )
            pos.qty = new_qty
            cash -= spend
        else:
            qty = min(fill.filled_qty, pos.qty)
            proceeds = qty * fill.avg_price - fill.fee_paid
            pos.qty -= qty
            cash += proceeds
            if pos.qty <= 1e-12:
                pos.qty, pos.avg_cost = 0.0, 0.0

        result.total_fees += fill.fee_paid
        result.total_slippage += fill.slippage_cost
        result.trades += 1

        equity_after = _mark_to_market(cash, pos, price)
        entry.traded = True
        entry.side = side
        entry.fill = asdict(fill)
        entry.equity_after = equity_after
        entry.position_qty = pos.qty
        entry.cash = cash
        result.entries.append(entry)

        # Track realised weight, not the target: a partial fill means the
        # strategy did not get the exposure it asked for, and pretending it did
        # would suppress the next rebalance.
        prior_weight = (
            (pos.qty * price) / (equity_after * spec.sizing_fraction)
            if equity_after > 0 and spec.sizing_fraction > 0 else 0.0
        )
        prior_weight = max(0.0, min(1.0, prior_weight))
        seq += 1

    result.final_equity = _mark_to_market(cash, pos, float(bars[-1]["c"]))
    return result


def write_ledger(result: RunResult, ledger_dir: Path | None = None) -> Path:
    """Append-only ledger, one JSON line per decision."""
    d = ledger_dir or LEDGER_DIR
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(time.time(), tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = d / f"{result.strategy_id}__{result.symbol}__{stamp}.jsonl"

    with path.open("w") as fh:
        fh.write(json.dumps({
            "_meta": True,
            "strategy_id": result.strategy_id,
            "symbol": result.symbol,
            "seal_path": result.seal_path,
            "initial_equity": result.initial_equity,
            "final_equity": result.final_equity,
            "net_return": result.net_return,
            "total_fees": result.total_fees,
            "total_slippage": result.total_slippage,
            "trades": result.trades,
            "bars": result.bars,
            "skipped_bars": result.skipped_bars,
            "warnings": result.warnings,
            "written_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        }, separators=(",", ":")) + "\n")
        for e in result.entries:
            fh.write(json.dumps(asdict(e), separators=(",", ":")) + "\n")
    return path


__all__ = [
    "run", "write_ledger", "RunResult", "LedgerEntry", "Position",
    "EngineError", "LEDGER_DIR", "MIN_REBALANCE_DELTA",
]
