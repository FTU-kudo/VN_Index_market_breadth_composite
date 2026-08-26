"""
breadth_calc.py — Market Breadth Indicator Engine (v2.0)

Indicators (Nhóm gốc):
  pct_above_ma5/20/50/200  — % cổ phiếu trên đường MA
  advances/declines/unchanged/adl  — Advance-Decline Line
  mcclellan_osc/mcclellan_sum  — McClellan Oscillator & Summation
  new_highs/new_lows/net_new_highs_pct  — Đỉnh/Đáy 52 tuần

Indicators mới (Nhóm 1 — Momentum & Thrust):
  breadth_thrust     — Whaley Breadth Thrust (10D EMA advances ratio)
  pct_rising_3d      — % cổ phiếu tăng liên tiếp 3 ngày

Indicators mới (Nhóm 2 — Volume Breadth / TRIN):
  adv_vol            — Tổng khối lượng của cổ phiếu tăng giá
  dec_vol            — Tổng khối lượng của cổ phiếu giảm giá
  upvol_ratio        — AdvVol / (AdvVol + DecVol) * 100 (%)
  trin               — Arms Index: (Adv/Dec) / (AdvVol/DecVol), TRIN < 1 = bullish

Indicators mới (Nhóm 3 — Cumulative Breadth):
  high_low_line      — Cumsum(NewHighs - NewLows), xác nhận xu hướng dài hạn
  adv_vol_line       — Cumsum(AdvVol - DecVol), xác nhận ADL bằng volume

Indicators mới (Nhóm 4 — Regime Detection & Composite Score):
  adl_slope          — Tốc độ thay đổi ADL chuẩn hóa (20D)
  regime             — Phân loại: 1=Bull, 0=Transition, -1=Bear
  composite_score    — Điểm tổng hợp 0-100 (nhiệt kế thị trường)
"""

from __future__ import annotations

import logging
from typing import Dict

import numpy as np
import pandas as pd

from .config import (
    BREADTH_THRUST_PERIOD,
    COMPOSITE_WEIGHTS,
    HIGH_LOW_LINE_SEED,
    HIGH_LOW_WINDOW,
    MA_WINDOWS,
    MCCLELLAN_FAST_EMA,
    MCCLELLAN_SLOW_EMA,
    MCCLELLAN_SUMMATION_SEED,
    MOMENTUM_CONSEC_DAYS,
    REGIME_ADL_SLOPE_WINDOW,
    REGIME_MA200_BEAR_THRESH,
    REGIME_MA200_BULL_THRESH,
)

logger = logging.getLogger(__name__)

OHLCVDict = Dict[str, pd.DataFrame]


def compute_all(ohlcv: OHLCVDict) -> pd.DataFrame:
    """
    Entry point chính. Trả về BreadthFrame — daily DataFrame với tất cả indicators.
    ohlcv: dict[ticker -> DataFrame với DatetimeIndex và các cột open/high/low/close/volume]
    """
    if not ohlcv:
        raise ValueError("ohlcv dict is empty — nothing to compute")

    close  = _build_close_matrix(ohlcv)
    volume = _build_volume_matrix(ohlcv)
    logger.info("Close matrix: %d dates × %d tickers", *close.shape)

    # Nhóm gốc
    ma_frame = _pct_above_ma(close)
    ad_frame = _advance_decline(close)
    mc_frame = _mcclellan(ad_frame["advances"], ad_frame["declines"])
    hl_frame = _high_low(close)

    # Nhóm 1 — Momentum & Thrust
    mom_frame = _momentum_thrust(close, ad_frame)

    # Nhóm 2 — Volume Breadth / TRIN (graceful nếu không có volume)
    vol_frame = _volume_breadth(close, volume)

    # Nhóm 3 — Cumulative Breadth
    cum_frame = _cumulative_breadth(ad_frame, vol_frame, hl_frame)

    # Ghép tất cả lại
    breadth = pd.concat(
        [ma_frame, ad_frame, mc_frame, hl_frame, mom_frame, vol_frame, cum_frame],
        axis=1,
    ).sort_index()
    breadth.index.name = "date"

    # Nhóm 4 — Regime & Composite Score (cần toàn bộ các chỉ báo trước)
    regime_frame = _regime_score(breadth)
    breadth = pd.concat([breadth, regime_frame], axis=1)

    logger.info("BreadthFrame ready: %d rows × %d cols", *breadth.shape)
    return breadth


# ---------------------------------------------------------------------------
# 1. % Stocks above Moving Average (gốc, mở rộng thêm MA5)
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
# 2. Advance-Decline Line (gốc, giữ nguyên)
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
# 3. McClellan Oscillator & Summation Index (gốc, giữ nguyên)
# ---------------------------------------------------------------------------

