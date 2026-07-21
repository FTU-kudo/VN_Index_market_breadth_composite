"""
notify.py — Gemini Vision phân tích chart + gửi Telegram

Flow:
  1. Đọc file PNG từ chart_render.py
  2. Encode base64 → gửi Gemini Vision kèm data text hôm nay
  3. Nhận phân tích markdown → format thành Telegram message
  4. Gửi qua Bot API

Secrets cần trong GitHub Actions:
  GEMINI_API_KEY
  TELEGRAM_TOKEN
  TELEGRAM_ID
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gemini Vision — phân tích chart
# ---------------------------------------------------------------------------

GEMINI_MODEL   = "gemini-3.1-flash-lite"
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

_ANALYSIS_PROMPT = """
Bạn là một chuyên gia phân tích kỹ thuật kỳ cựu tại thị trường chứng khoán Việt Nam (HOSE) với 10 năm kinh nghiệm, nổi tiếng với lối phân tích thực chiến, sắc bén và cô đọng.
Nhiệm vụ của bạn là kết hợp dữ liệu từ hình ảnh "Market Breadth Dashboard" và thông tin số liệu `{metrics_text}` của ngày hôm nay ({today}) để đưa ra một báo cáo phân tích độ rộng thị trường chuyên sâu.
Hãy liên kết các chỉ báo với nhau (ví dụ: so sánh giữa Momentum, Dòng tiền và Số lượng mã giữ xu hướng) để tìm ra bản chất thực sự của thị trường (Tích lũy, Phân phối, Bùng nổ hay Bẫy tăng giá).
Yêu cầu Output (Viết bằng tiếng Việt, ngắn gọn, súc tích, tổng dưới 280 từ):

🔍 **NHẬN ĐỊNH CHUNG**: (1-2 câu gọi tên chính xác trạng thái cốt lõi của thị trường và xu hướng chủ đạo).

📊 **CHI TIẾT 5 CHỈ BÁO** (Mỗi chỉ báo gói gọn trong 1 dòng, chỉ rõ trạng thái Tích cực/Tiêu cực/Trung lập + lý do kỹ thuật ngắn):
1. % Stocks Above MA20/50/200: [Trạng thái] → [Xu hướng ngắn/trung/dài hạn]
2. Advance-Decline Line (ADL): [Trạng thái] → [Sức mạnh dòng tiền tổng thể]
3. McClellan Oscillator & Summation Index: [Trạng thái] → [Động lượng ngắn hạn và trung hạn]
4. Net New 52W Highs/Lows: [Trạng thái] → [Chất lượng và độ bền của xu hướng]
5. Net A/D Ratio: [Trạng thái] → [Số mã giảm / tăng và nhận định chất lượng thị trường]

⏱ *NGẮN HẠN (1-4 tuần)*
[1 câu nhận định momentum và rủi ro gần]

📅 *DÀI HẠN (3-6 tháng)*
[1 câu nhận định xu hướng lớn từ ADL và MA200]

⚠️ **RỦI RO CẦN CHÚ Ý**: (Chỉ ra tín hiệu phân kỳ - Divergence, vùng quá mua/quá bán, hoặc sự suy yếu ngầm nếu có. Nếu không có, ghi "Chưa ghi nhận rủi ro lớn").

🎯 **HÀNH ĐỘNG CHIẾN LƯỢC**: (Gói gọn 1 câu: Đưa ra khuyến nghị vị thế [Thận trọng / Trung lập / Tích cực] kèm hành động ưu tiên cho danh mục).

