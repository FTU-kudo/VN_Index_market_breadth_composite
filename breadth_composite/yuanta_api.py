# breadth_composite/yuanta_api.py
"""
yuanta_api.py — HTTP client for the market-data provider (price board + history).

Everything that talks to the network lives here, so the rest of the pipeline can
be tested offline with a stub session.

Endpoints used
--------------
  GET {price_board}/stock/list_stock_info?exchange=HSX
        -> {"success": true, "response": [{StockCode, Exchange, SymbolType,
            RefP, CeilingP, FloorP, ..., TotalVol, TotalVal, ...}, ...]}
        Prices are in VND. Used for the universe.
  GET {price_board}/index/list_index
        -> same envelope, one row per index (IndexCode, IndexName, CurrIndex, ...).
        Not needed for breadth; useful for probing.
  GET {history}?symbol=VIC&resolution=1D&from=<epoch>&to=<epoch>&limit=100000
        -> {"s": "ok", "bars": [{t, o, h, l, c, v}, ...]} ascending.
        `t` is 00:00 UTC of the trade date; prices are in thousand VND;
        `v` is shares. Works for indexes too (symbol=VNINDEX).

Provider quirks handled here
----------------------------
1. A `to` in the future is required to receive the current day's bar, so the
   default end date is "latest finished session + 2 days".
2. Edge protection sometimes answers HTTP 200 with an HTML "Attention Required"
   page containing `window.location.href = "<same path>?jskey=..."`. That
   redirect is followed (same host only, max HTTP_MAX_CHALLENGE_HOPS hops) and
   cookies are kept per thread, which is what makes it work from CI runners.
3. `s != "ok"` is not an error: "no_data" means the symbol simply has no bars
   in the requested window, so an empty frame is returned.

Nothing in this module writes logs to user-facing output and no attribution is
added to reports: the subscription does not require one.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from .config import (
    HTTP_BACKOFF_BASE,
    HTTP_MAX_CHALLENGE_HOPS,
    HTTP_MAX_RETRIES,
    HTTP_TIMEOUT,
    HISTORY_LIMIT,
    HISTORY_RESOLUTION,
    SESSION_CLOSE_CUTOFF,
    VN_TZ,
    YUANTA_ALLOWED_HOSTS,
    YUANTA_HISTORY_BASE,
    YUANTA_PRICE_BOARD_BASE,
)

logger = logging.getLogger(__name__)

HISTORY_COLUMNS: Tuple[str, ...] = ("open", "high", "low", "close", "volume")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 breadth-composite/3.0"
)

_JS_REDIRECT_RE = re.compile(
    r"""(?:window\.|document\.|self\.)?location(?:\.href)?\s*=\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
_META_REFRESH_RE = re.compile(
    r"""<meta[^>]+http-equiv\s*=\s*['"]?refresh['"]?[^>]*content\s*=\s*['"][^'"]*?url\s*=\s*([^'"]+)['"]""",
    re.IGNORECASE,
)
_EPOCH_MS_THRESHOLD = 1e11   # 1e11 seconds ≈ year 5138, so bigger means milliseconds


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class YuantaError(RuntimeError):
    """Base class for every provider error raised by this module."""


class YuantaHTTPError(YuantaError):
    """Transport failure, HTTP 4xx/5xx, or exhausted retries."""


class YuantaChallengeError(YuantaError):
    """The edge protection page could not be resolved safely."""


class YuantaPayloadError(YuantaError):
    """The response was not JSON, or did not have the documented shape."""


# ---------------------------------------------------------------------------
# Market calendar
# ---------------------------------------------------------------------------

def vietnam_now() -> datetime:
    """Current time in Vietnam (the CI runner is UTC)."""
    return datetime.now(ZoneInfo(VN_TZ))


