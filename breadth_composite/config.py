"""
config.py — Breadth Composite Pipeline Configuration
All tunable parameters in one place. Import from here, never hardcode.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List

# ---------------------------------------------------------------------------
# Data & Fetch
# ---------------------------------------------------------------------------

DATA_SOURCE = "KBS"
EXCHANGE    = "HOSE"

BACKFILL_YEARS      = 6
CHART_DISPLAY_YEARS = 5

# ---------------------------------------------------------------------------
# Moving-Average Windows
# ---------------------------------------------------------------------------

MA_WINDOWS: List[int] = [5, 20, 50, 200]    # thêm MA5 cho momentum ngắn hạn

# ---------------------------------------------------------------------------
# Advance-Decline Line
# ---------------------------------------------------------------------------

ADL_LOOKBACK_DAYS = 252 * BACKFILL_YEARS

# ---------------------------------------------------------------------------
# McClellan Oscillator
# ---------------------------------------------------------------------------

MCCLELLAN_FAST_EMA       = 19
MCCLELLAN_SLOW_EMA       = 39
MCCLELLAN_SUMMATION_SEED = 0

# ---------------------------------------------------------------------------
# 52-Week High / Low
# ---------------------------------------------------------------------------

HIGH_LOW_WINDOW = 126

# ---------------------------------------------------------------------------
# [NEW] Momentum & Thrust (Nhóm 1)
# ---------------------------------------------------------------------------

BREADTH_THRUST_PERIOD = 10   # Số ngày EMA cho Whaley Breadth Thrust
MOMENTUM_CONSEC_DAYS  = 3    # Số ngày liên tiếp tăng để đếm pct_rising_Nd

# ---------------------------------------------------------------------------
# [NEW] Volume Breadth / TRIN (Nhóm 2)
# ---------------------------------------------------------------------------

TRIN_MA_PERIOD = 10          # MA làm mượt TRIN ngắn hạn
UPVOL_MA_PERIOD = 20         # MA làm mượt UpVol Ratio

# ---------------------------------------------------------------------------
# [NEW] Cumulative Breadth (Nhóm 3)
# ---------------------------------------------------------------------------

HIGH_LOW_LINE_SEED = 0       # Seed khởi đầu cho High-Low Line tích lũy

# ---------------------------------------------------------------------------
# [NEW] Regime Detection & Composite Score (Nhóm 4)
# ---------------------------------------------------------------------------

REGIME_ADL_SLOPE_WINDOW = 20          # Số ngày để tính slope của ADL
REGIME_MA200_BULL_THRESH = 60.0       # % > MA200 > ngưỡng này = bullish
REGIME_MA200_BEAR_THRESH = 40.0       # % > MA200 < ngưỡng này = bearish

# Trọng số Composite Score (tổng = 1.0)
# Mỗi thành phần được chuẩn hóa về 0-100 trước khi nhân trọng số
COMPOSITE_WEIGHTS: Dict[str, float] = {
    "pct_above_ma50":    0.25,   # Xu hướng trung hạn — quan trọng nhất
    "pct_above_ma200":   0.20,   # Xu hướng dài hạn
    "adl_norm":          0.20,   # Dòng tiền breadth tích lũy
    "mcclellan_norm":    0.15,   # Momentum ngắn hạn
    "net_new_highs_norm":0.10,   # Chất lượng đỉnh mới
    "upvol_ratio":       0.10,   # Dòng tiền volume (graceful nếu thiếu)
}

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

OUTPUT_DIR            = "output"
EXCEL_FILENAME        = "breadth_composite.xlsx"
DATA_CACHE_FILENAME   = "breadth_data_cache.parquet"

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

EMAIL_SUBJECT_PREFIX = "[Breadth] "

# ---------------------------------------------------------------------------
# Regime thresholds
# ---------------------------------------------------------------------------

@dataclass
class RegimeThresholds:
    bull_entry:       float = 70.0
    bear_entry:       float = 30.0
    hysteresis_band:  float = 5.0

REGIME_THRESHOLDS = RegimeThresholds()
