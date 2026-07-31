#!/usr/bin/env python3
"""
Execution costs: fees from the venue, slippage from the real order book.

Costs are where paper results go to die, and they are also the easiest thing to
quietly under-model. The literature is blunt about it: across 25 years of retail
trading studies the recurring finding is that gross returns roughly track the
market and net returns lose in almost exact proportion to turnover. Barber &
Odean (2000) found the most-active quintile of households netted ~11.4%/yr
against ~17.9% for the market, and the entire gap was cost.

So this module refuses to guess in the optimistic direction:

  * Fees come from the VENUE's own exchangeInfo, not a hardcoded constant.
    (Salvaged from mexc-trading-bot src/buy.py:143 get_fee_info -- that repo
    already did this correctly, which is more than most.)
  * Slippage is computed by walking the captured order book depth for the
    actual order size, not assumed to be zero or a flat guess.
  * The default assumption is TAKER on both sides -- you cross the spread. If
    you want to claim maker fills, you have to say so in the sealed spec, and
    you should expect to be wrong some of the time.

Every number produced here is recorded per-trade in the ledger, so evaluate.py
can report cost as a share of gross P&L. That ratio is usually the whole story.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

MEXC_BASE = "https://api.mexc.com"
FEE_CACHE = Path(__file__).resolve().parents[2] / "data" / "fees.json"

# Used only when the venue reports nothing and no cache exists. Deliberately
# pessimistic: it is better to reject a real edge than to bless a fake one.
FALLBACK_TAKER = 0.0010   # 10 bps
FALLBACK_MAKER = 0.0000
FALLBACK_SLIPPAGE_BPS = 25.0


class CostError(RuntimeError):
    """Raised when costs cannot be established honestly."""


# --------------------------------------------------------------------------- #
# Fees
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FeeSchedule:
    symbol: str
    maker: float
    taker: float
    source: str  # "venue" | "cache" | "fallback"

    def rate(self, mode: str) -> float:
        return self.maker if mode == "maker" else self.taker


def _http_json(url: str, timeout: int = 15) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "alpha-gate/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_fee_schedule(symbol: str, timeout: int = 15) -> FeeSchedule:
    """
    Pull the real commission rates for a symbol from MEXC exchangeInfo.

    MEXC reports these per-symbol as makerCommission / takerCommission. They are
    account-tier independent at this endpoint, so this is the public floor --
    your actual fees can be worse, never better, which is the right direction
    for a test that is trying to disprove something.
    """
    url = f"{MEXC_BASE}/api/v3/exchangeInfo?symbol={symbol}"
    try:
        data = _http_json(url, timeout=timeout)
        for s in data.get("symbols", []):
            if s.get("symbol") != symbol:
                continue
            maker = s.get("makerCommission")
            taker = s.get("takerCommission")
            if maker is None and taker is None:
                break
            sched = FeeSchedule(
                symbol=symbol,
                maker=float(maker if maker is not None else FALLBACK_MAKER),
                taker=float(taker if taker is not None else FALLBACK_TAKER),
                source="venue",
            )
            _cache_fee(sched)
            return sched
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
        pass

    cached = _load_cached_fee(symbol)
    if cached:
        return cached

    return FeeSchedule(symbol, FALLBACK_MAKER, FALLBACK_TAKER, "fallback")


def _cache_fee(sched: FeeSchedule) -> None:
    FEE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    blob = {}
    if FEE_CACHE.exists():
        try:
            blob = json.loads(FEE_CACHE.read_text())
        except ValueError:
            blob = {}
    blob[sched.symbol] = {"maker": sched.maker, "taker": sched.taker}
    FEE_CACHE.write_text(json.dumps(blob, indent=2, sort_keys=True) + "\n")


def _load_cached_fee(symbol: str) -> FeeSchedule | None:
    if not FEE_CACHE.exists():
        return None
    try:
        blob = json.loads(FEE_CACHE.read_text())
    except ValueError:
        return None
    entry = blob.get(symbol)
    if not entry:
        return None
    return FeeSchedule(symbol, float(entry["maker"]), float(entry["taker"]), "cache")


# --------------------------------------------------------------------------- #
# Slippage
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Fill:
    """The result of pretending to execute. Every field is reported, not netted
    away, so the post-mortem can attribute a loss to the right cause."""
    side: str              # "buy" | "sell"
    requested_notional: float
    filled_qty: float
    avg_price: float
    reference_price: float  # mid at decision time
    slippage_bps: float
    fee_rate: float
    fee_paid: float
    slippage_cost: float
    partial: bool          # book depth ran out before the order filled
    model: str

    @property
    def total_cost(self) -> float:
        return self.fee_paid + self.slippage_cost


def _mid(book: dict) -> float:
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        raise CostError("order book has no top-of-book; cannot price a fill")
    return (float(bids[0][0]) + float(asks[0][0])) / 2.0


def walk_book(levels: list[list], notional: float) -> tuple[float, float, bool]:
    """
    Consume real depth until the notional is filled.

    Returns (filled_qty, avg_price, partial).

    This is the part that separates liquid from illiquid honestly. On a deep
    BTCUSDT book a $20 order barely moves off the touch. On a thin pair it eats
    several levels, and the difference shows up here as real basis points
    instead of being assumed away.
    """
    if not levels:
        raise CostError("empty book side; cannot walk depth")

    remaining = Decimal(str(notional))
    total_qty = Decimal(0)
    total_cost = Decimal(0)

    for level in levels:
        price = Decimal(str(level[0]))
        qty = Decimal(str(level[1]))
        if price <= 0 or qty <= 0:
            continue
        level_notional = price * qty
        if level_notional >= remaining:
            take_qty = remaining / price
            total_qty += take_qty
            total_cost += remaining
            remaining = Decimal(0)
            break
        total_qty += qty
        total_cost += level_notional
        remaining -= level_notional

    if total_qty == 0:
        raise CostError("book depth insufficient to fill any quantity")

    avg_price = float(total_cost / total_qty)
    return float(total_qty), avg_price, remaining > 0


def simulate_fill(
    side: str,
    notional: float,
    book: dict,
    fees: FeeSchedule,
    fee_mode: str = "taker",
    model: str = "book_walk",
) -> Fill:
    """
    Price a paper order against the book that was actually captured at that bar.

    NOTE ON WHAT THIS DOES NOT MODEL, stated plainly rather than buried:
      * Market impact beyond the visible book (your order moving the market).
      * Queue position -- a 'maker' fill assumes you got filled at all.
      * Latency between the decision and the order arriving.
      * Adverse selection: the book is thinnest exactly when you most want it.
    All four biases point the same way -- they make real execution WORSE than
    what this reports. Treat every result here as an optimistic upper bound.
    """
    if side not in ("buy", "sell"):
        raise CostError(f"side must be buy|sell, got {side}")
    if notional <= 0:
        raise CostError(f"notional must be positive, got {notional}")

    mid = _mid(book)
    levels = book["asks"] if side == "buy" else book["bids"]

    if model == "spread":
        # Top-of-book only: cross the spread, assume infinite depth there.
        touch = float(levels[0][0])
        qty, avg_price, partial = notional / touch, touch, False
    else:
        qty, avg_price, partial = walk_book(levels, notional)

    # Slippage is signed against you in both directions: paying above mid to
    # buy, receiving below mid to sell.
    if side == "buy":
        slip_bps = (avg_price - mid) / mid * 10_000.0
    else:
        slip_bps = (mid - avg_price) / mid * 10_000.0

    filled_notional = qty * avg_price
    fee_rate = fees.rate(fee_mode)
    fee_paid = filled_notional * fee_rate
    slippage_cost = abs(slip_bps) / 10_000.0 * filled_notional

    return Fill(
        side=side,
        requested_notional=notional,
        filled_qty=qty,
        avg_price=avg_price,
        reference_price=mid,
        slippage_bps=slip_bps,
        fee_rate=fee_rate,
        fee_paid=fee_paid,
        slippage_cost=slippage_cost,
        partial=partial,
        model=model,
    )


__all__ = [
    "FeeSchedule", "Fill", "CostError",
    "fetch_fee_schedule", "simulate_fill", "walk_book",
    "FALLBACK_TAKER", "FALLBACK_MAKER",
]
