# breadth_composite/data_loader.py
"""
data_loader.py — HOSE universe + daily OHLCV + Parquet cache.

No third-party market-data library: every request goes through
`breadth_composite.yuanta_api.YuantaClient` (price board for the universe,
TradingView-style history for the bars).

Behaviour that matters
----------------------
* Universe: `list_stock_info?exchange=HSX`, ordinary shares only (`SymbolType`),
  de-duplicated and upper-cased. A schema change that hides `SymbolType` keeps
  the symbols and logs a warning instead of silently emptying the universe.
* Never caches an unfinished session: every bar is pruned to
  `latest_session_date()` (15:15 Vietnam cutoff, weekends roll back to Friday),
  and rows beyond it already sitting in a cache are trimmed too.
* Incremental per ticker: only symbols whose cached last date is behind the last
  finished session are requested, each starting ADJUST_CHECK_DAYS before its own
  cached last date. The overlap is compared with the cache; a >0.5% disagreement
  means the provider re-adjusted history (split/dividend), so that one symbol is
  re-downloaded in full. The alternative — trusting the cache — silently corrupts
  every moving average after a corporate action.
* A newly seen symbol is always backfilled from the full window, never from
  "tomorrow", so it enters the breadth counts with usable history.
* Failures are counted: after MAX_CONSECUTIVE_FAILURES in a row the run stops,
  returns what it has and reports `degraded=True` so the caller can fall back to
  the cache instead of publishing a pipeline built on a few symbols.

Price unit: the provider's history is in thousand VND. Breadth is scale-free, so
the raw unit is preserved (documented in config.HISTORY_PRICE_UNIT).
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from . import config as _config
from .config import (
    ADJUST_CHECK_DAYS,
    ADJUST_CHECK_TOLERANCE,
    BACKFILL_YEARS,
    DATA_CACHE_FILENAME,
    MAX_CONSECUTIVE_FAILURES,
    MAX_WORKERS,
    MIN_REQUEST_INTERVAL,
    SYMBOL_TYPE_FILTER,
    UNIVERSE_KEEP_UNKNOWN_TYPE,
    YUANTA_EXCHANGE_CODE,
)
from .yuanta_api import (  # noqa: F401  (latest_session_date re-exported for callers)
    YuantaClient,
    YuantaError,
    latest_session_date,
)

logger = logging.getLogger(__name__)

OHLCVDict = Dict[str, pd.DataFrame]
OHLCV_COLUMNS: Tuple[str, ...] = ("open", "high", "low", "close", "volume")

_client_lock = threading.Lock()
_default_client: Optional[YuantaClient] = None


# ---------------------------------------------------------------------------
# Client access
# ---------------------------------------------------------------------------

def get_client(client: Optional[YuantaClient] = None) -> YuantaClient:
    """Return the injected client, or a process-wide lazily built one."""
    global _default_client
    if client is not None:
        return client
    with _client_lock:
        if _default_client is None:
            _default_client = YuantaClient()
        return _default_client


# ---------------------------------------------------------------------------
# Dates / frames
# ---------------------------------------------------------------------------

def _backfill_start(today: Optional[date] = None) -> date:
    today = today or date.today()
    return today - timedelta(days=int(BACKFILL_YEARS * 365.25))


def _as_date(value: Any) -> date:
    """Accept date / datetime / ISO string."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(str(value)).date()


def _last_date(frame: Optional[pd.DataFrame]) -> Optional[date]:
    if frame is None or frame.empty:
        return None
    try:
        return pd.Timestamp(frame.index.max()).date()
    except (TypeError, ValueError):
        return None


