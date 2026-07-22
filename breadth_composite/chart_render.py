"""
chart_render.py — Render breadth indicators ra PNG để Gemini Vision đọc.

Output: 1 file PNG (2x2 grid):
  [0,0] % Stocks above MA20/MA50/MA200 + VN-Index overlay (style chart cũ)
  [0,1] Advance-Decline Line + 50D trend
  [1,0] McClellan Oscillator (bar) + Summation Index (line, dual axis)
  [1,1] Net Advance/Decline Ratio % (bar + 20D MA)
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

from .config import CHART_DISPLAY_YEARS, OUTPUT_DIR

logger = logging.getLogger(__name__)

CHART_PNG_FILENAME = "breadth_chart.png"

# ---------------------------------------------------------------------------
# Palette — giống chart cũ VN_Index_and_MA_ratio_analysis
# ---------------------------------------------------------------------------
C_DARK_BLUE  = "#1F3864"
C_MID_BLUE   = "#2E75B6"
C_PURPLE     = "#7030A0"   # VN-Index line
C_CYAN       = "#00BFFF"   # MA20
C_RED_LINE   = "#FF4444"   # MA50
C_ORANGE     = "#FFA500"   # MA200
C_GREEN      = "#70AD47"
C_RED        = "#C00000"
C_GREY       = "#A0A0A0"
C_BG         = "#F9F9F9"


# ---------------------------------------------------------------------------
# Master render function
# ---------------------------------------------------------------------------

def render_breadth_chart(
    breadth: pd.DataFrame,
    vnindex: Optional[pd.Series] = None,
    output_path: Optional[str] = None,
    display_years: int = CHART_DISPLAY_YEARS,
) -> str:
    out = Path(output_path or f"{OUTPUT_DIR}/{CHART_PNG_FILENAME}")
    out.parent.mkdir(parents=True, exist_ok=True)

    cutoff = pd.Timestamp(
        date.today() - timedelta(days=int(display_years * 365.25))
    )
    df  = breadth.loc[breadth.index >= cutoff].copy()
    if vnindex is not None:
        vni_clean = vnindex[~vnindex.index.duplicated(keep="last")]
        vni = vni_clean.loc[vni_clean.index >= cutoff].reindex(df.index)
    else:
        vni = None

    if df.empty:
        logger.warning("BreadthFrame empty after trim — skipping render")
        return str(out)

    today_str = date.today().strftime("%d/%m/%Y")

    fig, axes = plt.subplots(
        2, 2,
        figsize=(18, 11),
        facecolor=C_BG,
        gridspec_kw={"hspace": 0.42, "wspace": 0.32},
    )
    fig.suptitle(
        f"VN-Index Market Breadth Dashboard — {today_str}",
        fontsize=15, fontweight="bold", color=C_DARK_BLUE, y=0.98,
    )

    _plot_ma_ratios(axes[0, 0], df, vni)
    _plot_adl(axes[0, 1], df)
    _plot_mcclellan(axes[1, 0], df)
    _plot_net_ad_ratio(axes[1, 1], df)

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
# Shared helpers
# ---------------------------------------------------------------------------

def _fmt_xaxis(ax: plt.Axes) -> None:
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.xaxis.set_minor_locator(mdates.MonthLocator(bymonth=[4, 7, 10]))
    ax.tick_params(axis="x", which="major", labelsize=8, rotation=0)
    ax.tick_params(axis="x", which="minor", length=3, width=0.5)


def _style_ax(ax: plt.Axes, title: str) -> None:
    ax.set_facecolor("#FFFFFF")
    ax.set_title(title, fontsize=10, fontweight="bold", color=C_DARK_BLUE, pad=7)
    ax.tick_params(axis="y", labelsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color("#CCCCCC")
    ax.grid(axis="y", linestyle="--", linewidth=0.5, alpha=0.5, color="#DDDDDD")
    _fmt_xaxis(ax)


def _annotate_last(
    ax: plt.Axes,
    values: pd.Series,
    fmt: str = ".1f",
    suffix: str = "",
    offset_x: int = 6,
    offset_y: int = 0,
) -> None:
    valid = values.dropna()
    if valid.empty:
        return
    last_val  = valid.iloc[-1]
    last_date = valid.index[-1]
    color = C_GREEN if last_val >= 0 else C_RED
    ax.annotate(
        f"{last_val:{fmt}}{suffix}",
        xy=(last_date, last_val),
        xytext=(offset_x, offset_y),
        textcoords="offset points",
        fontsize=8, fontweight="bold", color=color,
        bbox=dict(boxstyle="round,pad=0.2", fc="white", ec=color, alpha=0.8),
    )


# ---------------------------------------------------------------------------
# Subplot [0,0]: % Stocks above MA — style giống chart cũ
# ---------------------------------------------------------------------------

def _plot_ma_ratios(
    ax: plt.Axes,
    df: pd.DataFrame,
    vni: Optional[pd.Series],
) -> None:
    _style_ax(ax, "Market Breadth: VN-Index & % Stocks Above MA Lines")

    dates = df.index

    # Smooth nhẹ 3-day để bớt noise — giống chart cũ
    cfg = [
        ("pct_above_ma20",  C_CYAN,     "% > MA20",  1.0, 0.80),
        ("pct_above_ma50",  C_RED_LINE, "% > MA50",  1.2, 0.90),
        ("pct_above_ma200", C_ORANGE,   "% > MA200", 1.4, 1.00),
    ]
    for col, color, label, lw, alpha in cfg:
        if col in df.columns:
            smoothed = df[col].rolling(3, min_periods=1).mean()
            ax.plot(dates, smoothed, color=color, lw=lw,
                    label=label, alpha=alpha)

    # Vùng tham chiếu 20% / 80%
    ax.axhline(80, color=C_GREEN, lw=0.8, ls="--", alpha=0.55)
    ax.axhline(60, color=C_GREEN, lw=0.5, ls=":",  alpha=0.35)
    ax.axhline(40, color=C_RED,   lw=0.5, ls=":",  alpha=0.35)
    ax.axhline(20, color=C_RED,   lw=0.8, ls="--", alpha=0.55)

    ax.fill_between(dates, 80, 100, color=C_GREEN, alpha=0.05)
    ax.fill_between(dates, 0,  20,  color=C_RED,   alpha=0.05)

    if len(dates) > 0:
        ax.text(dates[0], 81, "80%", fontsize=6.5, color=C_GREEN, alpha=0.75)
        ax.text(dates[0], 21, "20%", fontsize=6.5, color=C_RED,   alpha=0.75)

    ax.set_ylim(0, 100)
    ax.set_ylabel("Percentage (%)", fontsize=8, color=C_DARK_BLUE)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x)}%"))
    ax.legend(loc="upper left", fontsize=7, framealpha=0.75,
              ncol=3, columnspacing=0.8)

    if "pct_above_ma50" in df.columns:
        _annotate_last(ax, df["pct_above_ma50"].rolling(3, min_periods=1).mean(),
                       fmt=".1f", suffix="%")

    # VN-Index overlay — đường tím đậm giống chart cũ
    if vni is not None and vni.notna().any():
        ax2 = ax.twinx()
        ax2.plot(dates, vni, color=C_PURPLE, lw=1.8,
                 alpha=0.85, label="VN-Index")
        ax2.set_ylabel("VN-Index (Index Points)", fontsize=8, color=C_PURPLE)
        ax2.tick_params(axis="y", labelsize=7, labelcolor=C_PURPLE)
        ax2.spines[["top"]].set_visible(False)
        ax2.spines["right"].set_color("#CCCCCC")
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
                    where=(adl >= 0), color=C_GREEN, alpha=0.22, interpolate=True)
    ax.fill_between(dates, adl, 0,
                    where=(adl < 0),  color=C_RED,   alpha=0.22, interpolate=True)
    ax.plot(dates, adl, color=C_DARK_BLUE, lw=1.5)
    ax.axhline(0, color=C_GREY, lw=0.8)

    trend = adl.rolling(50, min_periods=10).mean()
    ax.plot(dates, trend, color=C_ORANGE, lw=1.0, ls="--",
            alpha=0.75, label="50D Trend")

    ax.set_ylabel("Cumulative Net A-D", fontsize=8, color=C_DARK_BLUE)
    ax.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda x, _: f"{int(x):,}")
    )
    ax.legend(loc="upper left", fontsize=7, framealpha=0.75)
    _annotate_last(ax, adl, fmt=",.0f")


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

    bar_colors = np.where(osc.values >= 0, C_GREEN, C_RED)
    ax.bar(dates, osc.values, color=bar_colors, alpha=0.72,
           width=1.5, label="Oscillator", zorder=2)
    ax.axhline(0, color=C_GREY, lw=0.8, zorder=3)

    signal = osc.ewm(span=10, adjust=False).mean()
    ax.plot(dates, signal, color=C_DARK_BLUE, lw=1.2,
            label="10D EMA", zorder=4)

    ax.set_ylabel("McClellan Osc", fontsize=8, color=C_DARK_BLUE)
    ax.legend(loc="upper left", fontsize=7, framealpha=0.75)
    _annotate_last(ax, osc, fmt=".1f")

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
# Subplot [1,1]: Net Advance/Decline Ratio — thay thế 52W H/L
# ---------------------------------------------------------------------------

def _plot_net_ad_ratio(ax: plt.Axes, df: pd.DataFrame) -> None:
    _style_ax(ax, "Net Advance / Decline Ratio (%)")

    if "advances" not in df.columns or "declines" not in df.columns:
        ax.text(0.5, 0.5, "No A/D data", transform=ax.transAxes,
                ha="center", va="center", color=C_GREY, fontsize=10)
        return

    dates    = df.index
    advances = df["advances"].fillna(0)
    declines = df["declines"].fillna(0)
    total    = advances + declines

    net_ratio = pd.Series(
        np.where(total > 0, (advances - declines) / total * 100, np.nan),
        index=dates,
    )

    bar_colors = np.where(net_ratio.fillna(0).values >= 0, C_GREEN, C_RED)
    ax.bar(dates, net_ratio.fillna(0).values,
           color=bar_colors, alpha=0.72, width=1.5, zorder=2)
    ax.axhline(0, color=C_GREY, lw=0.8, zorder=3)

    smooth = net_ratio.rolling(20, min_periods=3).mean()
    ax.plot(dates, smooth, color=C_DARK_BLUE, lw=1.5,
            label="20D MA", zorder=4)

    ax.axhline( 20, color=C_GREEN, lw=0.7, ls="--", alpha=0.5)
    ax.axhline(-20, color=C_RED,   lw=0.7, ls="--", alpha=0.5)

    ax.set_ylim(-105, 105)
    ax.set_ylabel("Net A/D %", fontsize=8, color=C_DARK_BLUE)
    ax.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda x, _: f"{int(x)}%")
    )
    ax.legend(loc="upper left", fontsize=7, framealpha=0.75)
    _annotate_last(ax, net_ratio, fmt=".1f", suffix="%")
