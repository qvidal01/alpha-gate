"""
alpha-gate -- a harness for honestly disproving trading strategies.

This package contains NO order-placement code and never will. It cannot trade.
That is a structural property, verified mechanically by scripts/check.sh, not a
policy someone has to remember at 2am.

Pipeline:
    prereg   seal a strategy before any data exists
    capture  collect bars forward-only; refuse backfill
    engine   replay them with real fees and real book slippage
    evaluate judge against buy-and-hold, deflated by the trial count
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
