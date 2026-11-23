# tests/test_yuanta_api.py
"""Contract tests for the provider client: parsing, calendar, retries, challenges."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from breadth_composite.yuanta_api import (
    YuantaChallengeError,
    YuantaHTTPError,
    YuantaPayloadError,
    latest_session_date,
)
from tests.conftest import FakeResponse, bars, epoch


# ---------------------------------------------------------------------------
# History parsing
# ---------------------------------------------------------------------------

def test_history_parses_bars_into_ascending_frame(make_client):
    client = make_client(FakeResponse(payload=bars([
        ("2026-11-20", 52.5), ("2026-11-18", 51.0), ("2026-11-19", 52.0),
    ])))

    frame = client.history("VIC", start="2026-11-01", max_date=date(2026, 11, 20))

    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert frame.index.name == "date"
    assert list(frame.index) == [pd.Timestamp("2026-11-18"), pd.Timestamp("2026-11-19"), pd.Timestamp("2026-11-20")]
    assert frame["close"].tolist() == [51.0, 52.0, 52.5]
    assert frame["close"].dtype == "float64"
    assert frame["volume"].tolist() == [1000.0, 1000.0, 1000.0]


def test_history_requests_a_future_end_date(make_client):
    """The provider hides the current bar when `to` is "now" (documented quirk)."""
    client = make_client(FakeResponse(payload=bars([("2026-11-23", 10.0)])))

    client.history("VIC", start="2026-11-01")

    params = client.test_session.calls[0]["params"]
    assert params["symbol"] == "VIC"
    assert params["resolution"] == "1D"
    assert params["limit"] == 100000
    # clock = Monday 2026-11-23 17:00 VN -> latest session 2026-11-23, +2 days buffer
    assert params["to"] == epoch("2026-11-25")
    assert params["from"] == epoch("2026-11-01")


def test_history_drops_an_unfinished_session(make_client):
    """A bar dated after the last finished session must never reach the cache."""
    client = make_client(FakeResponse(payload=bars([
        ("2026-11-20", 10.0), ("2026-11-23", 11.0),
    ])))

    frame = client.history("VIC", start="2026-11-01", max_date=date(2026, 11, 20))

    assert list(frame.index) == [pd.Timestamp("2026-11-20")]


def test_history_before_close_treats_today_as_unfinished(make_client):
    """14:00 Vietnam time: today's bar is not trusted yet."""
    early = lambda: datetime(2026, 11, 23, 14, 0)          # noqa: E731
    client = make_client(FakeResponse(payload=bars([("2026-11-20", 10.0)])), clock=early)

    client.history("VIC", start="2026-11-01")

    # last finished session is Friday 20 Nov, so `to` is 22 Nov
    assert client.test_session.calls[0]["params"]["to"] == epoch("2026-11-22")


def test_history_no_data_is_empty_not_an_error(make_client):
    client = make_client(FakeResponse(payload={"s": "no_data", "nextTime": None}))

    frame = client.history("ZZZ", start="2026-11-01")

    assert frame.empty
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]


def test_history_unknown_status_raises(make_client):
    client = make_client(FakeResponse(payload={"s": "error", "errmsg": "nope"}))

    with pytest.raises(YuantaPayloadError, match="status"):
        client.history("ZZZ", start="2026-11-01")


def test_history_skips_malformed_bars(make_client):
    payload = {"s": "ok", "bars": [
        {"t": epoch("2026-11-19"), "c": 10.0, "v": 5},
        {"t": None, "c": 11.0},                       # no timestamp
        {"t": epoch("2026-11-20"), "c": 0},           # zero close
        {"t": epoch("2026-11-20"), "c": "abc"},       # unparsable
        {"t": epoch("2026-11-20"), "c": 12.0},        # good, missing OHLC -> falls back to close
    ]}
    client = make_client(FakeResponse(payload=payload))

    frame = client.history("VIC", start="2026-11-01")

    assert list(frame.index) == [pd.Timestamp("2026-11-19"), pd.Timestamp("2026-11-20")]
    assert frame.loc["2026-11-20", "open"] == 12.0
    assert frame.loc["2026-11-20", "volume"] == 0.0


