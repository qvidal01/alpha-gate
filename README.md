# alpha-gate

**A harness for honestly disproving trading strategies.**

This repo cannot trade. It has no order-placement code, no request signing, no
credential path — and `scripts/check.sh` verifies that mechanically on every
run. It is a measuring instrument, and the thing it is built to do is say **no**.

---

## Why this exists

The evidence on aggressive discretionary and automated trading is unusually
strong for a finance question, and it is not encouraging:

- Chague, De-Losso & Giovannetti (2020) tracked **19,646** individuals who began
  day trading Brazilian equity futures via regulator records. Of those who
  persisted more than 300 days, **97% lost money**; 1.1% earned above minimum
  wage. They found **no evidence of learning over time**.
- Barber & Odean (2000): the most-active quintile of households netted ~11.4%/yr
  against ~17.9% for the market. Gross returns tracked the market; **the entire
  gap was turnover cost**.
- EU/FCA-mandated broker disclosures put retail CFD loss rates at **70–80%** —
  published by the brokers, against their own interest.
- Bailey, Borwein, López de Prado & Zhu (2014) proved that with only a few years
  of data, **a few dozen strategy variants is enough to produce an excellent
  Sharpe by pure chance.** Every strategy with a beautiful equity curve is the
  survivor of an unreported number of trials.

