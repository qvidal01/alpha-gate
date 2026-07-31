# alpha-gate — working notes

> **To make this auto-load in Claude Code sessions**, promote it to the repo
> root as `CLAUDE.md`:
> `touch ~/.claude/.claude-md-edit-pass && mv docs/CONVENTIONS.md CLAUDE.md`
> (left here because the protect-claude-md hook blocks agent-written CLAUDE.md
> files, and self-granting the pass is not allowed.)

A harness for honestly disproving trading strategies. Read `README.md` first;
this file is the operating discipline for anyone (human or agent) editing it.

## The one rule

**This repo must never gain the ability to place an order.**

No order endpoints, no request signing, no API keys, no credential storage.
`scripts/check.sh` asserts this mechanically and `tests/test_honesty.py::
test_no_order_placement_code_anywhere` asserts it again in CI. If a task seems
to require adding it, the answer is that the task belongs in a different repo.

This is why `creds.py` was salvaged from `mexc-trading-bot` and then deleted: a
harness that cannot trade has no business storing exchange keys, and latent
capability is how "let me just flip it live for a week" happens at 2am.

## The verify command

```bash
./scripts/check.sh     # 0 PASS · 1 FAIL · 3 UNKNOWN
```

3 is never a pass. Follows the house gate convention (dependency resolution
first, mutation guard, no silent success).

## Things that will bite you

**Do not edit `strategies/_indicators_salvaged.py`.** It is the defendant,
copied verbatim from `mexc-trading-bot@bb87e03`. Its hash is recorded in every
seal that references it; editing it invalidates every running trial. If you
want different behaviour, add a new strategy module and seal a new trial.
(Verified: tampering with it flips `check.sh` to FAIL with a code-hash
mismatch.)

**Do not edit anything in `registry/`.** Seals are immutable by design. A seal
you regret is still a trial, and it still counts toward deflation. That is the
mechanism, not a bug.

**Do not backfill `data/bars/`.** `capture.py` refuses bars more than ~1.5
intervals stale. If there is a gap, the honest response is to let `status` show
the gap — or, if it is large enough to matter, seal a fresh trial and restart
the clock. Never paper over it.

**MEXC kline intervals are not Binance's.** Hourly is `60m`; `1h` returns HTTP
400. Verified live 2026-07-31. Canonical names stay `1h` everywhere in specs
and filenames; `capture.VENUE_INTERVAL` translates at the API boundary only.

**DSR units are per-observation, not annualized.** `null_sr_variance(t) = 1/t`.
Using the familiar `V=1.0` (an annualized-scale default) against a per-bar
Sharpe sets the bar ~sqrt(bars_per_year) too high and the gate rejects
everything — which looks rigorous and is actually useless. Pinned by
`test_deflation_uses_per_observation_units`.

## Adding a strategy

1. New module in `src/alpha_gate/strategies/`, implementing `decide(bars) ->
   Decision`. Long/flat only in v1.
2. Register it in `strategies/__init__.py` **and add its source manifest to
   `_SOURCES`** — the manifest is what the seal hashes. Omitting a file there
   means edits to it slip past the seal.
3. Seal it. The trial counter goes up. That is intended and is the cost of
   trying another idea.

For an LLM-driven strategy, `model_cutoff` is **required** — all evaluated bars
must postdate it, or the model may be scoring outcomes it memorised in
training. `verify_seal` enforces it.

## Current state

Trial #1 sealed `2026-07-31T11:39:54Z`: `ta_aggregate` (min_confidence=65) on
BTC/ETH/XRP/FLR, hourly, 2% fixed fraction, taker fees, book-walk slippage.
Earliest possible verdict ~2026-10-29. Everything before then is
`INCONCLUSIVE`, and that is the correct output, not a failure.

Capture must run hourly or the record develops gaps that cannot be repaired.

## What this is for

Deciding whether a strategy is worth any capital at all — and the expected
answer is no. A `FAIL` costs nothing but time; a false `PASS` costs money. When
in doubt, make the gate stricter, not looser.
