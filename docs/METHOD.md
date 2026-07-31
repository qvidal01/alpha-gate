# Method — what this implements, and where it departs from the source

Validated 2026-07-31 against a 47-source NotebookLM corpus on López de Prado's
*Advances in Financial Machine Learning* and the Bailey–López de Prado papers.

The point of this document is the **departures**. Anyone can claim to implement
the Deflated Sharpe Ratio; the useful thing to write down is where this
implementation is not the canonical one, and in which direction each difference
errs.

## What matches the source

**The DSR formula itself.** Bailey & López de Prado define

```
DSR = Z[ (SR - SR0) * sqrt(T-1) / sqrt(1 - g3*SR + ((g4-1)/4) * SR^2) ]
```

with `g3` skewness and `g4` non-excess kurtosis. `evaluate.deflated_sharpe`
implements exactly this. The non-normality correction is not decorative: a
strategy that wins small most days and loses catastrophically now and then
(negative skew, fat tails) scores a *lower* DSR for the same headline Sharpe.
That is the correct treatment, because that return profile is the one that
blows up.

**The expected-maximum-Sharpe benchmark.**

```
SR0 = sqrt(V) * [ (1-γ)*Z⁻¹(1 - 1/N) + γ*Z⁻¹(1 - 1/(N*e)) ]
```

γ = Euler–Mascheroni. `evaluate.expected_max_sharpe` implements this.

**Trial counting must include every attempt.** The source is explicit that
researchers must record *all* trials to determine the number of effectively
independent ones, because uncontrolled multiplicity is what manufactures false
discoveries. `prereg.count_trials` reads every seal ever written to `registry/`,
including abandoned and failed ones. Seals are immutable for this reason.

**Standard Sharpe on short, non-normal, autocorrelated returns is inflationary.**
This is precisely why the harness never reports a bare Sharpe as a result.

## Departure 1 — V is the null sampling variance, not the observed trial spread

**Canonical:** `V = Var[{SR_n}]` — the variance of the Sharpe ratios *across the
N trials actually run*.

**Here:** `null_sr_variance(t) = 1/t` — the sampling variance of a single Sharpe
estimate under the null of no skill.

Why: with one sealed trial there is no cross-trial dispersion to observe, and
the registry currently stores seals rather than each trial's realised Sharpe.
Using the null sampling variance answers a well-posed question — "how high would
the best of N pure-noise draws be expected to reach?" — without inventing data.

⚠️ **Direction of the error, which matters once N grows.** Deliberately different
strategy variants will usually disperse *more* than pure noise, so the true
`Var[{SR_n}]` will tend to exceed `1/t`. That makes the true `SR0` higher than
the one computed here — meaning **this implementation becomes progressively too
lenient as the trial count rises.** With N=1 the distinction is academic. It is
not academic at N=10.

**Recommended fix before sealing more trials:** record each trial's realised
per-observation Sharpe alongside its seal, and switch `V` to the empirical
variance across trials once N ≥ 2, keeping `1/t` as the N=1 fallback. Until then,
read a PASS at high trial counts with suspicion.

## Departure 2 — the published required-Sharpe table assumes normality

`required_sharpe()` inverts the DSR threshold, but does so at `skew=0,
kurtosis=3`. The DSR *verdict* uses the real moments; the *advertised bar* does
not.

So the widely-quoted numbers from this repo — annualized Sharpe **3.31** at 90
days and one trial, **6.49** by the tenth variant, **1.65** stretched to a year —
are the **normal-case, best-case** bar. A strategy with negative skew and fat
tails must clear *more* than the table says.

The table is still the most useful thing the repo produces, and the ordering it
demonstrates holds regardless: **time buys more than cleverness.** Just do not
quote it as an exact threshold for a specific strategy.

## Departure 3 — this is a single-path evaluation, and the source says so

This is the most important limitation and the least comfortable one.

