#!/usr/bin/env python3
"""
The verdict. This module's job is to say NO.

Four outcomes, and only one of them is good news:

  INVALID       the seal was broken -- the result cannot mean anything
  INCONCLUSIVE  not enough evidence yet (usually: not enough time)
  FAIL          it ran, and it did not beat holding the asset
  PASS          it beat holding, net of real costs, by more than the number of
                trials can explain

PASS is deliberately hard, and it is hard in three independent ways:

1. THE HURDLE IS BUY-AND-HOLD, NOT ZERO.
   A positive return is not a result. The baseline is run through the same
   engine, over the same bars, paying the same entry fee and the same book
   slippage. Beating cash is trivial in a bull market; beating the asset you
   are trading is the actual claim every trading strategy implicitly makes.

2. THE SHARPE IS DEFLATED BY THE NUMBER OF TRIALS.
   This is the correction almost nobody applies, and it is the reason so many
   published strategies evaporate. Bailey & Lopez de Prado's Deflated Sharpe
   Ratio asks: given that you ran N variants, how surprising is the best one?
   With enough attempts a Sharpe of 1.5 is the EXPECTED outcome of pure luck.
   The trial count comes from registry/ -- every seal ever written, including
   the failures and the abandoned ones. Fishing is therefore self-penalising:
   the twentieth variant has to clear a visibly higher bar than the first, and
   the registry is the thing that remembers.

3. THERE IS A MINIMUM TIME, AND IT IS NOT NEGOTIABLE.
   Below spec.min_days (floor: 90) the verdict is INCONCLUSIVE no matter how
   good the numbers look. Short windows cannot separate a real edge from a
   lucky fortnight at any plausible effect size.

Failing here costs you nothing but time. Passing a rigged test costs money.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from alpha_gate.capture import audit_bars, read_bars
from alpha_gate.costs import FeeSchedule
from alpha_gate.engine import RunResult, run
from alpha_gate.prereg import Seal, StrategySpec, count_trials, verify_seal

EULER_MASCHERONI = 0.5772156649015329

# Minimum probability that the result is not a fluke, after accounting for how
# many things were tried. 0.95 is the conventional bar; it is already generous
# for a domain where the prior on "I found real alpha" should be very low.
DSR_THRESHOLD = 0.95

# Below this, per-trade noise dominates and the Sharpe estimate is not stable.
MIN_TRADES_FOR_VERDICT = 10


class Verdict:
    INVALID = "INVALID"
    INCONCLUSIVE = "INCONCLUSIVE"
    FAIL = "FAIL"
    PASS = "PASS"


# --------------------------------------------------------------------------- #
# Statistics (no scipy dependency -- this must run anywhere check.sh runs)
# --------------------------------------------------------------------------- #

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse normal CDF (Acklam's rational approximation, ~1e-9 accurate)."""
    if not (0.0 < p < 1.0):
        raise ValueError(f"norm_ppf domain is (0,1), got {p}")

    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00]

    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def _moments(xs: list[float]) -> tuple[float, float, float, float]:
    """mean, stdev (sample), skew, kurtosis (non-excess)."""
    n = len(xs)
    if n < 2:
        return (xs[0] if xs else 0.0), 0.0, 0.0, 3.0
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return mean, 0.0, 0.0, 3.0
    m3 = sum((x - mean) ** 3 for x in xs) / n
    m4 = sum((x - mean) ** 4 for x in xs) / n
    pop_sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / n)
    return mean, sd, m3 / pop_sd ** 3, m4 / pop_sd ** 4


def sharpe(returns: list[float]) -> float:
    """Per-observation Sharpe. Not annualized -- DSR needs the raw frequency."""
    if len(returns) < 2:
        return 0.0
    mean, sd, _, _ = _moments(returns)
    return mean / sd if sd > 0 else 0.0


def annualize_sharpe(sr: float, bars_per_year: float) -> float:
    return sr * math.sqrt(bars_per_year)


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """
    E[max Sharpe] across N independent trials of zero true skill.

    This is the number that makes multiple testing concrete. Run enough
    variants and an impressive Sharpe stops being evidence and becomes an
    arithmetic certainty -- this quantifies exactly how impressive it has to
    be before it means anything.
    """
    if n_trials < 2 or sr_variance <= 0:
        return 0.0
    n = float(n_trials)
    term = ((1 - EULER_MASCHERONI) * norm_ppf(1 - 1.0 / n)
            + EULER_MASCHERONI * norm_ppf(1 - 1.0 / (n * math.e)))
    return math.sqrt(sr_variance) * term


