# breadth_composite/notify.py
"""
notify.py — Gemini text analysis + Telegram delivery (v3.0)

Flow:
  1. Tổng hợp dữ liệu breadth thành text metrics
  2. Gửi TEXT-ONLY cho Gemini → nhận phân tích tiếng Việt
  3. Gửi Telegram: ảnh PNG (để xem) + text phân tích

Điểm quan trọng của v3.0 (để "kiểm tra được" và không im lặng khi lỗi):
  * `notify_daily()` trả về `NotifyResult` (delivered / analysis_ok / chars) thay vì None,
    nên `main.py --strict-notify` có thể exit code 3 khi không gửi được.
  * `telegram_check()` kiểm tra token + chat (getMe / getChat) **không** gửi tin nhắn,
    dùng cho workflow Diagnostics.
  * Ảnh lỗi (quá lớn, sai định dạng) chỉ là warning: text vẫn được gửi, vì text mới là báo cáo.
  * Text được chia theo dòng trước khi convert HTML → không bao giờ cắt giữa thẻ `<b>`
    (lỗi cũ: chia theo ký tự làm Telegram trả 400 rồi fallback toàn bộ sang plain text).

Secrets (đọc từ os.environ — KHÔNG hardcode):
  GEMINI_API_KEY, TELEGRAM_TOKEN, TELEGRAM_ID
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

logger = logging.getLogger(__name__)


GEMINI_MODEL = "gemini-3.5-flash-lite"

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_TEXT_LIMIT = 4000          # giới hạn thực tế của Telegram là 4096
TELEGRAM_TIMEOUT = 30
_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")

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


# ---------------------------------------------------------------------------
# Kết quả / lỗi cấu hình
# ---------------------------------------------------------------------------

class NotifyConfigError(RuntimeError):
    """Thiếu secret, token sai định dạng, hoặc Telegram từ chối (getMe/getChat/send)."""


@dataclass
class NotifyResult:
    """Kết quả một lần gửi báo cáo — dùng được cho log, CLI và exit code."""

    delivered: bool = False
    analysis_ok: bool = False
    analysis_chars: int = 0
    text_chars: int = 0
    photo_sent: bool = False
    error: str = ""

    def summary(self) -> str:
        parts = [
            f"delivered={'yes' if self.delivered else 'NO'}",
            f"gemini={'ok' if self.analysis_ok else 'failed'}",
            f"text={self.text_chars} chars",
            f"photo={'yes' if self.photo_sent else 'no'}",
        ]
        if self.error:
            parts.append(f"error={self.error}")
        return " | ".join(parts)


# ---------------------------------------------------------------------------
# Gemini analysis
# ---------------------------------------------------------------------------

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


def analysis_failed(analysis: str) -> bool:
    """True khi Gemini không trả về phân tích (text fallback bắt đầu bằng ❌)."""
    return not analysis or analysis.lstrip().startswith("❌")


def analyse_with_gemini(breadth: pd.DataFrame) -> str:
    """
    Gửi TEXT-ONLY (các chỉ số breadth) đến Gemini, nhận về phân tích tiếng Việt.
    Không gửi ảnh. API key đọc hoàn toàn từ os.environ.

    Returns: nội dung phân tích, hoặc text fallback (bắt đầu bằng ❌) nếu lỗi.
    """
    today_str = date.today().strftime("%d/%m/%Y")
    metrics_text = _build_metrics_text(breadth)

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        logger.error("GEMINI_API_KEY not set — kiểm tra GitHub Secrets hoặc file .env")
        # Vẫn gửi số liệu thô: thiếu AI không có nghĩa là không có báo cáo.
        return f"❌ Không thể phân tích: thiếu GEMINI_API_KEY.\n\n📊 Dữ liệu thô:\n{metrics_text}"
    prompt_text = _ANALYSIS_PROMPT.format(today=today_str, metrics_text=metrics_text)

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt_text,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=1200,
            ),
        )
        analysis = (response.text or "").strip()
        if not analysis:
            raise RuntimeError("Gemini trả về nội dung rỗng")
        logger.info("Gemini analysis received (%d chars, model=%s)", len(analysis), GEMINI_MODEL)
        return analysis

    except ImportError:
        logger.error("Missing google-genai library. pip install google-genai>=0.2.0")
        return f"❌ Gemini lỗi: Thiếu thư viện google-genai\n\n📊 Dữ liệu thô:\n{metrics_text}"
    except Exception as exc:
        logger.error("Gemini SDK error (model=%s): %s", GEMINI_MODEL, exc)
        return f"❌ Gemini lỗi: {exc}\n\n📊 Dữ liệu thô:\n{metrics_text}"


# ---------------------------------------------------------------------------
# Telegram: cấu hình & kiểm tra
# ---------------------------------------------------------------------------

def telegram_credentials() -> Tuple[str, str]:
    """(token, chat_id) từ env, kèm kiểm tra định dạng cơ bản. Raise NotifyConfigError."""
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_ID", "").strip()

    missing = [name for name, value in (("TELEGRAM_TOKEN", token), ("TELEGRAM_ID", chat_id)) if not value]
    if missing:
        raise NotifyConfigError(
            "Thiếu secret: " + ", ".join(missing)
            + " — thêm vào Settings → Secrets and variables → Actions"
        )

    if not _TOKEN_RE.match(token):
        raise NotifyConfigError(
            "TELEGRAM_TOKEN không đúng định dạng '<bot_id>:<hash>' — "
            "copy lại token từ @BotFather (đừng dán username hay URL)"
        )
    return token, chat_id


def _api_call(
    method: str,
    *,
    token: str,
    payload: Optional[Dict[str, Any]] = None,
    files: Optional[Dict[str, Any]] = None,
    timeout: int = TELEGRAM_TIMEOUT,
) -> Dict[str, Any]:
    """
    Gọi một Telegram Bot API method và trả `result`.
    Lỗi mạng / HTTP / `ok: false` đều thành NotifyConfigError với mô tả gốc của Telegram.
    """
    url = f"{TELEGRAM_API}/bot{token}/{method}"
    try:
        if files is not None:
            response = requests.post(url, data=payload or {}, files=files, timeout=timeout)
        elif payload is not None:
            response = requests.post(url, json=payload, timeout=timeout)
        else:
            response = requests.get(url, timeout=timeout)
    except Exception as exc:                      # transport, DNS, timeout…
        raise NotifyConfigError(
            f"{method}: không kết nối được tới api.telegram.org ({type(exc).__name__}: {exc})"
        ) from exc

    status = int(getattr(response, "status_code", 0) or 0)
    try:
        body = response.json()
    except Exception as exc:
        raise NotifyConfigError(
            f"{method}: HTTP {status}, phản hồi không phải JSON"
        ) from exc

    if not isinstance(body, dict) or not body.get("ok"):
        description = ""
        if isinstance(body, dict):
            description = str(body.get("description") or "")
        raise NotifyConfigError(f"{method}: HTTP {status} — {description or 'Telegram từ chối (ok=false)'}")

    return body.get("result") or {}


def telegram_check(timeout: int = 15) -> Dict[str, Any]:
    """
    Kiểm tra token + chat **không gửi tin nhắn nào**.

    Trả về: {'bot_username', 'bot_id', 'chat_id', 'chat_title', 'chat_type'}
    Raise NotifyConfigError với mô tả cụ thể (sai token / chat không tồn tại / bot chưa
    được add vào group).
    """
    token, chat_id = telegram_credentials()

    me = _api_call("getMe", token=token, timeout=timeout)
    chat = _api_call("getChat", token=token, payload={"chat_id": chat_id}, timeout=timeout)

    info = {
        "bot_username": me.get("username") or "",
        "bot_id": me.get("id"),
        "chat_id": chat.get("id"),
        "chat_title": chat.get("title") or chat.get("username") or chat.get("first_name") or "",
        "chat_type": chat.get("type") or "",
    }
    logger.info(
        "Telegram OK: bot=@%s | chat=%s (%s, id=%s)",
        info["bot_username"], info["chat_title"], info["chat_type"], info["chat_id"],
    )
    return info


# ---------------------------------------------------------------------------
# Telegram: gửi tin nhắn
# ---------------------------------------------------------------------------

def _text_to_html(text: str) -> str:
    """Escape HTML đặc biệt rồi đổi '**bold**' sang '<b>bold</b>'."""
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped, flags=re.DOTALL)


def _chunk_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> List[str]:
    """
    Chia text theo ranh giới dòng để mỗi chunk ≤ limit ký tự.
    Chia theo dòng (không theo ký tự) để `**bold**` không bị cắt đôi — lỗi cũ làm
    Telegram trả 400 "can't parse entities" và cả báo cáo rơi về plain text.
    """
    chunks: List[str] = []
    current = ""

    for line in text.splitlines(keepends=True):
        while len(line) > limit:                 # một dòng dài bất thường
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line

    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]


def _send_text_message(token: str, chat_id: str, text: str) -> bool:
    """Gửi text (chia chunk). Mỗi chunk thử HTML, nếu bị từ chối thì gửi plain text."""
    chunks = _chunk_text(text)
    all_ok = True

    for index, chunk in enumerate(chunks, start=1):
        try:
            _api_call(
                "sendMessage",
                token=token,
                payload={"chat_id": chat_id, "text": _text_to_html(chunk), "parse_mode": "HTML"},
            )
            logger.info("Telegram text chunk %d/%d sent (%d chars, HTML)", index, len(chunks), len(chunk))
            continue
        except NotifyConfigError as exc:
            logger.warning("HTML gửi lỗi ở chunk %d/%d (%s) — thử lại dạng plain text",
                           index, len(chunks), exc)

        try:
            _api_call(
                "sendMessage",
                token=token,
                payload={"chat_id": chat_id, "text": chunk.replace("**", "")},
            )
            logger.info("Telegram text chunk %d/%d sent (%d chars, plain)", index, len(chunks), len(chunk))
        except NotifyConfigError as exc:
            logger.error("Không gửi được chunk %d/%d: %s", index, len(chunks), exc)
            all_ok = False

    return all_ok


def send_telegram(text: str, image_path: Optional[str] = None) -> NotifyResult:
    """
    Gửi ảnh (nếu có) + text qua Telegram.

    Ảnh lỗi KHÔNG làm fail cả lần gửi: text mới là báo cáo. Lỗi text mới là thất bại.
    """
    result = NotifyResult(text_chars=len(text or ""))

    try:
        token, chat_id = telegram_credentials()
    except NotifyConfigError as exc:
        logger.error("Telegram: %s", exc)
        result.error = str(exc)
        return result

    if image_path and Path(image_path).exists():
        try:
            with open(image_path, "rb") as image:
                _api_call(
                    "sendPhoto",
                    token=token,
                    payload={"chat_id": chat_id},
                    files={"photo": image},
                )
            result.photo_sent = True
            logger.info("Telegram: sendPhoto OK (%s)", Path(image_path).name)
        except NotifyConfigError as exc:
            logger.warning("Telegram sendPhoto thất bại — vẫn gửi phần text: %s", exc)
    elif image_path:
        logger.warning("Không thấy file ảnh %s — chỉ gửi text", image_path)

    result.delivered = _send_text_message(token, chat_id, text)
    if not result.delivered:
        result.error = result.error or "sendMessage thất bại (xem log phía trên)"
    return result


def send_text(text: str) -> NotifyResult:
    """Gửi một tin nhắn text duy nhất (dùng cho kiểm tra / cảnh báo lỗi)."""
    return send_telegram(text, image_path=None)


# ---------------------------------------------------------------------------
# Master notify function — gọi từ main.py
# ---------------------------------------------------------------------------

def build_header(breadth: pd.DataFrame) -> str:
    """Header báo cáo: ngày + Composite Score + Regime."""
    today_str = date.today().strftime("%d/%m/%Y")
    composite_str = ""
    regime_str = ""

    if not breadth.empty:
        last = breadth.dropna(how="all").iloc[-1]
        score = last.get("composite_score")
        regime = last.get("regime")
        if score is not None and not (isinstance(score, float) and np.isnan(score)):
            composite_str = f" | Score: {score:.0f}/100"
        if regime is not None and not (isinstance(regime, float) and np.isnan(regime)):
            regime_icons = {1: "🟢 Bull", 0: "🟡 Transition", -1: "🔴 Bear"}
            regime_str = f" | {regime_icons.get(int(regime), '')}"

    return (
        f"📈 **VN-Index Breadth Report v3.0**\n"
        f"📅 {today_str}{composite_str}{regime_str}\n"
        f"{'─' * 34}\n\n"
    )


def notify_daily(
    breadth: pd.DataFrame,
    chart_png_path: Optional[str] = None,
    header_prefix: str = "",
) -> NotifyResult:
    """
    Phân tích bằng Gemini rồi gửi Telegram (ảnh + text).
    Không raise: lỗi được ghi log và phản ánh trong `NotifyResult`.
    """
    logger.info("=== Gemini text analysis (text-only, no image upload) ===")

    try:
        analysis = analyse_with_gemini(breadth)
    except Exception as exc:                      # defensive: analyse_with_gemini đã tự bắt
        logger.error("analyse_with_gemini crashed: %s", exc)
        analysis = f"❌ Lỗi phân tích Gemini: {exc}"

    result = NotifyResult(
        analysis_ok=not analysis_failed(analysis),
        analysis_chars=len(analysis),
    )
    if not result.analysis_ok:
        logger.warning("Gemini không trả về phân tích — báo cáo sẽ gồm số liệu thô")

    message = header_prefix + build_header(breadth) + analysis
    result.text_chars = len(message)

    logger.info("=== Send Telegram ===")
    try:
        sent = send_telegram(message, image_path=chart_png_path)
    except Exception as exc:                      # defensive: send_telegram đã tự bắt
        logger.error("send_telegram crashed: %s", exc)
        result.error = str(exc)
        return result

    result.delivered = sent.delivered
    result.photo_sent = sent.photo_sent
    result.error = sent.error

    if result.delivered:
        logger.info("Telegram notification sent (%s)", result.summary())
    else:
        logger.error("Telegram notification FAILED (%s)", result.summary())
    return result
