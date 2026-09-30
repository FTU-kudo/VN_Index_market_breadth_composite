# data_loader.py — Ticker listing & OHLCV fetcher (vnstock v4 Unified UI)
# - Đăng ký API key tự động từ env var VNSTOCK_API_KEY
# - Dùng Listing.symbols_by_exchange() để lấy ticker HOSE
# - Dùng Market.equity.ohlcv() cho từng ticker
# - Parquet cache incremental
# - Tối ưu Rate Limiter để tránh lỗi 60 req/phút (Community tier)
# - [FIX] Exponential backoff, batch splitting, progress tracking
# - [FIX] Rate limiter applies to ALL attempts (including retries)
# - [FIX] Conservative defaults: 8 req/min for API key, 5 req/min for guest
# - [FIX] Graceful quota exhaustion: keep cached data on rate limit

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
#  RATE LIMITER – cải thiện với adaptive delay
# ============================================================================
class RateLimiter:
    """
    Limit API calls to `max_calls` per `period` seconds.
    - Adaptive: tăng delay nếu close to limit
    - Safe for vnstock's internal retry mechanism
    - Applies to EVERY attempt, not just first try
    """
    def __init__(self, max_calls: int = 8, period: float = 60.0):
        self.max_calls = max_calls
        self.period = period
        self.calls = deque()
        self.min_interval = 1.5  # Tối thiểu 1.5s giữa các request (conservative)

    def wait(self):
        now = time.monotonic()
        # Xóa các timestamp đã hết hạn
        while self.calls and self.calls[0] <= now - self.period:
            self.calls.popleft()
        
        # Luôn đảm bảo min_interval giữa requests
        if self.calls:
            last_call = self.calls[-1]
            time_since_last = now - last_call
            if time_since_last < self.min_interval:
                sleep_time = self.min_interval - time_since_last
                time.sleep(sleep_time)
                now = time.monotonic()
        
        # Nếu đạt max_calls, đợi đến khi oldest call hết hạn
        if len(self.calls) >= self.max_calls:
            sleep_time = self.calls[0] + self.period - now + 0.2
            logger.debug("Rate limit (%d/%d in last 60s), sleeping %.1fs",
                        len(self.calls), self.max_calls, sleep_time)
            time.sleep(sleep_time)
            # Gọi đệ quy để kiểm tra lại sau khi ngủ
            self.wait()
        else:
            self.calls.append(time.monotonic())


# Giới hạn RẤT AN TOÀN: 8 requests/phút (dưới 60, dành cho retry nội bộ)
# Với API key: 60 req/min nominal, nhưng vnstock retries nội bộ có thể vượt.
# Không API key: 20 req/min. Để safe, dùng 5 cho guest.
_global_limiter = RateLimiter(max_calls=8, period=60.0)


# ============================================================================
#  RETRY & EXPONENTIAL BACKOFF (with built-in rate limiting)
# ============================================================================
def _retry_fetch(func, *args, max_retries: int = 3, **kwargs):
    """
    Thực thi func với exponential backoff khi gặp lỗi tạm thời.
    - **IMPORTANT**: Rate limiter được gọi TRƯỚC mỗi attempt (không chỉ cái đầu).
    - TooManyRequests, ConnectionError, Timeout → retry
    - RateLimitError explicit → retry với backoff dài hơn
    - Lỗi khác → raise immediately
    
    Returns: (success, result, error_msg)
        - (True, result, None) nếu thành công
        - (False, None, error_msg) nếu tất cả retry đều thất bại
    """
    base_delay = 5.0
    
    for attempt in range(max_retries):
        try:
            # === RATE LIMITER APPLIED HERE (before every attempt) ===
            _global_limiter.wait()
            
            return func(*args, **kwargs)
            
        except Exception as exc:
            error_msg = str(exc).lower()
            
            # Nhận diện rate limit / quota exhaustion
            is_rate_limited = any(
                marker in error_msg
                for marker in (
                    "rate limit",
                    "too many requests",
                    "429",
                    "quota",
                    "tham gia insiders",  # Specific vnstock message
                )
            )
            
            # Nhận diện lỗi tạm thời (retryable)
            is_retryable = is_rate_limited or any(
                marker in error_msg
                for marker in (
                    "timeout",
                    "connection",
                    "503",
                    "502",
                    "service unavailable",
                )
            )
            
            if not is_retryable:
                # Lỗi permanent — không retry
                raise
            
            if attempt == max_retries - 1:
                # Lần cuối cùng — raise
                raise
            
            # Tính sleep time
            if is_rate_limited:
                # Rate limit hit: sleep lâu hơn (80-90 giây) để hết quota
                sleep_time = 90.0 + (attempt * 10)
            else:
                # Lỗi tạm thời khác: exponential backoff (5s, 10s, 20s)
                sleep_time = base_delay * (2 ** attempt)
            
            logger.warning(
                "Attempt %d/%d failed (%s), sleeping %.1fs before retry...",
                attempt + 1,
                max_retries,
                str(exc)[:150],
                sleep_time,
            )
            time.sleep(sleep_time)
    
    # Không nên đến đây (vòng lặp trên raise)
    raise RuntimeError(f"_retry_fetch exhausted after {max_retries} attempts")


