# tools/probe_yuanta.py
"""
probe_yuanta.py — read-only diagnostic against the live market-data endpoints.

Run it from GitHub Actions (or any machine on the network your subscription is
bound to) and paste the output back. It answers the questions the offline test
suite cannot:

  1. Which indexes exist, and is `VNINDEX` among them?
  2. How many symbols come back per exchange, and what `SymbolType` values occur?
  3. Do history bars arrive with `t` on a UTC midnight boundary, ascending?
  4. Are history prices in thousand VND while `RefP` on the price board is in VND?
  5. Does the edge challenge appear, and does the client get past it?

Usage:
  python tools/probe_yuanta.py                 # default sample symbols
  python tools/probe_yuanta.py --symbols VIC,ANV,VNM
  python tools/probe_yuanta.py --json output/probe_yuanta.json

Nothing here writes to the cache or calls any paid-heavy endpoint more than a
handful of times. Exit code is 0 unless every probe failed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from breadth_composite.config import (  # noqa: E402
    HISTORY_PRICE_UNIT, YUANTA_EXCHANGE_CODE,
)
from breadth_composite.yuanta_api import (  # noqa: E402
    YuantaClient, latest_session_date, vietnam_now,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("probe")

DEFAULT_SYMBOLS = ["VIC", "ANV", "VNM"]
EXCHANGES = [YUANTA_EXCHANGE_CODE, "HNX", "UPCOM"]


def _safe(label, fn):
    try:
        value = fn()
        log.info("[ok]   %s", label)
        return {"ok": True, "value": value}
    except Exception as exc:                       # noqa: BLE001 - diagnostic tool
        log.error("[fail] %s -> %s: %s", label, type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def probe_indexes(client):
    rows = client.index_list()
    codes = [str(r.get("IndexCode")) for r in rows]
    sample = rows[0] if rows else {}
    return {
        "count": len(rows),
        "codes": codes,
        "has_vnindex": "VNINDEX" in codes,
        "item_keys": sorted(sample)[:40],
        "vnindex_row": next((r for r in rows if r.get("IndexCode") == "VNINDEX"), None),
    }


def probe_universe(client, exchange):
    rows = client.stock_info(exchange=exchange)
    types = {}
    for row in rows:
        key = str(row.get("SymbolType") or "<blank>")
        types[key] = types.get(key, 0) + 1
    first = rows[0] if rows else {}
    prices = [r.get("RefP") for r in rows[:200] if isinstance(r.get("RefP"), (int, float))]
    return {
        "rows": len(rows),
        "symbol_types": types,
        "item_keys": sorted(first)[:40],
        "first_symbol": first.get("StockCode"),
        "median_refp_sample": sorted(prices)[len(prices) // 2] if prices else None,
    }


def probe_history(client, symbol):
    latest = latest_session_date()
    frame = client.history(symbol, start=latest - timedelta(days=40), max_date=latest)
    if frame.empty:
        return {"symbol": symbol, "bars": 0}

    first, last = frame.index[0], frame.index[-1]
    # `t` must sit on a UTC midnight, otherwise dates shift by timezone
    raw_offsets = {int(ts.timestamp()) % 86400 for ts in frame.index}
    return {
        "symbol": symbol,
        "bars": int(len(frame)),
        "first_date": str(first.date()),
        "last_date": str(last.date()),
        "last_close": float(frame["close"].iloc[-1]),
        "last_volume": float(frame["volume"].iloc[-1]),
        "ascending": bool(frame.index.is_monotonic_increasing),
        "utc_midnight_aligned": raw_offsets == {0},
        "columns": list(frame.columns),
    }


def probe_unit(client, symbol):
    """Compare price-board RefP (VND) with the last history close."""
    rows = client.stock_info(exchange=YUANTA_EXCHANGE_CODE)
    row = next((r for r in rows if str(r.get("StockCode")).upper() == symbol.upper()), None)
    if row is None:
        return {"symbol": symbol, "error": "not on the price board"}
    frame = client.history(symbol, start=latest_session_date() - timedelta(days=15))
    if frame.empty:
        return {"symbol": symbol, "error": "no history"}
    refp = row.get("RefP")
    close = float(frame["close"].iloc[-1])
    ratio = (float(refp) / close) if close and refp else None
    return {
        "symbol": symbol,
        "refp_board": refp,
        "last_refp_history": row.get("LastMP"),
        "history_last_close": close,
        "ratio": ratio,
        "inferred_unit": ("thousand VND" if ratio and 500 < ratio < 2000
                          else "same unit as RefP" if ratio and 0.5 < ratio < 2
                          else "unclear"),
        "configured_unit": HISTORY_PRICE_UNIT,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--json", default="output/probe_yuanta.json")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    client = YuantaClient()
    now_vn = vietnam_now()

    report = {
        "generated_at_utc": date.today().isoformat(),
        "vietnam_now": str(now_vn),
        "latest_session_date": str(latest_session_date(now_vn)),
        "indexes": _safe("index list", lambda: probe_indexes(client)),
        "universes": {},
        "history": {},
        "units": {},
    }

    for exchange in EXCHANGES:
        report["universes"][exchange] = _safe(
            f"price board {exchange}", lambda e=exchange: probe_universe(client, e)
        )

    for symbol in symbols:
        report["history"][symbol] = _safe(f"history {symbol}", lambda s=symbol: probe_history(client, s))

    for symbol in symbols[:2] + ["VNINDEX"]:
        report["units"][symbol] = _safe(f"unit check {symbol}", lambda s=symbol: probe_unit(client, s))

    report["http"] = {
        "requests": client.request_count,
        "challenges_followed": client.challenge_count,
    }

    print("\n" + "=" * 72)
    print("PROBE REPORT")
    print("=" * 72)
    print(json.dumps(report, indent=2, default=str, ensure_ascii=False))

    checks = [report["indexes"], *report["universes"].values(),
              *report["history"].values(), *report["units"].values()]
    ok = any(item.get("ok") for item in checks)
    if not ok:
        print("\nEvery probe failed. If the error is an HTTP/challenge error, run this "
              "from a network the subscription allows (e.g. a GitHub Actions runner) "
              "and compare with the offline tests.", file=sys.stderr)

    out = Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    log.info("report written to %s", out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
