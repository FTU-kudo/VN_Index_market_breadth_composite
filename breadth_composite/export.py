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

import pandas as pd
import xlsxwriter

from .config import CHART_DISPLAY_YEARS, EXCEL_FILENAME, OUTPUT_DIR

logger = logging.getLogger(__name__)

# Palette
C_DARK_BLUE  = "#1F3864"
C_MID_BLUE   = "#2E75B6"
C_GREEN      = "#70AD47"
C_ORANGE     = "#ED7D31"
C_RED        = "#FF0000"
C_WHITE      = "#FFFFFF"
C_LIGHT_GREY = "#F2F2F2"


def export_excel(
    breadth: pd.DataFrame,
    vnindex: Optional[pd.Series] = None,
    output_path: Optional[str] = None,
) -> str:
    out = Path(output_path or (OUTPUT_DIR + "/" + EXCEL_FILENAME))
    out.parent.mkdir(parents=True, exist_ok=True)

    cutoff    = pd.Timestamp(date.today() - timedelta(days=int(CHART_DISPLAY_YEARS * 365.25)))
    chart_df  = breadth.loc[breadth.index >= cutoff].copy()
    if vnindex is not None:
        vni_clean = vnindex[~vnindex.index.duplicated(keep="last")]
        chart_vni = vni_clean.loc[vni_clean.index >= cutoff].reindex(chart_df.index)
    else:
        chart_vni = None
      
    wb = xlsxwriter.Workbook(str(out), {"nan_inf_to_errors": True})
    _add_data_sheet(wb, breadth)
    _add_ma_chart_sheet(wb, chart_df, chart_vni)
    _add_adl_chart_sheet(wb, chart_df)
    _add_hl_chart_sheet(wb, chart_df)
    wb.close()

    logger.info("Excel written → %s", out.resolve())
    return str(out.resolve())


# ---------------------------------------------------------------------------
# Sheet 1: breadth_data
# ---------------------------------------------------------------------------

def _add_data_sheet(wb: xlsxwriter.Workbook, df: pd.DataFrame) -> None:
    ws = wb.add_worksheet("breadth_data")

    hdr_fmt  = wb.add_format({"bold": True, "bg_color": C_DARK_BLUE,
                               "font_color": C_WHITE, "border": 1,
                               "align": "center", "valign": "vcenter"})
    date_fmt = wb.add_format({"num_format": "DD/MM/YYYY", "border": 1})
    num_fmt  = wb.add_format({"num_format": "0.00", "border": 1})
    int_fmt  = wb.add_format({"num_format": "0",    "border": 1})
    alt_num  = wb.add_format({"num_format": "0.00", "border": 1,
                               "bg_color": C_LIGHT_GREY})
    alt_int  = wb.add_format({"num_format": "0",    "border": 1,
                               "bg_color": C_LIGHT_GREY})
    alt_date = wb.add_format({"num_format": "DD/MM/YYYY", "border": 1,
                               "bg_color": C_LIGHT_GREY})

    int_cols = {"advances", "declines", "unchanged", "new_highs", "new_lows"}
    cols     = ["date"] + df.columns.tolist()

    ws.set_row(0, 36)
    ws.set_column(0, 0, 14)
    for i, col in enumerate(cols):
        ws.write(0, i, col, hdr_fmt)
        ws.set_column(i, i, max(12, len(col) + 2))

    for row_i, (idx, row) in enumerate(df.iterrows(), start=1):
        alt   = (row_i % 2 == 0)
        d_fmt = alt_date if alt else date_fmt
        ws.write_datetime(row_i, 0, idx.to_pydatetime(), d_fmt)
        for col_i, col in enumerate(df.columns, start=1):
            val = row[col]
            if pd.isna(val):
                ws.write_blank(row_i, col_i, None)
                continue
            if col in int_cols:
                ws.write_number(row_i, col_i, int(val),
                                alt_int if alt else int_fmt)
            else:
                ws.write_number(row_i, col_i, float(val),
                                alt_num if alt else num_fmt)

    ws.freeze_panes(1, 1)
    ws.autofilter(0, 0, len(df), len(cols) - 1)


