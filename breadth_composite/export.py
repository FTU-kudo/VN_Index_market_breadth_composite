"""
export.py — Excel Export Engine

Sheets:
  breadth_data  — full numeric table
  chart_ma      — %above MA20/50/200 vs VN-Index (combo)
  chart_adl     — ADL + McClellan Oscillator (dual-axis)
  chart_hl      — Net New 52W Highs/Lows (bar)
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import xlsxwriter

from .config import CHART_DISPLAY_YEARS, EXCEL_FILENAME, OUTPUT_DIR

logger = logging.getLogger(__name__)

# Palette
C_DARK_BLUE  = "#1F3864"
C_MID_BLUE   = "#2E75B6"
C_GREEN      = "#70AD47"
C_ORANGE     = "#ED7D31"
C_RED
