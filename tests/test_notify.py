# tests/test_notify.py
"""Telegram configuration checks and delivery, against a fake Bot API."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import breadth_composite.notify as notify
from breadth_composite.notify import (
    NotifyConfigError,
    NotifyResult,
    _chunk_text,
    _text_to_html,
    analyse_with_gemini,
    analysis_failed,
    build_header,
    notify_daily,
    send_telegram,
    send_text,
    telegram_check,
)

TOKEN = "123456789:AAHfakeTokenValueForTestsOnly_abc"
CHAT = "987654321"


# ---------------------------------------------------------------------------
# Fake Telegram transport
# ---------------------------------------------------------------------------

class FakeResp:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeTelegram:
    """Records calls; `behaviour` maps method -> ('ok'|'http'|'error', description)."""

    def __init__(self, behaviour=None):
        self.calls = []
        self.behaviour = behaviour or {}

    def _respond(self, method):
        mode, description = self.behaviour.get(method, ("ok", ""))
        if mode == "raise":
            raise ConnectionError("network down")
        if mode == "http":
            return FakeResp({"ok": False, "error_code": 400, "description": description}, 400)
        if mode == "badjson":
            return type("R", (), {"status_code": 502, "json": lambda self: (_ for _ in ()).throw(ValueError())})()
        return FakeResp({"ok": True, "result": self.behaviour.get(f"{method}:result", {})})

    def get(self, url, params=None, timeout=None, **kwargs):
        self.calls.append(("GET", url, params, None))
        return self._respond(url.rsplit("/", 1)[-1])

    def post(self, url, json=None, data=None, files=None, timeout=None, **kwargs):
        method = url.rsplit("/", 1)[-1]
        self.calls.append(("POST", url, json, files))
        return self._respond(method)


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_ID", CHAT)


@pytest.fixture
def fake_telegram(monkeypatch):
    def _install(behaviour=None):
        fake = FakeTelegram(behaviour)
        monkeypatch.setattr(notify, "requests", fake)
        return fake
    return _install


@pytest.fixture
def breadth_frame():
    index = pd.bdate_range("2026-06-01", periods=220)
    rng = np.random.default_rng(2)
    data = {}
    for i in range(30):
        closes = 10_000 * np.exp(np.cumsum(rng.normal(0.0002, 0.011, len(index))))
        data[f"S{i:02d}"] = pd.DataFrame({
            "open": closes, "high": closes * 1.01, "low": closes * 0.99,
            "close": closes, "volume": rng.integers(1e5, 2e6, len(index)).astype(float),
        }, index=index)
    from breadth_composite.breadth_calc import compute_all
    return compute_all(data)


# ---------------------------------------------------------------------------
# Credentials & checks
# ---------------------------------------------------------------------------

def test_check_reports_bot_and_chat(creds, fake_telegram):
    fake = fake_telegram({
        "getMe": ("ok", ""),
        "getMe:result": {"id": 42, "username": "breadth_bot"},
        "getChat:result": {"id": int(CHAT), "type": "private", "first_name": "Kudo"},
    })

    info = telegram_check()

    assert info == {
        "bot_username": "breadth_bot", "bot_id": 42,
        "chat_id": int(CHAT), "chat_title": "Kudo", "chat_type": "private",
    }
    # --check must never send a message
    assert all("sendMessage" not in url for _, url, _, _ in fake.calls)


def test_check_without_secrets_says_which_is_missing(monkeypatch, fake_telegram):
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ID", raising=False)
    fake_telegram()

    with pytest.raises(NotifyConfigError, match="TELEGRAM_TOKEN, TELEGRAM_ID"):
        telegram_check()


def test_bad_token_format_is_explained(monkeypatch, fake_telegram):
    monkeypatch.setenv("TELEGRAM_TOKEN", "https://api.telegram.org/bot123")
    monkeypatch.setenv("TELEGRAM_ID", CHAT)
    fake_telegram()

    with pytest.raises(NotifyConfigError, match="không đúng định dạng"):
        telegram_check()


def test_unauthorized_token_surfaces_the_telegram_message(creds, fake_telegram):
    fake_telegram({"getMe": ("http", "Unauthorized")})

    with pytest.raises(NotifyConfigError, match="Unauthorized"):
        telegram_check()


def test_chat_not_found_surfaces_the_telegram_message(creds, fake_telegram):
    fake_telegram({
        "getMe:result": {"username": "bot"},
        "getChat": ("http", "Bad Request: chat not found"),
    })

    with pytest.raises(NotifyConfigError, match="chat not found"):
        telegram_check()


def test_network_failure_is_wrapped(creds, fake_telegram):
    fake_telegram({"getMe": ("raise", "")})

    with pytest.raises(NotifyConfigError, match="không kết nối được"):
        telegram_check()


def test_non_json_response_is_wrapped(creds, fake_telegram):
    fake_telegram({"getMe": ("badjson", "")})

    with pytest.raises(NotifyConfigError, match="không phải JSON"):
        telegram_check()


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def test_send_text_success(creds, fake_telegram):
    fake = fake_telegram()

    result = send_text("**Xin chào**")

    assert result.delivered is True
    assert result.text_chars == len("**Xin chào**")
    method, url, payload, _ = fake.calls[0]
    assert url.endswith("/sendMessage")
    assert payload["parse_mode"] == "HTML"
    assert payload["text"] == "<b>Xin chào</b>"


def test_send_without_secrets_fails_but_does_not_raise(monkeypatch, fake_telegram):
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    fake_telegram()

    result = send_text("hello")

    assert result.delivered is False
    assert "TELEGRAM_TOKEN" in result.error


def test_html_rejection_falls_back_to_plain_text(creds, fake_telegram):
    fake = fake_telegram({"sendMessage": ("http", "can't parse entities")})
    # first attempt fails, plain fallback succeeds
    attempts = {"n": 0}
    original = fake._respond

    def respond(method):
        if method == "sendMessage":
            attempts["n"] += 1
            if attempts["n"] == 1:
                return FakeResp({"ok": False, "description": "can't parse entities"}, 400)
            return FakeResp({"ok": True, "result": {}})
        return original(method)

    fake._respond = respond

    result = send_text("**bold** text")

    assert result.delivered is True
    assert attempts["n"] == 2
    assert fake.calls[1][2]["text"] == "bold text"


def test_photo_failure_does_not_prevent_the_text(creds, fake_telegram, tmp_path):
    fake = fake_telegram({"sendPhoto": ("http", "Photo too big")})
    image = tmp_path / "chart.png"
    image.write_bytes(b"not really a png")

    result = send_telegram("báo cáo", image_path=str(image))

    assert result.photo_sent is False
    assert result.delivered is True
    assert any("sendPhoto" in url for _, url, _, _ in fake.calls)
    assert any("sendMessage" in url for _, url, _, _ in fake.calls)


def test_photo_and_text_success(creds, fake_telegram, tmp_path):
    fake = fake_telegram()
    image = tmp_path / "chart.png"
    image.write_bytes(b"png-bytes")

    result = send_telegram("báo cáo", image_path=str(image))

    assert result.photo_sent is True and result.delivered is True
    photo_call = next(c for c in fake.calls if "sendPhoto" in c[1])
    assert photo_call[3] is not None            # files dict, not a JSON payload


def test_missing_image_file_only_warns(creds, fake_telegram, tmp_path):
    fake_telegram()

    result = send_telegram("báo cáo", image_path=str(tmp_path / "absent.png"))

    assert result.delivered is True
    assert result.photo_sent is False


# ---------------------------------------------------------------------------
# Chunking / HTML (the old bug that silently downgraded the whole report)
# ---------------------------------------------------------------------------

def test_chunking_keeps_bold_pairs_intact():
    line = "**Đây là một dòng khá dài** " + "x" * 60 + "\n"
    text = line * 40

    chunks = _chunk_text(text, limit=200)

    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    for chunk in chunks:
        assert chunk.count("**") % 2 == 0, "a bold pair was split across chunks"


def test_single_huge_line_is_hard_split_but_bounded():
    chunks = _chunk_text("y" * 950, limit=400)

    assert [len(c) for c in chunks] == [400, 400, 150]


def test_long_message_is_sent_as_several_messages(creds, fake_telegram):
    fake = fake_telegram()

    result = send_text("\n".join(f"dòng {i} " + "z" * 100 for i in range(80)))

    assert result.delivered is True
    assert sum(1 for _, url, _, _ in fake.calls if url.endswith("sendMessage")) >= 2


def test_html_escapes_user_content():
    assert _text_to_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def test_header_shows_score_and_regime(breadth_frame):
    header = build_header(breadth_frame)

    score = breadth_frame["composite_score"].dropna().iloc[-1]
    assert f"{score:.0f}/100" in header
    assert "Breadth Report" in header


def test_analyse_without_key_returns_a_clear_fallback(monkeypatch, breadth_frame):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    analysis = analyse_with_gemini(breadth_frame)

    assert analysis_failed(analysis)
    assert "GEMINI_API_KEY" in analysis
    assert "Composite Score" in analysis        # raw metrics still shipped


def test_notify_daily_reports_delivery(creds, fake_telegram, breadth_frame, monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    fake_telegram()
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")

    result = notify_daily(breadth_frame, chart_png_path=str(image))

    assert isinstance(result, NotifyResult)
    assert result.delivered is True
    assert result.analysis_ok is False          # no key -> raw metrics only
    assert result.photo_sent is True
    assert "delivered=yes" in result.summary()
    assert "gemini=failed" in result.summary()


def test_notify_daily_honours_a_header_prefix(creds, fake_telegram, breadth_frame, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    fake = fake_telegram()

    notify_daily(breadth_frame, header_prefix="⚠️ BÁO CÁO MẪU\n\n")

    text = next(p["text"] for _, url, p, _ in fake.calls if url.endswith("sendMessage"))
    assert text.startswith("⚠️ BÁO CÁO MẪU")


def test_notify_daily_returns_not_delivered_when_telegram_is_broken(
    creds, fake_telegram, breadth_frame, monkeypatch
):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    fake_telegram({"sendMessage": ("http", "Forbidden: bot was blocked by the user")})

    result = notify_daily(breadth_frame)

    assert result.delivered is False
    assert "Forbidden" in result.error or "thất bại" in result.error
