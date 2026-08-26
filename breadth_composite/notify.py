"""
notify.py — Gemini Text Analysis + Telegram Notification (v2.1)

Flow:
  1. Tổng hợp dữ liệu breadth thành text metrics
  2. Gửi TEXT-ONLY cho Gemini → nhận phân tích tiếng Việt
  3. Gửi Telegram: ảnh PNG (để xem) + text phân tích của Gemini

Lý do dùng text-only (không gửi ảnh lên Gemini):
  - Ảnh PNG 3×2 @ 150dpi có thể > 10MB → 404 từ Gemini REST API
  - Gemini đọc số liệu text cũng cho kết quả phân tích tốt
  - Ảnh PNG vẫn được gửi qua Telegram để người dùng xem trực quan

Secrets cần trong GitHub Actions (đọc từ os.environ — KHÔNG hardcode):
  GEMINI_API_KEY
  TELEGRAM_TOKEN
  TELEGRAM_ID
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gemini Text Analysis (không dùng Vision/image)
# ---------------------------------------------------------------------------

GEMINI_MODEL   = "gemini-2.5-flash-lite"
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

_ANALYSIS_PROMPT = """
Bạn là một chuyên gia phân tích thị trường chứng khoán Việt Nam (HOSE) với 10 năm kinh nghiệm, nổi tiếng với lối phân tích thực chiến, sắc bén và cô đọng.

Nhiệm vụ: Dựa vào dữ liệu Market Breadth dưới đây của ngày {today}, hãy đưa ra báo cáo phân tích chuyên sâu.

--- DỮ LIỆU BREADTH HÔM NAY ---
{metrics_text}
--- HẾT DỮ LIỆU ---

Yêu cầu Output (Viết bằng tiếng Việt, ngắn gọn, súc tích, tổng dưới 350 từ):

🎯 **REGIME & COMPOSITE**: (Composite Score = bao nhiêu → trạng thái Bullish/Neutral/Bearish. Regime hiện tại là gì?)

🔍 **NHẬN ĐỊNH CHUNG**: (1-2 câu gọi tên chính xác trạng thái cốt lõi của thị trường)

📊 **CHI TIẾT 7 CHỈ BÁO** (mỗi chỉ báo 1 dòng, có số liệu chính xác):
1. **% Above MA50/200:** [số liệu] → [xu hướng trung/dài hạn]
2. **ADL + ADL Slope:** [số liệu] → [sức mạnh dòng tiền breadth]
3. **McClellan Osc/Sum:** [số liệu] → [momentum ngắn/trung hạn]
4. **Net New 52W H/L:** [số liệu] → [chất lượng đỉnh mới]
5. **Net A/D Ratio:** [số liệu] → [số mã tăng/giảm hôm nay]
6. **UpVol Ratio + TRIN:** [số liệu] → [chất lượng dòng tiền theo volume] (ghi "N/A" nếu không có)
7. **Breadth Thrust:** [số liệu] → [đo đà bùng phát/suy yếu ngắn hạn]

⏱ **NGẮN HẠN (1-4 tuần)**
[1 câu nhận định momentum và rủi ro gần]

📅 **DÀI HẠN (3-6 tháng)**
[1 câu nhận định xu hướng lớn từ ADL, MA200]

⚠️ **RỦI RO / PHÂN KỲ**: (Tín hiệu divergence, quá mua/quá bán, bất thường. Nếu không → "Chưa ghi nhận rủi ro lớn")

🎬 **HÀNH ĐỘNG CHIẾN LƯỢC**: (1 câu: vị thế [Thận trọng/Trung lập/Tích cực] + ưu tiên hành động cho danh mục)

