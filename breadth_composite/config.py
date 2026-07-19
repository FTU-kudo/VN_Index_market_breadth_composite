"""
config.py — Breadth Composite Pipeline Configuration
All tunable parameters in one place. Import from here, never hardcode.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import List

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

MA_WINDOWS: List[int] = [20, 50, 200]

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
# Regime thresholds (Bước 2)
# ---------------------------------------------------------------------------

@dataclass
class RegimeThresholds:
    bull_entry:       float = 70.0
    bear_entry:       float = 30.0
    hysteresis_band:  float = 5.0

REGIME_THRESHOLDS = RegimeThresholds()