def _mcclellan(advances: pd.Series, declines: pd.Series) -> pd.DataFrame:
    """Ratio-Adjusted McClellan — comparable across pool sizes."""
    total     = advances + declines
    ratio_net = pd.Series(
        np.where(total > 0, (advances - declines) / total * 1000, np.nan),
        index=advances.index,
    )

    fast       = ratio_net.ewm(span=MCCLELLAN_FAST_EMA, adjust=False, min_periods=5).mean()
    slow       = ratio_net.ewm(span=MCCLELLAN_SLOW_EMA, adjust=False, min_periods=5).mean()
    oscillator = fast - slow
    summation  = oscillator.cumsum() + MCCLELLAN_SUMMATION_SEED

    return pd.DataFrame({
        "mcclellan_osc": oscillator,
        "mcclellan_sum": summation,
    })


# ---------------------------------------------------------------------------
# 4. 52-Week High / Low (gốc, giữ nguyên)
# ---------------------------------------------------------------------------

def _high_low(close: pd.DataFrame) -> pd.DataFrame:
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
# [NEW] 5. Momentum & Thrust (Nhóm 1)
# ---------------------------------------------------------------------------

def _momentum_thrust(
    close: pd.DataFrame,
    ad_frame: pd.DataFrame,
) -> pd.DataFrame:
    """
    Whaley Breadth Thrust và momentum ngắn hạn.
    breadth_thrust: EMA của tỷ lệ Advances/(Advances+Declines), tính theo %.
    pct_rising_3d:  % cổ phiếu tăng liên tiếp trong MOMENTUM_CONSEC_DAYS ngày.
    """
    advances = ad_frame["advances"]
    declines = ad_frame["declines"]

    # Breadth Thrust — Whaley: EMA(Adv/Total)
    total     = advances + declines
    adv_ratio = pd.Series(
        np.where(total > 0, advances / total * 100, np.nan),
        index=advances.index,
    )
    breadth_thrust = adv_ratio.ewm(
        span=BREADTH_THRUST_PERIOD, adjust=False, min_periods=3
    ).mean()

    # % Stocks rising N consecutive days (vectorized)
    chg     = close.diff()
    n_days  = MOMENTUM_CONSEC_DAYS
    # Mỗi cổ phiếu: True nếu tất cả n ngày gần nhất đều dương
    # Dùng rolling min trên (chg > 0) — nếu min = 1 thì tất cả đều True
    rising_flag = (chg > 0).astype(float)
    consec_rise = rising_flag.rolling(n_days, min_periods=n_days).min()  # 1 nếu tất cả dương
    active      = close.notna() & close.shift(n_days).notna()
    n_active    = active.sum(axis=1).astype(float)
    n_consec    = (consec_rise == 1).sum(axis=1).astype(float)

    pct_rising_3d = pd.Series(
        np.where(n_active > 0, n_consec / n_active * 100, np.nan),
        index=close.index,
    )

    return pd.DataFrame({
        "breadth_thrust": breadth_thrust,
        "pct_rising_3d":  pct_rising_3d,
    })


# ---------------------------------------------------------------------------
# [NEW] 6. Volume Breadth / TRIN (Nhóm 2)
# ---------------------------------------------------------------------------