def _clean_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Guarantee the five canonical columns, floats, sorted, one row per date."""
    if frame is None or frame.empty:
        return pd.DataFrame(
            {col: pd.Series(dtype="float64") for col in OHLCV_COLUMNS},
            index=pd.DatetimeIndex([], name="date"),
        )
    out = frame.copy()
    for col in OHLCV_COLUMNS:
        if col not in out.columns:
            out[col] = float("nan")
    out = out[list(OHLCV_COLUMNS)].apply(pd.to_numeric, errors="coerce")
    out.index = pd.DatetimeIndex(pd.to_datetime(out.index)).normalize()
    out.index.name = "date"
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out


def trim_after(frame: pd.DataFrame, cutoff: date) -> pd.DataFrame:
    """Drop every bar newer than `cutoff` (an unfinished session, or poisoned cache)."""
    if frame is None or frame.empty:
        return frame
    return frame.loc[frame.index <= pd.Timestamp(cutoff)]


def merge_frames(cached: Optional[pd.DataFrame], fresh: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Cached history + fresh bars, fresh values winning on shared dates."""
    cached = _clean_frame(cached) if cached is not None else None
    fresh = _clean_frame(fresh) if fresh is not None else None

    if (cached is None or cached.empty) and (fresh is None or fresh.empty):
        return _clean_frame(None)
    if cached is None or cached.empty:
        return fresh
    if fresh is None or fresh.empty:
        return cached

    combined = pd.concat([cached, fresh])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    return combined[list(OHLCV_COLUMNS)]


def combine_with_cache(
    cached: OHLCVDict,
    fresh: OHLCVDict,
    tickers: Sequence[str],
) -> OHLCVDict:
    """
    Universe-shaped result: fresh frames win, cached frames survive for symbols
    that were not refetched (transient failure, --full re-download, delistings).

    Symbols outside `tickers` are dropped, so a delisting leaves the dataset on
    the next run instead of counting in the breadth totals for ever.
    """
    wanted = set(tickers)
    merged: OHLCVDict = {t: frame for t, frame in cached.items() if t in wanted}
    merged.update({t: frame for t, frame in fresh.items() if t in wanted and frame is not None})
    return {t: merged[t] for t in sorted(merged)}


def overlap_readjusted(fresh: pd.DataFrame, cached: pd.DataFrame) -> bool:
    """True when overlapping closes disagree, i.e. history was re-adjusted."""
    if fresh is None or fresh.empty or cached is None or cached.empty:
        return False
    common = fresh.index.intersection(cached.index)
    if len(common) < 3:
        return False
    a = pd.to_numeric(fresh.loc[common, "close"], errors="coerce")
    b = pd.to_numeric(cached.loc[common, "close"], errors="coerce")
    mask = a.notna() & b.notna() & (b > 0)
    if int(mask.sum()) < 3:
        return False
    divergence = ((a[mask] / b[mask]) - 1.0).abs()
    return bool(float(divergence.max()) > ADJUST_CHECK_TOLERANCE)


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def get_hose_tickers(client: Optional[YuantaClient] = None) -> List[str]:
    """
    Every ordinary-share symbol on the exchange, from the price board.

    Raises YuantaError (with the provider's message) if the universe cannot be
    built — silently trading a 5-symbol universe is worse than failing.
    """
    board = get_client(client)
    rows = board.stock_info(exchange=YUANTA_EXCHANGE_CODE)

    allowed_types = {t.upper() for t in SYMBOL_TYPE_FILTER}
    type_counts: Dict[str, int] = {}
    tickers: List[str] = []
    seen = set()
    unknown_type = 0

    for row in rows:
        code = str(row.get("StockCode") or "").strip().upper()
        if not code or code in seen:
            continue

        raw_type = str(row.get("SymbolType") or "").strip().upper()
        type_counts[raw_type or "<blank>"] = type_counts.get(raw_type or "<blank>", 0) + 1

        if raw_type and allowed_types and raw_type not in allowed_types:
            continue
        if not raw_type:
            unknown_type += 1
            if not UNIVERSE_KEEP_UNKNOWN_TYPE:
                continue

        seen.add(code)
        tickers.append(code)

    if not tickers:
        # Never fall back to an empty universe: keep everything and shout.
        logger.error(
            "universe filter produced 0 symbols (types=%s) — keeping all %d rows instead",
            type_counts, len(rows),
        )
        tickers = sorted({str(r.get("StockCode")).strip().upper() for r in rows if r.get("StockCode")})

    tickers.sort()
    logger.info(
        "Universe %s: %d ordinary shares from %d rows (types=%s%s)",
        YUANTA_EXCHANGE_CODE, len(tickers), len(rows), type_counts,
        f", kept {unknown_type} without SymbolType" if unknown_type else "",
    )
    return tickers


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

