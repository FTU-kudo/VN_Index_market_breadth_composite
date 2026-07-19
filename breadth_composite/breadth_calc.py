"""
breadth_calc.py — Market Breadth Indicator Engine

Indicators:
  pct_above_ma20/50/200  — % stocks above MA
  advances/declines/unchanged/adl  — Advance-Decline Line
  mcclellan_osc/mcclellan_sum  — McClellan Oscillator & Summation
  new_highs/new_lows/net_new_highs_pct  — 52-week High/Low
"""

from __future__ import annotations

import logging
from typing import Dict

import numpy as np
import pandas as pd

from .config import (
    HIGH_LOW_WINDOW,
    MA_WINDOWS,
    MCCLELLAN_FAST_EMA,
    MCCLELLAN_SLOW_EMA,
    MCCLELLAN_SUMMATION_SEED,
)

logger = logging.getLogger(__name__)

OHLCVDict = Dict[str, pd.DataFrame]


def compute_all(ohlcv: OHLCVDict) -> pd.DataFrame:
    """
    Entry point. Trả về BreadthFrame — daily DataFrame với tất cả indicators.
    """
    if not ohlcv:
        raise ValueError("ohlcv dict is empty — nothing to compute")

    close = _build_close_matrix(ohlcv)
    logger.info("Close matrix: %d dates × %d tickers", *close.shape)

    ma_frame = _pct_above_ma(close)
    ad_frame = _advance_decline(close)
    mc_frame = _mcclellan(ad_frame["advances"], ad_frame["declines"])
    hl_frame = _high_low(close)

    breadth = pd.concat([ma_frame, ad_frame, mc_frame, hl_frame], axis=1).sort_index()
    breadth.index.name = "date"

    logger.info("BreadthFrame ready: %d rows × %d cols", *breadth.shape)
    return breadth


# ---------------------------------------------------------------------------
# 1. % Stocks above Moving Average
# ---------------------------------------------------------------------------

def _pct_above_ma(close: pd.DataFrame) -> pd.DataFrame:
    frames = {}
    for window in MA_WINDOWS:
        ma    = close.rolling(window, min_periods=window).mean()
        valid = close.notna() & ma.notna()
        above = (close > ma) & valid

        n_valid = valid.sum(axis=1).astype(float)
        n_above = above.sum(axis=1).astype(float)

        pct = pd.Series(
            np.where(n_valid > 0, n_above / n_valid * 100, np.nan),
            index=close.index,
        )
        frames[f"pct_above_ma{window}"] = pct

    return pd.DataFrame(frames)


# ---------------------------------------------------------------------------
# 2. Advance-Decline Line
# ---------------------------------------------------------------------------

def _advance_decline(close: pd.DataFrame) -> pd.DataFrame:
    chg    = close.diff()
    traded = close.notna() & close.shift(1).notna()

    advances  = ((chg > 0) & traded).sum(axis=1).astype(int)
    declines  = ((chg < 0) & traded).sum(axis=1).astype(int)
    unchanged = traded.sum(axis=1).astype(int) - advances - declines
    adl       = (advances - declines).cumsum()

    return pd.DataFrame({
        "advances":  advances,
        "declines":  declines,
        "unchanged": unchanged,
        "adl":       adl,
    })


# ---------------------------------------------------------------------------
# 3. McClellan Oscillator & Summation Index
# ---------------------------------------------------------------------------

def _mcclellan(advances: pd.Series, declines: pd.Series) -> pd.DataFrame:
    """Ratio-Adjusted McClellan — comparable across pool sizes."""
    total     = advances + declines
    ratio_net = pd.Series(
        np.where(total > 0, (advances - declines) / total * 1000, np.nan),
        index=advances.index,
    )

    fast      = ratio_net.ewm(span=MCCLELLAN_FAST_EMA, adjust=False, min_periods=5).mean()
    slow      = ratio_net.ewm(span=MCCLELLAN_SLOW_EMA, adjust=False, min_periods=5).mean()
    oscillator = fast - slow
    summation  = oscillator.cumsum() + MCCLELLAN_SUMMATION_SEED

    return pd.DataFrame({
        "mcclellan_osc": oscillator,
        "mcclellan_sum": summation,
    })


# ---------------------------------------------------------------------------
# 4. 52-Week High / Low — min_periods giảm xuống để có data sớm hơn
# ---------------------------------------------------------------------------

def _high_low(close: pd.DataFrame) -> pd.DataFrame:
    # BUG FIX: min_periods = HIGH_LOW_WINDOW/2 thay vì HIGH_LOW_WINDOW
    # → có data sau ~3 tháng thay vì phải chờ đủ 6 tháng
    min_p = max(20, HIGH_LOW_WINDOW // 2)

    rolling_max = close.rolling(HIGH_LOW_WINDOW, min_periods=min_p).max()
    rolling_min = close.rolling(HIGH_LOW_WINDOW, min_periods=min_p).min()

    has_history = close.notna() & rolling_max.notna()
    new_highs   = ((close >= rolling_max) & has_history).sum(axis=1).astype(int)
    new_lows    = ((close <= rolling_min) & has_history).sum(axis=1).astype(int)
    active      = has_history.sum(axis=1)

    net_pct = pd.Series(
        np.where(active > 0, (new_highs - new_lows) / active * 100, np.nan),
        index=close.index,
    )

    return pd.DataFrame({
        "new_highs":         new_highs,
        "new_lows":          new_lows,
        "net_new_highs_pct": net_pct,
    })


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _build_close_matrix(ohlcv: OHLCVDict) -> pd.DataFrame:
    series = {
        ticker: df["close"]
        for ticker, df in ohlcv.items()
        if "close" in df.columns and not df.empty
    }
    if not series:
        raise ValueError("No valid 'close' data found in ohlcv dict")

    close = pd.DataFrame(series)
    close.index = pd.to_datetime(close.index)
    close = close.sort_index()

    # BUG FIX 1: Lọc giá âm hoặc bằng 0 — không hợp lệ
    close = close.where(close > 0)

    # BUG FIX 2: Lọc spike bất thường — thay đổi >50%/ngày bằng NaN
    # Đây là nguyên nhân gây đường thẳng đứng trên chart
    pct_chg = close.pct_change().abs()
    close   = close.where(pct_chg < 0.50)

    # BUG FIX 3: Forward-fill tối đa 3 ngày để tránh gap ngắn (ngày lễ, suspend)
    # nhưng không forward-fill quá lâu tránh tạo tín hiệu giả
    close = close.ffill(limit=3)

    # Bỏ ngày không phải trading (gần như toàn NaN)
    min_tickers = max(10, int(len(series) * 0.05))
    return close.dropna(thresh=min_tickers)