def latest_session_date(now_vn: Optional[datetime] = None) -> date:
    """
    The most recent *finished* trading session, Vietnam time.

    * Before SESSION_CLOSE_CUTOFF (15:15) the running session is not finished,
      so yesterday is returned.
    * Weekends roll back to Friday.
    * Public holidays are not modelled: on a holiday the provider simply has no
      new bar, so the previous session is returned by the data itself.

    A naive datetime is interpreted as Vietnam time.
    """
    if now_vn is None:
        now_vn = vietnam_now()
    elif now_vn.tzinfo is not None:
        now_vn = now_vn.astimezone(ZoneInfo(VN_TZ))

    cutoff_h, cutoff_m = SESSION_CLOSE_CUTOFF
    day = now_vn.date()
    if (now_vn.hour, now_vn.minute) < (cutoff_h, cutoff_m):
        day -= timedelta(days=1)
    while day.weekday() >= 5:          # Sat=5, Sun=6
        day -= timedelta(days=1)
    return day


# ---------------------------------------------------------------------------
# Small helpers (module-level so they are easy to unit test)
# ---------------------------------------------------------------------------

def _coerce_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(str(value)).date()


def _to_epoch(day: date) -> int:
    """00:00 UTC of `day`, which is how the provider encodes bar dates."""
    return int(datetime.combine(day, dtime(0, 0), tzinfo=timezone.utc).timestamp())


def _epoch_to_date(value: Any) -> Optional[date]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    unit = "ms" if abs(ts) >= _EPOCH_MS_THRESHOLD else "s"
    try:
        stamp = pd.Timestamp(ts, unit=unit, tz="UTC")
    except (ValueError, OverflowError, pd.errors.OutOfBoundsDatetime):
        return None
    return stamp.date()


def _looks_like_json(text: str) -> bool:
    stripped = (text or "").lstrip()
    return bool(stripped) and stripped[0] in "{["


def _extract_redirect_target(text: str) -> Optional[str]:
    """Find the JS (or meta-refresh) redirect target inside a challenge page."""
    for pattern in (_JS_REDIRECT_RE, _META_REFRESH_RE):
        match = pattern.search(text or "")
        if match:
            target = match.group(1).strip()
            if target:
                return target
    return None


def _empty_history() -> pd.DataFrame:
    return pd.DataFrame(
        {col: pd.Series(dtype="float64") for col in HISTORY_COLUMNS},
        index=pd.DatetimeIndex([], name="date"),
    )


