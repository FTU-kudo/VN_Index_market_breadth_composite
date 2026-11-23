# tests/test_pipeline_smoke.py
"""End-to-end checks on synthetic data: no network, no provider, no secrets."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from breadth_composite.breadth_calc import compute_all
from breadth_composite.chart_render import render_breadth_chart
from breadth_composite.export import export_excel

REPO_ROOT = Path(__file__).resolve().parents[1]


def _synthetic_ohlcv(symbols: int = 40, rows: int = 320, seed: int = 11):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2025-06-02", periods=rows)
    data = {}
    for i in range(symbols):
        closes = 10_000 * np.exp(np.cumsum(rng.normal(0.0003, 0.011, rows)))
        data[f"S{i:03d}"] = pd.DataFrame({
            "open": closes * (1 + rng.normal(0, 0.002, rows)),
            "high": closes * (1 + np.abs(rng.normal(0, 0.004, rows))),
            "low": closes * (1 - np.abs(rng.normal(0, 0.004, rows))),
            "close": closes,
            "volume": rng.integers(1e5, 4e6, rows).astype(float),
        }, index=index)
    return data


@pytest.fixture(scope="module")
def breadth():
    return compute_all(_synthetic_ohlcv())


def test_breadth_frame_has_every_indicator(breadth):
    expected = {
        "pct_above_ma5", "pct_above_ma20", "pct_above_ma50", "pct_above_ma200",
        "advances", "declines", "unchanged", "adl",
        "mcclellan_osc", "mcclellan_sum",
        "new_highs", "new_lows", "net_new_highs_pct",
        "breadth_thrust", "pct_rising_3d",
        "adv_vol", "dec_vol", "upvol_ratio", "trin",
        "high_low_line", "adv_vol_line",
        "adl_slope", "regime", "composite_score",
    }
    assert expected <= set(breadth.columns)
    assert breadth.index.name == "date"


def test_indicator_ranges_are_sane(breadth):
    for col in ("pct_above_ma5", "pct_above_ma20", "pct_above_ma50", "pct_above_ma200",
                "upvol_ratio", "pct_rising_3d"):
        series = breadth[col].dropna()
        assert series.between(0, 100).all(), col

    assert set(breadth["regime"].unique()) <= {-1, 0, 1}
    composite = breadth["composite_score"].dropna()
    assert composite.between(0, 100).all()
    assert (breadth["new_highs"] >= 0).all()
    assert (breadth["new_lows"] >= 0).all()


def test_adl_is_the_cumulative_net_advance_decline(breadth):
    expected = (breadth["advances"] - breadth["declines"]).cumsum()
    pd.testing.assert_series_equal(breadth["adl"], expected, check_names=False)


def test_advances_declines_unchanged_cover_the_universe(breadth):
    traded = breadth["advances"] + breadth["declines"] + breadth["unchanged"]
    # the first session has no previous close, so nothing is classified yet
    assert (traded.iloc[1:] > 0).all()
    assert (traded <= 40).all()


def test_high_low_line_accumulates_net_highs(breadth):
    expected = (breadth["new_highs"] - breadth["new_lows"]).cumsum()
    pd.testing.assert_series_equal(breadth["high_low_line"], expected, check_names=False)


def test_export_writes_all_sheets(tmp_path):
    frame = compute_all(_synthetic_ohlcv(symbols=25, rows=280))
    vnindex = frame["pct_above_ma50"].rolling(1).mean().fillna(1000) + 1000

    path = export_excel(frame, vnindex=vnindex, output_path=str(tmp_path / "report.xlsx"))

    assert Path(path).exists() and Path(path).stat().st_size > 10_000
    from zipfile import ZipFile
    with ZipFile(path) as archive:
        sheets = [n for n in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)]
    assert len(sheets) == 6, sheets


def test_chart_png_is_rendered(tmp_path):
    frame = compute_all(_synthetic_ohlcv(symbols=25, rows=280))
    vnindex = pd.Series(np.linspace(1200, 1700, len(frame)), index=frame.index, name="VNINDEX")

    path = render_breadth_chart(frame, vnindex=vnindex, output_path=str(tmp_path / "chart.png"))

    assert Path(path).exists()
    assert Path(path).stat().st_size > 50_000


# ---------------------------------------------------------------------------
# Repository hygiene: the market-data SDK is gone for good
# ---------------------------------------------------------------------------

def test_no_vnstock_or_vnai_references_anywhere():
    pattern = re.compile(r"\bvnstock\b|\bvnai\b|VNSTOCK_API_KEY|vnstocks\.com", re.IGNORECASE)
    offenders = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path == Path(__file__):          # this file names the patterns on purpose
            continue
        if any(part in {".git", ".venv", "venv", "node_modules", "__pycache__", "output", ".pytest_cache"}
               for part in path.parts):
            continue
        if path.suffix not in {
            ".py", ".txt", ".md", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".json", ".ipynb",
        }:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in pattern.finditer(text):
            line = text[:match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{line}: {match.group(0)}")

    assert not offenders, "leftover references:\n" + "\n".join(offenders)


def test_requirements_have_no_third_party_data_sdk():
    requirements = (REPO_ROOT / "requirements.txt").read_text().lower()
    assert "vnstock" not in requirements
    assert "extra-index-url" not in requirements
    assert "requests" in requirements


def test_no_provider_attribution_in_user_facing_modules():
    """The subscription needs no credit: no provider name in the report/prompt text."""
    for name in ("chart_render.py", "export.py", "notify.py"):
        text = (REPO_ROOT / "breadth_composite" / name).read_text(encoding="utf-8").lower()
        assert "yuanta" not in text, f"{name} must not mention the data provider"


def test_output_dir_is_read_at_call_time(tmp_path, monkeypatch):
    """`python main.py --output-dir X` must move the cache, Excel and PNG to X."""
    import breadth_composite.config as config
    import breadth_composite.data_loader as dl
    from tests.conftest import frame

    frame_ = compute_all(_synthetic_ohlcv(symbols=20, rows=260))

    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    dl.save_cache({"AAA": frame([("2026-11-19", 10.0), ("2026-11-20", 10.1)])})
    excel = export_excel(frame_, output_path=None)
    png = render_breadth_chart(frame_, output_path=None)

    assert (tmp_path / config.DATA_CACHE_FILENAME).exists()
    assert Path(excel).parent == tmp_path, excel
    assert Path(png).parent == tmp_path, png