def _volume_breadth(
    close: pd.DataFrame,
    volume: pd.DataFrame,
) -> pd.DataFrame:
    """
    Tính các chỉ báo dựa trên khối lượng giao dịch.
    Graceful fallback: nếu volume matrix trống, trả về NaN cho tất cả.

    adv_vol     : Tổng KL của cổ phiếu tăng giá hôm nay
    dec_vol     : Tổng KL của cổ phiếu giảm giá hôm nay
    upvol_ratio : AdvVol / (AdvVol + DecVol) * 100 (%)
    trin        : (Adv/Dec) / (AdvVol/DecVol) — TRIN < 1 = bullish
    """
    idx = close.index

    # Nếu không có volume — trả về NaN frame (không crash pipeline)
    if volume is None or volume.empty:
        logger.info("Volume matrix trống — TRIN/UpVol sẽ là NaN (graceful fallback)")
        nan_s = pd.Series(np.nan, index=idx)
        return pd.DataFrame({
            "adv_vol":    nan_s,
            "dec_vol":    nan_s,
            "upvol_ratio": nan_s,
            "trin":       nan_s,
        })

    # Align volume với close (chỉ lấy tickers có cả 2)
    common = close.columns.intersection(volume.columns)
    if len(common) == 0:
        logger.warning("Không có ticker nào có cả close lẫn volume — volume breadth sẽ là NaN")
        nan_s = pd.Series(np.nan, index=idx)
        return pd.DataFrame({
            "adv_vol":    nan_s,
            "dec_vol":    nan_s,
            "upvol_ratio": nan_s,
            "trin":       nan_s,
        })

    c = close[common]
    v = volume[common].reindex(c.index)

    chg     = c.diff()
    traded  = c.notna() & c.shift(1).notna() & v.notna() & (v > 0)

    advances = ((chg > 0) & traded).astype(float)
    declines = ((chg < 0) & traded).astype(float)

    # Tổng KL theo nhóm (vectorized — không dùng for loop)
    adv_vol_s = (advances * v).sum(axis=1)
    dec_vol_s = (declines * v).sum(axis=1)

    total_vol = adv_vol_s + dec_vol_s

    upvol_ratio = pd.Series(
        np.where(total_vol > 0, adv_vol_s / total_vol * 100, np.nan),
        index=idx,
    )

    # TRIN = (Advances/Declines) / (AdvVol/DecVol)
    # TRIN < 1 : Bullish (dòng tiền mạnh vào cổ phiếu tăng)
    # TRIN > 1 : Bearish (khối lượng lớn nghiêng về cổ phiếu giảm)
    adv_cnt = advances.sum(axis=1).astype(float)
    dec_cnt = declines.sum(axis=1).astype(float)

    trin = pd.Series(np.nan, index=idx)
    denom_ok = (dec_cnt > 0) & (dec_vol_s > 0)
    trin[denom_ok] = (
        (adv_cnt[denom_ok] / dec_cnt[denom_ok])
        / (adv_vol_s[denom_ok] / dec_vol_s[denom_ok])
    )
    # Clip TRIN ở mức 0.1 - 10 để tránh outlier cực đoan
    trin = trin.clip(0.1, 10.0)

    return pd.DataFrame({
        "adv_vol":     adv_vol_s,
        "dec_vol":     dec_vol_s,
        "upvol_ratio": upvol_ratio,
        "trin":        trin,
    })


# ---------------------------------------------------------------------------
# [NEW] 7. Cumulative Breadth (Nhóm 3)
# ---------------------------------------------------------------------------

def _cumulative_breadth(
    ad_frame: pd.DataFrame,
    vol_frame: pd.DataFrame,
    hl_frame: pd.DataFrame,
) -> pd.DataFrame:
    """
    Các chỉ báo breadth tích lũy dài hạn.

    high_low_line : Cumsum(NewHighs - NewLows) — xác nhận xu hướng chính
    adv_vol_line  : Cumsum(AdvVol - DecVol) — xác nhận ADL bằng dòng tiền
    """
    # High-Low Line
    new_highs   = hl_frame["new_highs"]
    new_lows    = hl_frame["new_lows"]
    high_low_line = (new_highs - new_lows).cumsum() + HIGH_LOW_LINE_SEED

    # Advance Volume Line (chỉ có khi volume hợp lệ)
    adv_vol = vol_frame["adv_vol"]
    dec_vol = vol_frame["dec_vol"]

    if adv_vol.notna().any():
        adv_vol_line = (adv_vol.fillna(0) - dec_vol.fillna(0)).cumsum()
        adv_vol_line[adv_vol.isna() & dec_vol.isna()] = np.nan
    else:
        adv_vol_line = pd.Series(np.nan, index=ad_frame.index)

    return pd.DataFrame({
        "high_low_line": high_low_line,
        "adv_vol_line":  adv_vol_line,
    })


# ---------------------------------------------------------------------------
# [NEW] 8. Regime Detection & Composite Score (Nhóm 4)
# ---------------------------------------------------------------------------