López de Prado explicitly criticises *both* walk-forward backtesting **and paper
trading / forward testing** on the same ground: each evaluates only **one
historical path**. A single path is easily overfit and is not representative of
future performance — training or testing across one particular rally or sell-off
sets a model up to fail when the regime changes. His recommended alternative is
**Combinatorial Purged Cross-Validation (CPCV)**, which generates thousands of
train/test combinations to produce many out-of-sample paths.

**alpha-gate is single-path by construction.** Forward-only capture means there
is exactly one realised sequence, and no amount of waiting produces a second.

So why build it this way? Because forward-only buys three things CPCV cannot:

1. **Pre-registration is enforceable.** The spec and strategy hashes are sealed
   before the data exists. Lookahead bias is not "controlled for", it is
   *impossible* — the bars had not happened yet.
2. **Costs are real.** Fees come from the venue's own `exchangeInfo` and
   slippage is computed by walking captured book depth, not assumed.
3. **It cannot be re-run.** The single most common way backtests lie is being
   quietly re-run until they look good. This one physically cannot be.

**These are complements, not substitutes.** The honest framing: alpha-gate can
tell you a strategy is *not* worth capital, with high confidence. A PASS here is
much weaker evidence — it is one path, and the source is clear that one path is
not representative. A PASS should trigger CPCV work on historical data, not a
deployment.

That sentence is already in the code: a passing report ends with *"This is ONE
result on ONE symbol. It is a reason to keep testing, not a reason to deploy
capital."*

## Departure 4 — no feature-importance analysis

The source recommends feature-importance analysis *instead of* backtesting as
the primary research tool: fit a classifier, evaluate generalisation error, then
examine which features actually carried the prediction and whether they survive
regime changes and other asset classes. That builds a theory you can believe,
rather than a curve you can fit.

alpha-gate does none of this. It is a backtest-shaped instrument, however
carefully constrained. It judges an already-specified rule; it does not help you
discover a defensible one. Treat it as the gate at the end of research, not the
research.

## Where the source endorses this shape

Paper trading is described as a mandatory **dress rehearsal** — the way you find
misconfigured alerts, symbol-mapping typos and data-feed hiccups before real
money is exposed. That is a genuine, separate reason to run this even knowing
the single-path limitation, and it is why the sensible use of a small live
allocation is *proving the plumbing*, not testing the edge. The edge question is
answered here, for free, with no capital at risk.

## Capture density (added 2026-07-31)

`elapsed_days` is measured from first bar to last, so an interrupted capture can
satisfy the 90-day gate while carrying a fraction of the information — 200 bars
scattered across 90 days reports `days=90` and looks like a complete 2,160-bar
record.

`audit_bars` now reports `expected_bars`, `missing_bars` and `coverage`, and
`evaluate` returns **INCONCLUSIVE** below `MIN_COVERAGE = 0.90`. INCONCLUSIVE,
not FAIL — the strategy did nothing wrong and nothing was falsified; the record
is simply too thin to judge, and that is recoverable by fixing capture and
waiting.

Holes are never backfilled. Backfilling would destroy the one property the
harness exists to guarantee: that every bar was observed forward, after it
closed. This makes CLAUDE.md's existing "if the gap is large enough to matter,
seal a fresh trial" policy mechanical rather than a judgment call.

## Minimum Track Record Length (added 2026-07-31)

`required_sharpe` answers "how good must it be, given how long it has run".
MinTRL answers the question an operator actually asks at month two: **"it is
running at the Sharpe it is running at — do I keep waiting, or is this
hopeless?"**

```
t >= 1 + (1 - g3*SR + (g4-1)/4 * SR^2) * (Z_threshold / (SR - SR0))^2
```

Returns infinity when `SR <= SR0`, which is both the honest answer and the
common one: a Sharpe that does not exceed the multiple-testing benchmark will
**never** clear the bar by waiting. Learning that at day 30 rather than day 90 is
most of this function's value.

`SR0` itself depends on `t`, so this is a single fixed-point pass computed at the
current `t` rather than an exact solve. `SR0` moves slowly in `t`, so it is a
good approximation — the report labels it an estimate.
