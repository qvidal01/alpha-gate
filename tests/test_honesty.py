#!/usr/bin/env python3
"""
Tests for the properties that make this repo worth anything.

These are not coverage tests. Each one pins a specific way a trading harness
can lie to its owner, and every one of these lies has shipped in real published
backtests:

  * feeding the strategy data it could not have had     (lookahead)
  * filling at the price you decided on                 (one-bar lookahead)
  * editing the strategy after seeing the result        (broken seal)
  * quietly backfilling history into a "live" run       (broken forward-only)
  * assuming free or frictionless execution             (cost fiction)
  * reporting the best of N attempts as if it were the only one (no deflation)

If any of these fail, the repo's verdicts are worthless and check.sh must not
return 0.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import pytest

from alpha_gate.capture import audit_bars, bars_path
from alpha_gate.costs import FeeSchedule, simulate_fill, walk_book, CostError
from alpha_gate.engine import run
from alpha_gate.evaluate import (Verdict, deflated_sharpe, evaluate,
                                 expected_max_sharpe, noise_floor, norm_ppf,
                                 sharpe)
from alpha_gate.prereg import (SealError, StrategySpec, count_trials, seal,
                               verify_seal)
from alpha_gate.strategies.base import Decision

HOUR = 3600.0
FEES = FeeSchedule("TESTUSDT", maker=0.0, taker=0.001, source="venue")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def make_bar(i: int, close: float, t0: float = 1_800_000_000.0,
             depth: float = 1e6, captured_offset: float = 1.0) -> dict:
    close_t = t0 + i * HOUR
    return {
        "symbol": "TESTUSDT", "interval": "1h",
        "open_time_unix": close_t - HOUR, "close_time_unix": close_t,
        "close_time_utc": f"bar-{i}",
        "o": close, "h": close * 1.01, "l": close * 0.99, "c": close,
        "v": 1000.0,
        "captured_at_unix": close_t + captured_offset,
        "book": {
            "bids": [[close * (1 - 0.0005 * k), depth] for k in range(1, 11)],
            "asks": [[close * (1 + 0.0005 * k), depth] for k in range(1, 11)],
            "captured_at_unix": close_t + captured_offset,
        },
    }


def rising_bars(n: int = 300, start: float = 100.0, step: float = 0.1) -> list[dict]:
    return [make_bar(i, start + i * step) for i in range(n)]


@pytest.fixture
def spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="buy_and_hold",
        hypothesis="a placeholder hypothesis long enough to satisfy validation",
        params={}, universe=["TESTUSDT"], initial_equity=1000.0,
        sizing_fraction=0.10,
    )


# --------------------------------------------------------------------------- #
# 1. Lookahead
# --------------------------------------------------------------------------- #

class SpyStrategy:
    """Records exactly what it was shown, so lookahead becomes detectable."""
    name = "spy"

    def __init__(self, **_):
        self.seen: list[int] = []

    def decide(self, bars):
        self.seen.append(len(bars))
        return Decision(target_weight=0.0)


def test_strategy_never_sees_future_bars(monkeypatch, spec):
    """The engine must hand over a SLICE, never the whole array."""
    import alpha_gate.engine as engine_mod

    spy = SpyStrategy()
    monkeypatch.setattr(engine_mod, "get_strategy", lambda _id, _p: spy)

    bars = rising_bars(50)
    run(spec, "TESTUSDT", bars, FEES)

    # Decision i must see exactly i+1 bars -- never more.
    assert spy.seen == list(range(1, len(bars))), (
        "strategy was shown a different number of bars than it should have "
        "been -- possible lookahead"
    )
    assert max(spy.seen) < len(bars), "strategy saw the final bar it trades into"


def test_decision_fills_on_next_bar_not_its_own(monkeypatch, spec):
    """
    Filling at the deciding bar's close is a one-bar lookahead worth several
    percent a year of pure fiction. The fill price must be bar i+1's.
    """
    import alpha_gate.engine as engine_mod

    class AlwaysLong:
        name = "always"
        def __init__(self, **_): pass
        def decide(self, bars): return Decision(target_weight=1.0)

    monkeypatch.setattr(engine_mod, "get_strategy", lambda _id, _p: AlwaysLong())

    bars = rising_bars(10)
    result = run(spec, "TESTUSDT", bars, FEES)
    first_trade = next(e for e in result.entries if e.traded)

    assert first_trade.price == bars[1]["c"], (
        "first fill did not happen at the NEXT bar's price"
    )
    assert first_trade.price != bars[0]["c"]


# --------------------------------------------------------------------------- #
# 2. The seal
# --------------------------------------------------------------------------- #

def test_seal_detects_edited_spec(tmp_path, spec):
    s = seal(spec, registry_dir=tmp_path)
    assert verify_seal(s).ok

    s.spec["sizing_fraction"] = 0.99  # the after-the-fact tweak
    v = verify_seal(s)
    assert not v.ok
    assert any("spec hash mismatch" in x for x in v.violations)


def test_seal_detects_edited_strategy_code(tmp_path, spec):
    s = seal(spec, registry_dir=tmp_path)
    s.code_sha256 = "0" * 64  # simulates the strategy file having changed
    v = verify_seal(s)
    assert not v.ok
    assert any("code hash mismatch" in x for x in v.violations)


def test_seal_rejects_bars_captured_before_it(tmp_path, spec):
    """The forward-only rule, and the single most important test here."""
    s = seal(spec, registry_dir=tmp_path)

    stale = make_bar(0, 100.0)
    stale["captured_at_unix"] = s.sealed_at_unix - 86400  # captured yesterday

    v = verify_seal(s, [stale])
    assert not v.ok
    assert any("captured BEFORE the seal" in x for x in v.violations)


def test_seal_rejects_data_predating_model_cutoff(tmp_path):
    """LLM leakage: a model cannot be tested on outcomes it may have memorised."""
    spec = StrategySpec(
        strategy_id="buy_and_hold",
        hypothesis="an llm strategy tested on post-cutoff data only, honestly",
        params={}, universe=["TESTUSDT"], model_cutoff="2030-01-01",
    )
    s = seal(spec, registry_dir=tmp_path)

    bar = make_bar(0, 100.0)
    bar["captured_at_unix"] = s.sealed_at_unix + 10
    bar["close_time_unix"] = time.mktime((2029, 6, 1, 0, 0, 0, 0, 0, 0))

    v = verify_seal(s, [bar])
    assert not v.ok
    assert any("model_cutoff" in x for x in v.violations)


def test_seals_are_immutable(tmp_path, spec, monkeypatch):
    """An existing seal file is never overwritten, even byte-identically."""
    import alpha_gate.prereg as prereg_mod
    monkeypatch.setattr(prereg_mod.time, "time", lambda: 1_800_000_000.0)

    first = seal(spec, registry_dir=tmp_path)
    assert first.path(tmp_path).exists()

    with pytest.raises(SealError, match="immutable|already exists"):
        seal(spec, registry_dir=tmp_path)


def test_trial_count_includes_every_seal(tmp_path):
    """Failed and abandoned trials must still count, or deflation is a lie."""
    for i in range(5):
        seal(StrategySpec(
            strategy_id="buy_and_hold",
            hypothesis=f"variant number {i} of a hypothesis, long enough here",
            params={}, universe=["TESTUSDT"],
        ), registry_dir=tmp_path)
    assert count_trials(tmp_path) == 5


def test_same_second_seals_do_not_collide(tmp_path, monkeypatch):
    """
    Two different variants sealed in the same second must both be recorded.
    A collision here would silently drop a trial from the deflation count --
    the exact under-reporting this repo exists to prevent.
    """
    import alpha_gate.prereg as prereg_mod
    monkeypatch.setattr(prereg_mod.time, "time", lambda: 1_800_000_000.0)

    for frac in (0.02, 0.03, 0.04):
        seal(StrategySpec(
            strategy_id="buy_and_hold",
            hypothesis="same second, different sizing, must count separately",
            params={}, universe=["TESTUSDT"], sizing_fraction=frac,
        ), registry_dir=tmp_path)
    assert count_trials(tmp_path) == 3


def test_spec_rejects_unfalsifiable_hypothesis():
    with pytest.raises(SealError, match="falsifiable"):
        StrategySpec(strategy_id="buy_and_hold", hypothesis="good",
                     params={}, universe=["X"]).validate()


def test_spec_rejects_reckless_sizing():
    with pytest.raises(SealError, match="sizing_fraction"):
        StrategySpec(
            strategy_id="buy_and_hold",
            hypothesis="a hypothesis that is long enough to pass validation",
            params={}, universe=["X"], sizing_fraction=0.90,
        ).validate()


def test_spec_rejects_short_windows():
    with pytest.raises(SealError, match="min_days"):
        StrategySpec(
            strategy_id="buy_and_hold",
            hypothesis="a hypothesis that is long enough to pass validation",
            params={}, universe=["X"], min_days=30,
        ).validate()


# --------------------------------------------------------------------------- #
# 3. Costs
# --------------------------------------------------------------------------- #

def test_book_walk_costs_more_on_thin_books():
    """Thin liquidity must cost more. This is the FLR-vs-BTC case."""
    deep = [[100.0 + 0.01 * k, 1000.0] for k in range(20)]
    thin = [[100.0 + 0.01 * k, 0.5] for k in range(20)]

    _, deep_px, _ = walk_book(deep, notional=1000.0)
    _, thin_px, _ = walk_book(thin, notional=1000.0)

    assert thin_px > deep_px, "thin book did not cost more than deep book"


def test_slippage_is_charged_in_both_directions():
    book = {"bids": [[99.0, 100.0]], "asks": [[101.0, 100.0]]}
    buy = simulate_fill("buy", 500.0, book, FEES)
    sell = simulate_fill("sell", 500.0, book, FEES)
    assert buy.slippage_bps > 0, "buying above mid recorded as free"
    assert sell.slippage_bps > 0, "selling below mid recorded as free"
    assert buy.fee_paid > 0 and sell.fee_paid > 0


def test_partial_fill_is_flagged_not_hidden():
    book = {"bids": [[99.0, 0.1]], "asks": [[101.0, 0.1]]}
    fill = simulate_fill("buy", 10_000.0, book, FEES)
    assert fill.partial, "ran out of book depth but did not report a partial fill"


def test_costs_reduce_returns(spec):
    """Sanity: a strategy that trades must end poorer than one that does not,
    on flat prices."""
    flat = [make_bar(i, 100.0) for i in range(60)]

    hold = run(spec, "TESTUSDT", flat, FEES)
    assert hold.total_fees >= 0

    churny = StrategySpec(**{**spec.__dict__, "strategy_id": "random_flat",
                             "params": {"seed": 7}})
    churn = run(churny, "TESTUSDT", flat, FEES)

    assert churn.trades > 0
    assert churn.final_equity < spec.initial_equity, (
        "churning a flat market was free -- costs are not being applied"
    )


def test_engine_never_goes_negative_on_cash(spec):
    bars = rising_bars(120)
    big = StrategySpec(**{**spec.__dict__, "strategy_id": "buy_and_hold",
                          "sizing_fraction": 0.25})
    result = run(big, "TESTUSDT", bars, FEES)
    for e in result.entries:
        assert e.cash >= -1e-6, f"cash went negative ({e.cash}) -- undeclared leverage"


# --------------------------------------------------------------------------- #
# 4. Deflation / multiple testing
# --------------------------------------------------------------------------- #

def test_more_trials_raises_the_bar():
    """The core anti-fishing property."""
    one = expected_max_sharpe(1, 1.0)
    ten = expected_max_sharpe(10, 1.0)
    hundred = expected_max_sharpe(100, 1.0)
    assert one < ten < hundred, (
        "running more variants did not raise the Sharpe you must beat"
    )


def test_same_returns_score_worse_after_more_trials():
    rets = [0.004, -0.001, 0.006, 0.002, -0.002, 0.005, 0.003,
            0.001, -0.0005, 0.004] * 12
    dsr_1, _ = deflated_sharpe(rets, n_trials=1)
    dsr_50, _ = deflated_sharpe(rets, n_trials=50)
    assert dsr_50 < dsr_1, "identical returns scored no worse after 50 trials"


def test_noise_floor_shrinks_with_sample_size():
    small = noise_floor([0.01, -0.01] * 10)
    large = noise_floor([0.01, -0.01] * 500)
    assert large > small  # absolute total return scales with sqrt(T)
    assert noise_floor([0.0]) == math.inf


def test_norm_ppf_roundtrip():
    from alpha_gate.evaluate import norm_cdf
    for p in (0.01, 0.1, 0.5, 0.9, 0.975, 0.99):
        assert abs(norm_cdf(norm_ppf(p)) - p) < 1e-6


def test_sharpe_of_constant_returns_is_zero():
    assert sharpe([0.01] * 50) == 0.0  # no variance -> undefined -> 0, not inf


# --------------------------------------------------------------------------- #
# 5. Verdicts
# --------------------------------------------------------------------------- #

def _write_bars(tmp_path: Path, bars: list[dict]) -> Path:
    d = tmp_path / "bars"
    d.mkdir(parents=True, exist_ok=True)
    p = bars_path("TESTUSDT", "1h", d)
    p.write_text("".join(json.dumps(b) + "\n" for b in bars))
    return d


def test_short_run_is_inconclusive_not_pass(tmp_path):
    """Even a beautiful 3-day result must not read as PASS."""
    spec = StrategySpec(
        strategy_id="buy_and_hold",
        hypothesis="holding beats holding, which is a tautology under test",
        params={}, universe=["TESTUSDT"],
    )
    s = seal(spec, registry_dir=tmp_path / "reg")

    t0 = s.sealed_at_unix + 10
    bars = [make_bar(i, 100.0 + i, t0=t0) for i in range(72)]
    data_dir = _write_bars(tmp_path, bars)

    report = evaluate(s, "TESTUSDT", FEES, bars=bars,
                      data_dir=data_dir, registry_dir=tmp_path / "reg")
    assert report.verdict == Verdict.INCONCLUSIVE
    assert "required days" in " ".join(report.reasons)


def test_broken_seal_yields_invalid_not_a_number(tmp_path):
    spec = StrategySpec(
        strategy_id="buy_and_hold",
        hypothesis="this run will be invalidated by pre-seal data on purpose",
        params={}, universe=["TESTUSDT"],
    )
    s = seal(spec, registry_dir=tmp_path / "reg")

    bars = [make_bar(i, 100.0 + i, t0=s.sealed_at_unix - 500_000)
            for i in range(200)]
    data_dir = _write_bars(tmp_path, bars)

    report = evaluate(s, "TESTUSDT", FEES, bars=bars,
                      data_dir=data_dir, registry_dir=tmp_path / "reg")
    assert report.verdict == Verdict.INVALID
    assert report.seal_violations


def test_random_strategy_does_not_pass(tmp_path):
    """
    The harness self-test. A coin flip paying real costs must never PASS.
    If this ever goes green, the bug is in the engine, not in the coin.
    """
    spec = StrategySpec(
        strategy_id="random_flat",
        hypothesis="a coin flip should not beat buy and hold after real costs",
        params={"seed": 42}, universe=["TESTUSDT"], min_days=90,
    )
    s = seal(spec, registry_dir=tmp_path / "reg")

    t0 = s.sealed_at_unix + 10
    # 120 days of hourly bars with mild noise and a gentle uptrend.
    bars = []
    price = 100.0
    for i in range(120 * 24):
        price *= 1.0 + (0.00008 if i % 3 else -0.00006)
        bars.append(make_bar(i, price, t0=t0))
    data_dir = _write_bars(tmp_path, bars)

    report = evaluate(s, "TESTUSDT", FEES, bars=bars,
                      data_dir=data_dir, registry_dir=tmp_path / "reg")
    assert report.verdict != Verdict.PASS, (
        f"a coin flip PASSED the gate -- the engine is broken.\n{report.summary()}"
    )


def test_audit_detects_rewritten_history(tmp_path):
    bars = rising_bars(20)
    bars[10], bars[11] = bars[11], bars[10]  # someone edited the ledger
    data_dir = _write_bars(tmp_path, bars)
    a = audit_bars("TESTUSDT", "1h", data_dir)
    assert not a["ok"]
    assert any("non-monotonic" in p for p in a["problems"])


def test_audit_detects_fabricated_capture_time(tmp_path):
    bars = rising_bars(20)
    bars[5]["captured_at_unix"] = bars[5]["close_time_unix"] - 10_000
    data_dir = _write_bars(tmp_path, bars)
    a = audit_bars("TESTUSDT", "1h", data_dir)
    assert not a["ok"]
    assert any("fabricated" in p for p in a["problems"])


# --------------------------------------------------------------------------- #
# 6. Structural: this package cannot trade
# --------------------------------------------------------------------------- #

def test_no_order_placement_code_anywhere():
    """
    The guarantee that makes this repo safe to leave running. Kept as a test as
    well as a check.sh gate so it fails in CI, in pytest, and in review.
    """
    import alpha_gate
    root = Path(alpha_gate.__file__).parent

    banned = ["/api/v3/order", "place_order", "create_order", "new_order",
              "submit_order", "X-MEXC-APIKEY", "hmac", "signature="]
    offenders = []
    for py in root.rglob("*.py"):
        text = py.read_text()
        for token in banned:
            if token in text:
                offenders.append(f"{py.name}: {token}")
    assert not offenders, (
        "order-placement capability found in a package that must never have "
        f"it: {offenders}"
    )


def test_deflation_uses_per_observation_units():
    """
    Units regression. Feeding an ANNUALIZED-scale trial variance (V=1.0) to a
    per-observation Sharpe sets the bar ~sqrt(bars_per_year) too high and the
    gate rejects everything, including genuinely good strategies. A gate that
    always says no measures nothing, so this pins the scale.
    """
    from alpha_gate.evaluate import (annualize_sharpe, null_sr_variance,
                                     required_sharpe)

    t = 90 * 24                       # 90 days of hourly bars
    bpy = 365.25 * 24

    assert abs(null_sr_variance(t) - 1.0 / t) < 1e-12

    bar = annualize_sharpe(required_sharpe(t, 1), bpy)
    assert 2.0 < bar < 5.0, (
        f"90-day single-trial bar is {bar:.2f} annualized Sharpe -- outside the "
        "plausible range, which means the DSR units are wrong again"
    )


def test_more_time_lowers_the_bar_more_than_more_variants_raise_it():
    """The incentive this repo is trying to create: wait, do not fish."""
    from alpha_gate.evaluate import annualize_sharpe, required_sharpe
    bpy = 365.25 * 24

    ninety_one_trial = annualize_sharpe(required_sharpe(90 * 24, 1), bpy)
    year_one_trial = annualize_sharpe(required_sharpe(365 * 24, 1), bpy)
    ninety_ten_trials = annualize_sharpe(required_sharpe(90 * 24, 10), bpy)

    assert year_one_trial < ninety_one_trial < ninety_ten_trials