def _num(value: Any, fallback: Optional[float] = None) -> Optional[float]:
    """float(value), or `fallback` when the field is missing/blank/NaN."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if pd.isna(number):
        return fallback
    return number


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class YuantaClient:
    """
    Thread-aware client.

    Parameters
    ----------
    session_factory : callable
        Returns a `requests.Session`-compatible object. A single stub can be
        passed in tests (`session_factory=lambda: stub`); by default each thread
        gets its own session so cookies from the challenge page stay together.
    sleep : callable
        Injected for tests to avoid real waiting.
    clock : callable
        Returns "now" (naive = Vietnam time). Injected for deterministic tests.
    """

    def __init__(
        self,
        *,
        session_factory: Optional[Callable[[], Any]] = None,
        price_board_base: str = YUANTA_PRICE_BOARD_BASE,
        history_base: str = YUANTA_HISTORY_BASE,
        timeout: float = HTTP_TIMEOUT,
        max_retries: int = HTTP_MAX_RETRIES,
        backoff_base: float = HTTP_BACKOFF_BASE,
        max_challenge_hops: int = HTTP_MAX_CHALLENGE_HOPS,
        allowed_hosts: Sequence[str] = YUANTA_ALLOWED_HOSTS,
        user_agent: str = DEFAULT_USER_AGENT,
        sleep: Callable[[float], None] = time.sleep,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._session_factory = session_factory or requests.Session
        self._local = threading.local()

        self.price_board_base = price_board_base.rstrip("/")
        self.history_base = history_base
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.backoff_base = float(backoff_base)
        self.max_challenge_hops = int(max_challenge_hops)
        self.allowed_hosts = tuple(h.lower() for h in allowed_hosts)
        self.user_agent = user_agent
        self._sleep = sleep
        self._clock = clock or vietnam_now
        self.request_count = 0
        self.challenge_count = 0
        self._count_lock = threading.Lock()

    # -- plumbing ----------------------------------------------------------

    @property
    def session(self) -> Any:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._session_factory()
            self._local.session = session
        return session

    def now(self) -> datetime:
        return self._clock()

    def latest_session_date(self) -> date:
        return latest_session_date(self.now())

    def _bump(self, attr: str) -> None:
        with self._count_lock:
            setattr(self, attr, getattr(self, attr) + 1)

    def _backoff(self, attempt: int, reason: str) -> None:
        delay = self.backoff_base * (2 ** attempt)
        logger.warning("request failed (%s) — retrying in %.1fs", reason, delay)
        self._sleep(delay)

    def _get(self, url: str, params: Optional[Mapping[str, Any]]) -> Any:
        self._bump("request_count")
        return self.session.get(
            url,
            params=dict(params) if params else None,
            timeout=self.timeout,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/plain, */*",
            },
        )

    def _assert_allowed_host(self, url: str) -> None:
        host = _host_of(url)
        if host not in self.allowed_hosts:
            raise YuantaChallengeError(
                f"challenge redirect points to an unlisted host ({host or 'none'}); refusing to follow"
            )

    def _request_json(
        self,
        url: str,
        params: Optional[Mapping[str, Any]] = None,
        hop: int = 0,
    ) -> Any:
        attempt = 0
        while True:
            try:
                response = self._get(url, params)
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise YuantaHTTPError(f"GET {url} failed after {attempt + 1} attempts: {exc}") from exc
                self._backoff(attempt, type(exc).__name__)
                attempt += 1
                continue

            status = int(getattr(response, "status_code", 200) or 200)
            text = getattr(response, "text", "") or ""

            if status == 429 or status >= 500:
                if attempt >= self.max_retries:
                    raise YuantaHTTPError(f"HTTP {status} from {url} after {attempt + 1} attempts")
                self._backoff(attempt, f"HTTP {status}")
                attempt += 1
                continue

            if status >= 400:
                raise YuantaHTTPError(f"HTTP {status} from {url}")

            if _looks_like_json(text):
                try:
                    return json.loads(text)
                except ValueError as exc:
                    raise YuantaPayloadError(f"invalid JSON from {url}: {text[:200]!r}") from exc

            target = _extract_redirect_target(text)
            if target:
                if hop >= self.max_challenge_hops:
                    raise YuantaChallengeError(
                        f"still challenged after {self.max_challenge_hops} redirects ({url})"
                    )
                next_url = target if target.lower().startswith("http") else urljoin(url, target)
                self._assert_allowed_host(next_url)
                self._bump("challenge_count")
                logger.info("edge challenge answered — following redirect (hop %d)", hop + 1)
                return self._request_json(next_url, None, hop + 1)

            raise YuantaPayloadError(f"non-JSON response from {url}: {text[:200]!r}")

    # -- public endpoints --------------------------------------------------

    def stock_info(
        self,
        *,
        exchange: Optional[str] = None,
        index_code: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """All symbols on one exchange, or the constituents of one index."""
        params: Dict[str, Any] = {}
        if exchange:
            params["exchange"] = str(exchange).strip().upper()
        if index_code:
            params["index_code"] = str(index_code).strip().upper()
        if not params:
            raise ValueError("stock_info() requires exchange= or index_code=")

        payload = self._request_json(f"{self.price_board_base}/stock/list_stock_info", params)
        rows = _unwrap_list(payload, context="list_stock_info")
        logger.info("price board: %d rows (params=%s)", len(rows), params)
        return rows

    def index_list(self) -> List[Dict[str, Any]]:
        payload = self._request_json(f"{self.price_board_base}/index/list_index")
        rows = _unwrap_list(payload, context="list_index")
        logger.info("price board: %d indexes", len(rows))
        return rows

    def history(
        self,
        symbol: str,
        *,
        start: Any = None,
        end: Any = None,
        resolution: str = HISTORY_RESOLUTION,
        limit: int = HISTORY_LIMIT,
        max_date: Optional[date] = None,
    ) -> pd.DataFrame:
        """
        Daily bars for one symbol, ascending, indexed by trade date.

        `max_date` drops bars after that date. Callers pass
        `latest_session_date()` so an unfinished session never reaches the cache.
        """
        sym = str(symbol).strip().upper()
        if not sym:
            raise ValueError("history() requires a symbol")

        start_day = _coerce_date(start) if start is not None else date(2000, 1, 1)
        if end is not None:
            end_day = _coerce_date(end)
        else:
            # A future `to` date is required to receive the current day's bar.
            anchor = max_date or self.latest_session_date()
            end_day = anchor + timedelta(days=2)

        if end_day < start_day:
            return _empty_history()

        payload = self._request_json(
            self.history_base,
            {
                "symbol": sym,
                "resolution": resolution,
                "from": _to_epoch(start_day),
                "to": _to_epoch(end_day),
                "limit": int(limit),
            },
        )
        frame = _parse_history(payload, symbol=sym, max_date=max_date)
        logger.debug("history %s: %d bars (%s → %s)", sym, len(frame), start_day, end_day)
        return frame


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _unwrap_list(payload: Any, *, context: str) -> List[Dict[str, Any]]:
    """Unwrap {"success": true, "response": [...]} defensively."""
    if isinstance(payload, Mapping):
        if payload.get("success") is False:
            raise YuantaPayloadError(
                f"{context}: provider reported failure ({payload.get('message') or payload.get('error') or 'no message'})"
            )
        data = payload.get("response", payload)
    else:
        data = payload

    if isinstance(data, Mapping):
        for key in ("data", "items", "list", "rows"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                return [row for row in candidate if isinstance(row, Mapping)]
        raise YuantaPayloadError(f"{context}: response is a dict without a list payload")

    if isinstance(data, list):
        return [row for row in data if isinstance(row, Mapping)]

    raise YuantaPayloadError(f"{context}: unexpected response type {type(data).__name__}")


def _parse_history(payload: Any, *, symbol: str, max_date: Optional[date] = None) -> pd.DataFrame:
    if not isinstance(payload, Mapping):
        raise YuantaPayloadError(f"history {symbol}: unexpected response type {type(payload).__name__}")

    status = str(payload.get("s", "")).strip().lower()
    if status and status != "ok":
        if status in {"no_data", "nodata", "no-data"}:
            logger.debug("history %s: no data for the requested window", symbol)
            return _empty_history()
        raise YuantaPayloadError(f"history {symbol}: provider status {status!r}")

    bars = payload.get("bars") or []
    if not isinstance(bars, list) or not bars:
        return _empty_history()

    records: List[Tuple[date, float, float, float, float, float]] = []
    for bar in bars:
        if not isinstance(bar, Mapping):
            continue
        day = _epoch_to_date(bar.get("t"))
        if day is None:
            continue
        close = _num(bar.get("c"))
        if close is None or close <= 0:
            continue
        # Missing OHLC fields fall back to the close, missing volume to 0.
        records.append((
            day,
            _num(bar.get("o"), close),
            _num(bar.get("h"), close),
            _num(bar.get("l"), close),
            close,
            _num(bar.get("v"), 0.0),
        ))

    if not records:
        return _empty_history()

    frame = pd.DataFrame(
        records, columns=["date", *HISTORY_COLUMNS]
    ).set_index("date")
    frame.index = pd.DatetimeIndex(frame.index, name="date")
    frame = frame.sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]

    if max_date is not None:
        frame = frame.loc[frame.index <= pd.Timestamp(max_date)]

    return frame.astype("float64")