Meanwhile the 2026 LLM-agent trading benchmarks are, at best, mixed. The most
rigorous of them — [KTD-Fin](https://arxiv.org/abs/2605.28359), leakage-controlled
across 10 frontier agents on CSI300 — found agent returns were *"largely
explained by passive market and style exposure, with limited evidence of
persistent stock-selection alpha."* They made money when the market went up.

None of that means an edge is impossible. It means **claims about edges are
cheap and evidence is expensive**, and almost every public trading repo, video
and prompt pack sells the first while implying the second.

This harness makes the evidence.

---

## What it actually enforces

Five things, each of them a specific way a trading result normally lies:

### 1. Pre-registration you cannot edit
Seal the strategy *before* any data exists. The seal hashes the spec **and the
strategy source**, timestamps it, and is immutable. Change a threshold after a
bad week and the code hash moves — `check.sh` fails, and every verdict from
that seal is void.

### 2. Forward-only data, backfill refused
`capture.py` will not write a bar more than ~1.5 intervals stale. You cannot
download history into it. Every bar carries a wall-clock `captured_at`, and any
bar predating the seal invalidates the run outright.

If the cron dies for two days you get a **two-day hole**, visible in `status`.
That is deliberate: a hole is information, a backfill is a lie.

### 3. Real costs, from the venue and the book
Fees come from MEXC's own `exchangeInfo`, not a hardcoded guess. Slippage is
computed by **walking real captured order-book depth** for the actual order
size. Measured at seal time:

| Symbol | Spread | Bid depth | Slippage on $5k |
|---|---|---|---|
| BTCUSDT | 0.0 bps | $200,573 | **0.0 bps** |
| ETHUSDT | 0.1 bps | $295,189 | **1.1 bps** |
| XRPUSDT | 0.9 bps | $947,161 | **1.0 bps** |
| FLRUSDT | 8.0 bps | $21,143 | **22.8 bps** |

FLR is in the universe deliberately. It is the case where slippage decides the
outcome, and where a harness that assumed costs away would flatter a strategy
into looking deployable.

### 4. The hurdle is buy-and-hold, not zero
A positive return is not a result. The baseline runs through the **same engine**
over the **same bars**, paying the **same fees and slippage** on its entry. If
the bot made 12% while holding made 30%, the bot destroyed 18% and generated
fees doing it. That comparison is the actual claim every strategy implicitly
makes, and it is the one usually omitted.

### 5. The trial count deflates your Sharpe
This is the part nobody else does. **Every seal in `registry/` counts as a
trial** — including the failures and the abandoned ones. That count feeds the
Deflated Sharpe Ratio (Bailey & López de Prado), which asks: *given that you
ran N variants, how surprising is the best one?*

The bar it produces, in annualized Sharpe, computed by this repo:

| Window | Bars | N=1 | N=5 | N=10 | N=25 | N=100 |
|---|---|---|---|---|---|---|
| **90 days** | 2,160 | **3.31** | 5.72 | 6.49 | 7.34 | 8.41 |
| 180 days | 4,320 | 2.34 | 4.04 | 4.59 | 5.19 | 5.95 |
| 365 days | 8,760 | 1.65 | 2.84 | 3.22 | 3.64 | 4.18 |
| 730 days | 17,520 | 1.16 | 2.01 | 2.28 | 2.58 | 2.95 |

Read that table before you read anything else. **At 90 days and one trial you
need an annualized Sharpe above 3.3.** By your tenth variant it is 6.5. That is
not this repo being harsh — it is a fact about how little information three
months contains, and it is precisely why "my bot did great last quarter" is not
evidence of anything.

It also means the honest move is usually *more time*, not more variants: going
from 90 days to a year drops the bar from 3.31 to 1.65, while running ten
variants in the same 90 days raises it to 6.49. Patience buys you more than
cleverness does.

`evaluate` prints the applicable number as `need Sharpe` next to every verdict,
so the bar is always visible rather than implicit.

---

## Verdicts

| Verdict | Meaning |
|---|---|
| `INVALID` | The seal was broken. The result proves nothing. |
| `INCONCLUSIVE` | Not enough evidence yet — usually not enough time. The default state for the first 90 days. |
| `FAIL` | It ran and did not beat holding the asset, or landed inside the noise band, or failed deflation. |
| `PASS` | Beat buy-and-hold net of real costs, over ≥90 days, by more than the trial count can explain. |

`PASS` is meant to be rare. A `FAIL` costs you nothing but time; a false `PASS`
costs money.

---

## Usage

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"

# 1. Seal — BEFORE any data exists
./.venv/bin/python -m alpha_gate.cli seal \
  --strategy ta_aggregate \
  --symbols BTCUSDT ETHUSDT XRPUSDT FLRUSDT \
  --params '{"min_confidence": 65.0}' \
  --hypothesis "..."          # must be falsifiable, >=20 chars

# 2. Capture — hourly, forever, no backfill
./.venv/bin/python -m alpha_gate.cli capture

# 3. Watch
./.venv/bin/python -m alpha_gate.cli status
./.venv/bin/python -m alpha_gate.cli trials

# 4. Judge (INCONCLUSIVE until day 90)
./.venv/bin/python -m alpha_gate.cli evaluate

# The gate — can this tree's verdicts be trusted?
./scripts/check.sh            # 0 PASS · 1 FAIL · 3 UNKNOWN
```

No API keys. Everything is public market data, so this runs unattended in CI
without a credential ever touching it.

### Scheduling

```
0 * * * *  cd ~/projects/alpha-gate && ./scripts/daily.sh capture
5 13 * * * cd ~/projects/alpha-gate && ./scripts/daily.sh report
```

---

## What is under test right now

**Trial #1**, sealed `2026-07-31T11:39:54Z`:
the RSI + MACD + Bollinger + trend aggregate salvaged verbatim from
`mexc-trading-bot@bb87e03`, at the `min_confidence=65` threshold that repo
actually runs, against BTC / ETH / XRP / FLR.

It is the right first defendant because it is already deployed and already
believed. Whether it produces signals is not in question; whether those signals
survive costs and beat simply holding the asset has never been checked.

Earliest possible verdict: **~2026-10-29**.

---

## What this does NOT model

Stated plainly rather than buried, because every omission below makes real
execution **worse** than what this reports:

- Market impact beyond the visible book — your order moving the price.
- Queue position; a "maker" fill assumes you were filled at all.
- Latency between decision and arrival.
- Adverse selection: the book is thinnest exactly when you most want it.
- Funding rates, borrow costs, and taxes. US short-term gains are taxed as
  ordinary income on every round trip.

**Treat every result here as an optimistic upper bound.** A strategy that fails
this gate would fail harder in production. A strategy that passes it has earned
one more test, not capital.

### Methodological limits — read `docs/METHOD.md`

Validated 2026-07-31 against the Bailey / López de Prado source material. The
formulas match; four departures are documented there with the direction each one
errs. Two you should know before trusting any verdict:

- **This is a SINGLE-PATH evaluation, and the source names that as a flaw.**
  López de Prado criticises walk-forward backtesting *and* forward/paper trading
  on identical grounds: one historical path is easily overfit and is not
  representative. His alternative is Combinatorial Purged Cross-Validation.
  Forward-only buys enforceable pre-registration and impossible lookahead, which
  CPCV cannot — but a PASS here should trigger CPCV work, not a deployment.
- **The deflation grows lenient as trials accumulate.** `V` uses the null
  sampling variance `1/t` rather than the observed spread of Sharpes across
  trials. Real variants disperse more than noise, so the true bar is higher than
  the computed one. Harmless at one trial; not at ten.

The required-Sharpe figures quoted above are also the **normal-case** bar —
`required_sharpe` inverts the threshold at zero skew and kurtosis 3, while the
verdict itself uses the real moments. Negative skew and fat tails require more.

---

## Salvage provenance

Roughly 1,100 LOC came from `mexc-trading-bot@bb87e03`:

| Taken | Why |
|---|---|
| `strategies/_indicators_salvaged.py` | The defendant. Copied **verbatim**; do not edit — that is what the code hash catches. |
| `costs.py` fee lookup | That repo already queried real `makerCommission`/`takerCommission` instead of hardcoding a guess. |
| `notify.py` | Already speaks the AIQSO `/notify` envelope. |
| `scripts/check.sh` shape | Its 0/1/3 gate convention, mutation guard, and dependency rule. |

Deliberately **not** taken: all ~2,500 LOC of order placement, and the tkinter
dashboards. `mexc-trading-bot` stays frozen; nothing here can reach an account.

---

## License / status

Internal AIQSO research tooling. Not investment advice, not a trading system,
and not capable of becoming one without a reviewed diff that adds a capability
this repo was specifically built to lack.
