# tests/conftest.py
"""Shared fakes: no test in this suite ever touches the network."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

import pandas as pd
import pytest

from breadth_composite.yuanta_api import YuantaClient


# ---------------------------------------------------------------------------
# HTTP fakes
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, *, payload: Any = None, text: Optional[str] = None,
                 status_code: int = 200, headers: Optional[Dict[str, str]] = None):
        self._payload = payload
        if text is not None:
            self.text = text
        elif payload is not None:
            self.text = json.dumps(payload)
        else:
            self.text = ""
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("response has no JSON body")
        return self._payload


class FakeSession:
    """Replays queued responses (or calls a selector) and records every request."""

    def __init__(self, responses: Any):
        self._callable: Optional[Callable[[str, Optional[dict]], Any]] = None
        if callable(responses):
            self._callable = responses
            self._queue: List[Any] = []
        else:
            self._queue = list(responses) if isinstance(responses, (list, tuple)) else [responses]
        self.calls: List[Dict[str, Any]] = []

    def get(self, url: str, params: Optional[dict] = None, timeout: Any = None,
            headers: Optional[dict] = None) -> Any:
        self.calls.append({
            "url": url,
            "params": dict(params or {}),
            "timeout": timeout,
            "headers": dict(headers or {}),
        })
        if self._callable is not None:
            outcome = self._callable(url, params)
        else:
            if not self._queue:
                raise AssertionError(f"unexpected request to {url}")
            outcome = self._queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @property
    def call_count(self) -> int:
        return len(self.calls)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def epoch(day: str) -> int:
    """00:00 UTC epoch for an ISO date, the provider's bar encoding."""
    return int(pd.Timestamp(day, tz="UTC").timestamp())


def bars(pairs: Sequence[tuple], volume: float = 1000.0) -> Dict[str, Any]:
    """Build a history payload from [(iso_date, close), ...]."""
    payload = []
    for day, close in pairs:
        payload.append({
            "t": epoch(day), "o": close, "h": close * 1.01,
            "l": close * 0.99, "c": close, "v": volume,
        })
    return {"s": "ok", "bars": payload}


def frame(pairs: Sequence[tuple], volume: float = 1000.0) -> pd.DataFrame:
    """Build an OHLCV frame directly (cache-shaped), same order of columns."""
    index = pd.DatetimeIndex([pd.Timestamp(d) for d, _ in pairs], name="date")
    closes = [c for _, c in pairs]
    return pd.DataFrame({
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [volume] * len(closes),
    }, index=index)


@pytest.fixture
def make_client():
    """Build a YuantaClient wired to a FakeSession, with fast retries."""

    def _factory(responses: Any, *, clock: Optional[Callable[[], datetime]] = None,
                 max_retries: int = 2, **kwargs: Any):
        session = FakeSession(responses)
        sleeps: List[float] = []
        client = YuantaClient(
            session_factory=lambda: session,
            sleep=sleeps.append,
            clock=clock or (lambda: datetime(2026, 11, 23, 17, 0)),   # Monday, after close
            allowed_hosts=("ysradar.yuanta.com.vn",),
            timeout=5.0,
            max_retries=max_retries,
            backoff_base=0.01,
            **kwargs,
        )
        client.test_session = session          # type: ignore[attr-defined]
        client.test_sleeps = sleeps            # type: ignore[attr-defined]
        return client

    return _factory


class StubClient:
    """Client double for data_loader tests: records calls, returns canned frames."""

    def __init__(self, frames: Optional[Dict[str, pd.DataFrame]] = None,
                 now: Optional[datetime] = None,
                 fail_on: Optional[Iterable[str]] = None,
                 fail_all: bool = False):
        self.frames = frames or {}
        self._now = now or datetime(2026, 11, 23, 17, 0)
        self.fail_on = set(fail_on or ())
        self.fail_all = fail_all
        self.history_calls: List[Dict[str, Any]] = []
        self.universe_calls: List[Dict[str, Any]] = []

    # data_loader relies on these two
    def now(self) -> datetime:
        return self._now

    def latest_session_date(self) -> date:
        from breadth_composite.yuanta_api import latest_session_date
        return latest_session_date(self._now)

    def stock_info(self, *, exchange: Optional[str] = None,
                   index_code: Optional[str] = None) -> List[Dict[str, Any]]:
        self.universe_calls.append({"exchange": exchange, "index_code": index_code})
        return [{"StockCode": "AAA", "SymbolType": "ST"}]

    def history(self, symbol: str, *, start: Any = None, end: Any = None,
                resolution: str = "1D", limit: int = 100000,
                max_date: Optional[date] = None) -> pd.DataFrame:
        self.history_calls.append({
            "symbol": symbol, "start": start, "end": end, "max_date": max_date,
        })
        if self.fail_all or symbol in self.fail_on:
            from breadth_composite.yuanta_api import YuantaHTTPError
            raise YuantaHTTPError(f"boom {symbol}")
        result = self.frames.get(symbol)
        if result is None:
            return pd.DataFrame(
                {c: pd.Series(dtype="float64") for c in ("open", "high", "low", "close", "volume")},
                index=pd.DatetimeIndex([], name="date"),
            )
        if max_date is not None:
            result = result.loc[result.index <= pd.Timestamp(max_date)]
        return result
