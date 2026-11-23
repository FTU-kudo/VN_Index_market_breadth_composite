# main.py
"""
main.py — Breadth Composite Pipeline Orchestrator

Usage:
  python main.py               # incremental (default)
  python main.py --full        # full 6-year re-fetch
  python main.py --dry-run     # compute only, no cache/Excel/chart/Telegram
  python main.py --send-email  # email Excel after export

Data comes from the price-board + history endpoints via breadth_composite.yuanta_api.
No API-key bootstrap is needed; GEMINI_API_KEY / TELEGRAM_* are only used by notify.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("main")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Breadth Composite Pipeline")
    p.add_argument("--full",       action="store_true", help="re-download the whole window")
    p.add_argument("--dry-run",    action="store_true", help="compute only, write nothing")
    p.add_argument("--send-email", action="store_true")
    p.add_argument("--output-dir", default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()

    # Config is patched before the package is imported, so every module sees it.
    if args.output_dir:
        import breadth_composite.config as cfg
        cfg.OUTPUT_DIR = args.output_dir

    from breadth_composite import (
        compute_all,
        export_excel,
        fetch_vnindex,
        get_hose_tickers,
        incremental_fetch,
        latest_session_date,
        load_cache,
        save_cache,
    )
    from breadth_composite.config import HISTORY_PRICE_UNIT

    session = latest_session_date()
    logger.info("Last finished session (Vietnam time): %s | prices in %s", session, HISTORY_PRICE_UNIT)

    # 1 — Universe (one price-board call)
    logger.info("=== STEP 1: Universe ===")
    tickers = get_hose_tickers()
    if not tickers:
        logger.error("Empty universe — aborting instead of publishing an empty report")
        return 1
    logger.info("Universe: %d symbols", len(tickers))

    # 2 — OHLCV
    logger.info("=== STEP 2: OHLCV fetch ===")
    cached = load_cache()
    if args.full:
        from breadth_composite import fetch_ohlcv_all
        from breadth_composite.data_loader import combine_with_cache
        fresh, fetch_degraded = fetch_ohlcv_all(tickers)
        # A full re-download replaces each symbol it returns, but a symbol that
        # failed mid-run keeps its cached history instead of disappearing.
        ohlcv = combine_with_cache(cached, fresh, tickers)
    else:
        ohlcv, fetch_degraded = incremental_fetch(cached, tickers)

    logger.info("OHLCV ready: %d symbols (degraded=%s)", len(ohlcv), fetch_degraded)

    # A partial run is worse than yesterday's cache: keep the cache when the
    # fetch collapsed, and never publish a report built on a handful of symbols.
    coverage = len(ohlcv) / max(1, len(tickers))
    if fetch_degraded and coverage < 0.5 and cached:
        logger.warning(
            "Fetch covered only %.0f%% of the universe — falling back to the %d-symbol cache",
            coverage * 100, len(cached),
        )
        ohlcv = cached

    if not ohlcv:
        logger.error("No OHLCV data available — cannot compute breadth")
        return 1

    # 3 — Cache (skipped on dry-run so a test run cannot poison it)
    if args.dry_run:
        logger.info("=== STEP 3: cache save skipped (--dry-run) ===")
    else:
        logger.info("=== STEP 3: Save cache ===")
        save_cache(ohlcv)

    # 4 — Compute breadth
    logger.info("=== STEP 4: Compute breadth ===")
    breadth = compute_all(ohlcv)
    logger.info(
        "Tail:\n%s",
        breadth[["pct_above_ma50", "pct_above_ma200",
                 "adl", "mcclellan_osc", "net_new_highs_pct"]].tail(3).to_string(),
    )

    # 5 — VN-Index overlay (best effort)
    logger.info("=== STEP 5: VN-Index close ===")
    vnindex = fetch_vnindex()

    if args.dry_run:
        logger.info("--dry-run: skipping export, chart and notifications")
        return 0

    # 6 — Export Excel
    logger.info("=== STEP 6: Export Excel ===")
    excel_path = export_excel(breadth, vnindex=vnindex)
    logger.info("Excel: %s", excel_path)

    # 7 — Render PNG chart
    logger.info("=== STEP 7: Render chart PNG ===")
    from breadth_composite.chart_render import render_breadth_chart
    chart_path = render_breadth_chart(breadth, vnindex=vnindex)
    logger.info("Chart PNG: %s", chart_path)

    # 8 — Gemini analysis + Telegram notify
    logger.info("=== STEP 8: Gemini + Telegram ===")
    from breadth_composite.notify import notify_daily
    notify_daily(breadth, chart_png_path=chart_path)

    # 9 — Email (disabled by default, kept for future use)
    if args.send_email:
        logger.info("=== STEP 9: Send email ===")
        _send_email(excel_path)

    logger.info("=== Pipeline complete ===")
    return 0


def _send_email(excel_path: str) -> None:
    import os, smtplib
    from datetime import date
    from email import encoders
    from email.mime.base import MIMEBase
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    sender     = os.environ["GMAIL_USER"]
    password   = os.environ["GMAIL_APP_PASSWORD"]
    recipients = [r.strip() for r in
                  os.environ.get("EMAIL_RECIPIENTS", sender).split(",")]
    today_str  = date.today().strftime("%d/%m/%Y")

    msg = MIMEMultipart()
    msg["From"]    = sender
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = f"[Breadth] VN-Index Market Breadth Report — {today_str}"

    msg.attach(MIMEText(
        f"<h3>VN-Index Market Breadth — {today_str}</h3>"
        "<p>File đính kèm: báo cáo breadth composite HOSE hôm nay.<br>"
        "Bao gồm: % stocks above MA20/50/200, ADL, McClellan Oscillator, "
        "Net New Highs/Lows.</p>"
        "<p><em>Auto-generated by breadth_composite pipeline.</em></p>",
        "html", "utf-8"
    ))

    with open(excel_path, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition",
                    f'attachment; filename="breadth_{date.today().strftime("%Y%m%d")}.xlsx"')
    msg.attach(part)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipients, msg.as_string())

    logger.info("Email sent → %s", recipients)


if __name__ == "__main__":
    sys.exit(main())
