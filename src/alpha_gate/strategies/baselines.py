#!/usr/bin/env python3
"""
Baselines. The strategies that must be beaten.

BuyAndHold is not decoration -- it is the hurdle, and it is the hurdle that
kills most strategies. "The bot made 12%" means nothing on its own; if holding
the asset made 30% over the same window, the bot destroyed 18% of value while
generating fees. Reporting absolute return without this comparison is the most
common way a losing strategy gets marketed as a winner.

BuyAndHold pays real entry costs too. It is charged the same fee and the same
slippage on its single entry that the active strategy is charged on every one
of its trades, so the comparison is like-for-like rather than rigged in the
baseline's favour.

RandomFlat exists to sanity-check the harness itself: run it and you should get
a result indistinguishable from noise after costs, drifting slightly negative
as turnover eats you. If it ever shows a confident edge, the bug is in the
engine, not in the coin flip.
"""

from __future__ import annotations

import hashlib
from typing import Any

from alpha_gate.strategies.base import Decision


class BuyAndHold:
    """Enter once on the first bar, never leave. The hurdle."""

    name = "buy_and_hold"

    def __init__(self, **_: Any) -> None:
        pass

    def decide(self, bars: list[dict[str, Any]]) -> Decision:
        if not bars:
            return Decision(target_weight=0.0, reasons=["no data"])
        return Decision(target_weight=1.0, confidence=100.0, reasons=["hold"])


class RandomFlat:
    """
    Deterministic pseudo-random long/flat. A harness self-test, not a strategy.

    Seeded from the bar's own close time so a re-run reproduces exactly --
    a "random" baseline that changed between runs would make the engine
    untestable.
    """

    name = "random_flat"

    def __init__(self, seed: int = 1337, p_long: float = 0.5, **_: Any) -> None:
        self.seed = seed
        self.p_long = p_long

    def decide(self, bars: list[dict[str, Any]]) -> Decision:
        if not bars:
            return Decision(target_weight=0.0, reasons=["no data"])
        key = f"{self.seed}:{bars[-1]['close_time_unix']}".encode()
        draw = int(hashlib.sha256(key).hexdigest()[:8], 16) / 0xFFFFFFFF
        long = draw < self.p_long
        return Decision(
            target_weight=1.0 if long else 0.0,
            confidence=50.0,
            reasons=[f"coin flip {draw:.3f} -> {'long' if long else 'flat'}"],
        )


__all__ = ["BuyAndHold", "RandomFlat"]
