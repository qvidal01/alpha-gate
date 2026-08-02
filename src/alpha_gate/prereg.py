#!/usr/bin/env python3
"""
Pre-registration: seal a strategy BEFORE it sees any data.

This is the load-bearing module of the whole repo. Everything else measures;
this is the part that makes the measurement mean something.

The problem it solves: a backtest you can edit after seeing the result is not
evidence, it is a drawing. Bailey, Borwein, Lopez de Prado & Zhu (2014) showed
that with only a few years of data, a few dozen strategy variants is enough to
produce an excellent-looking Sharpe by pure chance. Every strategy you see with
a beautiful equity curve is the survivor of an unreported number of trials.

So a seal records three things that cannot be retrofitted:

  1. WHAT was claimed  - the full spec, hashed.
  2. WHICH CODE ran it - the strategy source, hashed.
  3. WHEN it was fixed - a UTC timestamp, before any bar exists.

and one thing that is easy to hide and matters more than any of them:

  4. HOW MANY TIMES you have tried - the trial index.

The trial index is the honest part. Every seal in registry/ counts, whether it
passed, failed, or was abandoned. evaluate.py feeds that count into the Deflated
Sharpe Ratio, so registering your twentieth variant automatically raises the bar
that variant has to clear. You cannot quietly fish for a winner: the fishing is
recorded in the same directory as the catch.

A seal is never edited. If you want different parameters, you seal a new one and
the trial counter goes up. That is the whole mechanism.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_DIR = REPO_ROOT / "registry"


class SealError(RuntimeError):
    """Raised when a seal cannot be created or has been violated."""


# --------------------------------------------------------------------------- #
# The spec
# --------------------------------------------------------------------------- #

@dataclass
class StrategySpec:
    """
    Everything that must be decided BEFORE data arrives.

    Every field here is a degree of freedom that, left open, becomes a way to
    fit the answer to the data after the fact. Fixing them in advance is the
    entire point; there is no field here that is merely bookkeeping.
    """

    strategy_id: str
    """Registry key of the strategy under test (see strategies/__init__.py)."""

    hypothesis: str
    """
    A falsifiable claim in plain English, written before the run.
    Bad:  "the indicator strategy should do well"
    Good: "the RSI+MACD+BB aggregate at min_confidence=65 beats buy-and-hold on
           XRPUSDT net of fees over 90 days"
    If you cannot state how it could be wrong, you are not testing anything.
    """

    params: dict[str, Any]
    """Strategy parameters. Frozen at seal time. Changing one = a new trial."""

    universe: list[str]
    """Symbols, e.g. ["BTCUSDT", "XRPUSDT"]."""

    bar_interval: str = "1h"
    """Decision cadence. Must match what capture.py is collecting."""

    initial_equity: float = 1000.0
    """Notional starting equity, in quote currency. Paper only."""

    sizing_mode: str = "fixed_fraction"
    """Only fixed_fraction is supported. Position = fraction * current equity."""

    sizing_fraction: float = 0.02
    """
    Fraction of equity per position. Default 2%.
    Deliberately not user-tunable upward without a new seal: over-sizing is the
    single fastest way to turn a real edge into a guaranteed loss (bet above the
    Kelly fraction and expected log growth goes negative even when the edge is
    genuinely positive).
    """

    min_days: int = 90
    """
    Minimum elapsed days before a PASS is even possible. Below this the verdict
    is INCONCLUSIVE regardless of how good the numbers look.
    """

    model_cutoff: str | None = None
    """
    ISO date (YYYY-MM-DD) of the training cutoff of any model used to make
    decisions. REQUIRED for LLM-driven strategies, None for pure-TA ones.

    Why it is a first-class field: an LLM with a 2025 cutoff already knows how
    every liquid asset moved through 2025. Any evaluation overlapping that
    window inherits leakage that lives inside the model weights, where careful
    data handling cannot reach it. Correcting for it has been measured to cut
    apparent in-sample returns by up to 67% on memorised dates. The only real
    defence is to evaluate strictly on data that did not exist when the model
    was trained -- which is what this field lets the evaluator enforce.
    """

    fee_mode: str = "taker"
    """taker | maker. Default taker: assume you cross the spread. Assuming maker
    fills is the most common way a paper result flatters itself."""

    slippage_model: str = "book_walk"
    """book_walk (consume real captured depth) | spread (top-of-book only)."""

    notes: str = ""

    def validate(self) -> None:
        if not self.strategy_id:
            raise SealError("strategy_id is required")
        if not self.hypothesis or len(self.hypothesis) < 20:
            raise SealError(
                "hypothesis must be a real falsifiable sentence, not a label. "
                "State what would have to happen for this to be WRONG."
            )
        if not self.universe:
            raise SealError("universe must contain at least one symbol")
        if self.sizing_mode != "fixed_fraction":
            raise SealError(f"unsupported sizing_mode: {self.sizing_mode}")
        if not (0 < self.sizing_fraction <= 0.25):
            raise SealError(
                f"sizing_fraction {self.sizing_fraction} outside (0, 0.25]. "
                "Above 25% per position this stops being a test of the signal "
                "and becomes a test of your luck."
            )
        if self.min_days < 90:
            raise SealError(
                f"min_days={self.min_days} below the floor of 90. Shorter "
                "windows cannot separate signal from noise at any plausible "
                "effect size -- see evaluate.py:noise_floor()."
            )
        if self.fee_mode not in ("taker", "maker"):
            raise SealError(f"fee_mode must be taker|maker, got {self.fee_mode}")
        if self.slippage_model not in ("book_walk", "spread"):
            raise SealError(f"unknown slippage_model: {self.slippage_model}")
        if self.model_cutoff is not None:
            try:
                datetime.strptime(self.model_cutoff, "%Y-%m-%d")
            except ValueError as exc:
                raise SealError(f"model_cutoff must be YYYY-MM-DD: {exc}") from exc


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_spec_hash(spec: StrategySpec) -> str:
    """Hash of the spec, key-order independent so cosmetic edits are visible."""
    blob = json.dumps(asdict(spec), sort_keys=True, separators=(",", ":"))
    return _sha256_bytes(blob.encode())


def strategy_code_hash(strategy_id: str) -> str:
    """
    Hash the source of the strategy module AND its declared dependencies.

    This is what catches the quiet edit -- tweaking a threshold inside the
    strategy after a bad week, while the spec file still says what it always
    said. check.sh re-derives this hash and fails if it moved.
    """
    from alpha_gate.strategies import source_files_for

    h = hashlib.sha256()
    for path in sorted(source_files_for(strategy_id)):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else "uncommitted"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


# --------------------------------------------------------------------------- #
# Seals
# --------------------------------------------------------------------------- #

@dataclass
class Seal:
    spec: dict[str, Any]
    spec_sha256: str
    code_sha256: str
    sealed_at_utc: str
    sealed_at_unix: float
    trial_index: int
    git_commit: str
    seal_version: int = 1
    superseded_by: str | None = None
    """Set when a later seal replaces this one. A superseded seal still counts
    as a trial -- that is the point. You do not get to un-try something."""

    def path(self, registry_dir: Path | None = None) -> Path:
        """
        Where this seal lives.

        The spec-hash suffix is not decoration: two variants of the same
        strategy sealed within the same second would otherwise collide, and the
        collision would be silently reported as "seals are immutable" -- losing
        a trial from the count and quietly weakening the deflation. A lost
        trial is exactly the failure this repo exists to prevent.
        """
        d = registry_dir or REGISTRY_DIR
        stamp = datetime.fromtimestamp(
            self.sealed_at_unix, tz=timezone.utc
        ).strftime("%Y%m%dT%H%M%SZ")
        return d / (
            f"{self.spec['strategy_id']}__{stamp}__{self.spec_sha256[:8]}.seal.json"
        )


def count_trials(registry_dir: Path | None = None) -> int:
    """
    Every seal ever written, including failed and superseded ones.

    This number is the multiple-testing correction. It is deliberately NOT
    filtered by outcome: the whole hazard is that you run twenty variants,
    report the one that worked, and silently drop the nineteen.
    """
    d = registry_dir or REGISTRY_DIR
    if not d.exists():
        return 0
    return len(list(d.glob("*.seal.json")))


def seal(spec: StrategySpec, registry_dir: Path | None = None) -> Seal:
    """
    Freeze a strategy. Call this BEFORE capture has collected a single bar you
    intend to evaluate on.

    Refuses to overwrite an existing seal. Refuses to seal a strategy whose code
    does not currently import. Both are cases where a later "it passed" would
    not mean anything.
    """
    spec.validate()
    d = registry_dir or REGISTRY_DIR
    d.mkdir(parents=True, exist_ok=True)

    # Fail early if the strategy does not resolve -- a seal pointing at a
    # strategy that cannot load is a seal that can be quietly redefined later.
    from alpha_gate.strategies import get_strategy
    get_strategy(spec.strategy_id, spec.params)

    now = time.time()
    s = Seal(
        spec=asdict(spec),
        spec_sha256=canonical_spec_hash(spec),
        code_sha256=strategy_code_hash(spec.strategy_id),
        sealed_at_utc=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        sealed_at_unix=now,
        trial_index=count_trials(d) + 1,
        git_commit=_git_commit(),
    )

    target = s.path(d)
    if target.exists():
        raise SealError(f"seal already exists: {target} -- seals are immutable")

    target.write_text(json.dumps(asdict(s), indent=2, sort_keys=True) + "\n")
    return s


def load_seal(path: Path) -> Seal:
    data = json.loads(Path(path).read_text())
    return Seal(**data)


def load_seals(registry_dir: Path | None = None) -> list[Seal]:
    d = registry_dir or REGISTRY_DIR
    if not d.exists():
        return []
    return sorted(
        (load_seal(p) for p in d.glob("*.seal.json")),
        key=lambda s: s.sealed_at_unix,
    )


def latest_seal(strategy_id: str, registry_dir: Path | None = None) -> Seal | None:
    matches = [s for s in load_seals(registry_dir)
               if s.spec["strategy_id"] == strategy_id and not s.superseded_by]
    return matches[-1] if matches else None


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #

@dataclass
class SealVerdict:
    ok: bool
    violations: list[str] = field(default_factory=list)
    # Reasons the seal could not be CHECKED, as distinct from reasons it was
    # BROKEN. Both block a verdict -- `ok` is False either way, so the
    # fail-closed property is unchanged -- but only `violations` is an
    # accusation. See verify_seal for why the distinction is load-bearing.
    unprovable: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


def verify_seal(s: Seal, bars: list[dict] | None = None) -> SealVerdict:
    """
    Check that a seal has not been violated. Any violation invalidates the run
    outright -- there is no partial credit, because a broken seal means the
    result could have been fitted after the fact.
    """
    violations: list[str] = []
    unprovable: list[str] = []

    # 1. The spec has not been edited since sealing.
    spec = StrategySpec(**s.spec)
    if canonical_spec_hash(spec) != s.spec_sha256:
        violations.append(
            "spec hash mismatch: the sealed spec has been edited after the fact"
        )

    # 2. The strategy code has not been edited since sealing.
    #
    # A failure to HASH is not evidence of an edit, and must not be reported as
    # one. It still blocks the verdict (ok stays False, so this remains
    # fail-closed and a deliberately-broken import cannot be used to dodge the
    # check) -- but it goes to `unprovable`, not `violations`.
    #
    # Why this matters more here than almost anywhere else: measured 2026-08-02,
    # a fresh `git clone` of this repo reports
    #     "SEAL VIOLATED - verdicts from these runs are void:
    #      ta_aggregate ...: strategy code cannot be hashed: No module named 'pandas'"
    # strategy_code_hash reads SOURCE BYTES -- the hash basis is sound and
    # environment-independent -- but resolving WHICH files to hash imports
    # alpha_gate.strategies, which pulls in pandas. So an independent party
    # verifying this trial the obvious way (clone it, run the gate) is told the
    # seal is VIOLATED, i.e. that the strategy was edited after the fact, when
    # in truth a dependency is merely absent.
    #
    # For a harness whose entire product is that its verdicts can be trusted by
    # someone who does not trust the operator, accusing yourself of fraud when a
    # library is missing is the worst possible failure mode. It is also the same
    # confusion the estate's evaluator contract exists to prevent: 1 means FAILED,
    # 3 means CANNOT TELL, and they are never the same claim.
    #
    # NOT changed: what gets hashed. Trial #1 is sealed against the current basis
    # and any change to it would silently void a live trial.
    try:
        current = strategy_code_hash(spec.strategy_id)
    except Exception as exc:  # noqa: BLE001 - cannot hash => cannot prove, still blocks
        unprovable.append(f"strategy code cannot be hashed: {exc}")
    else:
        if current != s.code_sha256:
            violations.append(
                f"code hash mismatch for '{spec.strategy_id}': the strategy was "
                f"edited after sealing (sealed {s.code_sha256[:12]}, now "
                f"{current[:12]}). Seal a new trial instead."
            )

    # 3. THE forward-only rule: no bar may predate the seal.
    #    This is what makes the whole thing forward-only rather than a backtest
    #    with extra steps.
    if bars:
        early = [b for b in bars if b.get("captured_at_unix", 0) < s.sealed_at_unix]
        if early:
            violations.append(
                f"{len(early)} bar(s) were captured BEFORE the seal "
                f"({s.sealed_at_utc}). Forward-only means the strategy was fixed "
                f"before the data existed; these bars break that."
            )

        # 4. LLM leakage rule: all data must postdate the declared model cutoff.
        if spec.model_cutoff:
            cutoff = datetime.strptime(spec.model_cutoff, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            ).timestamp()
            leaked = [b for b in bars if b.get("close_time_unix", 0) < cutoff]
            if leaked:
                violations.append(
                    f"{len(leaked)} bar(s) predate the declared model_cutoff "
                    f"{spec.model_cutoff}. The model may have memorised these "
                    f"outcomes during training; results on them are not evidence."
                )

    # ok is False when the seal is broken OR merely uncheckable: fail-closed in
    # both cases. The lists are what tell an operator which one happened.
    return SealVerdict(
        ok=not violations and not unprovable,
        violations=violations,
        unprovable=unprovable,
    )


__all__ = [
    "StrategySpec", "Seal", "SealError", "SealVerdict",
    "seal", "load_seal", "load_seals", "latest_seal", "verify_seal",
    "count_trials", "canonical_spec_hash", "strategy_code_hash",
    "REGISTRY_DIR", "REPO_ROOT",
]