def null_sr_variance(t: int) -> float:
    """
    Variance of the per-observation Sharpe ESTIMATE under the null of no skill.

    Units matter here and getting them wrong breaks the test in whichever
    direction you got it wrong. Bailey & Lopez de Prado's DSR is defined on
    per-observation Sharpes, so the trial spread V must be per-observation too.
    The familiar V=1.0 default belongs to ANNUALIZED Sharpes; feeding it a
    per-bar Sharpe sets a benchmark roughly sqrt(bars_per_year) too high and
    the gate rejects everything, including strategies that are genuinely good.
    A gate that always says no is not measuring anything.

    Under the null, the standard error of a Sharpe estimate over t observations
    is approximately sqrt((1 + SR^2/2)/t), which at SR~0 is 1/sqrt(t). So the
    variance of the estimate is ~1/t.
    """
    return 1.0 / t if t > 0 else 1.0


def deflated_sharpe(returns: list[float], n_trials: int,
                    sr_variance: float | None = None) -> tuple[float, float]:
    """
    Deflated Sharpe Ratio (Bailey & Lopez de Prado).

    Returns (dsr_probability, benchmark_sr), both per-observation. The
    probability answers: "given N trials and the non-normality of these
    returns, what is the chance the true Sharpe is above the benchmark rather
    than this being the luckiest of N draws?"

    Non-normality is handled, and matters: a strategy that wins small most days
    and loses catastrophically occasionally (negative skew, fat tails) gets a
    LOWER DSR for the same headline Sharpe. That is correct -- that return
    profile is exactly the one that blows up.
    """
    t = len(returns)
    if t < 3:
        return 0.0, 0.0

    sr = sharpe(returns)
    _, _, skew, kurt = _moments(returns)

    v = sr_variance if sr_variance is not None else null_sr_variance(t)
    sr0 = expected_max_sharpe(n_trials, v)

    denom_sq = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr
    if denom_sq <= 0:
        return 0.0, sr0
    z = (sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom_sq)
    return norm_cdf(z), sr0


def required_sharpe(t: int, n_trials: int, threshold: float = 0.95) -> float:
    """
    The per-observation Sharpe needed to clear the DSR threshold.

    Reported alongside every verdict so the bar is VISIBLE rather than implicit.
    Seeing that 90 days of hourly bars demands an annualized Sharpe above ~3 is
    itself the most useful output this repo produces: it is a fact about how
    little information three months contains, and it is why "my bot did well
    last month" is not evidence of anything.
    """
    if t < 3:
        return float("inf")
    sr0 = expected_max_sharpe(n_trials, null_sr_variance(t))
    return sr0 + norm_ppf(threshold) / math.sqrt(t - 1)


def max_drawdown(equity: list[float]) -> float:
    if not equity:
        return 0.0
    peak, mdd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    return mdd


def noise_floor(returns: list[float], confidence: float = 0.95) -> float:
    """
    The smallest total return distinguishable from zero at this sample size.

    Report this next to the result and a lot of "profitable" strategies stop
    looking profitable: if a bot returns 3% over 90 days and the noise floor is
    8%, the 3% is not a small edge, it is indistinguishable from nothing.
    """
    t = len(returns)
    if t < 2:
        return float("inf")
    _, sd, _, _ = _moments(returns)
    z = norm_ppf(confidence)
    return z * sd * math.sqrt(t)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

