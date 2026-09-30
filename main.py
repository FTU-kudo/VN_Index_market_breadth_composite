"""
main.py — Breadth Composite Pipeline Orchestrator

Usage:
  python main.py               # incremental (default)
  python main.py --full        # full 6-year re-fetch
  python main.py --dry-run     # compute only, no Excel/email
  python main.py --send-email  # email Excel sau khi export
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
    p.add_argument("--full",       action="store_true")
    p.add_argument("--dry-run",    action="store_true")
    p.add_argument("--send-email", action="store_true")
    p.add_argument("--output-dir", default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.output_dir:
        import breadth_composite.config as cfg
        cfg.OUTPUT_DIR = args.output_dir

    from breadth_composite import (
        compute_all, export_excel,
        get_hose_tickers, incremental_fetch,
        load_cache, save_cache,
    )

    # 0 — Weekend guard toàn pipeline
    import datetime
    dow = datetime.date.today().weekday()
    if dow >= 5 and not args.full:
        logger.info(
            "Hôm nay là %s — thị trường đóng, pipeline dùng cache cũ.",
            ["T2","T3","T4","T5","T6","T7","CN"][dow],
        )
        # Vẫn chạy compute + export + notify nhưng skip fetch
        from breadth_composite import load_cache, compute_all, export_excel
        from breadth_composite.data_loader import fetch_vnindex
        cached = load_cache()
        if not cached:
            logger.error("Không có cache — cần chạy full fetch ngày thường trước")
            return 1
        ohlcv = cached
        quota_exhausted = False
    else:
        # 1 — Ticker list
        logger.info("=== STEP 1: Ticker list ===")
        tickers = get_hose_tickers()
        logger.info("Universe: %d tickers", len(tickers))

        # 2 — OHLCV
        logger.info("=== STEP 2: OHLCV fetch ===")
        if args.full:
            from breadth_composite.data_loader import fetch_ohlcv_all
            ohlcv, quota_exhausted = fetch_ohlcv_all(tickers)
        else:
            ohlcv, quota_exhausted = incremental_fetch(load_cache(), tickers)
        
        logger.info("OHLCV loaded: %d tickers (quota_exhausted=%s)", len(ohlcv), quota_exhausted)
        
        # If quota exhausted and we have no/minimal data, fall back to cache
        if quota_exhausted and len(ohlcv) < 50:
            logger.warning(
                "Quota exhausted with only %d tickers — attempting to use cached data",
                len(ohlcv)
            )
            cached = load_cache()
            if cached:
                logger.info("Loaded %d tickers from cache", len(cached))
                ohlcv = cached
            else:
                logger.error("No cache available and fetch incomplete — cannot proceed")
                return 1

        # 3 — Save cache (only if we got new data)
        logger.info("=== STEP 3: Save cache ===")
        if len(ohlcv) > 0:
            save_cache(ohlcv)
        else:
            logger.warning("No OHLCV data to cache")

  
    # 4 — Compute breadth
    if not ohlcv:
        logger.error("No OHLCV data available — cannot compute breadth")
        return 1
    
    logger.info("=== STEP 4: Compute breadth ===")
    breadth = compute_all(ohlcv)
    logger.info(
        "Tail:\n%s",
        breadth[["pct_above_ma50", "pct_above_ma200",
                  "adl", "mcclellan_osc", "net_new_highs_pct"]].tail(3).to_string()
    )

    # 5 — VN-Index overlay (best-effort)
    logger.info("=== STEP 5: VN-Index close ===")
    from breadth_composite.data_loader import fetch_vnindex
    vnindex = fetch_vnindex()

    # 6 — Export
    if args.dry_run:
        logger.info("--dry-run: skipping export & email")
        return 0

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

    # 9 — Email (disabled by default, keep for future use)
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
        "Net New 52W Highs/Lows.</p>"
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