Lưu ý: Sử dụng emoji phù hợp để tăng tính scannable. Tuyệt đối không viết lan man, tập trung vào tính thực chiến cho nhà đầu tư.
"""


def _build_metrics_text(breadth: pd.DataFrame) -> str:
    """Trích xuất dòng dữ liệu cuối cùng thành text cho prompt."""
    if breadth.empty:
        return "Không có dữ liệu."

    last = breadth.dropna(how="all").iloc[-1]

    def _fmt(col: str, fmt: str = ".1f") -> str:
        v = last.get(col)
        if v is None or pd.isna(v):
            return "N/A"
        return f"{v:{fmt}}"

    lines = [
        f"- % > MA20  : {_fmt('pct_above_ma20')}%",
        f"- % > MA50  : {_fmt('pct_above_ma50')}%",
        f"- % > MA200 : {_fmt('pct_above_ma200')}%",
        f"- ADL       : {_fmt('adl', ',.0f')}",
        f"- McClellan Osc : {_fmt('mcclellan_osc')}",
        f"- McClellan Sum : {_fmt('mcclellan_sum', ',.0f')}",
        f"- New Highs : {_fmt('new_highs', '.0f')}",
        f"- New Lows  : {_fmt('new_lows', '.0f')}",
        f"- Net A/D Ratio: {_fmt('net_new_highs_pct')}%",
        f"- Advances  : {_fmt('advances', '.0f')}",
        f"- Declines  : {_fmt('declines', '.0f')}",
    ]
    return "\n".join(lines)


def analyse_with_gemini(
    chart_png_path: str,
    breadth: pd.DataFrame,
) -> str:
    """
    Gửi PNG + data text đến Gemini Vision, nhận về phân tích tiếng Việt.

    Returns
    -------
    str  — nội dung phân tích, hoặc fallback text nếu lỗi
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        logger.error("GEMINI_API_KEY not set")
        return "❌ Không thể phân tích: thiếu GEMINI_API_KEY."

    # Encode ảnh
    png_bytes = Path(chart_png_path).read_bytes()
    b64_image = base64.b64encode(png_bytes).decode("utf-8")

    today_str    = date.today().strftime("%d/%m/%Y")
    metrics_text = _build_metrics_text(breadth)
    prompt_text  = _ANALYSIS_PROMPT.format(
        today=today_str,
        metrics_text=metrics_text,
    )

    payload = {
        "contents": [{
            "parts": [
                {
                    "inline_data": {
                        "mime_type": "image/png",
                        "data": b64_image,
                    }
                },
                {"text": prompt_text},
            ]
        }],
        "generationConfig": {
            "temperature":     0.3,   # ổn định, ít sáng tạo tuỳ tiện
            "maxOutputTokens": 800,
        },
    }

    try:
        resp = requests.post(
            GEMINI_API_URL,
            params={"key": api_key},
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        analysis = (
            data["candidates"][0]["content"]["parts"][0]["text"]
        )
        logger.info("Gemini analysis received (%d chars)", len(analysis))
        return analysis.strip()

    except Exception as exc:
        logger.error("Gemini API error: %s", exc)
        return f"❌ Gemini lỗi: {exc}\n\n📊 Dữ liệu thô:\n{metrics_text}"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(
    text: str,
    image_path: Optional[str] = None,
) -> bool:
    """
    Gửi 2 message riêng biệt:
      1. sendPhoto (ảnh chart, không caption)
      2. sendMessage (toàn bộ text phân tích, max 4096 ký tự)
    Tách ra để text không bị giới hạn 1024 ký tự của caption.
    """
    token   = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_ID", "").strip()

    if not token or not chat_id:
        logger.error("TELEGRAM_TOKEN or TELEGRAM_ID not set")
        return False

    base_url = f"https://api.telegram.org/bot{token}"
    success  = True

    # Message 1: ảnh chart (không caption)
    if image_path and Path(image_path).exists():
        try:
            with open(image_path, "rb") as img:
                resp = requests.post(
                    f"{base_url}/sendPhoto",
                    data={"chat_id": chat_id},
                    files={"photo": img},
                    timeout=30,
                )
            resp.raise_for_status()
            logger.info("Telegram: sendPhoto OK")
        except Exception as exc:
            logger.warning("Telegram sendPhoto failed: %s", exc)
            success = False

    # Message 2: text phân tích đầy đủ (4096 ký tự)
    try:
        resp = requests.post(
            f"{base_url}/sendMessage",
            json={
                "chat_id":    chat_id,
                "text":       text[:4096],
                "parse_mode": "Markdown",
            },
            timeout=30,
        )
        resp.raise_for_status()
        logger.info("Telegram: sendMessage OK (%d chars)", len(text[:4096]))
    except Exception as exc:
        logger.error("Telegram sendMessage failed: %s", exc)
        success = False

    return success


def _send_text_message(base_url: str, chat_id: str, text: str) -> bool:
    """Gửi tin nhắn text thuần, tự cắt nếu vượt 4096 ký tự."""
    MAX_LEN = 4096
    chunks  = [text[i: i + MAX_LEN] for i in range(0, len(text), MAX_LEN)]
    success = True

    for chunk in chunks:
        try:
            resp = requests.post(
                f"{base_url}/sendMessage",
                json={
                    "chat_id":    chat_id,
                    "text":       chunk,
                    "parse_mode": "Markdown",
                },
                timeout=30,
            )
            resp.raise_for_status()
            logger.info("Telegram message sent (%d chars)", len(chunk))
        except Exception as exc:
            logger.error("Telegram sendMessage failed: %s", exc)
            success = False

    return success


# ---------------------------------------------------------------------------
# Master notify function — gọi từ main.py
# ---------------------------------------------------------------------------

def notify_daily(
    breadth: pd.DataFrame,
    chart_png_path: str,
) -> None:
    """
    Hàm duy nhất được gọi từ main.py:
      1. Gemini phân tích chart + data
      2. Gửi Telegram: ảnh + phân tích

    Lỗi được log nhưng không raise để không crash pipeline chính.
    """
    today_str = date.today().strftime("%d/%m/%Y")
    logger.info("=== Gemini analysis ===")

    try:
        analysis = analyse_with_gemini(chart_png_path, breadth)
    except Exception as exc:
        logger.error("analyse_with_gemini crashed: %s", exc)
        analysis = f"❌ Lỗi phân tích Gemini: {exc}"

    header = (
        f"📈 *VN-Index Breadth Report*\n"
        f"📅 {today_str}\n"
        f"{'─' * 32}\n\n"
    )
    full_message = header + analysis

    logger.info("=== Send Telegram ===")
    try:
        ok = send_telegram(full_message, image_path=chart_png_path)
        if ok:
            logger.info("Telegram notification sent successfully")
        else:
            logger.error("Telegram send returned False")
    except Exception as exc:
        logger.error("send_telegram crashed: %s", exc)