def _regime_score(breadth: pd.DataFrame) -> pd.DataFrame:
    """
    Phân loại trạng thái thị trường và tính Composite Score.

    adl_slope       : Tốc độ thay đổi ADL chuẩn hóa (20D diff / rolling std)
                      > 0 = ADL đang tăng, < 0 = đang giảm

    regime          : +1 = Bull, 0 = Transition, -1 = Bear
                      Dựa trên pct_above_ma200 và adl_slope (rule-based)

    composite_score : Điểm tổng hợp 0-100
                      Tổng trọng số của các chỉ báo cốt lõi đã chuẩn hóa
    """
    idx = breadth.index

    # --- adl_slope ---
    adl       = breadth.get("adl", pd.Series(np.nan, index=idx))
    adl_diff  = adl.diff(REGIME_ADL_SLOPE_WINDOW)
    adl_std   = adl.rolling(REGIME_ADL_SLOPE_WINDOW * 2, min_periods=10).std()
    adl_slope = pd.Series(
        np.where(adl_std > 0, adl_diff / adl_std, np.nan),
        index=idx,
    )

    # --- regime (rule-based) ---
    ma200 = breadth.get("pct_above_ma200", pd.Series(np.nan, index=idx))
    regime = pd.Series(0, index=idx, dtype=int)  # default = Transition

    bull_mask = (ma200 >= REGIME_MA200_BULL_THRESH) & (adl_slope > 0)
    bear_mask = (ma200 <= REGIME_MA200_BEAR_THRESH) & (adl_slope < 0)

    regime[bull_mask] = 1
    regime[bear_mask] = -1

    # --- Composite Score (0-100) ---
    # Bước 1: Chuẩn hóa từng chỉ báo về [0, 100]
    def _norm_series(s: pd.Series, lo: float, hi: float) -> pd.Series:
        """Min-max normalize với bounds cố định để nhất quán qua thời gian."""
        return ((s - lo) / (hi - lo) * 100).clip(0, 100)

    components: Dict[str, pd.Series] = {}

    # pct_above_ma50: đã là % nên bounds = 0-100
    if "pct_above_ma50" in breadth.columns:
        components["pct_above_ma50"] = _norm_series(breadth["pct_above_ma50"], 0, 100)

    # pct_above_ma200: đã là %
    if "pct_above_ma200" in breadth.columns:
        components["pct_above_ma200"] = _norm_series(breadth["pct_above_ma200"], 0, 100)

    # adl_norm: chuẩn hóa ADL slope (range -5 đến +5)
    if adl_slope.notna().any():
        components["adl_norm"] = _norm_series(adl_slope, -3, 3)

    # mcclellan_norm: McClellan Osc trong khoảng -100 đến +100
    if "mcclellan_osc" in breadth.columns:
        components["mcclellan_norm"] = _norm_series(breadth["mcclellan_osc"], -150, 150)

    # net_new_highs_norm: net_new_highs_pct là %
    if "net_new_highs_pct" in breadth.columns:
        components["net_new_highs_norm"] = _norm_series(
            breadth["net_new_highs_pct"], -50, 50
        )

    # upvol_ratio: đã là % (0-100)
    if "upvol_ratio" in breadth.columns and breadth["upvol_ratio"].notna().any():
        components["upvol_ratio"] = _norm_series(breadth["upvol_ratio"], 0, 100)

    # Bước 2: Tính weighted average (chỉ dùng components có dữ liệu)
    composite = pd.Series(np.nan, index=idx)
    if components:
        weight_sum = 0.0
        score_sum  = pd.Series(0.0, index=idx)
        for key, series in components.items():
            w = COMPOSITE_WEIGHTS.get(key, 0.0)
            if w > 0:
                score_sum  = score_sum.add(series.fillna(50) * w)  # 50 = neutral nếu NaN
                weight_sum += w

        if weight_sum > 0:
            composite = (score_sum / weight_sum).clip(0, 100)
            # Nếu hầu hết components là NaN → composite cũng NaN
            # Tính số components có dữ liệu thực
            has_data = pd.DataFrame(
                {k: s.notna().astype(int) for k, s in components.items()}
            ).sum(axis=1)
            composite[has_data < 2] = np.nan

    return pd.DataFrame({
        "adl_slope":       adl_slope,
        "regime":          regime,
        "composite_score": composite,
    })


# ---------------------------------------------------------------------------
# Utility — Build matrices từ ohlcv dict
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

    # Lọc giá âm hoặc bằng 0 — không hợp lệ
    close = close.where(close > 0)

    # Lọc spike bất thường — thay đổi >50%/ngày bằng NaN
    pct_chg = close.pct_change().abs()
    close   = close.where(pct_chg < 0.50)

    # Forward-fill tối đa 3 ngày (ngày lễ, suspend)
    close = close.ffill(limit=3)

    # Bỏ ngày không phải trading (gần như toàn NaN)
    min_tickers = max(10, int(len(series) * 0.05))
    return close.dropna(thresh=min_tickers)


def _build_volume_matrix(ohlcv: OHLCVDict) -> pd.DataFrame:
    """
    Xây dựng ma trận volume từ ohlcv dict.
    Trả về DataFrame rỗng nếu không có ticker nào có dữ liệu volume hợp lệ.
    """
    series = {
        ticker: df["volume"]
        for ticker, df in ohlcv.items()
        if "volume" in df.columns and not df.empty and df["volume"].notna().any()
    }
    if not series:
        logger.info("Không có dữ liệu volume trong cache — TRIN/UpVol sẽ là NaN")
        return pd.DataFrame()

    volume = pd.DataFrame(series)
    volume.index = pd.to_datetime(volume.index)
    volume = volume.sort_index()

    # Lọc volume âm hoặc bằng 0
    volume = volume.where(volume > 0)

    logger.info(
        "Volume matrix: %d dates × %d tickers (coverage %.1f%%)",
        volume.shape[0], volume.shape[1],
        volume.notna().mean().mean() * 100,
    )
    return volume
