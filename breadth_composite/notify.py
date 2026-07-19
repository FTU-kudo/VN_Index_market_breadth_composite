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

GEMINI_MODEL   = "gemini-2.0-flash"
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

_ANALYSIS_PROMPT = """
Bạn là chuyên gia phân tích kỹ thuật thị trường chứng khoán Việt Nam (HOSE).
Hãy phân tích hình ảnh Market Breadth Dashboard gồm 4 chỉ báo:
1. % Stocks Above MA20/MA50/MA200 — đánh giá độ rộng thị trường theo xu hướng
2. Advance-Decline Line (ADL) — sức mạnh tổng thể dòng tiền
3. McClellan Oscillator & Summation Index — momentum ngắn và trung hạn
4. Net New 52W Highs/Lows — chất lượng xu hướng

Dữ liệu số hôm nay ({today}):
{metrics_text}

Yêu cầu output (viết bằng tiếng Việt, ngắn gọn súc tích):
🔍 **NHẬN ĐỊNH CHUNG** (1-2 câu tóm tắt trạng thái thị trường)
📊 **CHI TIẾT 4 CHỈ BÁO** (mỗi chỉ báo 1 dòng, nêu tín hiệu tích cực/tiêu cực/trung lập)
⚠️ **RỦI RO CẦN CHÚ Ý** (nếu có divergence hoặc tín hiệu cảnh báo)
🎯 **GỢI Ý CHIẾN LƯỢC** (1 câu: thận trọng / trung lập / tích cực)

Giữ toàn bộ output dưới 280 từ. Dùng emoji phù hợp.
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
        f"- Net H/L % : {_fmt('net_new_highs_pct')}%",
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
    Gửi message (và tuỳ chọn ảnh) qua Telegram Bot API.

    Parameters
    ----------
    text       : nội dung tin nhắn (Markdown)
    image_path : đường dẫn PNG đính kèm (tuỳ chọn)

    Returns
    -------
    bool  True nếu thành công
    """
    token   = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_ID", "").strip()

    if not token or not chat_id:
        logger.error("TELEGRAM_TOKEN or TELEGRAM_ID not set")
        return False

    base_url = f"https://api.telegram.org/bot{token}"

    # Gửi ảnh kèm caption nếu có
    if image_path and Path(image_path).exists():
        try:
            with open(image_path, "rb") as img:
                resp = requests.post(
                    f"{base_url}/sendPhoto",
                    data={
                        "chat_id":    chat_id,
                        "caption":    text[:1024],   # Telegram caption limit
                        "parse_mode": "Markdown",
                    },
                    files={"photo": img},
                    timeout=30,
                )
            resp.raise_for_status()
            logger.info("Telegram photo sent (caption %d chars)", len(text))

            # Nếu text dài hơn caption limit → gửi phần còn lại thành message riêng
            if len(text) > 1024:
                _send_text_message(base_url, chat_id, text[1024:])

            return True

        except Exception as exc:
            logger.warning("Telegram sendPhoto failed: %s — fallback to text", exc)

    # Fallback: chỉ gửi text
    return _send_text_message(base_url, chat_id, text)


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
