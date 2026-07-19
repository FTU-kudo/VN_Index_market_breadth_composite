"""
chart_render.py — Render breadth indicators ra PNG để Gemini Vision đọc.

Output: 1 file PNG duy nhất (2x2 grid, 4 subplots) gồm:
  [0,0] % Stocks above MA20/50/200 + VN-Index (dual axis)
  [0,1] Advance-Decline Line
  [1,0] McClellan Oscillator (bar) + Summation Index (line, dual axis)
  [1,1] Net New 52W Highs/Lows (bar, green/red) + 20D MA line
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # non-interactive backend, safe cho GitHub Actions
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

from .config import CHART_DISPLAY_YEARS, OUTPUT_DIR

logger = logging.getLogger(__name__)

CHART_PNG_FILENAME = "breadth_chart.png"

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
C_DARK_BLUE = "#1F3864"
C_MID_BLUE  = "#2E75B6"
C_ORANGE    = "#ED7D31"
C_RED       = "#C00000"
C_GREEN     = "#70AD47"
C_GREY      = "#A0A0A0"
C_BG        = "#F9F9F9"


# ---------------------------------------------------------------------------
# Master render function
# ---------------------------------------------------------------------------

def render_breadth_chart(
    breadth: pd.DataFrame,
    vnindex: Optional[pd.Series] = None,
    output_path: Optional[str] = None,
    display_years: int = CHART_DISPLAY_YEARS,
) -> str:
    """
    Render PNG tổng hợp breadth indicators.

    Parameters
    ----------
    breadth       : BreadthFrame từ breadth_calc.compute_all()
    vnindex       : VN-Index close series (tuỳ chọn, overlay subplot MA)
    output_path   : override đường dẫn PNG output
    display_years : số năm hiển thị trên chart

    Returns
    -------
    str  đường dẫn tuyệt đối của file PNG
    """
    out = Path(output_path or f"{OUTPUT_DIR}/{CHART_PNG_FILENAME}")
    out.parent.mkdir(parents=True, exist_ok=True)

    # Trim theo display_years
    cutoff = pd.Timestamp(
        date.today() - timedelta(days=int(display_years * 365.25))
    )
    df  = breadth.loc[breadth.index >= cutoff].copy()
    vni = (
        vnindex.loc[vnindex.index >= cutoff].reindex(df.index)
        if vnindex is not None else None
    )

    if df.empty:
        logger.warning("render_breadth_chart: BreadthFrame empty after trim — skipping")
        return str(out)

    today_str = date.today().strftime("%d/%m/%Y")

    fig, axes = plt.subplots(
        2, 2,
        figsize=(18, 11),
        facecolor=C_BG,
        gridspec_kw={"hspace": 0.40, "wspace": 0.30},
    )
    fig.suptitle(
        f"VN-Index Market Breadth Dashboard — {today_str}",
        fontsize=15, fontweight="bold", color=C_DARK_BLUE, y=0.98,
    )

    _plot_ma_ratios(axes[0, 0], df, vni)
    _plot_adl(axes[0, 1], df)
    _plot_mcclellan(axes[1, 0], df)
    _plot_high_low(axes[1, 1], df)

    fig.text(
        0.99, 0.005,
        "github.com/FTU-kudo/VN_Index_market_breadth_composite",
        ha="right", va="bottom", fontsize=7, color=C_GREY, alpha=0.6,
    )

    fig.savefig(str(out), dpi=150, bbox_inches="tight", facecolor=C_BG)
    plt.close(fig)
    logger.info("Chart PNG saved → %s", out.resolve())
    return str(out.resolve())


# ---------------------------------------------------------------------------
# Shared axis styling
# ---------------------------------------------------------------------------

def _fmt_xaxis(ax: plt.Axes) -> None:
    """Trục X: năm lớn + quý nhỏ."""
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.xaxis.set_minor_locator(mdates.MonthLocator(bymonth=[4, 7, 10]))
    ax.tick_params(axis="x", which="major", labelsize=8, rotation=0)
    ax.tick_params(axis="x", which="minor", length=3, width=0.5)


def _style_ax(ax: plt.Axes, title: str) -> None:
    """Style chuẩn cho mọi subplot."""
    ax.set_facecolor("#FFFFFF")
    ax.set_title(title, fontsize=10, fontweight="bold", color=C_DARK_BLUE, pad=7)
    ax.tick_params(axis="y", labelsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color("#CCCCCC")
    ax.grid(axis="y", linestyle="--", linewidth=0.5, alpha=0.6, color="#DDDDDD")
    _fmt_xaxis(ax)


def _annotate_last(
    ax: plt.Axes,
    dates: pd.DatetimeIndex,
    values: pd.Series,
    fmt: str = ".1f",
    suffix: str = "",
    offset_x: int = 6,
    offset_y: int = 0,
) -> None:
    """Ghi giá trị cuối lên chart."""
    valid = values.dropna()
    if valid.empty:
        return
    last_val  = valid.iloc[-1]
    last_date = valid.index[-1]
    color     = C_GREEN if last_val >= 0 else C_RED
    ax.annotate(
        f"{last_val:{fmt}}{suffix}",
        xy=(last_date, last_val),
        xytext=(offset_x, offset_y),
        textcoords="offset points",
        fontsize=8, fontweight="bold", color=color,
        bbox=dict(boxstyle="round,pad=0.2", fc="white", ec=color, alpha=0.7),
    )


# ---------------------------------------------------------------------------
# Subplot [0,0]: % Stocks above MA20 / MA50 / MA200 + VN-Index overlay
# ---------------------------------------------------------------------------

def _plot_ma_ratios(
    ax: plt.Axes,
    df: pd.DataFrame,
    vni: Optional[pd.Series],
) -> None:
    _style_ax(ax, "% Stocks Above MA20 / MA50 / MA200")

    dates = df.index

    series_cfg = [
        ("pct_above_ma20",  C_ORANGE,   "% > MA20",  1.2, 0.85),
        ("pct_above_ma50",  C_MID_BLUE, "% > MA50",  1.6, 1.00),
        ("pct_above_ma200", C_RED,      "% > MA200", 1.6, 1.00),
    ]
    for col, color, label, lw, alpha in series_cfg:
        if col in df.columns:
            ax.plot(dates, df[col], color=color, lw=lw,
                    label=label, alpha=alpha)

    # Vùng tham chiếu 30 / 70
    ax.axhline(70, color=C_GREEN, lw=0.8, ls="--", alpha=0.55)
    ax.axhline(30, color=C_RED,   lw=0.8, ls="--", alpha=0.55)
    ax.fill_between(dates, 70, 100, color=C_GREEN, alpha=0.04)
    ax.fill_between(dates, 0,  30,  color=C_RED,   alpha=0.04)
    ax.text(dates[0], 71, "Overbought 70", fontsize=6.5,
            color=C_GREEN, alpha=0.7, va="bottom")
    ax.text(dates[0], 31, "Oversold 30",   fontsize=6.5,
            color=C_RED,   alpha=0.7, va="bottom")

    ax.set_ylim(0, 100)
    ax.set_ylabel("% Stocks", fontsize=8, color=C_DARK_BLUE)
    ax.legend(loc="upper left", fontsize=7, framealpha=0.75,
              ncol=3, columnspacing=0.8)

    # Annotate last value của MA50 (chỉ báo chính)
    if "pct_above_ma50" in df.columns:
        _annotate_last(ax, dates, df["pct_above_ma50"], fmt=".1f", suffix="%")

    # VN-Index overlay trên trục phụ
    if vni is not None and vni.notna().any():
        ax2 = ax.twinx()
        ax2.plot(dates, vni, color=C_DARK_BLUE, lw=0.9,
                 ls=":", alpha=0.45, label="VN-Index")
        ax2.set_ylabel("VN-Index", fontsize=7, color=C_DARK_BLUE, alpha=0.6)
        ax2.tick_params(axis="y", labelsize=7, labelcolor=C_DARK_BLUE)
        ax2.spines[["top", "right"]].set_color("#CCCCCC")
        ax2.spines["left"].set_visible(False)
        ax2.legend(loc="upper right", fontsize=7, framealpha=0.75)


# ---------------------------------------------------------------------------
# Subplot [0,1]: Advance-Decline Line
# ---------------------------------------------------------------------------

def _plot_adl(ax: plt.Axes, df: pd.DataFrame) -> None:
    _style_ax(ax, "Advance-Decline Line (ADL)")

    if "adl" not in df.columns or df["adl"].dropna().empty:
        ax.text(0.5, 0.5, "No ADL data", transform=ax.transAxes,
                ha="center", va="center", color=C_GREY, fontsize=10)
        return

    dates = df.index
    adl   = df["adl"]

    ax.fill_between(dates, adl, 0,
                    where=(adl >= 0), color=C_GREEN,
                    alpha=0.22, interpolate=True)
    ax.fill_between(dates, adl, 0,
                    where=(adl < 0), color=C_RED,
                    alpha=0.22, interpolate=True)
    ax.plot(dates, adl, color=C_DARK_BLUE, lw=1.5)
    ax.axhline(0, color=C_GREY, lw=0.8)

    ax.set_ylabel("Cumulative Net A-D", fontsize=8, color=C_DARK_BLUE)
    _annotate_last(ax, dates, adl, fmt=",.0f")

    # Trend line (50-day rolling mean)
    trend = adl.rolling(50, min_periods=10).mean()
    ax.plot(dates, trend, color=C_ORANGE, lw=1.0, ls="--",
            alpha=0.7, label="50D trend")
    ax.legend(loc="upper left", fontsize=7, framealpha=0.75)


# ---------------------------------------------------------------------------
# Subplot [1,0]: McClellan Oscillator + Summation Index
# ---------------------------------------------------------------------------

def _plot_mcclellan(ax: plt.Axes, df: pd.DataFrame) -> None:
    _style_ax(ax, "McClellan Oscillator & Summation Index")

    if "mcclellan_osc" not in df.columns or df["mcclellan_osc"].dropna().empty:
        ax.text(0.5, 0.5, "No McClellan data", transform=ax.transAxes,
                ha="center", va="center", color=C_GREY, fontsize=10)
        return

    dates = df.index
    osc   = df["mcclellan_osc"].fillna(0)

    # Bar: oscillator — xanh dương khi dương, đỏ khi âm
    bar_colors = np.where(osc.values >= 0, C_GREEN, C_RED)
    ax.bar(dates, osc.values, color=bar_colors, alpha=0.72,
           width=1.5, label="Oscillator", zorder=2)
    ax.axhline(0, color=C_GREY, lw=0.8, zorder=3)

    # Signal line: 10-day EMA của oscillator
    signal = osc.ewm(span=10, adjust=False).mean()
    ax.plot(dates, signal, color=C_DARK_BLUE, lw=1.2,
            label="10D EMA", zorder=4)

    ax.set_ylabel("McClellan Osc", fontsize=8, color=C_DARK_BLUE)
    ax.legend(loc="upper left", fontsize=7, framealpha=0.75)
    _annotate_last(ax, dates, osc, fmt=".1f")

    # Summation Index trên trục phụ
    if "mcclellan_sum" in df.columns and df["mcclellan_sum"].dropna().any():
        summ = df["mcclellan_sum"]
        ax2  = ax.twinx()
        ax2.plot(dates, summ, color=C_MID_BLUE, lw=1.4,
                 alpha=0.8, label="Summation")
        ax2.axhline(0, color=C_MID_BLUE, lw=0.5, ls="--", alpha=0.4)
        ax2.set_ylabel("Summation Index", fontsize=7, color=C_MID_BLUE)
        ax2.tick_params(axis="y", labelsize=7, labelcolor=C_MID_BLUE)
        ax2.spines[["top"]].set_visible(False)
        ax2.spines["right"].set_color("#CCCCCC")
        ax2.legend(loc="upper right", fontsize=7, framealpha=0.75)


# ---------------------------------------------------------------------------
# Subplot [1,1]: Net New 52W Highs / Lows
# ---------------------------------------------------------------------------

def _plot_high_low(ax: plt.Axes, df: pd.DataFrame) -> None:
    _style_ax(ax, "Net New 52-Week Highs / Lows (%)")

    if "net_new_highs_pct" not in df.columns or \
       df["net_new_highs_pct"].dropna().empty:
        ax.text(0.5, 0.5, "No H/L data", transform=ax.transAxes,
                ha="center", va="center", color=C_GREY, fontsize=10)
        return

    dates   = df.index
    net_pct = df["net_new_highs_pct"].fillna(0)

    bar_colors = np.where(net_pct.values >= 0, C_GREEN, C_RED)
    ax.bar(dates, net_pct.values, color=bar_colors,
           alpha=0.72, width=1.5, zorder=2)
    ax.axhline(0, color=C_GREY, lw=0.8, zorder=3)

    # 20-day smoothing line
    smooth = net_pct.rolling(20, min_periods=5).mean()
    ax.plot(dates, smooth, color=C_DARK_BLUE, lw=1.4,
            label="20D MA", zorder=4)

    ax.set_ylabel("Net New Highs %", fontsize=8, color=C_DARK_BLUE)
    ax.legend(loc="upper left", fontsize=7, framealpha=0.75)
    _annotate_last(ax, dates, net_pct, fmt=".1f", suffix="%")

    # Secondary: raw counts new_highs / new_lows nếu có
    if "new_highs" in df.columns and "new_lows" in df.columns:
        nh = df["new_highs"].fillna(0)
        nl = df["new_lows"].fillna(0)
        ax2 = ax.twinx()
        ax2.plot(dates, nh, color=C_GREEN, lw=0.8,
                 alpha=0.45, label="New Highs")
        ax2.plot(dates, nl, color=C_RED,   lw=0.8,
                 alpha=0.45, label="New Lows")
        ax2.set_ylabel("Count", fontsize=7, color=C_GREY)
        ax2.tick_params(axis="y", labelsize=7, labelcolor=C_GREY)
        ax2.spines[["top"]].set_visible(False)
        ax2.spines["right"].set_color("#CCCCCC")
        ax2.legend(loc="upper right", fontsize=7, framealpha=0.75,
                   ncol=2, columnspacing=0.5)
