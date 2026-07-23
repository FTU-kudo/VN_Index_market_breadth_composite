# data_loader.py — Ticker listing & OHLCV fetcher (vnstock v4 Unified UI)
# - Đăng ký API key tự động từ env var VNSTOCK_API_KEY
# - Dùng Reference.equity.list_by_exchange() để lấy ticker HOSE
# - Dùng Market.equity.ohlcv() cho từng ticker
# - Parquet cache incremental để tránh re-fetch toàn bộ mỗi ngày

from __future__ import annotations

import logging
import os
import time
from collections import deque
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

# ---------------------------------------------------------------------------
# Config (import từ module config của bạn – giữ nguyên)
# ---------------------------------------------------------------------------
from .config import (
    BACKFILL_YEARS,
    DATA_CACHE_FILENAME,
    EXCHANGE,
    OUTPUT_DIR,
)

logger = logging.getLogger(__name__)


# ============================================================================
#  RATE LIMITER (dùng chung cho mọi request đến vnstock)
# ============================================================================
class RateLimiter:
    """Limit API calls to `max_calls` per `period` seconds."""
    def __init__(self, max_calls: int = 50, period: float = 60.0):
        self.max_calls = max_calls
        self.period = period
        self.calls = deque()

    def wait(self):
        now = time.monotonic()
        # Remove timestamps older than period
        while self.calls and self.calls[0] <= now - self.period:
            self.calls.popleft()
        if len(self.calls) >= self.max_calls:
            sleep_time = self.calls[0] + self.period - now + 0.1
            logger.debug("Rate limit approaching, sleeping %.1fs", sleep_time)
            time.sleep(sleep_time)
            # Recursive call to re-check after sleep (could be many waits)
            self.wait()
        else:
            self.calls.append(now)


# Giới hạn an toàn: 45 requests/phút (dưới ngưỡng 60 của Community)
_global_limiter = RateLimiter(max_calls=45, period=60.0)


# ============================================================================
#  BOOTSTRAP VNSTOCK (đăng ký API key)
# ============================================================================
def _bootstrap_vnstock() -> None:
    """Đăng ký VNSTOCK_API_KEY từ env nếu có (60 req/phút vs 20 guest)."""
    api_key = os.environ.get("VNSTOCK_API_KEY", "").strip()
    if not api_key:
        logger.warning("VNSTOCK_API_KEY not set — running as guest (20 req/min)")
        return
    try:
        from vnstock import register_user
        register_user(api_key=api_key)
        logger.info("vnstock: authenticated (Community tier, 60 req/min)")
    except Exception as exc:
        logger.warning("vnstock register_user failed: %s — continuing as guest", exc)


_bootstrap_vnstock()


