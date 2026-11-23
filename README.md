<!-- README.md -->
# 📊 VN-Index Market Breadth Composite

<div align="center">

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Tests](https://img.shields.io/badge/tests-pytest-0A9EDC?style=for-the-badge&logo=pytest&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)
![GitHub Actions](https://img.shields.io/badge/GitHub_Actions-Automated-2088FF?style=for-the-badge&logo=github-actions&logoColor=white)

**Pipeline tự động phân tích độ rộng thị trường (Market Breadth) cho sàn HOSE — VN-Index**

Tính toán 24 chỉ báo breadth, render biểu đồ 3×2 panel, phân tích bằng Gemini AI và gửi báo cáo hàng ngày qua Telegram.

</div>

---

## 🎯 Mục tiêu

Thay vì chỉ nhìn vào giá VN-Index, hệ thống này đo lường **"sức khỏe bên trong"** của thị trường bằng cách phân tích toàn bộ cổ phiếu trên sàn HOSE:

- Bao nhiêu cổ phiếu đang trong xu hướng tăng?
- Dòng tiền đang chảy vào hay rút ra?
- Chất lượng của đà tăng có bền vững không?
- Thị trường đang ở regime Bull, Bear hay Transition?

---

## 🔌 Nguồn dữ liệu (không dùng SDK bên thứ ba)

Toàn bộ dữ liệu lấy trực tiếp qua HTTPS từ API của nhà cung cấp dữ liệu đã mua (Yuanta), trong **một module duy nhất** — `breadth_composite/yuanta_api.py`:

| Dữ liệu | Endpoint | Ghi chú |
|---|---|---|
| Universe (mã niêm yết) | `GET /api/v3/market_data/price_board/stock/list_stock_info?exchange=HSX` | Giá tính bằng **VND**; lọc `SymbolType == "ST"` (cổ phiếu thường) |
| Danh sách chỉ số | `GET /api/v3/market_data/price_board/index/list_index` | Dùng cho chẩn đoán (`tools/probe_yuanta.py`) |
| OHLCV ngày | `GET /v2/tradingview/history?symbol=VIC&resolution=1D&from=…&to=…&limit=100000` | `t` = 00:00 UTC của ngày giao dịch; giá theo **nghìn VND**; `v` = cổ phiếu. Dùng được cho `symbol=VNINDEX` |

Các đặc thù của API đã được xử lý sẵn trong client:

- **`to` phải ở tương lai** mới nhận được bar của ngày hiện tại → mặc định `to = phiên hoàn tất gần nhất + 2 ngày`.
- **Không bao giờ lưu phiên chưa kết thúc**: bar mới hơn `latest_session_date()` bị loại bỏ (cutoff **15:15 giờ Việt Nam**, cuối tuần lùi về thứ Sáu). Chạy trước giờ đóng cửa sẽ không ghi bar dở vào cache.
- **Challenge của edge** (HTTP 200 + HTML "Attention Required" kèm `?jskey=…`) được theo tự động: chỉ host trong allowlist, tối đa 3 hop, cookie giữ theo từng thread.
- **Lịch sử đã điều chỉnh (split/cổ tức)**: mỗi lần fetch incremental sẽ tải lại 15 ngày chồng lấn và so sánh với cache. Lệch > 0.5% ⇒ mã đó được tải lại **toàn bộ** lịch sử và **thay thế** cache (không merge), tránh trộn hai hệ giá.
- **Mã mới** luôn được backfill đủ 6 năm, không phải chỉ từ "ngày mai".
- Sau `MAX_CONSECUTIVE_FAILURES` lỗi liên tiếp, run dừng và trả cờ `degraded` để `main.py` giữ cache cũ thay vì xuất báo cáo dựa trên vài mã.

Giá được giữ nguyên theo đơn vị của nhà cung cấp (nghìn VND). Breadth là chỉ báo không phụ thuộc thang giá, nên điều này không ảnh hưởng kết quả.

---

## ✨ Tính năng

### 📈 24 Market Breadth Indicators (4 nhóm)

#### Nhóm Gốc — Classical Breadth
| Chỉ báo | Mô tả |
|---|---|
| `pct_above_ma5/20/50/200` | % cổ phiếu giao dịch trên đường MA tương ứng |
| `advances / declines / unchanged` | Số lượng mã tăng / giảm / đứng giá mỗi ngày |
| `adl` | Advance-Decline Line — dòng tiền breadth tích lũy |
| `mcclellan_osc` | McClellan Oscillator (Ratio-Adjusted) — momentum ngắn hạn |
| `mcclellan_sum` | McClellan Summation Index — momentum trung hạn |
| `new_highs / new_lows` | Số mã đạt đỉnh / đáy trong cửa sổ `HIGH_LOW_WINDOW` phiên |
| `net_new_highs_pct` | % Net New Highs/Lows — chất lượng xu hướng |

> ⚠️ `HIGH_LOW_WINDOW = 126` (≈ 6 tháng), trong khi nhãn trên chart/Excel/Gemini vẫn ghi "52W". Đây là điểm chưa thống nhất, xem mục *Việc cần làm*.

#### Nhóm 1 — Momentum & Thrust
| Chỉ báo | Mô tả |
|---|---|
| `breadth_thrust` | Whaley Breadth Thrust — 10D EMA(Advances/Total), phát hiện bùng phát mạnh |
| `pct_rising_3d` | % cổ phiếu tăng liên tiếp 3 ngày — đo đà tích cực liên tục |

#### Nhóm 2 — Volume Breadth / TRIN
| Chỉ báo | Mô tả |
|---|---|
| `upvol_ratio` | AdvVol / TotalVol × 100 — % khối lượng chảy vào cổ phiếu tăng |
| `adv_vol / dec_vol` | Tổng khối lượng của cổ phiếu tăng / giảm giá |
| `trin` | Arms Index (TRIN) = (Adv/Dec) / (AdvVol/DecVol) — TRIN < 1 = Bullish |

#### Nhóm 3 — Cumulative Breadth
| Chỉ báo | Mô tả |
|---|---|
| `high_low_line` | Cumsum(NewHighs − NewLows) — xác nhận xu hướng dài hạn |
| `adv_vol_line` | Cumsum(AdvVol − DecVol) — xác nhận ADL bằng dòng tiền volume |

#### Nhóm 4 — Regime Detection & Composite Score
| Chỉ báo | Mô tả |
|---|---|
| `adl_slope` | Tốc độ thay đổi ADL chuẩn hóa (20D) — dương = breadth cải thiện |
| `regime` | `+1` Bull / `0` Transition / `-1` Bear (rule-based) |
| `composite_score` | Điểm tổng hợp **0–100** ("nhiệt kế thị trường") — weighted average 6 chỉ báo cốt lõi |

### 📊 Dashboard 3×2 Panel (PNG)

```
┌──────────────────────────┬──────────────────────────┐
│  % Stocks Above MA       │  ADL + High-Low Line     │
│  + VN-Index overlay      │  + McClellan Osc         │
├──────────────────────────┼──────────────────────────┤
│  McClellan Osc/Sum       │  Net A/D Ratio %         │
│  (bar + dual axis)       │  + Breadth Thrust        │
├──────────────────────────┼──────────────────────────┤
│  Volume Breadth          │  Composite Score         │
│  UpVol% + TRIN           │  + Regime Shading        │
└──────────────────────────┴──────────────────────────┘
```

### 🤖 AI-Powered Analysis
- **Gemini** nhận bộ số liệu breadth dạng text (không upload ảnh) → sinh báo cáo phân tích tiếng Việt (< 350 từ)
- Phân tích 7 chỉ báo cốt lõi, nhận định ngắn hạn/dài hạn, rủi ro divergence, khuyến nghị vị thế

### 📱 Telegram Daily Report
- Gửi chart PNG + báo cáo Gemini hàng ngày
- Header hiển thị Composite Score và Market Regime hiện tại

### 📁 Excel Export (6 sheets)
- `breadth_data` — toàn bộ dữ liệu số (24 cột)
- `chart_ma` — % above MA + VN-Index
- `chart_adl` — ADL + High-Low Line + McClellan Oscillator
- `chart_hl` — Net New Highs/Lows
- `chart_volume` — UpVol Ratio + TRIN
- `chart_composite` — Composite Score + Breadth Thrust

---

## 🏗️ Cấu trúc dự án

```
VN_Index_market_breadth_composite/
│
├── main.py                        # Orchestrator — chạy toàn bộ pipeline
│
├── breadth_composite/
│   ├── __init__.py                # Public API exports
│   ├── config.py                  # Tất cả tham số cấu hình (không hardcode)
│   ├── yuanta_api.py              # HTTP client: price board + history, challenge, lịch giao dịch
│   ├── data_loader.py             # Universe, fetch incremental, phát hiện điều chỉnh, Parquet cache
│   ├── breadth_calc.py            # Engine tính toán 24 chỉ báo breadth
│   ├── chart_render.py            # Render PNG dashboard 3×2
│   ├── export.py                  # Export Excel 6 sheets
│   └── notify.py                  # Gemini AI analysis + Telegram sender
│
├── tests/                         # pytest — toàn bộ chạy offline với HTTP giả lập
│   ├── conftest.py                # FakeSession / FakeResponse / StubClient
│   ├── test_yuanta_api.py         # Parse bar, lịch giao dịch, retry, challenge
│   ├── test_data_loader.py        # Universe, incremental, điều chỉnh giá, cache
│   └── test_pipeline_smoke.py     # compute → Excel/PNG + kiểm tra repo sạch SDK
│
├── tools/
│   └── probe_yuanta.py            # Chẩn đoán endpoint thật (chạy trong Actions)
│
├── .github/workflows/breadth_daily.yml
├── requirements.txt
├── requirements-dev.txt
├── pytest.ini
└── README.md
```

---

## ⚡ Bắt đầu nhanh

### 1. Clone repo

```bash
git clone https://github.com/FTU-kudo/VN_Index_market_breadth_composite.git
cd VN_Index_market_breadth_composite
```

### 2. Cài đặt môi trường

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS

pip install -r requirements.txt       # chạy pipeline
pip install -r requirements-dev.txt   # nếu muốn chạy test
```

Không cần API key để lấy dữ liệu thị trường: endpoint price board / history không yêu cầu key, quyền truy cập gắn với mạng của tài khoản đã mua.

### 3. Cấu hình secrets (chỉ cho báo cáo)

Tạo file `.env` ở thư mục gốc (đã gitignore — **không commit**):

```env
GEMINI_API_KEY=your_gemini_api_key_here
TELEGRAM_TOKEN=your_telegram_bot_token_here
TELEGRAM_ID=your_telegram_chat_id_here
```

File `.env` không tự được load: khi chạy local hãy `export` biến môi trường, hoặc dùng `python-dotenv` như README cũ. Trong GitHub Actions các giá trị này đến từ Secrets.

### 4. Chạy pipeline

```bash
# Chạy bình thường (incremental — chỉ fetch phần thiếu)
python main.py

# Full re-fetch 6 năm (lần đầu hoặc khi cache hỏng)
python main.py --full

# Chỉ tính toán, không ghi cache/Excel/PNG/Telegram
python main.py --dry-run

# Gửi email sau khi export
python main.py --send-email
```

### 5. Kiểm thử

```bash
python -m pytest          # 64 test, không cần mạng
```

Test dùng HTTP giả lập (`FakeSession`) nên chạy được cả khi mạng bị chặn: chúng kiểm tra parse bar, cutoff 15:15, lịch cuối tuần, retry/backoff, challenge redirect (cùng host), universe filter, backfill mã mới, phát hiện điều chỉnh giá, circuit breaker, cache round-trip, và toàn bộ pipeline compute → Excel → PNG trên dữ liệu tổng hợp.

### 6. Chẩn đoán endpoint thật

```bash
python tools/probe_yuanta.py --symbols VIC,ANV,VNM --json output/probe_yuanta.json
```

Script in ra: số index và có `VNINDEX` hay không, số mã + phân bố `SymbolType` theo sàn, `t` có đúng 00:00 UTC không, `first/last` date & close, tỷ lệ `RefP / close` để xác nhận đơn vị giá (nghìn VND), và số lần gặp challenge. Đây là cách xác nhận các giả định mà test offline không thể kiểm chứng.

---

## 🔄 Tự động hóa với GitHub Actions

Workflow `.github/workflows/breadth_daily.yml` chạy: **cài đặt → pytest → pipeline → lưu cache → upload artifact**.

### Cấu hình GitHub Secrets

**Settings → Secrets and variables → Actions**:

| Secret | Mô tả |
|---|---|
| `GEMINI_API_KEY` | Google Gemini API key cho AI analysis |
| `TELEGRAM_TOKEN` | Telegram Bot Token |
| `TELEGRAM_ID` | Telegram Chat ID nhận báo cáo |

`GMAIL_USER` / `GMAIL_APP_PASSWORD` / `EMAIL_RECIPIENTS` chỉ cần nếu bật job email (hiện đang tắt).

### Kích hoạt pipeline

**Cách 1: Trigger thủ công** — Actions → "Breadth Composite Daily" → "Run workflow" (tuỳ chọn `full_fetch`, `send_email`).

**Cách 2: Bật lịch tự động** — bỏ comment trong `breadth_daily.yml`:

```yaml
on:
  schedule:
    - cron: "30 10 * * 1-5"   # 17:30 ICT (UTC+7), Thứ 2–Thứ 6
```

**Cách 3: Repository Dispatch**

```bash
curl -X POST \
  -H "Authorization: token YOUR_GITHUB_PAT" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/FTU-kudo/VN_Index_market_breadth_composite/dispatches \
  -d '{"event_type":"run-breadth"}'
```

### Cache OHLCV

Parquet được lưu bằng `actions/cache` với **key riêng cho mỗi run** (`ohlcv-cache-<os>-<run_id>`) và `restore-keys: ohlcv-cache-<os>-`.

Lý do: cache của Actions là **bất biến**. Nếu key cố định (ví dụ chỉ dựa vào hash của `config.py`), lần lưu thứ hai trở đi sẽ bị từ chối và mọi run sau đó đều khôi phục đúng một bản cache cũ — "incremental" biến thành "tải lại từ ngày đó mỗi lần".

---

## 📦 Output files

```
output/
├── breadth_data_cache.parquet    # Cache OHLCV (Parquet, dùng lại giữa các run)
├── breadth_composite.xlsx        # Báo cáo Excel 6 sheets
├── breadth_chart.png             # Dashboard PNG 3×2 panels
└── probe_yuanta.json             # (tuỳ chọn) kết quả chẩn đoán endpoint
```

Thư mục `output/` nằm trong `.gitignore` — không commit dữ liệu lên git (cache đi qua GitHub Actions cache, báo cáo đi qua artifact/Telegram).

---

## 🔒 Bảo mật

- ✅ Không hardcode API key, token trong source code
- ✅ Credentials chỉ đọc từ `os.environ` / GitHub Secrets
- ✅ `.env`, `output/`, `*.parquet`, `*.xlsx` đều có trong `.gitignore`
- ✅ `requirements.txt` không còn `--extra-index-url` trỏ tới index bên thứ ba (tránh rủi ro dependency confusion); mọi thư viện đến từ PyPI
- ✅ Báo cáo (PNG, Excel, Telegram, prompt Gemini) **không ghi tên nhà cung cấp dữ liệu** — test `test_no_provider_attribution_in_user_facing_modules` chặn việc vô tình thêm vào

---

## 🛠️ Dependencies

```
pandas>=2.0.0         # Xử lý dữ liệu
numpy>=1.24.0         # Tính toán số học vectorized
pyarrow>=14.0.0       # Đọc/ghi Parquet cache
XlsxWriter>=3.1.0     # Export Excel
matplotlib>=3.7.0     # Render chart PNG
requests>=2.31.0      # HTTP (price board, history, Gemini, Telegram)
google-genai>=0.2.0   # Gemini SDK
pytest>=7.4.0         # (dev) test suite
```

---

## 📐 Composite Score — Cách tính

`composite_score` là điểm tổng hợp **0–100** từ 6 chỉ báo cốt lõi, mỗi chỉ báo được chuẩn hóa về [0, 100] trước khi nhân trọng số:

| Chỉ báo | Trọng số | Bounds chuẩn hóa |
|---|---|---|
| `pct_above_ma50` | 25% | 0–100% |
| `pct_above_ma200` | 20% | 0–100% |
| `adl_slope` (20D normalized) | 20% | −3 đến +3 σ |
| `mcclellan_osc` | 15% | −150 đến +150 |
| `net_new_highs_pct` | 10% | −50% đến +50% |
| `upvol_ratio` | 10% | 0–100% (bỏ qua nếu không có volume) |

**Đọc kết quả:**
- `> 70`: 🟢 **Bullish** — thị trường rộng và mạnh
- `50–70`: 🟡 **Neutral/Cautious** — breadth trung bình
- `< 30`: 🔴 **Bearish** — áp lực bán rộng khắp

**Regime Detection (rule-based):**
- 🟢 `Bull (+1)`: `pct_above_ma200 ≥ 60%` VÀ `adl_slope > 0`
- 🔴 `Bear (−1)`: `pct_above_ma200 ≤ 40%` VÀ `adl_slope < 0`
- 🟡 `Transition (0)`: tất cả trường hợp còn lại

---

## 🤖 Gemini AI Analysis

Mỗi ngày, Gemini nhận bộ số liệu breadth dạng text và sinh báo cáo gồm:

- 🎯 **Regime & Composite**: Tóm tắt điểm số và trạng thái thị trường
- 🔍 **Nhận định chung**: Gọi tên chính xác trạng thái cốt lõi
- 📊 **Chi tiết 7 chỉ báo**: % MA, ADL+HL Line, McClellan, Net H/L, A/D Ratio, UpVol+TRIN, Breadth Thrust
- ⏱ **Ngắn hạn (1-4 tuần)**: Momentum và rủi ro gần
- 📅 **Dài hạn (3-6 tháng)**: Xu hướng lớn
- ⚠️ **Rủi ro / Phân kỳ**: Divergence, quá mua/bán
- 🎬 **Hành động chiến lược**: Khuyến nghị vị thế danh mục

---

## 📝 Việc cần làm (đã biết, chưa sửa)

1. `HIGH_LOW_WINDOW = 126` nhưng nhãn "52W" xuất hiện trên chart, Excel và prompt Gemini → hoặc đổi thành 252, hoặc đổi nhãn thành "6M".
2. Một số tham số trong `config.py` chưa được dùng ở đâu (`ADL_LOOKBACK_DAYS`, `REGIME_THRESHOLDS`, `EMAIL_SUBJECT_PREFIX`, `TRIN_MA_PERIOD`, `UPVOL_MA_PERIOD`); chart đang hardcode `rolling(10)` cho UpVol và không làm mượt TRIN.
3. `GEMINI_MODEL` cần được xác nhận là model còn tồn tại; nếu sai, `notify.py` chỉ gửi "❌ Gemini lỗi" kèm số liệu thô.
4. Chưa có lịch nghỉ lễ: ngày lễ không có bar mới nên pipeline tự dùng phiên trước, nhưng `latest_session_date()` không biết ngày lễ.

---

## 📝 Giấy phép

MIT License — Xem file [LICENSE](LICENSE) để biết thêm chi tiết.

---

## 🙏 Tài nguyên & Cảm ơn

- [McClellan Financial Publications](https://www.mcoscillator.com/) — Lý thuyết McClellan Oscillator
- [StockCharts — ChartSchool](https://school.stockcharts.com/doku.php?id=market_indicators) — Market Breadth Theory
- Google Gemini AI — Phân tích báo cáo

---

<div align="center">
<sub>©️ FTU-Kudo</sub>
</div>