# ============================================================================
#  BOOTSTRAP VNSTOCK (đăng ký API key)
# ============================================================================
def _bootstrap_vnstock() -> None:
    """Đăng ký VNSTOCK_API_KEY từ env nếu có (60 req/phút nominal)."""
    api_key = os.environ.get("VNSTOCK_API_KEY", "").strip()
    if not api_key:
        logger.warning("VNSTOCK_API_KEY not set — running as guest (~20 req/min, limited to 5/min to be safe)")
        # Giảm limit cho guest
        _global_limiter.max_calls = 5
        _global_limiter.min_interval = 2.0
        return
    try:
        from vnstock import register_user
        register_user(api_key=api_key)
        logger.info("vnstock: authenticated (Community tier)")
        # Vẫn dùng 8 req/min conservative ngay cả với API key
        # Vì vnstock có retry nội bộ
    except Exception as exc:
        logger.error("vnstock register_user failed: %s — falling back to guest mode", exc)
        _global_limiter.max_calls = 5
        _global_limiter.min_interval = 2.0


_bootstrap_vnstock()


# ============================================================================
#  HELPERS
# ============================================================================
def _cache_path() -> Path:
    p = Path(OUTPUT_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p / DATA_CACHE_FILENAME


def _progress_checkpoint_path() -> Path:
    """Đường dẫn file để lưu progress fetch hiện tại."""
    p = Path(OUTPUT_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p / ".fetch_progress.pkl"


def _start_date() -> str:
    d = date.today() - timedelta(days=int(BACKFILL_YEARS * 365.25))
    return d.strftime("%Y-%m-%d")


def _today() -> str:
    return date.today().strftime("%Y-%m-%d")


def _last_trading_day() -> str:
    """Ngày giao dịch gần nhất – thứ 7 → thứ 6, chủ nhật → thứ 6."""
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
      3. Fallback cuối: all_symbols()
    """
    from vnstock import Listing
    listing = Listing()

    # ------------------------------------------------------------------
    # Attempt 1: symbols_by_exchange
    # ------------------------------------------------------------------
    try:
        _global_limiter.wait()
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
    # Attempt 3: all_symbols()
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
        "kiểm tra vnstock version và network"
    )


# ============================================================================
#  FETCH OHLCV – với Rate Limiter, Retry, Batch Splitting & Progress Tracking
# ============================================================================
def fetch_ohlcv_all(
    tickers: list[str],
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    batch_size: int = 50,
) -> tuple[Dict[str, pd.DataFrame], bool]:
    """
    Fetch OHLCV cho mọi ticker với các cải thiện:
    - RateLimiter applied to EVERY attempt (including retries)
    - Retry exponential backoff khi gặp lỗi tạm thời
    - Batch splitting để tránh timeout
    - Progress tracking & graceful resume
    - Graceful stop khi gặp explicit rate limit error
    
    Parameters:
    -----------
    tickers : list[str]
        Danh sách mã cổ phiếu
    start, end : str
        Ngày bắt đầu/kết thúc (YYYY-MM-DD)
    batch_size : int
        Kích thước batch để chia nhỏ fetch (default 50)

    Returns:
    --------
    (dict[ticker -> DataFrame], quota_exhausted: bool)
        - dict: OHLCV data thành công
        - quota_exhausted: True nếu dừng vì rate limit (để gọi hàm biết để keep cache)
    """
    start = start or _start_date()
    end = end or _last_trading_day()

    if start > end:
        logger.info("start (%s) > end (%s) — bỏ qua fetch", start, end)
        return {}, False

    logger.info("fetch_ohlcv_all: %d tickers | %s → %s | batch_size=%d",
                len(tickers), start, end, batch_size)

    from vnstock import Market
    results: Dict[str, pd.DataFrame] = {}
    failed_tickers = []
    quota_exhausted = False
    market = None

    # Chia nhỏ thành batches
    batches = [tickers[i:i+batch_size] for i in range(0, len(tickers), batch_size)]
    logger.info("Processing %d batches of ~%d tickers", len(batches), batch_size)

    for batch_idx, batch in enumerate(batches, 1):
        logger.info("=== Batch %d/%d (%d tickers) ===", batch_idx, len(batches), len(batch))
        
        # Instantiate Market once per batch (not per ticker)
        try:
            market = Market()
        except Exception as exc:
            logger.warning("Failed to instantiate Market: %s", exc)
            market = None
        
        for i, ticker in enumerate(batch, 1):
            ticker_idx = (batch_idx - 1) * batch_size + i
            
            try:
                # === RateLimiter + Retry (with rate limit applied to all attempts) ===
                if market is None:
                    market = Market()
                
                raw = _retry_fetch(
                    lambda t=ticker: market.equity(t).ohlcv(
                        start=start,
                        end=end,
                        interval="1D",
                    ),
                    max_retries=3
                )
                
                df = _normalise_ohlcv(raw, ticker)
                if df is not None and not df.empty:
                    results[ticker] = df
                    logger.debug("[%d/%d] %s ✓ (%d rows)", ticker_idx, len(tickers), ticker, len(df))
                else:
                    logger.debug("[%d/%d] %s - no data", ticker_idx, len(tickers), ticker)

            except Exception as exc:
                error_msg = str(exc).lower()
                
                # Explicit rate limit error → dừng hẳn (nhưng không raise)
                if any(marker in error_msg for marker in ("rate limit", "too many requests", "429", "quota", "tham gia insiders")):
                    logger.error(
                        "[%d/%d] %s — Rate limit / quota exhausted. Stopping fetch. "
                        "Will continue with cached data.",
                        ticker_idx, len(tickers), ticker
                    )
                    quota_exhausted = True
                    break
                
                # Lỗi khác → log và tiếp tục với ticker tiếp theo
                logger.warning("[%d/%d] %s ✗ %s", ticker_idx, len(tickers), ticker, 
                              str(exc)[:100])
                failed_tickers.append(ticker)
        
        if quota_exhausted:
            break
        
        # Ngủ nhẹ giữa các batch để tránh overwhelm
        if batch_idx < len(batches):
            logger.info("Batch %d complete. Waiting 3s before next batch...", batch_idx)
            time.sleep(3.0)

    logger.info(
        "fetch_ohlcv_all done: ✓ %d / %d tickers | ✗ %d failed | quota_exhausted=%s",
        len(results), len(tickers), len(failed_tickers), quota_exhausted
    )
    
    if failed_tickers:
        logger.info("Failed tickers (first 10): %s", ', '.join(failed_tickers[:10]))
    
    return results, quota_exhausted


def _normalise_ohlcv(raw: pd.DataFrame, ticker: str) -> Optional[pd.DataFrame]:
    """Chuẩn hoá output từ vnstock v4 → DatetimeIndex + [open, high, low, close, volume]."""
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
#  VN-INDEX (tùy chọn)
# ============================================================================
def fetch_vnindex(start: Optional[str] = None, end: Optional[str] = None) -> Optional[pd.Series]:
    """Fetch VN-Index close series với retry."""
    start = start or _start_date()
    end = end or _last_trading_day()
    try:
        from vnstock import Market
        
        raw = _retry_fetch(
            lambda: Market().index("VNINDEX").ohlcv(start=start, end=end, interval="1D"),
            max_retries=2
        )
        
        df = _normalise_ohlcv(raw, "VNINDEX")
        if df is not None:
            series = df["close"].rename("VNINDEX")
            series = series[~series.index.duplicated(keep="last")]
            logger.info("VN-Index fetched: %d rows", len(series))
            return series
        return None
    except Exception as exc:
        logger.warning("fetch_vnindex failed: %s", exc)
        return None


# ============================================================================
#  CACHE LAYER – incremental updates
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
) -> tuple[Dict[str, pd.DataFrame], bool]:
    """
    Chỉ fetch ngày mới hơn ngày cuối trong cache.
    
    Returns:
    --------
    (merged_data, quota_exhausted: bool)
        - merged_data: cached + newly fetched data
        - quota_exhausted: True nếu fetch dừng vì rate limit
    """
    # Bỏ qua nếu cuối tuần
    import datetime
    today_dow = datetime.date.today().weekday()
    if today_dow >= 5:
        logger.info(
            "Hôm nay là %s — thị trường đóng cửa, bỏ qua incremental fetch.",
            ["Thứ 2","Thứ 3","Thứ 4","Thứ 5","Thứ 6","Thứ 7","Chủ nhật"][today_dow],
        )
        return cached, False

    today = _today()

    if cached:
        last_dates = [df.index.max() for df in cached.values() if not df.empty]
        cache_end = max(last_dates).strftime("%Y-%m-%d") if last_dates else _start_date()
    else:
        cache_end = _start_date()

    if cache_end >= today:
        logger.info("Cache current (%s) — skip fetch", cache_end)
        return cached, False

    new_start = (pd.Timestamp(cache_end) + timedelta(days=1)).strftime("%Y-%m-%d")
    logger.info("Incremental fetch: %s → %s", new_start, today)
    fresh, quota_exhausted = fetch_ohlcv_all(
        tickers,
        start=new_start,
        end=today,
        batch_size=50,
    )

    merged: Dict[str, pd.DataFrame] = {}
    for t in set(cached) | set(fresh):
        parts = [df for df in [cached.get(t), fresh.get(t)] if df is not None and not df.empty]
        if parts:
            combined = pd.concat(parts).sort_index()
            merged[t] = combined[~combined.index.duplicated(keep="last")]

    return merged, quota_exhausted


# ============================================================================
#  (Optional) Main entry point for testing
# ============================================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    tickers = get_hose_tickers()
    print(f"Found {len(tickers)} tickers. First 5: {tickers[:5]}")
    sample = tickers[:10]
    data, quota_ex = fetch_ohlcv_all(sample, start="2025-01-01", end="2025-01-10")
    for t, df in data.items():
        print(f"{t}: {len(df)} rows")
    print(f"Quota exhausted: {quota_ex}")
