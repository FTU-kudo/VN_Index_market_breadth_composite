"""
chart_render.py — Render breadth indicators ra PNG để Gemini Vision đọc.

Output: 1 file PNG (3×2 grid — 6 subplots):
  [0,0] % Stocks above MA5/20/50/200 + VN-Index overlay
  [0,1] Advance-Decline Line + 50D trend + High-Low Line (dual axis)
  [1,0] McClellan Oscillator (bar) + Summation Index (line, dual axis)
  [1,1] Net Advance/Decline Ratio % (bar + 20D MA)
  [2,0] Volume Breadth — UpVol Ratio % (bar) + TRIN (line, dual axis)
  [2,1] Composite Score (0–100) + Regime shading
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
# Palette
# ---------------------------------------------------------------------------
C_DARK_BLUE  = "#1F3864"
C_MID_BLUE   = "#2E75B6"
C_PURPLE     = "#7030A0"   # VN-Index line
C_CYAN       = "#00BFFF"   # MA20 / MA5
C_RED_LINE   = "#FF4444"   # MA50
C_ORANGE     = "#FFA500"   # MA200
C_GREEN      = "#70AD47"
C_RED        = "#C00000"
C_GREY       = "#A0A0A0"
C_BG         = "#F9F9F9"
C_TEAL       = "#00897B"   # UpVol / Volume Breadth
C_GOLD       = "#F9A825"   # Composite Score


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
    df = breadth.loc[breadth.index >= cutoff].copy()
    if vnindex is not None:
        vni_clean = vnindex[~vnindex.index.duplicated(keep="last")]
        vni = vni_clean.loc[vni_clean.index >= cutoff].reindex(df.index)
    else:
        vni = None

    if df.empty:
        logger.warning("BreadthFrame empty after trim — skipping render")
        return str(out)

    today_str = date.today().strftime("%d/%m/%Y")

    # Mở rộng sang 3×2 grid
    fig, axes = plt.subplots(
        3, 2,
        figsize=(18, 16),
        facecolor=C_BG,
        gridspec_kw={"hspace": 0.45, "wspace": 0.32},
    )
    fig.suptitle(
        f"VN-Index Market Breadth Dashboard — {today_str}",
        fontsize=15, fontweight="bold", color=C_DARK_BLUE, y=0.99,
    )

    _plot_ma_ratios(axes[0, 0], df, vni)
    _plot_adl(axes[0, 1], df)
    _plot_mcclellan(axes[1, 0], df)
    _plot_net_ad_ratio(axes[1, 1], df)
    _plot_volume_breadth(axes[2, 0], df)
    _plot_composite_score(axes[2, 1], df)

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
# Subplot [0,0]: % Stocks above MA (thêm MA5)
# ---------------------------------------------------------------------------

def _plot_ma_ratios(
    ax: plt.Axes,
    df: pd.DataFrame,
    vni: Optional[pd.Series],
) -> None:
    _style_ax(ax, "Market Breadth: % Stocks Above MA Lines + VN-Index")

    dates = df.index

    cfg = [
        ("pct_above_ma5",   C_CYAN,     "% > MA5",   0.8, 0.70),
        ("pct_above_ma20",  "#4FC3F7",  "% > MA20",  1.0, 0.80),
        ("pct_above_ma50",  C_RED_LINE, "% > MA50",  1.2, 0.90),
        ("pct_above_ma200", C_ORANGE,   "% > MA200", 1.5, 1.00),
    ]
    for col, color, label, lw, alpha in cfg:
        if col in df.columns:
            smoothed = df[col].rolling(3, min_periods=1).mean()
            ax.plot(dates, smoothed, color=color, lw=lw,
                    label=label, alpha=alpha)

    # Vùng tham chiếu
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
              ncol=2, columnspacing=0.8)

    if "pct_above_ma50" in df.columns:
        _annotate_last(ax, df["pct_above_ma50"].rolling(3, min_periods=1).mean(),
                       fmt=".1f", suffix="%")

    # VN-Index overlay
    if vni is not None and vni.notna().any():
        ax2 = ax.twinx()
        ax2.plot(dates, vni, color=C_PURPLE, lw=1.8,
                 alpha=0.85, label="VN-Index")
        ax2.set_ylabel("VN-Index", fontsize=8, color=C_PURPLE)
        ax2.tick_params(axis="y", labelsize=7, labelcolor=C_PURPLE)
        ax2.spines[["top"]].set_visible(False)
        ax2.spines["right"].set_color("#CCCCCC")
        ax2.legend(loc="upper right", fontsize=7, framealpha=0.75)


# ---------------------------------------------------------------------------
# Subplot [0,1]: ADL + High-Low Line (dual axis)
# ---------------------------------------------------------------------------

def _plot_adl(ax: plt.Axes, df: pd.DataFrame) -> None:
    _style_ax(ax, "Advance-Decline Line + High-Low Line")

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
    ax.plot(dates, adl, color=C_DARK_BLUE, lw=1.5, label="ADL")
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

    # High-Low Line trên trục phụ
    if "high_low_line" in df.columns and df["high_low_line"].notna().any():
        hl_line = df["high_low_line"]
        ax2 = ax.twinx()
        ax2.plot(dates, hl_line, color=C_TEAL, lw=1.2,
                 alpha=0.75, ls="--", label="HL Line")
        ax2.set_ylabel("High-Low Line", fontsize=7, color=C_TEAL)
        ax2.tick_params(axis="y", labelsize=7, labelcolor=C_TEAL)
        ax2.spines[["top"]].set_visible(False)
        ax2.spines["right"].set_color("#CCCCCC")
        ax2.legend(loc="lower right", fontsize=7, framealpha=0.75)


# ---------------------------------------------------------------------------
# Subplot [1,0]: McClellan Oscillator + Summation Index (giữ nguyên)
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
# Subplot [1,1]: Net Advance/Decline Ratio (giữ nguyên)
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

    # Breadth Thrust overlay
    if "breadth_thrust" in df.columns and df["breadth_thrust"].notna().any():
        # Chuyển Breadth Thrust (0-100%) thành net ratio scale (-100 đến +100)
        # bằng cách offset: (bt - 50) * 2
        bt_scaled = (df["breadth_thrust"].fillna(50) - 50) * 2
        ax.plot(dates, bt_scaled, color=C_GOLD, lw=1.0, ls=":",
                alpha=0.8, label="Thrust*2", zorder=5)

    ax.axhline( 20, color=C_GREEN, lw=0.7, ls="--", alpha=0.5)
    ax.axhline(-20, color=C_RED,   lw=0.7, ls="--", alpha=0.5)

    ax.set_ylim(-105, 105)
    ax.set_ylabel("Net A/D %", fontsize=8, color=C_DARK_BLUE)
    ax.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda x, _: f"{int(x)}%")
    )
    ax.legend(loc="upper left", fontsize=7, framealpha=0.75)
    _annotate_last(ax, net_ratio, fmt=".1f", suffix="%")


# ---------------------------------------------------------------------------
# [NEW] Subplot [2,0]: Volume Breadth — UpVol Ratio + TRIN
# ---------------------------------------------------------------------------

def _plot_volume_breadth(ax: plt.Axes, df: pd.DataFrame) -> None:
    _style_ax(ax, "Volume Breadth — UpVol Ratio (%) & TRIN")

    has_upvol = ("upvol_ratio" in df.columns
                 and df["upvol_ratio"].notna().any())
    has_trin  = ("trin" in df.columns
                 and df["trin"].notna().any())

    if not has_upvol and not has_trin:
        ax.text(
            0.5, 0.5,
            "Volume data unavailable\n(graceful fallback — chạy lại với --full để có volume)",
            transform=ax.transAxes, ha="center", va="center",
            color=C_GREY, fontsize=9, multialignment="center",
        )
        return

    dates = df.index

    if has_upvol:
        upvol = df["upvol_ratio"]
        bar_colors = np.where(upvol.fillna(50).values >= 50, C_TEAL, C_RED)
        ax.bar(dates, upvol.fillna(50).values,
               color=bar_colors, alpha=0.65, width=1.5, zorder=2,
               label="UpVol Ratio %")
        ax.axhline(50, color=C_GREY, lw=0.8, zorder=3)
        ax.axhline(70, color=C_TEAL, lw=0.7, ls="--", alpha=0.55)
        ax.axhline(30, color=C_RED,  lw=0.7, ls="--", alpha=0.55)

        smooth = upvol.rolling(10, min_periods=2).mean()
        ax.plot(dates, smooth, color=C_DARK_BLUE, lw=1.4,
                label="10D MA", zorder=4)

        if len(dates) > 0:
            ax.text(dates[0], 71, "70%", fontsize=6.5, color=C_TEAL, alpha=0.75)
            ax.text(dates[0], 31, "30%", fontsize=6.5, color=C_RED,  alpha=0.75)

        ax.set_ylim(0, 100)
        ax.set_ylabel("UpVol Ratio (%)", fontsize=8, color=C_DARK_BLUE)
        ax.yaxis.set_major_formatter(
            mticker.FuncFormatter(lambda x, _: f"{int(x)}%")
        )
        ax.legend(loc="upper left", fontsize=7, framealpha=0.75)
        _annotate_last(ax, upvol, fmt=".1f", suffix="%")

    # TRIN trên trục phụ (inverted: TRIN thấp = tốt)
    if has_trin:
        trin = df["trin"]
        ax2  = ax.twinx()
        ax2.plot(dates, trin, color=C_ORANGE, lw=1.2,
                 alpha=0.80, label="TRIN", zorder=5)
        ax2.axhline(1.0, color=C_ORANGE, lw=0.7, ls="--", alpha=0.5)
        ax2.set_ylabel("TRIN (< 1 = Bullish)", fontsize=7, color=C_ORANGE)
        ax2.tick_params(axis="y", labelsize=7, labelcolor=C_ORANGE)
        ax2.spines[["top"]].set_visible(False)
        ax2.spines["right"].set_color("#CCCCCC")
        ax2.set_ylim(0.1, 5.0)
        ax2.legend(loc="upper right", fontsize=7, framealpha=0.75)

        # Annotate TRIN cuối
        valid_trin = trin.dropna()
        if not valid_trin.empty:
            last_trin  = valid_trin.iloc[-1]
            last_date  = valid_trin.index[-1]
            trin_color = C_TEAL if last_trin < 1.0 else C_RED
            ax2.annotate(
                f"{last_trin:.2f}",
                xy=(last_date, last_trin),
                xytext=(6, 0), textcoords="offset points",
                fontsize=8, fontweight="bold", color=trin_color,
                bbox=dict(boxstyle="round,pad=0.2", fc="white",
                          ec=trin_color, alpha=0.8),
            )


# ---------------------------------------------------------------------------
# [NEW] Subplot [2,1]: Composite Score 0-100 + Regime shading
# ---------------------------------------------------------------------------

def _plot_composite_score(ax: plt.Axes, df: pd.DataFrame) -> None:
    _style_ax(ax, "Composite Breadth Score (0–100) & Market Regime")

    has_score  = ("composite_score" in df.columns
                  and df["composite_score"].notna().any())
    has_regime = ("regime" in df.columns
                  and df["regime"].notna().any())

    if not has_score:
        ax.text(0.5, 0.5, "No Composite Score data", transform=ax.transAxes,
                ha="center", va="center", color=C_GREY, fontsize=10)
        return

    dates = df.index
    score = df["composite_score"]

    # --- Vùng màu theo Regime ---
    if has_regime:
        regime = df["regime"]
        ax.fill_between(
            dates, 0, 100,
            where=(regime == 1),
            color=C_GREEN, alpha=0.08, label="Bull Regime",
        )
        ax.fill_between(
            dates, 0, 100,
            where=(regime == -1),
            color=C_RED, alpha=0.08, label="Bear Regime",
        )

    # --- Đường Composite Score ---
    # Tô màu gradient dựa trên mức điểm
    score_vals = score.fillna(50)
    line_colors = np.where(score_vals.values >= 60, C_TEAL,
                           np.where(score_vals.values <= 40, C_RED, C_GOLD))
    ax.plot(dates, score, color=C_GOLD, lw=2.0, zorder=4, label="Composite Score")

    # 21D MA để làm mượt
    score_ma = score.rolling(21, min_periods=5).mean()
    ax.plot(dates, score_ma, color=C_DARK_BLUE, lw=1.2, ls="--",
            alpha=0.8, zorder=5, label="21D MA")

    # Ngưỡng tham chiếu
    ax.axhline(70, color=C_GREEN, lw=0.8, ls="--", alpha=0.55)
    ax.axhline(50, color=C_GREY,  lw=0.8, ls="-",  alpha=0.40)
    ax.axhline(30, color=C_RED,   lw=0.8, ls="--", alpha=0.55)

    ax.fill_between(dates, 70, 100, color=C_GREEN, alpha=0.05)
    ax.fill_between(dates, 0,  30,  color=C_RED,   alpha=0.05)

    if len(dates) > 0:
        ax.text(dates[0], 71, "70 — Bullish",  fontsize=6.5, color=C_GREEN, alpha=0.80)
        ax.text(dates[0], 31, "30 — Bearish",  fontsize=6.5, color=C_RED,   alpha=0.80)
        ax.text(dates[0], 51, "50 — Neutral",  fontsize=6.5, color=C_GREY,  alpha=0.70)

    ax.set_ylim(0, 100)
    ax.set_ylabel("Composite Score", fontsize=8, color=C_DARK_BLUE)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x)}"))
    ax.legend(loc="upper left", fontsize=7, framealpha=0.75, ncol=2)

    # Annotate điểm cuối cùng
    valid_score = score.dropna()
    if not valid_score.empty:
        last_score = valid_score.iloc[-1]
        last_date  = valid_score.index[-1]
        score_color = (C_TEAL if last_score >= 60
                       else (C_RED if last_score <= 40 else C_GOLD))
        ax.annotate(
            f"{last_score:.1f}",
            xy=(last_date, last_score),
            xytext=(6, 0), textcoords="offset points",
            fontsize=9, fontweight="bold", color=score_color,
            bbox=dict(boxstyle="round,pad=0.3", fc="white",
                      ec=score_color, alpha=0.9),
        )
