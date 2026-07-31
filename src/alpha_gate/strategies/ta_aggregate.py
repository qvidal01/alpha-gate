#!/usr/bin/env python3
"""
The first defendant: the salvaged RSI + MACD + Bollinger + trend aggregate.

This wraps mexc-trading-bot's TechnicalIndicators.get_aggregated_signal() --
the confidence-weighted 4-indicator vote that repo actually runs -- and exposes
it under the Strategy contract so it can be sealed, run forward-only, charged
real fees and slippage, and compared against buy-and-hold.

It is the right first test precisely because it is already deployed and already
believed. The interesting question is not whether it produces signals; it
plainly does. The question is whether those signals survive costs and clear the
bar set by simply holding the asset -- which nobody has ever checked.

The default min_confidence=65 is taken from the live CAPITAL_ANALYSIS.md config
rather than chosen here, so the thing on trial is the strategy as actually run,
not a version tuned for the occasion.
"""

from __future__ import annotations

from typing import Any

from alpha_gate.strategies.base import Decision, StrategyError, closes
from alpha_gate.strategies._indicators_salvaged import TechnicalIndicators, Signal

# Enough history for the slowest indicator to be meaningful. The salvaged
# defaults use a 200-period long RMA, so anything shorter is warm-up and the
# strategy must stay flat rather than act on a half-formed trend read.
MIN_BARS = 200


class TAAggregate:
    """Confidence-gated long/flat wrapper over the salvaged indicator stack."""

    name = "ta_aggregate"

    def __init__(
        self,
        min_confidence: float = 65.0,
        rsi_period: int = 14,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        bb_period: int = 20,
        bb_std: float = 2.0,
        rma_short: int = 20,
        rma_long: int = 200,
        weights: dict[str, float] | None = None,
        scale_by_confidence: bool = False,
    ) -> None:
        if not (0 <= min_confidence <= 100):
            raise StrategyError(f"min_confidence {min_confidence} outside [0,100]")

        self.min_confidence = min_confidence
        self.scale_by_confidence = scale_by_confidence
        self.weights = weights
        self._ti = TechnicalIndicators(
            rsi_period=rsi_period,
            macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
            bb_period=bb_period, bb_std=bb_std,
            rma_short=rma_short, rma_long=rma_long,
        )
        self._min_bars = max(MIN_BARS, rma_long, macd_slow + macd_signal, bb_period)

    def decide(self, bars: list[dict[str, Any]]) -> Decision:
        if len(bars) < self._min_bars:
            return Decision(
                target_weight=0.0,
                reasons=[f"warm-up: {len(bars)}/{self._min_bars} bars"],
            )

        prices = closes(bars)
        result = self._ti.get_aggregated_signal(
            prices, current_price=prices[-1], weights=self.weights
        )

        bullish = result.action in (Signal.STRONG_BUY, Signal.BUY)
        confident = result.confidence >= self.min_confidence

        if bullish and confident:
            weight = (result.confidence / 100.0) if self.scale_by_confidence else 1.0
        else:
            # Anything that is not a confident buy is flat. No shorting: an
            # untested signal is not something to bet against either.
            weight = 0.0

        return Decision(
            target_weight=weight,
            confidence=float(result.confidence),
            reasons=[f"{result.action.value} @ {result.confidence:.1f}%"]
                    + list(result.reasons),
        )


__all__ = ["TAAggregate", "MIN_BARS"]