# ============================================================================
#  HELPERS
# ============================================================================
def _cache_path() -> Path:
    p = Path(OUTPUT_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p / DATA_CACHE_FILENAME


def _start_date() -> str:
    d = date.today() - timedelta(days=int(BACKFILL_YEARS * 365.25))
    return d.strftime("%Y-%m-%d")


def _today() -> str:
    return date.today().strftime("%Y-%m-%d")


def _last_trading_day() -> str:
    """
    Ngày giao dịch gần nhất — dùng làm end date khi fetch.
    T7 → T6, CN → T6, ngày thường → hôm nay.
    """
    today = date.today()
    dow = today.weekday()
    if dow == 5:          # Thứ 7
        today = today - timedelta(days=1)
    elif dow == 6:        # Chủ nhật
        today = today - timedelta(days=2)
    return today.strftime("%Y-%m-%d")


# ============================================================================
#  LẤY DANH SÁCH MÃ CỔ PHIẾU (có áp dụng RateLimiter)
# ============================================================================
def get_hose_tickers() -> list[str]:
    """
    Lấy danh sách tất cả mã cổ phiếu HOSE.
    Strategy:
      1. symbols_by_exchange() → filter HOSE + STOCK
      2. Fallback: symbols_by_group("HOSE")
      3. Fallback cuối: all_symbols() (toàn thị trường, không filter sàn)
    """
    from vnstock.explorer.vci.listing import Listing
    listing = Listing()

    # ------------------------------------------------------------------
    # Attempt 1: symbols_by_exchange — filter theo giá trị thực tế
    # ------------------------------------------------------------------
    try:
        _global_limiter.wait()  # <--- Áp dụng rate limit
        df = listing.symbols_by_exchange()
        logger.info(
            "symbols_by_exchange columns: %s | sample exchange values: %s",
            df.columns.tolist(),
            df["exchange"].unique()[:10].tolist() if "exchange" in df.columns else "N/A",
        )

        if "exchange" in df.columns and "type" in df.columns:
            ex_upper = df["exchange"].astype(str).str.upper()
            type_upper = df["type"].astype(str).str.upper()
            hose_mask = ex_upper.isin(["HOSE", "HSX"])
            stock_mask = type_upper == "STOCK"
            filtered = df.loc[hose_mask & stock_mask, "symbol"]
        elif "exchange" in df.columns:
            ex_upper = df["exchange"].astype(str).str.upper()
            hose_mask = ex_upper.isin(["HOSE", "HSX"])
            filtered = df.loc[hose_mask, "symbol"]
        else:
            # Không có cột exchange → lấy tất cả STOCK
            filtered = df.loc[
                df["type"].astype(str).str.upper() == "STOCK", "symbol"
            ] if "type" in df.columns else df["symbol"]

        tickers = (
            filtered.dropna()
            .astype(str).str.upper().str.strip()
            .sort_values().unique().tolist()
        )
        logger.info("Attempt 1 (symbols_by_exchange): %d tickers", len(tickers))
        if len(tickers) > 0:
            return tickers
    except Exception as exc:
        logger.warning("Attempt 1 failed: %s", exc)

    # ------------------------------------------------------------------
    # Attempt 2: symbols_by_group("HOSE")
    # ------------------------------------------------------------------
    try:
        _global_limiter.wait()
        series = listing.symbols_by_group("HOSE")
        tickers = (
            series.dropna()
            .astype(str).str.upper().str.strip()
            .sort_values().unique().tolist()
        )
        logger.info("Attempt 2 (symbols_by_group HOSE): %d tickers", len(tickers))
        if len(tickers) > 0:
            return tickers
    except Exception as exc:
        logger.warning("Attempt 2 failed: %s", exc)

    # ------------------------------------------------------------------
    # Attempt 3: all_symbols() — toàn thị trường, không filter sàn
    # ------------------------------------------------------------------
    try:
        _global_limiter.wait()
        df = listing.all_symbols()
        tickers = (
            df["symbol"].dropna()
            .astype(str).str.upper().str.strip()
            .sort_values().unique().tolist()
        )
        logger.info("Attempt 3 (all_symbols fallback): %d tickers", len(tickers))
        if len(tickers) > 0:
            return tickers
    except Exception as exc:
        logger.warning("Attempt 3 failed: %s", exc)

    raise RuntimeError(
        "get_hose_tickers: tất cả 3 attempts đều thất bại — "
        "kiểm tra vnstock version và network trong GitHub Actions"
    )


# ============================================================================
#  FETCH OHLCV CHO NHIỀU TICKER (có Rate Limiter & retry thông minh)
# ============================================================================
def fetch_ohlcv_all(
    tickers: list[str],
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    retry: int = 2,
    sleep_between: float = 0.1,   # chỉ để tránh CPU spike, RateLimiter đã điều tiết
) -> Dict[str, pd.DataFrame]:
    """
    Fetch OHLCV cho mọi ticker dùng Market.equity.ohlcv() (vnstock v4).
    Trả về dict[ticker -> DataFrame(DatetimeIndex, open/high/low/close/volume)].
    Tickers lỗi liên tục bị bỏ qua.
    """
    start = start or _start_date()
    end = end or _last_trading_day()

    # Guard: không có ngày mới cần fetch
    if start > end:
        logger.info("start (%s) > end (%s) — bỏ qua fetch", start, end)
        return {}

    logger.info("fetch_ohlcv_all: %d tickers | %s → %s", len(tickers), start, end)

    from vnstock import Market
    market = Market()
    results: Dict[str, pd.DataFrame] = {}

    for i, ticker in enumerate(tickers, 1):
        success = False
        for attempt in range(1, retry + 1):
            try:
                # ---- Áp dụng Rate Limiter TRƯỚC mỗi request ----
                _global_limiter.wait()
                # ------------------------------------------------
                raw = market.equity(ticker).ohlcv(
                    start=start,
                    end=end,
                    interval="1D",
                )
                df = _normalise_ohlcv(raw, ticker)
                if df is not None and not df.empty:
                    results[ticker] = df
                    success = True
                    break
                else:
                    # Nếu df rỗng (không có dữ liệu) thì coi như thành công nhưng không lưu
                    success = True
                    break
            except Exception as exc:
                # Kiểm tra xem có phải lỗi rate limit không
                error_msg = str(exc).lower()
                if "rate limit" in error_msg or "too many requests" in error_msg:
                    # Chờ lâu hơn, có thể 60s, rồi thử lại
                    wait_time = 60.0
                    logger.warning(
                        "Rate limit hit for %s, sleeping %.1fs before retry",
                        ticker, wait_time
                    )
                    time.sleep(wait_time)
                    # Tiếp tục vòng lặp (attempt sẽ tăng)
                    continue
                else:
                    # Lỗi khác (mạng, timeout,...)
                    if attempt == retry:
                        logger.warning(
                            "SKIP %s after %d attempts: %s", ticker, retry, exc
                        )
                    else:
                        wait_time = sleep_between * (2 ** attempt)  # exponential backoff
                        time.sleep(wait_time)

        if not success:
            logger.warning("Failed to fetch %s after %d attempts", ticker, retry)

        if i % 50 == 0:
            logger.info("  fetched %d / %d tickers...", i, len(tickers))

        # Giữ sleep nhẹ để tránh quá tải local (không ảnh hưởng đến rate limit)
        time.sleep(sleep_between)

    logger.info("fetch_ohlcv_all done: %d / %d tickers", len(results), len(tickers))
    return results


def _normalise_ohlcv(raw: pd.DataFrame, ticker: str) -> Optional[pd.DataFrame]:
    """
    Chuẩn hoá output từ vnstock v4 → schema chuẩn:
    DatetimeIndex + columns [open, high, low, close, volume].
    """
    if raw is None or raw.empty:
        return None

    df = raw.copy()

    # --- Date index ----------------------------------------------------------
    if isinstance(df.index, pd.DatetimeIndex):
        df.index.name = "date"
    else:
        date_candidates = ["time", "date", "tradingDate", "TradingDate", "Date"]
        date_col = next((c for c in date_candidates if c in df.columns), None)
        if date_col is None:
            # Thử dùng index nếu convert được
            try:
                df.index = pd.to_datetime(df.index, errors="raise")
                df.index.name = "date"
            except Exception:
                logger.debug("%s: no usable date column in %s", ticker, df.columns.tolist())
                return None
        else:
            df["date"] = pd.to_datetime(df[date_col], errors="coerce")
            df = df.dropna(subset=["date"]).set_index("date")

    df = df.sort_index()

    # --- Rename columns → canonical -----------------------------------------
    col_map = {
        "open":   ["open",   "Open",   "mở cửa",   "openPrice"],
        "high":   ["high",   "High",   "cao nhất",  "highPrice"],
        "low":    ["low",    "Low",    "thấp nhất", "lowPrice"],
        "close":  ["close",  "Close",  "đóng cửa",  "closePrice"],
        "volume": ["volume", "Volume", "khối lượng","matchingVolume"],
    }
    rename: dict[str, str] = {}
    for canonical, aliases in col_map.items():
        found = next((c for c in aliases if c in df.columns), None)
        if found and found != canonical:
            rename[found] = canonical

    df = df.rename(columns=rename)

    required = ["open", "high", "low", "close"]
    if not all(c in df.columns for c in required):
        logger.debug("%s: missing columns %s", ticker,
                     [c for c in required if c not in df.columns])
        return None

    keep = required + (["volume"] if "volume" in df.columns else [])
    df = df[keep].apply(pd.to_numeric, errors="coerce").dropna(subset=["close"])
    return df


# ============================================================================
#  VN-INDEX OVERLAY
# ============================================================================
def fetch_vnindex(start: Optional[str] = None, end: Optional[str] = None) -> Optional[pd.Series]:
    """Fetch VN-Index close series dùng Market.index.ohlcv() (vnstock v4)."""
    start = start or _start_date()
    end = end or _last_trading_day()
    try:
        from vnstock import Market
        # Áp dụng rate limit cho request này
        _global_limiter.wait()
        raw = Market().index("VNINDEX").ohlcv(start=start, end=end, interval="1D")
        df = _normalise_ohlcv(raw, "VNINDEX")
        if df is not None:
            series = df["close"].rename("VNINDEX")
            # Dedup: giữ ngày cuối nếu trùng
            series = series[~series.index.duplicated(keep="last")]
            logger.info("VN-Index fetched: %d rows", len(series))
            return series
        return None
    except Exception as exc:
        logger.warning("fetch_vnindex failed: %s", exc)
        return None


# ============================================================================
#  CACHE LAYER — incremental updates
# ============================================================================
def load_cache() -> Dict[str, pd.DataFrame]:
    p = _cache_path()
    if not p.exists():
        logger.info("No cache at %s — full fetch required", p)
        return {}
    try:
        combined = pd.read_parquet(p)
        result: Dict[str, pd.DataFrame] = {}
        for ticker, grp in combined.groupby("ticker"):
            result[str(ticker)] = grp.drop(columns="ticker").set_index("date")
        logger.info("Cache loaded: %d tickers", len(result))
        return result
    except Exception as exc:
        logger.warning("Cache read error (%s) — full fetch will run", exc)
        return {}


def save_cache(data: Dict[str, pd.DataFrame]) -> None:
    if not data:
        return
    frames = []
    for ticker, df in data.items():
        tmp = df.copy().reset_index()
        tmp["ticker"] = ticker
        frames.append(tmp)
    combined = pd.concat(frames, ignore_index=True)
    combined["date"] = pd.to_datetime(combined["date"])
    _cache_path().parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(_cache_path(), index=False, engine="pyarrow")
    logger.info("Cache saved: %d tickers", len(data))


def incremental_fetch(
    cached: Dict[str, pd.DataFrame],
    tickers: list[str],
) -> Dict[str, pd.DataFrame]:
    """Chỉ fetch ngày mới hơn ngày cuối trong cache."""
    # ── Guard: bỏ qua nếu hôm nay là cuối tuần ──────────────────────────
    import datetime
    today_dow = datetime.date.today().weekday()  # 0=Thứ 2 ... 6=Chủ nhật
    if today_dow >= 5:  # 5=Thứ 7, 6=Chủ nhật
        logger.info(
            "Hôm nay là %s — thị trường đóng cửa, bỏ qua incremental fetch.",
            ["Thứ 2","Thứ 3","Thứ 4","Thứ 5","Thứ 6","Thứ 7","Chủ nhật"][today_dow],
        )
        return cached   # trả nguyên cache, không fetch gì cả
    # ─────────────────────────────────────────────────────────────────────

    today = _today()

    if cached:
        last_dates = [df.index.max() for df in cached.values() if not df.empty]
        cache_end = max(last_dates).strftime("%Y-%m-%d") if last_dates else _start_date()
    else:
        cache_end = _start_date()

    if cache_end >= today:
        logger.info("Cache current (%s) — skip fetch", cache_end)
        return cached

    new_start = (pd.Timestamp(cache_end) + timedelta(days=1)).strftime("%Y-%m-%d")
    logger.info("Incremental fetch: %s → %s", new_start, today)
    fresh = fetch_ohlcv_all(
        tickers,
        start=new_start,
        end=today,
        sleep_between=0.1,      # nhỏ hơn vì chỉ lấy vài ngày
        retry=1,                # ít retry để nhanh
    )

    merged: Dict[str, pd.DataFrame] = {}
    for t in set(cached) | set(fresh):
        parts = [df for df in [cached.get(t), fresh.get(t)] if df is not None]
        if parts:
            combined = pd.concat(parts).sort_index()
            merged[t] = combined[~combined.index.duplicated(keep="last")]

    return merged


# ============================================================================
#  (Optional) Main entry point for testing
# ============================================================================
if __name__ == "__main__":
    # Simple test: fetch tickers and a few OHLCV
    logging.basicConfig(level=logging.INFO)
    tickers = get_hose_tickers()
    print(f"Found {len(tickers)} tickers. First 5: {tickers[:5]}")

    # Test fetch small subset
    sample = tickers[:5]
    data = fetch_ohlcv_all(sample, start="2025-01-01", end="2025-01-10")
    for t, df in data.items():
        print(f"{t}: {len(df)} rows")
