# tools/notify_test.py
"""
notify_test.py — kiểm tra kênh Telegram mà không cần chạy cả pipeline.

Bốn chế độ (chọn đúng một):

  --check            Kiểm tra token + chat (getMe / getChat). KHÔNG gửi tin nhắn.
                     Trả lời câu hỏi: bot có tồn tại, chat_id có đúng, bot có trong chat?
  --send [TEXT]      Gửi một tin nhắn test qua đúng hàm mà báo cáo dùng.
  --sample-report    Dựng dữ liệu tổng hợp → compute_all → PNG dashboard → notify_daily.
                     Kiểm tra toàn bộ đường đi (ảnh + text + Gemini) mà không gọi API
                     thị trường, nên dùng được ngay cả khi dữ liệu thật chưa sẵn sàng.
  --failure --log F  Gửi cảnh báo lỗi kèm ~30 dòng cuối log. Workflow gọi tự động
                     khi pipeline fail, để bạn luôn nhận được tin nhắn.

Mã thoát: 0 = thành công, 1 = thất bại (in mô tả lỗi gốc của Telegram).

Ví dụ:
  python tools/notify_test.py --check
  python tools/notify_test.py --send "Xin chào từ breadth pipeline"
  python tools/notify_test.py --sample-report --png output/sample_chart.png
  python tools/notify_test.py --failure --log output/pipeline.log
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("notify_test")

DEFAULT_TEST_MESSAGE = (
    "✅ **Breadth pipeline — Telegram hoạt động**\n"
    "Đây là tin nhắn kiểm tra từ GitHub Actions.\n"
    "Nếu bạn đọc được tin này, TELEGRAM_TOKEN và TELEGRAM_ID đã đúng và báo cáo "
    "hằng ngày sẽ tới được đây."
)


def _credentials_report() -> None:
    """In tình trạng secret (không in giá trị) — hữu ích trong log Actions."""
    print("— Cấu hình kênh báo cáo (giá trị không được in ra) —")
    for name in ("TELEGRAM_TOKEN", "TELEGRAM_ID", "GEMINI_API_KEY"):
        value = os.environ.get(name, "")
        if value:
            print(f"  {name:14s}: đã set ({len(value)} ký tự)")
        else:
            print(f"  {name:14s}: THIẾU")


# ---------------------------------------------------------------------------
# Chế độ
# ---------------------------------------------------------------------------

def mode_check() -> int:
    from breadth_composite.notify import NotifyConfigError, telegram_check

    _credentials_report()
    print()
    try:
        info = telegram_check()
    except NotifyConfigError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        print(
            "\nGợi ý:\n"
            "  • 'Unauthorized' → TELEGRAM_TOKEN sai hoặc đã bị thu hồi (lấy lại từ @BotFather).\n"
            "  • 'chat not found' → TELEGRAM_ID sai, hoặc bạn chưa /start bot, hoặc bot chưa\n"
            "    được add vào group/kênh.\n"
            "  • Lấy chat id: nhắn cho bot, mở\n"
            "    https://api.telegram.org/bot<TOKEN>/getUpdates và xem result[].message.chat.id.\n"
            "  • Với group/kênh, chat id thường có dạng -100xxxxxxxxxx.",
            file=sys.stderr,
        )
        return 1

    print(f"[ok]   bot        : @{info['bot_username']} (id {info['bot_id']})")
    print(f"[ok]   chat       : {info['chat_title']!r} ({info['chat_type']}, id {info['chat_id']})")
    print("\nToken và chat hợp lệ. Chạy --send để thử gửi tin nhắn thật.")
    return 0


def mode_send(text: str) -> int:
    from breadth_composite.notify import send_text

    _credentials_report()
    print()
    result = send_text(text)
    print(f"[{'ok' if result.delivered else 'FAIL'}]   {result.summary()}")
    if not result.delivered:
        print("\nKhông gửi được. Chạy `--check` để xem lỗi cấu hình cụ thể.", file=sys.stderr)
        return 1
    print("Đã gửi. Kiểm tra Telegram của bạn.")
    return 0


def _synthetic_breadth(symbols: int = 60, rows: int = 420, seed: int = 5):
    """Dữ liệu tổng hợp để render dashboard mà không cần API thị trường."""
    import numpy as np
    import pandas as pd

    from breadth_composite.breadth_calc import compute_all

    rng = np.random.default_rng(seed)
    index = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=rows)
    data = {}
    for i in range(symbols):
        closes = 10_000 * np.exp(np.cumsum(rng.normal(0.0003, 0.011, rows)))
        data[f"S{i:03d}"] = pd.DataFrame({
            "open": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": rng.integers(1e5, 4e6, rows).astype(float),
        }, index=index)
    return compute_all(data)


def mode_sample_report(png_path: str) -> int:
    from breadth_composite.chart_render import render_breadth_chart
    from breadth_composite.notify import GEMINI_MODEL, notify_daily

    _credentials_report()
    print()

    breadth = _synthetic_breadth()
    print(f"[ok]   dữ liệu tổng hợp: {breadth.shape[0]} phiên × {breadth.shape[1]} chỉ báo (KHÔNG phải dữ liệu thật)")

    out = Path(png_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    chart = render_breadth_chart(breadth, output_path=str(out))
    print(f"[ok]   chart PNG: {chart} ({Path(chart).stat().st_size / 1024:.0f} KB)")

    warning = (
        "⚠️ **ĐÂY LÀ BÁO CÁO MẪU** — dữ liệu tổng hợp, không phải thị trường thật.\n"
        "Mục đích: kiểm tra đường đi chart → Telegram (và Gemini nếu có key).\n\n"
    )
    result = notify_daily(breadth, chart_png_path=chart, header_prefix=warning)
    if not result.delivered:
        print(f"[FAIL]   {result.summary()}", file=sys.stderr)
        return 1

    print(f"[ok]   Telegram: {result.summary()}")
    if result.analysis_ok:
        print(f"[ok]   Gemini ({GEMINI_MODEL}): phân tích {result.analysis_chars} ký tự")
    else:
        print("[warn] Gemini không trả về phân tích — tin nhắn chỉ còn số liệu thô.")
        print("       Kiểm tra GEMINI_API_KEY và GEMINI_MODEL trong breadth_composite/notify.py.",
              file=sys.stderr)
    return 0


def mode_failure(log_path: str) -> int:
    from breadth_composite.notify import send_text

    tail = "Không đọc được log."
    path = Path(log_path)
    if path.exists():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-30:])
        if len(tail) > 3000:
            tail = tail[-3000:]

    message = (
        "❌ **Breadth pipeline FAILED**\n"
        f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC\n"
        "Xem chi tiết: Actions → run vừa rồi → step 'Run pipeline'.\n\n"
        f"```\n{tail}\n```"
    )
    result = send_text(message)
    print(f"[{'ok' if result.delivered else 'FAIL'}]   {result.summary()}")
    # Chế độ này không che giấu lỗi pipeline gốc: luôn trả 0 để job vẫn hiển thị
    # đúng lỗi thật của pipeline, trừ khi chính việc gửi cảnh báo thất bại.
    return 0 if result.delivered else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Kiểm tra kênh Telegram của breadth pipeline")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="kiểm tra token/chat, không gửi tin")
    group.add_argument("--send", nargs="?", const=DEFAULT_TEST_MESSAGE, metavar="TEXT",
                       help="gửi tin nhắn test")
    group.add_argument("--sample-report", action="store_true",
                      help="dashboard tổng hợp + gửi báo cáo mẫu")
    group.add_argument("--failure", action="store_true", help="gửi cảnh báo lỗi kèm log tail")
    parser.add_argument("--log", default="output/pipeline.log", help="log cho --failure")
    parser.add_argument("--png", default="output/sample_chart.png", help="nơi lưu PNG cho --sample-report")
    args = parser.parse_args()

    try:
        if args.check:
            return mode_check()
        if args.send is not None:
            return mode_send(args.send)
        if args.sample_report:
            return mode_sample_report(args.png)
        return mode_failure(args.log)
    except Exception:                      # noqa: BLE001 - CLI phải in lỗi rõ ràng
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
