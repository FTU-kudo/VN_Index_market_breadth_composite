# breadth_composite/config.py
"""
config.py — Breadth Composite Pipeline Configuration
All tunable parameters in one place. Import from here, never hardcode.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Tuple

# ---------------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------------
# The price board + daily history endpoints of the market-data provider.
# No API key is required by these endpoints; the paid subscription is bound to
# the account/network that calls them.
DATA_SOURCE = "price_board"

EXCHANGE              = "HOSE"   # label used in logs / reports
YUANTA_EXCHANGE_CODE  = "HSX"    # the code the price-board endpoint expects

# Universe = every symbol on the exchange whose SymbolType is an ordinary share.
# Symbols with a missing/blank SymbolType are kept (and logged) so a schema
# change by the provider cannot silently empty the universe.
SYMBOL_TYPE_FILTER: Tuple[str, ...] = ("ST",)
UNIVERSE_KEEP_UNKNOWN_TYPE = True

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
# Momentum & Thrust (Nhóm 1)
# ---------------------------------------------------------------------------

BREADTH_THRUST_PERIOD = 10   # Số ngày EMA cho Whaley Breadth Thrust
MOMENTUM_CONSEC_DAYS  = 3    # Số ngày liên tiếp tăng để đếm pct_rising_Nd

# ---------------------------------------------------------------------------
# Volume Breadth / TRIN (Nhóm 2)
# ---------------------------------------------------------------------------

TRIN_MA_PERIOD = 10          # MA làm mượt TRIN ngắn hạn
UPVOL_MA_PERIOD = 20         # MA làm mượt UpVol Ratio

# ---------------------------------------------------------------------------
# Cumulative Breadth (Nhóm 3)
# ---------------------------------------------------------------------------

HIGH_LOW_LINE_SEED = 0       # Seed khởi đầu cho High-Low Line tích lũy

# ---------------------------------------------------------------------------
# Regime Detection & Composite Score (Nhóm 4)
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
# HTTP / API client
# ---------------------------------------------------------------------------

YUANTA_PRICE_BOARD_BASE = "https://ysradar.yuanta.com.vn/api/v3/market_data/price_board"
YUANTA_HISTORY_BASE     = "https://ysradar.yuanta.com.vn/v2/tradingview/history"
YUANTA_API_BASE         = "https://ysradarapi.yuanta.com.vn"   # reserved (financial statements)

# Challenge pages redirect with a ?jskey=… query; only these hosts may be followed.
YUANTA_ALLOWED_HOSTS: Tuple[str, ...] = ("ysradar.yuanta.com.vn", "ysradarapi.yuanta.com.vn")

HTTP_TIMEOUT            = 30.0    # seconds per request
HTTP_MAX_RETRIES        = 3       # retries per request on 429/5xx/timeout
HTTP_BACKOFF_BASE       = 1.5     # exponential backoff: 1.5s, 3s, 6s
HTTP_MAX_CHALLENGE_HOPS = 3       # max same-host challenge redirects to follow

HISTORY_RESOLUTION = "1D"
HISTORY_LIMIT      = 100_000      # bars per request (provider caps it anyway)

# The provider returns history in thousand VND (e.g. 51.06 for VIC in 2020).
# Breadth indicators are scale-free, so the pipeline keeps the provider unit.
HISTORY_PRICE_UNIT = "thousand VND"

# Incremental fetch tuning
ADJUST_CHECK_DAYS     = 15    # overlap re-downloaded to detect a re-adjusted history
ADJUST_CHECK_TOLERANCE = 0.005  # 0.5% close mismatch ⇒ corporate action, refetch all
MAX_CONSECUTIVE_FAILURES = 25  # abort the run (keep cache) after this many in a row
MAX_WORKERS          = 6      # concurrent history requests
MIN_REQUEST_INTERVAL = 0.12   # seconds between request starts (global throttle)

# ---------------------------------------------------------------------------
# Market calendar (Vietnam time)
# ---------------------------------------------------------------------------

VN_TZ = "Asia/Ho_Chi_Minh"
# A bar dated "today" is only trusted after the HOSE closing auction is done.
# Runs before this hour never cache an unfinished session.
SESSION_CLOSE_CUTOFF: Tuple[int, int] = (15, 15)   # (hour, minute) Vietnam time

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