# ---------------------------------------------------------------------------
# Sheet 2: chart_ma — %Above MA20/50/200 vs VN-Index
# ---------------------------------------------------------------------------

def _add_ma_chart_sheet(
    wb: xlsxwriter.Workbook,
    df: pd.DataFrame,
    vnindex: Optional[pd.Series],
) -> None:
    ws = wb.add_worksheet("chart_ma")
    ws.hide_gridlines(2)

    hdr_fmt  = wb.add_format({"bold": True, "bg_color": C_DARK_BLUE,
                               "font_color": C_WHITE, "border": 1})
    date_fmt = wb.add_format({"num_format": "DD/MM/YYYY"})

    headers = ["date", "pct_above_ma20", "pct_above_ma50", "pct_above_ma200"]
    if vnindex is not None:
        headers.append("vni_close")

    for ci, h in enumerate(headers):
        ws.write(0, ci, h, hdr_fmt)

    for ri, (idx, row) in enumerate(df.iterrows(), start=1):
        ws.write_datetime(ri, 0, idx.to_pydatetime(), date_fmt)
        for ci, col in enumerate(
            ["pct_above_ma20", "pct_above_ma50", "pct_above_ma200"], start=1
        ):
            v = row.get(col)
            if v is not None and not pd.isna(v):
                ws.write_number(ri, ci, float(v))
        if vnindex is not None:
            v = vnindex.iloc[ri - 1] if ri - 1 < len(vnindex) else None
            if v is not None and not pd.isna(v):
                ws.write_number(ri, 4, float(v))

    n     = len(df) + 1
    sname = "chart_ma"
    combo = wb.add_chart({"type": "line"})

    for label, colour, ci in [
        ("% > MA20",  C_ORANGE,   1),
        ("% > MA50",  C_MID_BLUE, 2),
        ("% > MA200", C_RED,      3),
    ]:
        combo.add_series({
            "name":       label,
            "categories": [sname, 1, 0, n - 1, 0],
            "values":     [sname, 1, ci, n - 1, ci],
            "line":       {"color": colour, "width": 1.75},
        })

    combo.set_y_axis({"name": "% Stocks above MA", "min": 0, "max": 100,
                      "major_gridlines": {"visible": True,
                                          "line": {"color": "#DDDDDD"}}})
    combo.set_x_axis({"name": "Date", "date_axis": True,
                      "num_format": "MM/YY",
                      "major_unit": 90, "major_unit_type": "days"})
    combo.set_title({"name": "VN-Index Breadth — % Stocks above MA20 / MA50 / MA200"})
    combo.set_legend({"position": "bottom"})
    combo.set_size({"width": 900, "height": 400})

    if vnindex is not None:
        vni_chart = wb.add_chart({"type": "line"})
        vni_chart.add_series({
            "name":       "VN-Index",
            "categories": [sname, 1, 0, n - 1, 0],
            "values":     [sname, 1, 4, n - 1, 4],
            "line":       {"color": C_DARK_BLUE, "width": 1.5,
                           "dash_type": "dash"},
            "y2_axis":    True,
        })
        vni_chart.set_y2_axis({"name": "VN-Index"})
        combo.combine(vni_chart)

    ws.insert_chart("G2", combo)


# ---------------------------------------------------------------------------
# Sheet 3: chart_adl — ADL + McClellan Oscillator
# ---------------------------------------------------------------------------