Dùng emoji tăng tính scannable. Tập trung vào tính thực chiến cho nhà đầu tư.
"""


def _build_metrics_text(breadth: pd.DataFrame) -> str:
    """Trích xuất dòng dữ liệu cuối cùng thành text để Gemini đọc."""
    if breadth.empty:
        return "Không có dữ liệu."

    last = breadth.dropna(how="all").iloc[-1]

    def _fmt(col: str, fmt: str = ".1f") -> str:
        v = last.get(col)
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return "N/A"
        return f"{v:{fmt}}"

    # Xác định trạng thái regime
    regime_val = last.get("regime")
    if regime_val is None or (isinstance(regime_val, float) and np.isnan(regime_val)):
        regime_label = "N/A"
    elif int(regime_val) == 1:
        regime_label = "🟢 BULL"
    elif int(regime_val) == -1:
        regime_label = "🔴 BEAR"
    else:
        regime_label = "🟡 TRANSITION"

    lines = [
        "=== CHỈ BÁO CỐT LÕI ===",
        f"- % > MA5    : {_fmt('pct_above_ma5')}%",
        f"- % > MA20   : {_fmt('pct_above_ma20')}%",
        f"- % > MA50   : {_fmt('pct_above_ma50')}%",
        f"- % > MA200  : {_fmt('pct_above_ma200')}%",
        "",
        "=== ADVANCE-DECLINE ===",
        f"- ADL              : {_fmt('adl', ',.0f')}",
        f"- ADL Slope (20D)  : {_fmt('adl_slope')}",
        f"- Advances         : {_fmt('advances', '.0f')}",
        f"- Declines         : {_fmt('declines', '.0f')}",
        f"- Unchanged        : {_fmt('unchanged', '.0f')}",
        "",
        "=== MCCLELLAN ===",
        f"- McClellan Osc    : {_fmt('mcclellan_osc')}",
        f"- McClellan Sum    : {_fmt('mcclellan_sum', ',.0f')}",
        "",
        "=== NEW HIGHS/LOWS ===",
        f"- New Highs        : {_fmt('new_highs', '.0f')}",
        f"- New Lows         : {_fmt('new_lows', '.0f')}",
        f"- Net H/L %        : {_fmt('net_new_highs_pct')}%",
        "",
        "=== CHỈ BÁO MỚI ===",
        f"- Breadth Thrust   : {_fmt('breadth_thrust')}%  (>61.5% = bullish thrust signal)",
        f"- % Rising 3D      : {_fmt('pct_rising_3d')}%",
        f"- UpVol Ratio      : {_fmt('upvol_ratio')}%  (>50% = bullish volume flow)",
        f"- TRIN             : {_fmt('trin')}  (<1.0 = bullish volume pressure)",
        f"- High-Low Line    : {_fmt('high_low_line', ',.0f')}",
        f"- AdvVol Line      : {_fmt('adv_vol_line', ',.0f')}",
        "",
        "=== REGIME & COMPOSITE ===",
        f"- Composite Score  : {_fmt('composite_score')}/100",
        f"- Market Regime    : {regime_label}",
    ]
    return "\n".join(lines)


def analyse_with_gemini(
    breadth: pd.DataFrame,
) -> str:
    """
    Gửi TEXT-ONLY (các chỉ số breadth) đến Gemini, nhận về phân tích tiếng Việt.
    Không gửi ảnh — tránh lỗi 404 do payload quá lớn.
    API key đọc hoàn toàn từ os.environ — không bao giờ hardcode.

    Returns
    -------
    str  — nội dung phân tích, hoặc fallback text nếu lỗi
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        logger.error("GEMINI_API_KEY not set — kiểm tra GitHub Secrets hoặc file .env")
        return "❌ Không thể phân tích: thiếu GEMINI_API_KEY."

    today_str    = date.today().strftime("%d/%m/%Y")
    metrics_text = _build_metrics_text(breadth)
    prompt_text  = _ANALYSIS_PROMPT.format(
        today=today_str,
        metrics_text=metrics_text,
    )

    # Text-only payload — không gửi ảnh, tránh 404 do payload quá lớn
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt_text},
            ]
        }],
        "generationConfig": {
            "temperature":     0.3,
            "maxOutputTokens": 1200,
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
        data     = resp.json()
        analysis = data["candidates"][0]["content"]["parts"][0]["text"]
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
    Gửi ảnh + text phân tích qua Telegram.
    Token và chat_id đọc hoàn toàn từ os.environ — không bao giờ hardcode.
    """
    token   = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_ID", "").strip()

    if not token or not chat_id:
        logger.error(
            "TELEGRAM_TOKEN hoặc TELEGRAM_ID không được set — "
            "kiểm tra GitHub Secrets hoặc file .env"
        )
        return False

    base_url = f"https://api.telegram.org/bot{token}"
    success  = True

    # Message 1: ảnh chart PNG (để user xem trực quan)
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

    # Message 2: text phân tích từ Gemini
    if not _send_text_message(base_url, chat_id, text):
        success = False

    return success


def _text_to_html(text: str) -> str:
    """Convert '**bold**' sang '<b>bold</b>' và escape HTML special chars."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    return text


def _send_text_message(base_url: str, chat_id: str, text: str) -> bool:
    """Gửi message với HTML parse mode. Fallback về plain text nếu lỗi."""
    MAX_LEN = 4000
    success  = True

    html_text = _text_to_html(text)
    chunks = [html_text[i: i + MAX_LEN] for i in range(0, len(html_text), MAX_LEN)]

    for chunk in chunks:
        try:
            resp = requests.post(
                f"{base_url}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                },
                timeout=30,
            )
            resp.raise_for_status()
            logger.info("HTML sent (%d chars)", len(chunk))
        except Exception as exc:
            response_text = ""
            if hasattr(exc, "response") and exc.response is not None:
                try:
                    response_text = exc.response.text
                except Exception:
                    response_text = "Could not read response"
            logger.error(
                "HTML parse failed: %s\nResponse body: %s", exc, response_text
            )

            # Fallback: plain text
            logger.info("Falling back to plain text…")
            plain_text   = text.replace("**", "")
            plain_chunks = [plain_text[i: i + 4000]
                            for i in range(0, len(plain_text), 4000)]
            for plain in plain_chunks:
                try:
                    resp = requests.post(
                        f"{base_url}/sendMessage",
                        json={"chat_id": chat_id, "text": plain},
                        timeout=30,
                    )
                    resp.raise_for_status()
                    logger.info("Plain text sent (%d chars)", len(plain))
                except Exception as fallback_exc:
                    logger.error("Plain text fallback also failed: %s", fallback_exc)
                    success = False
            break

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
      1. Gemini phân tích TEXT chỉ số breadth (không gửi ảnh lên Gemini)
      2. Telegram: gửi ảnh PNG (để xem) + text phân tích Gemini

    Ảnh PNG vẫn được gửi qua Telegram — chỉ là Gemini không đọc ảnh nữa.
    Lỗi được log nhưng không raise để không crash pipeline chính.
    """
    today_str = date.today().strftime("%d/%m/%Y")
    logger.info("=== Gemini text analysis (v2.1 — text-only, no image upload) ===")

    try:
        analysis = analyse_with_gemini(breadth)
    except Exception as exc:
        logger.error("analyse_with_gemini crashed: %s", exc)
        analysis = f"❌ Lỗi phân tích Gemini: {exc}"

    # Header với Composite Score và Regime
    composite_str = ""
    regime_str    = ""
    if not breadth.empty:
        last = breadth.dropna(how="all").iloc[-1]
        cs   = last.get("composite_score")
        rg   = last.get("regime")
        if cs is not None and not (isinstance(cs, float) and np.isnan(cs)):
            composite_str = f" | Score: {cs:.0f}/100"
        if rg is not None and not (isinstance(rg, float) and np.isnan(rg)):
            regime_icons = {1: "🟢 Bull", 0: "🟡 Transition", -1: "🔴 Bear"}
            regime_str = f" | {regime_icons.get(int(rg), '')}"

    header = (
        f"📈 **VN-Index Breadth Report v2.1**\n"
        f"📅 {today_str}{composite_str}{regime_str}\n"
        f"{'─' * 34}\n\n"
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