@dataclass
class Report:
    verdict: str
    strategy_id: str
    symbol: str
    reasons: list[str] = field(default_factory=list)

    elapsed_days: float = 0.0
    required_days: int = 90
    bars: int = 0
    trades: int = 0

    net_return: float = 0.0
    baseline_return: float = 0.0
    excess_return: float = 0.0

    sharpe_annual: float = 0.0
    baseline_sharpe_annual: float = 0.0
    max_drawdown: float = 0.0

    total_fees: float = 0.0
    total_slippage: float = 0.0
    total_costs: float = 0.0
    cost_share_of_gross: float | None = None

    n_trials: int = 0
    dsr: float = 0.0
    dsr_threshold: float = DSR_THRESHOLD
    benchmark_sharpe: float = 0.0
    required_sharpe_annual: float = 0.0
    noise_floor_return: float = 0.0

    seal_violations: list[str] = field(default_factory=list)
    data_problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        icon = {"PASS": "PASS", "FAIL": "FAIL",
                "INCONCLUSIVE": "INCONCLUSIVE", "INVALID": "INVALID"}[self.verdict]
        lines = [
            f"{icon}  {self.strategy_id} on {self.symbol}",
            "",
            f"  elapsed        {self.elapsed_days:.1f} days "
            f"(need {self.required_days})",
            f"  bars / trades  {self.bars} / {self.trades}",
            "",
            f"  net return     {self.net_return:+.2%}",
            f"  buy & hold     {self.baseline_return:+.2%}",
            f"  excess         {self.excess_return:+.2%}   <- the actual claim",
            "",
            f"  Sharpe (ann.)  {self.sharpe_annual:.2f} "
            f"(baseline {self.baseline_sharpe_annual:.2f})",
            f"  max drawdown   {self.max_drawdown:.2%}",
            f"  noise floor    +/-{self.noise_floor_return:.2%} "
            f"<- results inside this band are not results",
            "",
            f"  fees paid      {self.total_fees:.4f}",
            f"  slippage paid  {self.total_slippage:.4f}",
            f"  total costs    {self.total_costs:.4f}",
        ]
        if self.cost_share_of_gross is not None:
            lines.append(
                f"  costs / gross  {self.cost_share_of_gross:.1%} "
                "<- share of gross P&L eaten by execution"
            )
        lines += [
            "",
            f"  trials to date {self.n_trials}  (every seal in registry/)",
            f"  need Sharpe    {self.required_sharpe_annual:.2f} annualized "
            "<- the bar, given this trial count and sample size",
            f"  deflated SR    {self.dsr:.3f} (need >= {self.dsr_threshold})",
        ]
        if self.reasons:
            lines += ["", "  why:"] + [f"    - {r}" for r in self.reasons]
        return "\n".join(lines)


def _bars_per_year(interval: str) -> float:
    from alpha_gate.capture import INTERVAL_SECONDS
    return 365.25 * 86400.0 / INTERVAL_SECONDS[interval]


def _equity_curve(result: RunResult) -> list[float]:
    return [e.equity_after for e in result.entries] or [result.initial_equity]


def _returns(equity: list[float]) -> list[float]:
    out = []
    for prev, cur in zip(equity, equity[1:]):
        if prev > 0:
            out.append((cur - prev) / prev)
    return out


