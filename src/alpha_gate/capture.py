#!/usr/bin/env python3
"""
Forward-only market data capture.

The rule this module enforces, and the reason it exists as a separate step
rather than "just download the history":

    A bar may only be written close to the moment it actually closed.

Backfill is refused. If you ask for a bar that closed six hours ago, capture
drops it. That sounds like a limitation; it is the entire product. It is what
makes the difference between "I tested this on 90 days of data" and "I tested
this on 90 days of data that did not exist when I wrote the strategy" -- and
only the second sentence is evidence of anything.

Each record carries captured_at_unix, a wall-clock stamp written at capture
time. prereg.verify_seal() compares that against the seal timestamp and fails
the whole run if a single bar predates it. Because the file is append-only
JSONL and check.sh verifies monotonicity, retroactively inserting data means
producing a file that fails the gate.

No API key is required. Everything here is public market data, which means the
harness can run unattended in CI without a credential ever touching it.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

MEXC_BASE = "https://api.mexc.com"
DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "bars"

INTERVAL_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}

# MEXC's kline endpoint does NOT use the Binance-style codes it otherwise
# resembles: hourly is "60m", and "1h" returns HTTP 400. Verified against the
# live endpoint 2026-07-31.
#
# The canonical name stays "1h" everywhere in specs, seals and filenames so a
# sealed spec does not become unreadable if a second venue is ever added; the
# venue's dialect is translated only here, at the boundary.
VENUE_INTERVAL = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "60m", "4h": "4h", "1d": "1d",
}

# How stale a bar may be and still be written. One interval of grace covers a
# cron that fires a few minutes late; beyond that it is backfill and refused.
STALENESS_GRACE = 1.5

DEFAULT_DEPTH = 20


class CaptureError(RuntimeError):
    """Raised when data cannot be captured honestly."""


@dataclass(frozen=True)
class Bar:
    symbol: str
    interval: str
    open_time_unix: float
    close_time_unix: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    captured_at_unix: float
    book: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps({
            "symbol": self.symbol,
            "interval": self.interval,
            "open_time_unix": self.open_time_unix,
            "close_time_unix": self.close_time_unix,
            "close_time_utc": datetime.fromtimestamp(
                self.close_time_unix, tz=timezone.utc).isoformat(),
            "o": self.open, "h": self.high, "l": self.low, "c": self.close,
            "v": self.volume,
            "captured_at_unix": self.captured_at_unix,
            "captured_at_utc": datetime.fromtimestamp(
                self.captured_at_unix, tz=timezone.utc).isoformat(),
            "book": self.book,
        }, separators=(",", ":"))


def _http_json(url: str, timeout: int = 15) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "alpha-gate/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise CaptureError(f"fetch failed for {url}: {exc}") from exc


def bars_path(symbol: str, interval: str, data_dir: Path | None = None) -> Path:
    d = data_dir or DATA_DIR
    return d / f"{symbol}_{interval}.jsonl"


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #

def fetch_latest_closed_bar(symbol: str, interval: str) -> dict[str, Any]:
    """
    Fetch the most recent CLOSED kline. Never the in-progress one -- an
    unclosed bar's high/low/close still move, and a strategy that decides on a
    forming bar is reading a number that has not settled yet. That is a subtle
    and very common form of lookahead.
    """
    if interval not in INTERVAL_SECONDS:
        raise CaptureError(f"unsupported interval {interval}")

    venue_iv = VENUE_INTERVAL[interval]
    url = f"{MEXC_BASE}/api/v3/klines?symbol={symbol}&interval={venue_iv}&limit=2"
    rows = _http_json(url)
    if not rows or len(rows) < 2:
        raise CaptureError(f"no kline data for {symbol} {interval}")

    # MEXC returns oldest-first; the last row is still forming, so take [-2].
    row = rows[-2]
    return {
        "open_time_unix": float(row[0]) / 1000.0,
        "open": float(row[1]), "high": float(row[2]),
        "low": float(row[3]), "close": float(row[4]),
        "volume": float(row[5]),
        "close_time_unix": float(row[6]) / 1000.0,
    }


def fetch_book(symbol: str, depth: int = DEFAULT_DEPTH) -> dict[str, Any]:
    """
    Snapshot real order book depth. This is what makes slippage a measurement
    instead of an assumption -- and it is only available live, which is another
    reason this cannot be reconstructed from history after the fact.
    """
    url = f"{MEXC_BASE}/api/v3/depth?symbol={symbol}&limit={depth}"
    data = _http_json(url)
    bids = [[float(p), float(q)] for p, q in data.get("bids", [])[:depth]]
    asks = [[float(p), float(q)] for p, q in data.get("asks", [])[:depth]]
    if not bids or not asks:
        raise CaptureError(f"empty order book for {symbol}")
    return {"bids": bids, "asks": asks, "captured_at_unix": time.time()}


# --------------------------------------------------------------------------- #
# Write (append-only, forward-only)
# --------------------------------------------------------------------------- #

def read_bars(symbol: str, interval: str,
              data_dir: Path | None = None) -> list[dict[str, Any]]:
    path = bars_path(symbol, interval, data_dir)
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def iter_bars(symbol: str, interval: str,
              data_dir: Path | None = None) -> Iterator[dict[str, Any]]:
    path = bars_path(symbol, interval, data_dir)
    if not path.exists():
        return
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def capture_once(symbol: str, interval: str = "1h",
                 data_dir: Path | None = None,
                 depth: int = DEFAULT_DEPTH,
                 now: float | None = None) -> Bar | None:
    """
    Capture the latest closed bar, if and only if it is genuinely fresh.

    Returns the Bar written, or None when there is nothing new (already have it)
    or the bar is too stale to write honestly.

    Refusing stale bars is what makes gaps VISIBLE. If your cron dies for two
    days you get a two-day hole in the record, not two days of silently
    backfilled data that make the strategy look like it traded through a period
    it never saw. A hole is information; a backfill is a lie.
    """
    t_now = now if now is not None else time.time()
    interval_s = INTERVAL_SECONDS[interval]

    kline = fetch_latest_closed_bar(symbol, interval)
    close_t = kline["close_time_unix"]

    age = t_now - close_t
    if age > interval_s * STALENESS_GRACE:
        return None  # backfill territory -- refuse

    existing = read_bars(symbol, interval, data_dir)
    if existing and existing[-1]["close_time_unix"] >= close_t:
        return None  # already captured

    bar = Bar(
        symbol=symbol, interval=interval,
        open_time_unix=kline["open_time_unix"],
        close_time_unix=close_t,
        open=kline["open"], high=kline["high"],
        low=kline["low"], close=kline["close"], volume=kline["volume"],
        captured_at_unix=t_now,
        book=fetch_book(symbol, depth),
    )

    path = bars_path(symbol, interval, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(bar.to_json() + "\n")
    return bar


def capture_universe(symbols: list[str], interval: str = "1h",
                     data_dir: Path | None = None) -> dict[str, str]:
    """Capture every symbol. Never raises for one bad symbol -- a venue hiccup
    on FLR must not cost you the BTC bar for that hour."""
    results = {}
    for sym in symbols:
        try:
            bar = capture_once(sym, interval, data_dir)
            results[sym] = "written" if bar else "skipped"
        except CaptureError as exc:
            results[sym] = f"error: {exc}"
    return results


# --------------------------------------------------------------------------- #
# Integrity
# --------------------------------------------------------------------------- #

def audit_bars(symbol: str, interval: str,
               data_dir: Path | None = None) -> dict[str, Any]:
    """
    Check the captured record for the things that would invalidate a run.
    check.sh calls this; it is the mechanical proof that the data is what it
    claims to be.
    """
    bars = read_bars(symbol, interval, data_dir)
    problems: list[str] = []

    if not bars:
        return {"symbol": symbol, "bars": 0, "problems": ["no data captured"],
                "ok": False, "days": 0.0, "gaps": 0}

    interval_s = INTERVAL_SECONDS[interval]

    # Monotonic close times: an out-of-order row means the file was edited.
    for prev, cur in zip(bars, bars[1:]):
        if cur["close_time_unix"] <= prev["close_time_unix"]:
            problems.append(
                f"non-monotonic close_time at {cur.get('close_time_utc')} "
                "-- the append-only record has been rewritten"
            )
            break

    # Every bar must have been captured AFTER it closed. The reverse means the
    # timestamp was fabricated.
    for b in bars:
        if b["captured_at_unix"] < b["close_time_unix"] - 5:
            problems.append(
                f"bar closing {b.get('close_time_utc')} claims capture before "
                "its own close -- fabricated timestamp"
            )
            break

    gaps = sum(
        1 for prev, cur in zip(bars, bars[1:])
        if cur["close_time_unix"] - prev["close_time_unix"] > interval_s * 1.5
    )

    span_days = (bars[-1]["close_time_unix"] - bars[0]["close_time_unix"]) / 86400.0

    return {
        "symbol": symbol,
        "bars": len(bars),
        "days": round(span_days, 2),
        "gaps": gaps,
        "first": bars[0].get("close_time_utc"),
        "last": bars[-1].get("close_time_utc"),
        "problems": problems,
        "ok": not problems,
    }


__all__ = [
    "Bar", "CaptureError", "INTERVAL_SECONDS",
    "capture_once", "capture_universe", "read_bars", "iter_bars",
    "fetch_book", "fetch_latest_closed_bar", "audit_bars", "bars_path",
]
