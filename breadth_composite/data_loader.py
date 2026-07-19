"""
data_loader.py — Ticker listing & OHLCV fetcher (vnstock v4 Unified UI)
- Đăng ký API key tự động từ env var VNSTOCK_API_KEY
- Dùng Reference.equity.list_by_exchange() để lấy ticker HOSE
- Dùng Market.equity.ohlcv() cho từng ticker
- Parquet cache incremental để tránh re-fetch toàn bộ mỗi ngày
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

from .config import (
    BACKFILL_YEARS,
    DATA_CACHE_FILENAME,
    EXCHANGE,
    OUTPUT_DIR,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# vnstock v4 bootstrap — đăng ký API key 1 lần khi module load
# ---------------------------------------------------------------------------

def _bootstrap_vnstock() -> None:
    """Đăng ký VNSTOCK_API_KEY từ env nếu có (60 req/phút vs 20 guest)."""
    api_key = os.environ.get("VNSTOCK_API_KEY", "").strip()
    if not api_key:
        logger.warning("VNSTOCK_API_KEY not set — running as guest (20 req/min)")
        return
    try:
        from vnstock import register_user
        register_user(api_key=api_key)
        logger.info("vnstock: authenticated (Community tier, 60 req/min)")
    except Exception as exc:
        logger.warning("vnstock register_user failed: %s — continuing as guest", exc)


_bootstrap_vnstock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    """
    Lấy danh sách tất cả mã cổ phiếu HOSE bằng vnstock v4.
    Dùng Listing().symbols_by_exchange() (VCI source) → filter exchange == HOSE
    → chỉ lấy type == STOCK.
    """
    try:
        from vnstock.explorer.vci.listing import Listing
        df = Listing().symbols_by_exchange()

        # Filter HOSE + STOCK only
        if "exchange" in df.columns:
            df = df[df["exchange"].str.upper() == EXCHANGE]
        if "type" in df.columns:
            df = df[df["type"].str.upper() == "STOCK"]

        tickers = (
            df["symbol"]
            .dropna()
            .astype(str)
            .str.upper()
            .str.strip()
            .sort_values()
            .tolist()
        )
        logger.info("Listing (HOSE): %d tickers", len(tickers))
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
    sleep_between: float = 1.0,   # 60 req/min → ~1s gap an toàn
) -> Dict[str, pd.DataFrame]:
    """
    Fetch OHLCV cho mọi ticker dùng Market.equity.ohlcv() (vnstock v4).
    Trả về dict[ticker -> DataFrame(DatetimeIndex, open/high/low/close/volume)].
    Tickers lỗi liên tục bị bỏ qua.
    """
    start = start or _start_date()
    end   = end   or _today()

    from vnstock import Market
    market = Market()

    results: Dict[str, pd.DataFrame] = {}

    for i, ticker in enumerate(tickers, 1):
        for attempt in range(1, retry + 1):
            try:
                raw = market.equity(ticker).ohlcv(
                    start=start,
                    end=end,
                    interval="1D",
                )
                df = _normalise_ohlcv(raw, ticker)
                if df is not None and not df.empty:
                    results[ticker] = df
                break  # thành công

            except Exception as exc:
                if attempt == retry:
                    logger.warning("SKIP %s after %d attempts: %s", ticker, retry, exc)
                else:
                    wait = sleep_between * (2 ** attempt)
                    logger.debug("Retry %s attempt %d in %.1fs: %s", ticker, attempt, wait, exc)
                    time.sleep(wait)

        if i % 50 == 0:
            logger.info("  fetched %d / %d tickers...", i, len(tickers))

        time.sleep(sleep_between)

    logger.info("fetch_ohlcv_all done: %d / %d tickers", len(results), len(tickers))
    return results


def _normalise_ohlcv(raw: pd.DataFrame, ticker: str) -> Optional[pd.DataFrame]:
    """
    Chuẩn hoá output từ vnstock v4 → schema chuẩn:
    DatetimeIndex + columns [open, high, low, close, volume].
    """
    if raw is None or raw.empty:
        return None

    df = raw.copy()

    # --- Date index ----------------------------------------------------------
    if isinstance(df.index, pd.DatetimeIndex):
        df.index.name = "date"
    else:
        date_candidates = ["time", "date", "tradingDate", "TradingDate", "Date"]
        date_col = next((c for c in date_candidates if c in df.columns), None)
        if date_col is None:
            # Thử dùng index nếu convert được
            try:
                df.index = pd.to_datetime(df.index, errors="raise")
                df.index.name = "date"
            except Exception:
                logger.debug("%s: no usable date column in %s", ticker, df.columns.tolist())
                return None
        else:
            df["date"] = pd.to_datetime(df[date_col], errors="coerce")
            df = df.dropna(subset=["date"]).set_index("date")

    df = df.sort_index()

    # --- Rename columns → canonical -----------------------------------------
    col_map = {
        "open":   ["open",   "Open",   "mở cửa",   "openPrice"],
        "high":   ["high",   "High",   "cao nhất",  "highPrice"],
        "low":    ["low",    "Low",    "thấp nhất", "lowPrice"],
        "close":  ["close",  "Close",  "đóng cửa",  "closePrice"],
        "volume": ["volume", "Volume", "khối lượng","matchingVolume"],
    }
    rename: dict[str, str] = {}
    for canonical, aliases in col_map.items():
        found = next((c for c in aliases if c in df.columns), None)
        if found and found != canonical:
            rename[found] = canonical

    df = df.rename(columns=rename)

    required = ["open", "high", "low", "close"]
    if not all(c in df.columns for c in required):
        logger.debug("%s: missing columns %s", ticker,
                     [c for c in required if c not in df.columns])
        return None

    keep = required + (["volume"] if "volume" in df.columns else [])
    df = df[keep].apply(pd.to_numeric, errors="coerce").dropna(subset=["close"])
    return df


# ---------------------------------------------------------------------------
# VN-Index overlay
# ---------------------------------------------------------------------------

def fetch_vnindex(start: Optional[str] = None, end: Optional[str] = None) -> Optional[pd.Series]:
    """Fetch VN-Index close series dùng Market.index.ohlcv() (vnstock v4)."""
    start = start or _start_date()
    end   = end   or _today()
    try:
        from vnstock import Market
        raw = Market().index("VNINDEX").ohlcv(start=start, end=end, interval="1D")
        df  = _normalise_ohlcv(raw, "VNINDEX")
        if df is not None:
            logger.info("VN-Index fetched: %d rows", len(df))
            return df["close"].rename("VNINDEX")
        return None
    except Exception as exc:
        logger.warning("fetch_vnindex failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Cache layer — incremental updates
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
    _cache_path().parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(_cache_path(), index=False, engine="pyarrow")
    logger.info("Cache saved: %d tickers", len(data))


def incremental_fetch(
    cached: Dict[str, pd.DataFrame],
    tickers: list[str],
) -> Dict[str, pd.DataFrame]:
    """Chỉ fetch ngày mới hơn ngày cuối trong cache."""
    today = _today()

    if cached:
        last_dates = [df.index.max() for df in cached.values() if not df.empty]
        cache_end  = max(last_dates).strftime("%Y-%m-%d") if last_dates else _start_date()
    else:
        cache_end = _start_date()

    if cache_end >= today:
        logger.info("Cache current (%s) — skip fetch", cache_end)
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