def _add_adl_chart_sheet(wb: xlsxwriter.Workbook, df: pd.DataFrame) -> None:
    ws = wb.add_worksheet("chart_adl")
    ws.hide_gridlines(2)

    hdr_fmt  = wb.add_format({"bold": True, "bg_color": C_DARK_BLUE,
                               "font_color": C_WHITE, "border": 1})
    date_fmt = wb.add_format({"num_format": "DD/MM/YYYY"})

    for ci, h in enumerate(["date", "adl", "mcclellan_osc", "mcclellan_sum"]):
        ws.write(0, ci, h, hdr_fmt)

    for ri, (idx, row) in enumerate(df.iterrows(), start=1):
        ws.write_datetime(ri, 0, idx.to_pydatetime(), date_fmt)
        for ci, col in enumerate(
            ["adl", "mcclellan_osc", "mcclellan_sum"], start=1
        ):
            v = row.get(col)
            if v is not None and not pd.isna(v):
                ws.write_number(ri, ci, float(v))

    n     = len(df) + 1
    sname = "chart_adl"

    adl_chart = wb.add_chart({"type": "line"})
    adl_chart.add_series({
        "name":       "ADL",
        "categories": [sname, 1, 0, n - 1, 0],
        "values":     [sname, 1, 1, n - 1, 1],
        "line":       {"color": C_DARK_BLUE, "width": 1.75},
    })

    osc_chart = wb.add_chart({"type": "column"})
    osc_chart.add_series({
        "name":       "McClellan Oscillator",
        "categories": [sname, 1, 0, n - 1, 0],
        "values":     [sname, 1, 2, n - 1, 2],
        "fill":       {"color": C_MID_BLUE},
        "y2_axis":    True,
    })

    adl_chart.combine(osc_chart)
    adl_chart.set_title({"name": "Advance-Decline Line & McClellan Oscillator"})
    adl_chart.set_y_axis({"name": "ADL (cumulative)"})
    adl_chart.set_y2_axis({"name": "McClellan Osc"})
    adl_chart.set_x_axis({"name": "Date", "date_axis": True,
                           "num_format": "MM/YY",
                           "major_unit": 90, "major_unit_type": "days"})
    adl_chart.set_legend({"position": "bottom"})
    adl_chart.set_size({"width": 900, "height": 380})
    ws.insert_chart("G2", adl_chart)


# ---------------------------------------------------------------------------
# Sheet 4: chart_hl — Net New 52W Highs/Lows
# ---------------------------------------------------------------------------

def _add_hl_chart_sheet(wb: xlsxwriter.Workbook, df: pd.DataFrame) -> None:
    ws = wb.add_worksheet("chart_hl")
    ws.hide_gridlines(2)

    hdr_fmt  = wb.add_format({"bold": True, "bg_color": C_DARK_BLUE,
                               "font_color": C_WHITE, "border": 1})
    date_fmt = wb.add_format({"num_format": "DD/MM/YYYY"})

    for ci, h in enumerate(["date", "net_new_highs_pct", "new_highs", "new_lows"]):
        ws.write(0, ci, h, hdr_fmt)

    for ri, (idx, row) in enumerate(df.iterrows(), start=1):
        ws.write_datetime(ri, 0, idx.to_pydatetime(), date_fmt)
        for ci, col in enumerate(
            ["net_new_highs_pct", "new_highs", "new_lows"], start=1
        ):
            v = row.get(col)
            if v is not None and not pd.isna(v):
                ws.write_number(ri, ci, float(v))

    n     = len(df) + 1
    sname = "chart_hl"

    chart = wb.add_chart({"type": "column"})
    chart.add_series({
        "name":          "Net New Highs %",
        "categories":    [sname, 1, 0, n - 1, 0],
        "values":        [sname, 1, 1, n - 1, 1],
        "fill":          {"color": C_GREEN},
        "negative_fill": {"color": C_RED},
    })
    chart.set_title({"name": "Net New 52-Week Highs / Lows (% of Universe)"})
    chart.set_y_axis({"name": "Net %",
                      "major_gridlines": {"visible": True}})
    chart.set_x_axis({"name": "Date", "date_axis": True,
                       "num_format": "MM/YY",
                       "major_unit": 90, "major_unit_type": "days"})
    chart.set_legend({"position": "none"})
    chart.set_size({"width": 900, "height": 360})
    ws.insert_chart("G2", chart)
