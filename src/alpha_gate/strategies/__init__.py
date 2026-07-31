#!/usr/bin/env python3
"""
Strategy registry.

Registration is explicit and by name because prereg.py hashes the SOURCE FILES
behind each registered id. A strategy that could be swapped at runtime -- via a
plugin path, an env var, a dynamic import -- would make that hash meaningless,
and with it the whole seal. So the mapping is a literal dict in version control,
and source_files_for() knows exactly which files back each entry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from alpha_gate.strategies.base import Decision, Strategy, StrategyError, closes
from alpha_gate.strategies.baselines import BuyAndHold, RandomFlat
from alpha_gate.strategies.ta_aggregate import TAAggregate

_HERE = Path(__file__).resolve().parent

REGISTRY: dict[str, Callable[..., Any]] = {
    "ta_aggregate": TAAggregate,
    "buy_and_hold": BuyAndHold,
    "random_flat": RandomFlat,
}

# Files whose contents define each strategy's behaviour. Getting this wrong
# would let an edit slip past the seal, so it is declared rather than inferred:
# ta_aggregate's behaviour lives mostly in the salvaged indicator file, and any
# change there must invalidate the seal even though ta_aggregate.py is untouched.
_SOURCES: dict[str, list[str]] = {
    "ta_aggregate": ["ta_aggregate.py", "_indicators_salvaged.py", "base.py"],
    "buy_and_hold": ["baselines.py", "base.py"],
    "random_flat": ["baselines.py", "base.py"],
}


def get_strategy(strategy_id: str, params: dict[str, Any] | None = None) -> Any:
    if strategy_id not in REGISTRY:
        raise StrategyError(
            f"unknown strategy '{strategy_id}'. Registered: "
            f"{', '.join(sorted(REGISTRY))}"
        )
    try:
        return REGISTRY[strategy_id](**(params or {}))
    except TypeError as exc:
        raise StrategyError(
            f"cannot construct '{strategy_id}' with params {params}: {exc}"
        ) from exc


def source_files_for(strategy_id: str) -> list[Path]:
    if strategy_id not in _SOURCES:
        raise StrategyError(f"no source manifest for strategy '{strategy_id}'")
    paths = [_HERE / name for name in _SOURCES[strategy_id]]
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise StrategyError(
            f"source manifest for '{strategy_id}' names missing files: "
            f"{[p.name for p in missing]}"
        )
    return paths


__all__ = [
    "REGISTRY", "get_strategy", "source_files_for",
    "Decision", "Strategy", "StrategyError", "closes",
    "TAAggregate", "BuyAndHold", "RandomFlat",
]
