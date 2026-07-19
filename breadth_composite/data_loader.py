"""
data_loader.py — Ticker listing & OHLCV fetcher
- Lists all HOSE tickers via vnstock
- Fetches OHLCV history (KBS) với per-ticker retry
- Persists parquet cache để incremental runs chỉ fetch ngày mới
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

from .config import (
    BACKFILL_YEARS,
    DATA_CACHE_FILENAME,
    DATA_SOURCE,
    EXCHANGE,
    OUTPUT_DIR,
)

logger = logging.getLogger(__name__)


def _cache_path() -> Path:
    p = Path(OUTPUT_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p / DATA_CACHE_FILENAME


def _start_date() -> str:
    d = date.today() - timedelta(days=int(BACKFILL_YEARS * 365.25))
    return d.strftime("%Y-%m-%d")


def _today() -> str:
    return date.today().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_hose_tickers() -> list[str]:
    """Return sorted list of all HOSE-listed equity tickers."""
    try:
        from vnstock import listing
        df = listing().all_symbols()
        tickers = (
            df.loc[df["exchange"].str.upper() == EXCHANGE, "symbol"]
            .dropna()
            .str.upper()
            .sort_values()
            .tolist()
        )
        logger.info("Listing: %d HOSE tickers fetched", len(tickers))
        return tickers
    except Exception as exc:
        logger.error("get_hose_tickers failed: %s", exc)
        raise


def fetch_ohlcv_all(
    tickers: list[str],
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    retry: int = 3,
    sleep_between: float = 0.15,
) -> Dict[str, pd.DataFrame]:
    """
    Fetch OHLCV cho mọi ticker. Trả về dict[ticker -> DataFrame].
    Tickers lỗi liên tục bị bỏ qua và log lại.
    """
    start = start or _start_date()
    end   = end   or _today()

    try:
        from vnstock import stock_historical_data
    except ImportError:
        stock_historical_data = None

    results: Dict[str, pd.DataFrame] = {}

    for i, ticker in enumerate(tickers, 1):
        for attempt in range(1, retry + 1):
            try:
                if stock_historical_data is not None:
                    raw = stock_historical_data(
                        symbol=ticker,
                        start_date=start,
                        end_date=end,
                        resolution="1D",
                        type="stock",
                        beautify=True,
                        source=DATA_SOURCE,
                    )
                else:
                    from vnstock import Vnstock
                    raw = (
                        Vnstock()
                        .stock(symbol=ticker, source=DATA_SOURCE)
                        .quote.history(start=start, end=end, interval="1D")
                    )

                df = _normalise_ohlcv(raw, ticker)
                if df is not None and not df.empty:
                    results[ticker] = df
                break

            except Exception as exc:
                if attempt == retry:
                    logger.warning("SKIP %s after %d attempts: %s", ticker, retry, exc)
                else:
                    time.sleep(sleep_between * (2 ** attempt))

        if i % 50 == 0:
            logger.info("  fetched %d / %d tickers ...", i, len(tickers))
        time.sleep(sleep_between)

    logger.info("fetch_ohlcv_all done: %d / %d tickers loaded", len(results), len(tickers))
    return results


def _normalise_ohlcv(raw: pd.DataFrame, ticker: str) -> Optional[pd.DataFrame]:
    if raw is None or raw.empty:
        return None

    df = raw.copy()

    date_candidates = ["time", "date", "tradingDate", "TradingDate", "Date"]
    date_col = next((c for c in date_candidates if c in df.columns), None)
    if date_col is None:
        logger.debug("%s: no date column in %s", ticker, df.columns.tolist())
        return None

    df["date"] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date").sort_index()

    col_map = {
        "open":   ["open",  "Open",  "mở cửa"],
        "high":   ["high",  "High",  "cao nhất"],
        "low":    ["low",   "Low",   "thấp nhất"],
        "close":  ["close", "Close", "đóng cửa", "closePrice"],
        "volume": ["volume","Volume","khối lượng"],
    }
    rename: dict[str, str] = {}
    for canonical, aliases in col_map.items():
        found = next((c for c in aliases if c in df.columns), None)
        if found:
            rename[found] = canonical

    df = df.rename(columns=rename)
    required = ["open", "high", "low", "close"]
    if not all(c in df.columns for c in required):
        logger.debug("%s: missing OHLC columns after normalise", ticker)
        return None

    keep = required + (["volume"] if "volume" in df.columns else [])
    df = df[keep].apply(pd.to_numeric, errors="coerce").dropna(subset=["close"])
    return df


# ---------------------------------------------------------------------------
# Cache layer
# ---------------------------------------------------------------------------

def load_cache() -> Dict[str, pd.DataFrame]:
    p = _cache_path()
    if not p.exists():
        logger.info("No cache at %s — full fetch required", p)
        return {}
    try:
        combined = pd.read_parquet(p)
        result: Dict[str, pd.DataFrame] = {}
        for ticker, grp in combined.groupby("ticker"):
            result[str(ticker)] = grp.drop(columns="ticker").set_index("date")
        logger.info("Cache loaded: %d tickers", len(result))
        return result
    except Exception as exc:
        logger.warning("Cache read error (%s) — full fetch will run", exc)
        return {}


def save_cache(data: Dict[str, pd.DataFrame]) -> None:
    if not data:
        return
    frames = []
    for ticker, df in data.items():
        tmp = df.copy().reset_index()
        tmp["ticker"] = ticker
        frames.append(tmp)
    combined = pd.concat(frames, ignore_index=True)
    combined["date"] = pd.to_datetime(combined["date"])
    p = _cache_path()
    combined.to_parquet(p, index=False, engine="pyarrow")
    logger.info("Cache saved: %d tickers → %s", len(data), p)


def incremental_fetch(
    cached: Dict[str, pd.DataFrame],
    tickers: list[str],
) -> Dict[str, pd.DataFrame]:
    """Chỉ fetch ngày mới — tickers chưa có cache thì full fetch."""
    today = _today()

    if cached:
        last_dates = [df.index.max() for df in cached.values() if not df.empty]
        cache_end  = max(last_dates).strftime("%Y-%m-%d") if last_dates else _start_date()
    else:
        cache_end = _start_date()

    if cache_end >= today:
        logger.info("Cache is current (%s) — skipping fetch", cache_end)
        return cached

    new_start = (pd.Timestamp(cache_end) + timedelta(days=1)).strftime("%Y-%m-%d")
    logger.info("Incremental fetch: %s → %s", new_start, today)

    fresh = fetch_ohlcv_all(tickers, start=new_start, end=today)

    merged: Dict[str, pd.DataFrame] = {}
    for t in set(cached) | set(fresh):
        parts = [df for df in [cached.get(t), fresh.get(t)] if df is not None]
        if parts:
            combined = pd.concat(parts).sort_index()
            merged[t] = combined[~combined.index.duplicated(keep="last")]

    return merged