def test_history_accepts_millisecond_timestamps(make_client):
    payload = {"s": "ok", "bars": [
        {"t": epoch("2026-11-20") * 1000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
    ]}
    client = make_client(FakeResponse(payload=payload))

    assert list(client.history("VIC", start="2026-11-01").index) == [pd.Timestamp("2026-11-20")]


def test_history_duplicate_dates_keep_the_last(make_client):
    payload = {"s": "ok", "bars": [
        {"t": epoch("2026-11-20"), "c": 10.0},
        {"t": epoch("2026-11-20"), "c": 20.0},
    ]}
    client = make_client(FakeResponse(payload=payload))

    frame = client.history("VIC", start="2026-11-01")

    assert len(frame) == 1
    assert frame["close"].iloc[0] == 20.0


# ---------------------------------------------------------------------------
# Challenge pages (edge protection seen from CI runner IPs)
# ---------------------------------------------------------------------------

def test_challenge_redirect_is_followed_on_the_same_host(make_client):
    challenge = FakeResponse(text=(
        '<html><head><script>window.location.href="/v2/tradingview/history'
        '?symbol=VIC&jskey=abc123";</script></head></html>'
    ))
    client = make_client([challenge, FakeResponse(payload=bars([("2026-11-20", 10.0)]))])

    frame = client.history("VIC", start="2026-11-01")

    assert client.test_session.call_count == 2
    second = client.test_session.calls[1]["url"]
    assert second.endswith("?symbol=VIC&jskey=abc123")
    assert second.startswith("https://ysradar.yuanta.com.vn/v2/tradingview/history")
    assert len(frame) == 1
    assert client.challenge_count == 1


def test_challenge_redirect_to_another_host_is_refused(make_client):
    challenge = FakeResponse(text='<script>location.href="https://evil.example.com/x?jskey=1";</script>')
    client = make_client([challenge])

    with pytest.raises(YuantaChallengeError, match="unlisted host"):
        client.history("VIC", start="2026-11-01")


def test_challenge_hop_limit_is_enforced(make_client):
    endless = lambda *_: FakeResponse(text='<script>location.href="/again?jskey=1";</script>')  # noqa: E731
    client = make_client(endless)

    with pytest.raises(YuantaChallengeError, match="still challenged"):
        client.history("VIC", start="2026-11-01")


def test_html_without_a_redirect_is_a_payload_error(make_client):
    client = make_client(FakeResponse(text="<html>Attention Required</html>"))

    with pytest.raises(YuantaPayloadError, match="non-JSON"):
        client.history("VIC", start="2026-11-01")


def test_meta_refresh_redirect_is_understood(make_client):
    challenge = FakeResponse(text=(
        '<html><meta http-equiv="refresh" content="0;url=/v2/tradingview/history?jskey=zz"></html>'
    ))
    client = make_client([challenge, FakeResponse(payload=bars([("2026-11-20", 10.0)]))])

    client.history("VIC", start="2026-11-01")

    assert client.test_session.call_count == 2


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------

def test_retries_a_503_then_succeeds(make_client):
    client = make_client([
        FakeResponse(text="", status_code=503),
        FakeResponse(payload=bars([("2026-11-20", 10.0)])),
    ])

    frame = client.history("VIC", start="2026-11-01")

    assert len(frame) == 1
    assert client.test_session.call_count == 2
    assert client.test_sleeps, "backoff should have slept between attempts"


def test_gives_up_after_max_retries(make_client):
    client = make_client([FakeResponse(text="", status_code=500)] * 5, max_retries=2)

    with pytest.raises(YuantaHTTPError, match="HTTP 500"):
        client.history("VIC", start="2026-11-01")


def test_retries_connection_errors(make_client):
    import requests
    client = make_client([
        requests.ConnectionError("connection reset"),
        FakeResponse(payload=bars([("2026-11-20", 10.0)])),
    ])

    assert len(client.history("VIC", start="2026-11-01")) == 1


def test_4xx_is_not_retried(make_client):
    client = make_client([FakeResponse(text="", status_code=403)] * 3, max_retries=2)

    with pytest.raises(YuantaHTTPError, match="HTTP 403"):
        client.history("VIC", start="2026-11-01")
    assert client.test_session.call_count == 1


# ---------------------------------------------------------------------------
# Price board endpoints
# ---------------------------------------------------------------------------

def test_stock_info_unwraps_the_response_envelope(make_client):
    payload = {"success": True, "response": [
        {"StockCode": "AAA", "SymbolType": "ST", "RefP": 6820},
        {"StockCode": "E1VFVN30", "SymbolType": "ETF", "RefP": 21000},
    ]}
    client = make_client(FakeResponse(payload=payload))

    rows = client.stock_info(exchange="hsx")

    assert [r["StockCode"] for r in rows] == ["AAA", "E1VFVN30"]
    call = client.test_session.calls[0]
    assert call["url"].endswith("/api/v3/market_data/price_board/stock/list_stock_info")
    assert call["params"] == {"exchange": "HSX"}


def test_stock_info_supports_index_constituents(make_client):
    client = make_client(FakeResponse(payload={"success": True, "response": [{"StockCode": "VNM"}]}))

    client.stock_info(index_code="vn100")

    assert client.test_session.calls[0]["params"] == {"index_code": "VN100"}


def test_stock_info_requires_a_parameter(make_client):
    client = make_client(FakeResponse(payload={"success": True, "response": []}))

    with pytest.raises(ValueError):
        client.stock_info()


def test_success_false_raises_with_the_provider_message(make_client):
    client = make_client(FakeResponse(payload={"success": False, "message": "invalid exchange"}))

    with pytest.raises(YuantaPayloadError, match="invalid exchange"):
        client.stock_info(exchange="HSX")


def test_index_list_unwraps(make_client):
    client = make_client(FakeResponse(payload={"success": True, "response": [
        {"IndexCode": "VNINDEX"}, {"IndexCode": "VN30"},
    ]}))

    rows = client.index_list()

    assert [r["IndexCode"] for r in rows] == ["VNINDEX", "VN30"]
    assert client.test_session.calls[0]["url"].endswith("/index/list_index")


# ---------------------------------------------------------------------------
# Market calendar
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("moment, expected", [
    (datetime(2026, 11, 23, 9, 0),  date(2026, 11, 20)),   # Mon before close -> Fri
    (datetime(2026, 11, 23, 15, 14), date(2026, 11, 20)),  # Mon 15:14 -> Fri
    (datetime(2026, 11, 23, 15, 15), date(2026, 11, 23)),  # Mon after close -> Mon
    (datetime(2026, 11, 24, 16, 0),  date(2026, 11, 24)),  # Tue after close
    (datetime(2026, 11, 21, 12, 0),  date(2026, 11, 20)),  # Sat -> Fri
    (datetime(2026, 11, 22, 20, 0),  date(2026, 11, 20)),  # Sun -> Fri
    (datetime(2026, 11, 23, 0, 30),  date(2026, 11, 20)),  # Mon 00:30 -> Fri
])
def test_latest_session_date(moment, expected):
    assert latest_session_date(moment) == expected


def test_latest_session_date_converts_aware_timestamps_to_vietnam_time():
    # 08:15 UTC == 15:15 in Vietnam -> the session just finished
    assert latest_session_date(datetime(2026, 11, 23, 8, 15, tzinfo=timezone.utc)) == date(2026, 11, 23)
    # 08:14 UTC == 15:14 -> not yet
    assert latest_session_date(datetime(2026, 11, 23, 8, 14, tzinfo=timezone.utc)) == date(2026, 11, 20)
