#!/usr/bin/env python3
"""
The Strategy contract.

Deliberately narrow. A strategy receives the bars that have closed SO FAR and
returns a target position. That is all it can do:

  * It cannot see future bars -- engine.py hands it a slice, not the array.
  * It cannot place orders -- it returns a target weight, and the engine prices
    the resulting trade against the real captured book.
  * It cannot size itself -- sizing lives in the sealed spec, not the strategy,
    so a strategy cannot quietly lever up to rescue a weak signal.
  * It cannot hold state across runs except through what it is given, so a
    re-run on the same data produces the same decisions.

Every one of those restrictions removes a way to accidentally cheat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Decision:
    """
    What the strategy wants, expressed as a target -- not as an order.

    target_weight is the fraction of the position budget to hold, in [0, 1]:
        0.0 = flat
        1.0 = fully in (one unit of the sealed sizing_fraction)

    Long-only by design. Shorting and leverage are excluded from v1 because
    they change the risk model completely and a harness that cannot yet
    validate a long-only signal has no business evaluating a levered one.
    """
    target_weight: float
    confidence: float = 0.0          # 0-100, informational only
    reasons: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not (0.0 <= self.target_weight <= 1.0):
            raise ValueError(
                f"target_weight {self.target_weight} outside [0,1]. "
                "Shorting and leverage are not supported in v1."
            )


@runtime_checkable
class Strategy(Protocol):
    """Implement this to get put on trial."""

    name: str

    def decide(self, bars: list[dict[str, Any]]) -> Decision:
        """
        Decide a target position from bars[0..t], where bars[-1] is the most
        recently CLOSED bar.

        Contract:
          * MUST be pure with respect to `bars` -- same input, same output.
          * MUST NOT reach outside `bars` for market data (no network calls for
            prices; that is how lookahead sneaks in).
          * MUST tolerate short histories by returning a flat Decision rather
            than raising -- the first N bars of any run are warm-up.
        """
        ...


class StrategyError(RuntimeError):
    """Raised when a strategy is misconfigured or misbehaves."""


def closes(bars: list[dict[str, Any]]) -> list[float]:
    """Extract closing prices. The one helper every strategy needs."""
    return [float(b["c"]) for b in bars]


__all__ = ["Decision", "Strategy", "StrategyError", "closes"]