class _Throttle:
    """Space out request starts across threads (protects the provider's edge)."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next_allowed - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._next_allowed = now + self.min_interval


class _FetchStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counts: Dict[str, int] = {}

    def bump(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self.counts[key] = self.counts.get(key, 0) + amount

    def as_dict(self) -> Dict[str, int]:
        with self._lock:
            return dict(self.counts)


def _fetch_one(
    ticker: str,
    cached: Optional[pd.DataFrame],
    client: YuantaClient,
    latest: date,
    backfill_start: date,
    stats: _FetchStats,
) -> Tuple[str, Optional[pd.DataFrame], str]:
    """Fetch (or skip) one symbol. Returns (ticker, frame, status)."""
    cached_frame = _clean_frame(cached) if cached is not None else None
    if cached_frame is not None and cached_frame.empty:
        cached_frame = None

    last_cached = _last_date(cached_frame)
    if last_cached is not None and last_cached >= latest:
        stats.bump("current")
        return ticker, cached_frame, "current"

    full_history = last_cached is None
    if last_cached is None:
        start = backfill_start
    else:
        start = max(last_cached - timedelta(days=ADJUST_CHECK_DAYS), backfill_start)

    fresh = client.history(ticker, start=start, max_date=latest)

    if cached_frame is not None and not fresh.empty and start > backfill_start:
        if overlap_readjusted(fresh, cached_frame):
            logger.info("%s: overlapping closes changed — re-downloading full history", ticker)
            fresh = client.history(ticker, start=backfill_start, max_date=latest)
            full_history = True
            stats.bump("readjusted")

    if fresh is None or fresh.empty:
        if cached_frame is None:
            stats.bump("empty")
            return ticker, None, "empty"
        stats.bump("stale")
        return ticker, cached_frame, "stale"

    if full_history:
        # A full download replaces the cache instead of merging: after a split or
        # dividend every older close is rescaled, so keeping them would corrupt
        # the moving averages with two different price bases.
        if cached_frame is not None and len(fresh) < len(cached_frame):
            logger.warning(
                "%s: full history returned %d bars but the cache held %d — keeping the "
                "fresh (re-adjusted) series; run --full if this repeats",
                ticker, len(fresh), len(cached_frame),
            )
        stats.bump("backfilled")
        return ticker, _clean_frame(fresh), "backfilled"

    stats.bump("updated")
    return ticker, merge_frames(cached_frame, fresh), "updated"


def _fetch_many(
    tickers: Sequence[str],
    cached: Dict[str, pd.DataFrame],
    *,
    client: YuantaClient,
    latest: date,
    backfill_start: date,
    max_workers: int,
    min_interval: float,
) -> Tuple[OHLCVDict, bool, List[str]]:
    """Threaded fetch with a consecutive-failure circuit breaker."""
    stats = _FetchStats()
    results: OHLCVDict = {}
    failures: List[str] = []
    stop = threading.Event()
    throttle = _Throttle(min_interval)
    guard_lock = threading.Lock()
    state = {"consecutive": 0}

    def work(ticker: str) -> Tuple[str, Optional[pd.DataFrame], str]:
        if stop.is_set():
            return ticker, None, "skipped"
        try:
            throttle.wait()
            outcome = _fetch_one(ticker, cached.get(ticker), client, latest, backfill_start, stats)
        except (YuantaError, ValueError) as exc:
            with guard_lock:
                state["consecutive"] += 1
                tripped = state["consecutive"] >= MAX_CONSECUTIVE_FAILURES
            if tripped:
                stop.set()
            logger.warning("%s: fetch failed (%s)%s", ticker, str(exc)[:180],
                           " — stopping, too many consecutive failures" if tripped else "")
            return ticker, None, "failed"
        except Exception as exc:  # pragma: no cover - defensive
            with guard_lock:
                state["consecutive"] += 1
                tripped = state["consecutive"] >= MAX_CONSECUTIVE_FAILURES
            if tripped:
                stop.set()
            logger.warning("%s: unexpected error (%s)", ticker, str(exc)[:180])
            return ticker, None, "failed"
        else:
            with guard_lock:
                state["consecutive"] = 0
            return outcome

    workers = max(1, min(int(max_workers), max(1, len(tickers))))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ohlcv") as pool:
        futures = {pool.submit(work, t): t for t in tickers}
        for done, future in enumerate(as_completed(futures), start=1):
            ticker = futures[future]
            try:
                t, frame, status = future.result()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("%s: worker crashed (%s)", ticker, exc)
                failures.append(ticker)
                continue
            if frame is not None and not frame.empty:
                results[t] = frame
            if status == "failed":
                failures.append(t)
            if done % 200 == 0 or done == len(futures):
                logger.info("progress: %d/%d symbols", done, len(futures))

    counts = stats.as_dict()
    logger.info(
        "fetch finished: %d symbols returned | %s | failures=%d",
        len(results),
        ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no counters",
        len(failures),
    )
    if failures:
        logger.warning("failed symbols (first 15): %s", ", ".join(failures[:15]))
    return results, stop.is_set(), failures


def fetch_ohlcv_all(
    tickers: Sequence[str],
    *,
    start: Optional[Any] = None,
    end: Optional[Any] = None,
    client: Optional[YuantaClient] = None,
    max_workers: int = MAX_WORKERS,
    min_interval: float = MIN_REQUEST_INTERVAL,
    latest: Optional[date] = None,
) -> Tuple[OHLCVDict, bool]:
    """
    Fetch full history for every symbol (used for --full and for new symbols).

    Returns (data, degraded). `degraded=True` means the run stopped early after
    repeated failures, so the caller should prefer its cache over a thin result.
    """
    board = get_client(client)
    latest_session = latest or latest_session_date(board.now())
    backfill_start = _as_date(start) if start else _backfill_start()
    if end is not None:
        latest_session = min(latest_session, _as_date(end))

    if backfill_start > latest_session:
        logger.info("start (%s) > last session (%s) — nothing to fetch", backfill_start, latest_session)
        return {}, False

    symbols = [t for t in tickers if t]
    if not symbols:
        return {}, False

    logger.info(
        "fetch_ohlcv_all: %d symbols | %s → last session %s | workers=%d",
        len(symbols), backfill_start, latest_session, max_workers,
    )
    return _fetch_many(
        symbols,
        {},
        client=board,
        latest=latest_session,
        backfill_start=backfill_start,
        max_workers=max_workers,
        min_interval=min_interval,
    )[:2]


def incremental_fetch(
    cached: OHLCVDict,
    tickers: Sequence[str],
    *,
    client: Optional[YuantaClient] = None,
    max_workers: int = MAX_WORKERS,
    min_interval: float = MIN_REQUEST_INTERVAL,
    latest: Optional[date] = None,
) -> Tuple[OHLCVDict, bool]:
    """
    Update the cache with only the bars that are missing.

    Symbols already current for the last finished session are not requested at
    all, so a normal weekday run costs one request per stale symbol plus the
    universe call. Returns (merged_data, degraded) where merged_data is
    restricted to symbols still present in `tickers` (delistings are dropped).
    """
    board = get_client(client)
    latest_session = latest or latest_session_date(board.now())

    cached = cached or {}
    cleaned: OHLCVDict = {}
    trimmed = 0
    for ticker, frame in cached.items():
        fixed = trim_after(_clean_frame(frame), latest_session)
        if len(fixed) != len(frame):
            trimmed += 1
        if not fixed.empty:
            cleaned[ticker] = fixed
    if trimmed:
        logger.info("cache self-heal: trimmed bars newer than %s from %d symbols", latest_session, trimmed)

    backfill_start = _backfill_start()
    stale = [
        t for t in tickers
        if t and (_last_date(cleaned.get(t)) is None or _last_date(cleaned.get(t)) < latest_session)
    ]
    logger.info(
        "incremental: %d/%d symbols need updating (last finished session %s)",
        len(stale), len(tickers), latest_session,
    )

    fresh: OHLCVDict = {}
    degraded = False
    if stale:
        fresh, degraded, _ = _fetch_many(
            stale,
            cleaned,
            client=board,
            latest=latest_session,
            backfill_start=backfill_start,
            max_workers=max_workers,
            min_interval=min_interval,
        )

    return combine_with_cache(cleaned, fresh, tickers), degraded


# ---------------------------------------------------------------------------
# VN-Index
# ---------------------------------------------------------------------------

def fetch_vnindex(
    client: Optional[YuantaClient] = None,
    *,
    start: Optional[Any] = None,
) -> Optional[pd.Series]:
    """VN-Index close series for the chart overlay (None when unavailable)."""
    board = get_client(client)
    latest_session = latest_session_date(board.now())
    backfill_start = _as_date(start) if start else _backfill_start()
    try:
        frame = board.history("VNINDEX", start=backfill_start, max_date=latest_session)
    except YuantaError as exc:
        logger.warning("VN-Index history unavailable: %s", exc)
        return None
    if frame is None or frame.empty:
        logger.warning("VN-Index history returned no bars")
        return None
    series = frame["close"].rename("VNINDEX")
    series = series[~series.index.duplicated(keep="last")]
    logger.info("VN-Index: %d closes, last %s = %.2f",
                len(series), series.index[-1].date(), series.iloc[-1])
    return series


# ---------------------------------------------------------------------------
# Parquet cache
# ---------------------------------------------------------------------------

def _cache_path() -> Path:
    # Read config at call time: --output-dir patches config after import.
    path = Path(_config.OUTPUT_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path / DATA_CACHE_FILENAME


def load_cache() -> OHLCVDict:
    """Read the Parquet cache; an unreadable/corrupt file means "no cache"."""
    path = _cache_path()
    if not path.exists():
        logger.info("no cache at %s — a full fetch is required", path)
        return {}
    try:
        combined = pd.read_parquet(path)
    except Exception as exc:
        logger.warning("cache read error (%s) — ignoring cache", exc)
        return {}

    if combined.empty or "ticker" not in combined.columns:
        logger.warning("cache at %s is empty or malformed — ignoring", path)
        return {}

    result: OHLCVDict = {}
    try:
        combined["date"] = pd.to_datetime(combined["date"])
        for ticker, group in combined.groupby("ticker", sort=False):
            frame = group.drop(columns="ticker").set_index("date")
            frame = _clean_frame(frame)
            if not frame.empty:
                result[str(ticker)] = frame
    except Exception as exc:
        logger.warning("cache parse error (%s) — ignoring cache", exc)
        return {}

    logger.info("cache loaded: %d symbols, last date %s",
                len(result), max((_last_date(f) for f in result.values()), default=None))
    return result


def save_cache(data: OHLCVDict) -> None:
    """Write the cache atomically (temp file + replace) so a crash cannot truncate it."""
    if not data:
        logger.warning("save_cache called with no data — leaving the existing cache untouched")
        return

    frames = []
    for ticker, frame in data.items():
        cleaned = _clean_frame(frame)
        if cleaned.empty:
            continue
        tmp = cleaned.reset_index()
        tmp["ticker"] = ticker
        frames.append(tmp)
    if not frames:
        logger.warning("save_cache: nothing to write")
        return

    combined = pd.concat(frames, ignore_index=True)
    combined["date"] = pd.to_datetime(combined["date"])

    path = _cache_path()
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    combined.to_parquet(tmp_path, index=False, engine="pyarrow")
    tmp_path.replace(path)
    logger.info("cache saved: %d symbols × %d rows → %s", len(frames), len(combined), path)


# ---------------------------------------------------------------------------
# Manual smoke test (no network mocks): python -m breadth_composite.data_loader
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    tickers = get_hose_tickers()
    print(f"{len(tickers)} symbols; first 10: {tickers[:10]}")
    sample, degraded = fetch_ohlcv_all(tickers[:5])
    for name, frame in sample.items():
        print(f"{name}: {len(frame)} bars, {frame.index.min().date()} → {frame.index.max().date()}")
    print(f"degraded={degraded}")