def evaluate(
    s: Seal,
    symbol: str,
    fees: FeeSchedule,
    bars: list[dict[str, Any]] | None = None,
    data_dir: Path | None = None,
    registry_dir: Path | None = None,
) -> Report:
    """Run the sealed strategy and its baseline, then judge."""
    spec = StrategySpec(**s.spec)
    if bars is None:
        bars = read_bars(symbol, spec.bar_interval, data_dir)

    report = Report(
        verdict=Verdict.INVALID,
        strategy_id=spec.strategy_id,
        symbol=symbol,
        required_days=spec.min_days,
        bars=len(bars),
        n_trials=count_trials(registry_dir),
    )

    # --- Gate 1: the seal ---------------------------------------------------
    sv = verify_seal(s, bars)
    if not sv.ok:
        report.seal_violations = sv.violations
        report.reasons = ["seal violated -- this run proves nothing"] + sv.violations
        return report

    # --- Gate 2: the data ---------------------------------------------------
    audit = audit_bars(symbol, spec.bar_interval, data_dir) if bars else {
        "problems": ["no bars"], "days": 0.0}
    report.data_problems = audit.get("problems", [])
    if report.data_problems:
        report.reasons = ["captured data failed integrity audit"] + report.data_problems
        return report

    report.elapsed_days = audit.get("days", 0.0)

    if len(bars) < 3:
        report.verdict = Verdict.INCONCLUSIVE
        report.reasons = [f"only {len(bars)} bars captured"]
        return report

    # --- Run strategy and baseline through the SAME engine ------------------
    strat_result = run(spec, symbol, bars, fees, s)

    baseline_spec = StrategySpec(**{**s.spec, "strategy_id": "buy_and_hold",
                                    "params": {}})
    base_result = run(baseline_spec, symbol, bars, fees, s)

    strat_eq = _equity_curve(strat_result)
    base_eq = _equity_curve(base_result)
    strat_rets = _returns(strat_eq)
    base_rets = _returns(base_eq)

    bpy = _bars_per_year(spec.bar_interval)

    report.trades = strat_result.trades
    report.net_return = strat_result.net_return
    report.baseline_return = base_result.net_return
    report.excess_return = report.net_return - report.baseline_return
    report.sharpe_annual = annualize_sharpe(sharpe(strat_rets), bpy)
    report.baseline_sharpe_annual = annualize_sharpe(sharpe(base_rets), bpy)
    report.max_drawdown = max_drawdown(strat_eq)
    report.total_fees = strat_result.total_fees
    report.total_slippage = strat_result.total_slippage
    report.total_costs = strat_result.total_costs
    report.noise_floor_return = noise_floor(strat_rets)

    gross = (strat_result.final_equity - strat_result.initial_equity
             + strat_result.total_costs)
    if abs(gross) > 1e-9:
        report.cost_share_of_gross = strat_result.total_costs / abs(gross)

    dsr, sr0 = deflated_sharpe(strat_rets, max(report.n_trials, 1))
    report.dsr = dsr
    report.benchmark_sharpe = sr0
    report.required_sharpe_annual = annualize_sharpe(
        required_sharpe(len(strat_rets), max(report.n_trials, 1)), bpy)

    # --- Gate 3: enough time? ----------------------------------------------
    if report.elapsed_days < spec.min_days:
        report.verdict = Verdict.INCONCLUSIVE
        report.reasons = [
            f"only {report.elapsed_days:.1f} of {spec.min_days} required days. "
            "Numbers above are provisional and must not be acted on."
        ]
        return report

    if report.trades < MIN_TRADES_FOR_VERDICT:
        report.verdict = Verdict.INCONCLUSIVE
        report.reasons = [
            f"only {report.trades} trades (need {MIN_TRADES_FOR_VERDICT}); "
            "too few to estimate a stable Sharpe"
        ]
        return report

    # --- Gate 4: the actual test -------------------------------------------
    reasons: list[str] = []
    failed = False

    if report.excess_return <= 0:
        failed = True
        reasons.append(
            f"did not beat buy-and-hold: {report.net_return:+.2%} vs "
            f"{report.baseline_return:+.2%} ({report.excess_return:+.2%} excess). "
            "Holding the asset would have done better after costs."
        )

    if abs(report.net_return) < report.noise_floor_return:
        failed = True
        reasons.append(
            f"return {report.net_return:+.2%} is inside the +/-"
            f"{report.noise_floor_return:.2%} noise band at this sample size -- "
            "indistinguishable from chance."
        )

    if report.dsr < DSR_THRESHOLD:
        failed = True
        reasons.append(
            f"deflated Sharpe {report.dsr:.3f} < {DSR_THRESHOLD}. Across "
            f"{report.n_trials} registered trial(s) and {len(strat_rets)} "
            f"observations, clearing this needs about "
            f"{report.required_sharpe_annual:.2f} annualized; this run made "
            f"{report.sharpe_annual:.2f}."
        )

    if failed:
        report.verdict = Verdict.FAIL
        report.reasons = reasons
        return report

    report.verdict = Verdict.PASS
    report.reasons = [
        f"beat buy-and-hold by {report.excess_return:+.2%} net of "
        f"{report.total_costs:.4f} in real fees and slippage",
        f"survived deflation across {report.n_trials} trial(s): "
        f"DSR {report.dsr:.3f}",
        f"ran {report.elapsed_days:.1f} days forward-only from a sealed spec",
        "This is ONE result on ONE symbol. It is a reason to keep testing, "
        "not a reason to deploy capital.",
    ]
    return report


__all__ = [
    "Verdict", "Report", "evaluate",
    "sharpe", "annualize_sharpe", "deflated_sharpe", "expected_max_sharpe",
    "max_drawdown", "noise_floor", "norm_cdf", "norm_ppf",
    "required_sharpe", "null_sr_variance",
    "DSR_THRESHOLD", "MIN_TRADES_FOR_VERDICT",
]
