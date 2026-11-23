# tests/test_data_loader.py
"""Universe building, incremental fetch semantics, cache round-trip."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

import breadth_composite.config as config
import breadth_composite.data_loader as dl
from tests.conftest import StubClient, frame

LATEST = date(2026, 11, 23)          # Monday, after the close


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

class RecordingBoard:
    """Client double whose price board returns canned rows."""

    def __init__(self, rows):
        self.rows = rows
        self.universe_calls = []

    def now(self):
        return datetime(2026, 11, 23, 17, 0)

    def stock_info(self, *, exchange=None, index_code=None):
        self.universe_calls.append({"exchange": exchange, "index_code": index_code})
        return self.rows


def _board(rows):
    return RecordingBoard(rows)


@pytest.mark.parametrize("rows, expected", [
    ([{"StockCode": "AAA", "SymbolType": "ST"},
      {"StockCode": "BBB", "SymbolType": "ST"},
      {"StockCode": "E1VFVN30", "SymbolType": "ETF"}],
     ["AAA", "BBB"]),
    ([{"StockCode": "aaa", "SymbolType": "st"},          # case + duplicates collapse
      {"StockCode": "AAA", "SymbolType": "ST"}],
     ["AAA"]),
    ([{"StockCode": " AAA ", "SymbolType": "ST"}],
     ["AAA"]),
    ([{"StockCode": "AAA"},                              # SymbolType missing/blank -> kept
      {"StockCode": "BBB", "SymbolType": ""}],
     ["AAA", "BBB"]),
    ([{"StockCode": ""}, {"StockCode": None}],
     []),
])
def test_universe_filters_ordinary_shares(rows, expected):
    assert dl.get_hose_tickers(client=_board(rows)) == expected


def test_universe_keeps_everything_when_the_filter_would_empty_it():
    board = _board([{"StockCode": "AAA", "SymbolType": "ETF"},
                    {"StockCode": "BBB", "SymbolType": "FUND"}])

    assert dl.get_hose_tickers(client=board) == ["AAA", "BBB"]


def test_universe_requests_the_exchange_code():
    board = _board([{"StockCode": "AAA", "SymbolType": "ST"}])

    dl.get_hose_tickers(client=board)

    assert board.universe_calls == [{"exchange": "HSX", "index_code": None}]


# ---------------------------------------------------------------------------
# Incremental fetch
# ---------------------------------------------------------------------------

def _history(days: int, end: date = LATEST, base: float = 10.0) -> pd.DataFrame:
    """Closes depend on the calendar date, so an overlap has identical values
    unless `base` changes (which is exactly what a re-adjustment does)."""
    pairs = []
    for offset in range(days):
        day = end - timedelta(days=offset)
        pairs.append((day.isoformat(), base + (day - LATEST).days * 0.1))
    return frame(sorted(pairs))


def test_current_symbols_are_not_requested():
    cached = {"AAA": _history(30)}
    client = StubClient(frames={})

    merged, degraded = dl.incremental_fetch(cached, ["AAA"], client=client, latest=LATEST,
                                            min_interval=0)

    assert client.history_calls == []
    assert degraded is False
    assert list(merged) == ["AAA"]
    assert merged["AAA"]["close"].iloc[-1] == cached["AAA"]["close"].iloc[-1]


def test_new_symbol_is_backfilled_from_the_full_window():
    client = StubClient(frames={"NEW": _history(20)})

    merged, _ = dl.incremental_fetch({}, ["NEW"], client=client, latest=LATEST, min_interval=0)

    call = client.history_calls[0]
    assert call["start"] <= LATEST - timedelta(days=365 * 5)   # six-year window
    assert call["max_date"] == LATEST
    assert "NEW" in merged


def test_lagging_symbol_starts_at_the_overlap_window():
    cached = {"AAA": _history(30, end=LATEST - timedelta(days=5))}
    client = StubClient(frames={"AAA": _history(10)})

    dl.incremental_fetch(cached, ["AAA"], client=client, latest=LATEST, min_interval=0)

    start = client.history_calls[0]["start"]
    assert start == (LATEST - timedelta(days=5)) - timedelta(days=dl.ADJUST_CHECK_DAYS)


def test_corporate_action_triggers_a_full_re_download():
    cached = {"AAA": _history(30, end=LATEST - timedelta(days=5), base=100.0)}
    # Provider re-adjusted history: the overlap disagrees by ~10%
    adjusted = _history(10, base=90.0)
    client = StubClient(frames={"AAA": adjusted})

    merged, _ = dl.incremental_fetch(cached, ["AAA"], client=client, latest=LATEST, min_interval=0)

    assert len(client.history_calls) == 2, "expected a follow-up full-history request"
    assert client.history_calls[1]["start"] <= LATEST - timedelta(days=365 * 5)
    # the stale pre-adjustment rows must be replaced, not merged
    assert merged["AAA"]["close"].iloc[-1] == pytest.approx(adjusted["close"].iloc[-1])
    assert merged["AAA"]["close"].max() < 100.0


def test_unchanged_overlap_does_not_re_download():
    cached = {"AAA": _history(30, end=LATEST - timedelta(days=5))}
    fresh = _history(10)
    client = StubClient(frames={"AAA": fresh})

    merged, _ = dl.incremental_fetch(cached, ["AAA"], client=client, latest=LATEST, min_interval=0)

    assert len(client.history_calls) == 1
    # merged history keeps the old rows and appends the new ones
    assert merged["AAA"].index.max() == pd.Timestamp(LATEST)
    assert len(merged["AAA"]) > len(fresh)


def test_poisoned_future_rows_are_trimmed_from_the_cache():
    cached = {"AAA": _history(30, end=LATEST + timedelta(days=3))}
    client = StubClient(frames={})

    merged, _ = dl.incremental_fetch(cached, ["AAA"], client=client, latest=LATEST, min_interval=0)

    assert merged["AAA"].index.max() == pd.Timestamp(LATEST)


def test_delisted_symbols_leave_the_cache():
    cached = {"GONE": _history(10), "AAA": _history(30)}
    client = StubClient(frames={})

    merged, _ = dl.incremental_fetch(cached, ["AAA"], client=client, latest=LATEST, min_interval=0)

    assert "GONE" not in merged


def test_circuit_breaker_stops_after_consecutive_failures(monkeypatch):
    monkeypatch.setattr(dl, "MAX_CONSECUTIVE_FAILURES", 3)
    client = StubClient(fail_all=True)

    results, degraded, failures = dl._fetch_many(
        ["A", "B", "C", "D", "E", "F", "G", "H"],
        {},
        client=client,
        latest=LATEST,
        backfill_start=LATEST - timedelta(days=365),
        max_workers=1,
        min_interval=0,
    )

    assert degraded is True
    assert results == {}
    assert 3 <= len(failures) <= 8
    assert len(client.history_calls) <= 3, "must stop requesting once the breaker trips"


def test_missing_symbol_is_reported_as_empty_not_cached():
    client = StubClient(frames={})

    results, degraded, failures = dl._fetch_many(
        ["ZZZ"], {}, client=client, latest=LATEST,
        backfill_start=LATEST - timedelta(days=365), max_workers=1, min_interval=0,
    )

    assert results == {}
    assert degraded is False
    assert failures == []


# ---------------------------------------------------------------------------
# Frame helpers
# ---------------------------------------------------------------------------

def test_overlap_readjusted_detects_a_small_drift():
    cached = frame([("2026-11-18", 10.0), ("2026-11-19", 10.0), ("2026-11-20", 10.0)])
    drifted = frame([("2026-11-18", 10.2), ("2026-11-19", 10.0), ("2026-11-20", 10.0)])

    assert dl.overlap_readjusted(drifted, cached) is True
    assert dl.overlap_readjusted(cached, cached) is False


def test_overlap_check_needs_three_shared_dates():
    cached = frame([("2026-11-20", 10.0)])
    other = frame([("2026-11-20", 50.0)])

    assert dl.overlap_readjusted(other, cached) is False


def test_merge_prefers_fresh_values_and_sorts():
    cached = frame([("2026-11-19", 10.0), ("2026-11-20", 10.0)])
    fresh = frame([("2026-11-20", 11.0), ("2026-11-23", 12.0)])

    merged = dl.merge_frames(cached, fresh)

    assert list(merged.index) == [pd.Timestamp("2026-11-19"), pd.Timestamp("2026-11-20"),
                                  pd.Timestamp("2026-11-23")]
    assert merged.loc["2026-11-20", "close"] == 11.0
    assert list(merged.columns) == ["open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# Parquet cache
# ---------------------------------------------------------------------------

def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    data = {"AAA": _history(5), "BBB": _history(3)}

    dl.save_cache(data)
    loaded = dl.load_cache()

    assert set(loaded) == {"AAA", "BBB"}
    pd.testing.assert_frame_equal(loaded["AAA"], data["AAA"], check_freq=False)
    assert not list(tmp_path.glob("*.tmp")), "temp file must be renamed, not left behind"


def test_corrupt_cache_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    (tmp_path / dl.DATA_CACHE_FILENAME).write_bytes(b"not a parquet file")

    assert dl.load_cache() == {}


def test_saving_nothing_keeps_the_previous_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    dl.save_cache({"AAA": _history(3)})
    before = (tmp_path / dl.DATA_CACHE_FILENAME).read_bytes()

    dl.save_cache({})

    assert (tmp_path / dl.DATA_CACHE_FILENAME).read_bytes() == before


# ---------------------------------------------------------------------------
# VN-Index overlay
# ---------------------------------------------------------------------------

def test_fetch_vnindex_returns_a_close_series():
    client = StubClient(frames={"VNINDEX": _history(10, base=1600.0)})

    series = dl.fetch_vnindex(client=client, start="2026-01-01")

    assert series is not None
    assert series.name == "VNINDEX"
    assert series.iloc[-1] > 1000
    assert client.history_calls[0]["max_date"] == LATEST


def test_fetch_vnindex_failure_is_not_fatal():
    class Boom(StubClient):
        def history(self, *a, **kw):
            from breadth_composite.yuanta_api import YuantaHTTPError
            raise YuantaHTTPError("down")

    assert dl.fetch_vnindex(client=Boom(), start="2026-01-01") is None


def test_full_fetch_keeps_history_of_symbols_that_failed():
    cached = {"AAA": _history(30), "BBB": _history(30)}
    fresh = {"AAA": _history(30, base=20.0)}          # BBB failed / was not returned

    merged = dl.combine_with_cache(cached, fresh, ["AAA", "BBB", "NEW"])

    assert set(merged) == {"AAA", "BBB"}
    assert merged["AAA"]["close"].iloc[-1] == pytest.approx(20.0 + (LATEST - LATEST).days * 0.1)
    assert merged["BBB"]["close"].iloc[-1] == pytest.approx(cached["BBB"]["close"].iloc[-1])


def test_combine_with_cache_drops_delisted_symbols():
    merged = dl.combine_with_cache({"GONE": _history(5), "AAA": _history(5)}, {}, ["AAA"])

    assert set(merged) == {"AAA"}
