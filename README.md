# 📊 VN-Index Market Breadth Composite

<div align="center">

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![vnstock](https://img.shields.io/badge/vnstock-4.0+-00BCD4?style=for-the-badge)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)
![GitHub Actions](https://img.shields.io/badge/GitHub_Actions-Automated-2088FF?style=for-the-badge&logo=github-actions&logoColor=white)

**Pipeline tự động phân tích độ rộng thị trường (Market Breadth) cho sàn HOSE — VN-Index**

Tính toán 15+ chỉ báo breadth, render biểu đồ 3×2 panel, phân tích bằng Gemini AI và gửi báo cáo hàng ngày qua Telegram.

</div>

---

## 🎯 Mục tiêu

Thay vì chỉ nhìn vào giá VN-Index, hệ thống này đo lường **"sức khỏe bên trong"** của thị trường bằng cách phân tích toàn bộ cổ phiếu trên sàn HOSE:

- Bao nhiêu cổ phiếu đang trong xu hướng tăng?
- Dòng tiền đang chảy vào hay rút ra?
- Chất lượng của đà tăng có bền vững không?
- Thị trường đang ở regime Bull, Bear hay Transition?

---

## ✨ Tính năng

### 📈 15+ Market Breadth Indicators (4 nhóm)

#### Nhóm Gốc — Classical Breadth
| Chỉ báo | Mô tả |
|---|---|
| `pct_above_ma5/20/50/200` | % cổ phiếu giao dịch trên đường MA tương ứng |
| `advances / declines / unchanged` | Số lượng mã tăng / giảm / đứng giá mỗi ngày |
| `adl` | Advance-Decline Line — dòng tiền breadth tích lũy |
| `mcclellan_osc` | McClellan Oscillator (Ratio-Adjusted) — momentum ngắn hạn |
| `mcclellan_sum` | McClellan Summation Index — momentum trung hạn |
| `new_highs / new_lows` | Số mã đạt đỉnh / đáy 52 tuần |
| `net_new_highs_pct` | % Net New 52W Highs/Lows — chất lượng xu hướng |

#### Nhóm 1 — Momentum & Thrust *(mới)*
| Chỉ báo | Mô tả |
|---|---|
| `breadth_thrust` | Whaley Breadth Thrust — 10D EMA(Advances/Total), phát hiện bùng phát mạnh |
| `pct_rising_3d` | % cổ phiếu tăng liên tiếp 3 ngày — đo đà tích cực liên tục |

#### Nhóm 2 — Volume Breadth / TRIN *(mới)*
| Chỉ báo | Mô tả |
|---|---|
| `upvol_ratio` | AdvVol / TotalVol × 100 — % khối lượng chảy vào cổ phiếu tăng |
| `adv_vol / dec_vol` | Tổng khối lượng của cổ phiếu tăng / giảm giá |
| `trin` | Arms Index (TRIN) = (Adv/Dec) / (AdvVol/DecVol) — TRIN < 1 = Bullish |

#### Nhóm 3 — Cumulative Breadth *(mới)*
| Chỉ báo | Mô tả |
|---|---|
| `high_low_line` | Cumsum(NewHighs − NewLows) — xác nhận xu hướng dài hạn |
| `adv_vol_line` | Cumsum(AdvVol − DecVol) — xác nhận ADL bằng dòng tiền volume |

#### Nhóm 4 — Regime Detection & Composite Score *(mới)*
| Chỉ báo | Mô tả |
|---|---|
| `adl_slope` | Tốc độ thay đổi ADL chuẩn hóa (20D) — dương = breadth cải thiện |
| `regime` | `+1` Bull / `0` Transition / `-1` Bear (rule-based) |
| `composite_score` | Điểm tổng hợp **0–100** ("nhiệt kế thị trường") — weighted average 6 chỉ báo cốt lõi |

### 📊 Dashboard 3×2 Panel (PNG)

```
┌──────────────────────────┬──────────────────────────┐
│  % Stocks Above MA       │  ADL + High-Low Line      │
│  + VN-Index overlay      │  + McClellan Osc          │
├──────────────────────────┼──────────────────────────┤
│  McClellan Osc/Sum       │  Net A/D Ratio %          │
│  (bar + dual axis)       │  + Breadth Thrust         │
├──────────────────────────┼──────────────────────────┤
│  Volume Breadth          │  Composite Score          │
│  UpVol% + TRIN           │  + Regime Shading         │
└──────────────────────────┴──────────────────────────┘
```

### 🤖 AI-Powered Analysis
- **Gemini Vision** đọc chart PNG + dữ liệu số → sinh báo cáo phân tích tiếng Việt (< 350 từ)
- Phân tích 7 chỉ báo cốt lõi, nhận định ngắn hạn/dài hạn, rủi ro divergence, khuyến nghị vị thế

### 📱 Telegram Daily Report
- Gửi chart PNG + báo cáo Gemini hàng ngày
- Header hiển thị Composite Score và Market Regime hiện tại

### 📁 Excel Export (6 sheets)
- `breadth_data` — toàn bộ dữ liệu số (15+ cột)
- `chart_ma` — biểu đồ % above MA + VN-Index
- `chart_adl` — ADL + High-Low Line + McClellan Oscillator
- `chart_hl` — Net New 52W Highs/Lows
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
│   ├── data_loader.py             # Fetch OHLCV từ vnstock v4 + Parquet cache
│   ├── breadth_calc.py            # Engine tính toán 15+ chỉ báo breadth
│   ├── chart_render.py            # Render PNG dashboard 3×2
│   ├── export.py                  # Export Excel 6 sheets
│   └── notify.py                  # Gemini AI analysis + Telegram sender
│
├── .github/
│   └── workflows/
│       └── breadth_daily.yml      # GitHub Actions — chạy tự động hàng ngày
│
├── requirements.txt
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
# Tạo virtual environment (khuyến nghị)
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS

# Cài dependencies
pip install -r requirements.txt
```

### 3. Cấu hình secrets

Tạo file `.env` trong thư mục gốc (đã có trong `.gitignore` — **không bao giờ commit file này**):

```env
# .env — KHÔNG commit file này lên GitHub!
VNSTOCK_API_KEY=your_vnstock_api_key_here
GEMINI_API_KEY=your_gemini_api_key_here
TELEGRAM_TOKEN=your_telegram_bot_token_here
TELEGRAM_ID=your_telegram_chat_id_here
```

Để load `.env` khi chạy local, thêm vào đầu `main.py` hoặc dùng `python-dotenv`:

```bash
pip install python-dotenv
```

```python
# Đầu main.py (chỉ dùng local)
from dotenv import load_dotenv
load_dotenv()
```

### 4. Chạy pipeline

```bash
# Chạy bình thường (incremental — chỉ fetch ngày mới)
python main.py

# Full re-fetch 6 năm (lần đầu hoặc khi cache hỏng)
python main.py --full

# Chỉ tính toán, không export/gửi (để test nhanh)
python main.py --dry-run

# Gửi email sau khi export
python main.py --send-email
```

---

## 🔄 Tự động hóa với GitHub Actions

Pipeline được thiết kế để chạy hoàn toàn tự động trên GitHub Actions.

### Cấu hình GitHub Secrets

Vào **Settings → Secrets and variables → Actions** của repo, thêm:

| Secret | Mô tả |
|---|---|
| `VNSTOCK_API_KEY` | API key vnstock để fetch dữ liệu HOSE |
| `GEMINI_API_KEY` | Google Gemini API key cho AI analysis |
| `TELEGRAM_TOKEN` | Telegram Bot Token |
| `TELEGRAM_ID` | Telegram Chat ID nhận báo cáo |

### Kích hoạt pipeline

**Cách 1: Trigger thủ công** từ GitHub UI → Actions → "Breadth Composite Daily" → "Run workflow"

**Cách 2: Bật lịch tự động** — bỏ comment dòng schedule trong `breadth_daily.yml`:

```yaml
# .github/workflows/breadth_daily.yml
on:
  schedule:
    - cron: "30 10 * * 1-5"   # 17:30 ICT (UTC+7), Thứ 2–Thứ 6
```

**Cách 3: Repository Dispatch** — trigger từ script bên ngoài:

```bash
curl -X POST \
  -H "Authorization: token YOUR_GITHUB_PAT" \
  -H "Accept: application/vnd.github.v3+json" \
  https://api.github.com/repos/FTU-kudo/VN_Index_market_breadth_composite/dispatches \
  -d '{"event_type":"run-breadth"}'
```

---

## 📦 Output files

Sau khi pipeline chạy xong, các file được tạo trong thư mục `output/` (không commit lên git):

```
output/
├── breadth_data_cache.parquet    # Cache OHLCV (Parquet, tái sử dụng)
├── breadth_composite.xlsx        # Báo cáo Excel 6 sheets
└── breadth_chart.png             # Dashboard PNG 3×2 panels
```

> **Lưu ý**: Thư mục `output/` được gitignore — dữ liệu không bao giờ bị commit lên repo.

---

## 🔒 Bảo mật

Dự án được thiết kế với nguyên tắc **zero secrets in code**:

- ✅ **Không hardcode** bất kỳ API key, token nào trong source code
- ✅ Tất cả credentials đọc hoàn toàn từ `os.environ` hoặc GitHub Secrets
- ✅ File `.env`, `output/`, `*.parquet`, `*.xlsx` đều có trong `.gitignore`
- ✅ `secrets.json` cũng được gitignore

---

## 🛠️ Dependencies

```
vnstock>=4.0.0        # Dữ liệu thị trường Việt Nam (HOSE, HNX, UPCOM)
pandas>=2.0.0         # Xử lý dữ liệu
numpy>=1.24.0         # Tính toán số học vectorized
pyarrow>=14.0.0       # Đọc/ghi Parquet cache
XlsxWriter>=3.1.0     # Export Excel
matplotlib>=3.7.0     # Render chart PNG
requests>=2.31.0      # HTTP calls (Gemini API + Telegram API)
```

---

## 📐 Composite Score — Cách tính

`composite_score` là điểm tổng hợp **0–100** từ 6 chỉ báo cốt lõi, mỗi chỉ báo được chuẩn hóa về [0, 100] trước khi nhân trọng số:

| Chỉ báo | Trọng số | Bounds chuẩn hóa |
|---|---|---|
| `pct_above_ma50` | 25% | 0–100% |
| `pct_above_ma200` | 20% | 0–100% |
| `adl_slope` (20D normalized) | 20% | -3 đến +3 σ |
| `mcclellan_osc` | 15% | -150 đến +150 |
| `net_new_highs_pct` | 10% | -50% đến +50% |
| `upvol_ratio` | 10% | 0–100% (NaN nếu không có volume) |

**Đọc kết quả:**
- `> 70`: 🟢 **Bullish** — thị trường rộng và mạnh
- `50–70`: 🟡 **Neutral/Cautious** — breadth trung bình
- `< 30`: 🔴 **Bearish** — áp lực bán rộng khắp
- `< 30`: 🔴 **Bearish** — áp lực bán rộng khắp

**Regime Detection (rule-based):**
- 🟢 `Bull (+1)`: `pct_above_ma200 ≥ 60%` VÀ `adl_slope > 0`
- 🔴 `Bear (-1)`: `pct_above_ma200 ≤ 40%` VÀ `adl_slope < 0`
- 🟡 `Transition (0)`: tất cả trường hợp còn lại

---

## 🤖 Gemini AI Analysis

Mỗi ngày, Gemini Vision nhận chart PNG 3×2 + dữ liệu số và sinh báo cáo gồm:

- 🎯 **Regime & Composite**: Tóm tắt điểm số và trạng thái thị trường
- 🔍 **Nhận định chung**: Gọi tên chính xác trạng thái cốt lõi
- 📊 **Chi tiết 7 chỉ báo**: % MA, ADL+HL Line, McClellan, Net H/L, A/D Ratio, UpVol+TRIN, Breadth Thrust
- ⏱ **Ngắn hạn (1-4 tuần)**: Momentum và rủi ro gần
- 📅 **Dài hạn (3-6 tháng)**: Xu hướng lớn
- ⚠️ **Rủi ro / Phân kỳ**: Divergence, quá mua/bán
- 🎬 **Hành động chiến lược**: Khuyến nghị vị thế danh mục

---

## 📝 Giấy phép

MIT License — Xem file [LICENSE](LICENSE) để biết thêm chi tiết.

---

## 🙏 Tài nguyên & Cảm ơn

- [vnstock](https://github.com/thinh-vu/vnstock) — Thư viện dữ liệu chứng khoán Việt Nam
- [McClellan Financial Publications](https://www.mcoscillator.com/) — Lý thuyết McClellan Oscillator
- [StockCharts — ChartSchool](https://school.stockcharts.com/doku.php?id=market_indicators) — Market Breadth Theory
- Google Gemini AI — Phân tích chart bằng Vision API

---

<div align="center">
<sub>©️ FTU-Kudo</sub>
</div>
