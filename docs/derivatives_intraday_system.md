# Hệ thống phái sinh intraday-ready cho Việt Nam

Tài liệu này phác thảo kiến trúc & quy trình xây dựng **hệ thống phái sinh intraday-ready** cho thị trường Việt Nam, tập trung vào **chất lượng dữ liệu**, **tái lập backtest**, **quan sát rủi ro**, và **khả năng mở rộng từ research → paper → live**.

> ⚠️ **[CẦN KIỂM CHỨNG]**: Các quy định về phiên giao dịch, lịch nghỉ lễ, giới hạn API, và các thay đổi KRX cần xác thực từ nguồn chính thức (HOSE/HNX/VSDC hoặc Sở GDCK).

---

## 1. Mục tiêu hệ thống

- **Dữ liệu chuẩn & kiểm soát chất lượng**
  - Chuẩn hóa timezone, timestamp, định dạng symbol (VN30F1M, VN30F2024...).
  - Kiểm soát *clock drift*, mất mẫu, trễ dữ liệu, và outliers.
- **Backtest tái lập**
  - Nhất quán giữa dữ liệu thực tế và dữ liệu backtest (schema, timezone, phiên giao dịch).
  - Lưu snapshot dữ liệu thô + pipeline transform có version.
- **Quan sát & kiểm soát rủi ro hệ thống**
  - Giám sát độ trễ, độ phủ dữ liệu, và mức độ lệch dữ liệu real-time.
  - Theo dõi rủi ro vận hành (API throttling, lỗi kết nối, dữ liệu thiếu).
- **Mở rộng research → paper → live**
  - Dùng chung hạ tầng dữ liệu, feature store, và signal engine.
  - Tách rõ ràng mô-đun ingest, xử lý, signal, execution.

---

## 2. Kiến trúc tổng quan (Research → Live)

```
[Source APIs] → [Ingestion Layer] → [Quality & Validation] → [Storage]
                                            ↓
                                     [Feature Store]
                                            ↓
                          [Backtest Engine] / [Paper Trading]
                                            ↓
                                    [Execution Engine]
                                            ↓
                                    [Risk & Observability]
```

### Thành phần cốt lõi

1. **Ingestion Layer**
   - Thu thập dữ liệu intraday, L1/L2 (nếu có), dữ liệu lịch sử.
   - Đảm bảo timestamp nhất quán (timezone + trading session).

2. **Quality & Validation**
   - Kiểm tra gap trong dữ liệu (missing bar/tick).
   - Kiểm tra *clock drift* bằng so sánh timestamp local vs exchange time.
   - Kiểm tra outlier (giá/khối lượng cực trị so với rolling stats).

3. **Storage**
   - Raw store (immutable): lưu dữ liệu thô.
   - Clean store: dữ liệu đã chuẩn hóa cho backtest.

4. **Feature Store**
   - Lưu feature theo phiên và theo timeframe (1s/1m/5m).
   - Lưu metadata (schema version, data quality metrics).

5. **Backtest & Paper**
   - Dùng chung data format với live (không rewrite logic).
   - Giữ seed/ config để tái lập chiến lược.

6. **Execution Engine**
   - Thực thi lệnh + quản lý trạng thái vị thế.
   - Đồng bộ với risk module trước khi gửi lệnh.

7. **Risk & Observability**
   - Risk: max drawdown theo ngày, limit position, kill-switch.
   - Observability: dashboard theo dõi latency, errors, PnL.

---

## 3. Đặc thù phái sinh Việt Nam (Intraday)

- **Phiên giao dịch cố định**
  - Thanh khoản tập trung vào phiên sáng/chiều.
  - Gaps intraday thường do ngắt phiên, cần xử lý theo session.

- **Độ trễ & ổn định dữ liệu**
  - Dữ liệu intraday có thể lệch timestamp khi API bị nghẽn.
  - Cần đo latency và alert khi vượt ngưỡng.

- **Sai lệch timezone/clock drift**
  - Việt Nam dùng **Asia/Ho_Chi_Minh (UTC+7)**.
  - Cần đồng bộ server clock (NTP) và log chênh lệch thời gian.

- **⚠️ CẦN KIỂM CHỨNG**
  - Quy định phiên giao dịch theo KRX.
  - Lịch nghỉ lễ chính thức.
  - Giới hạn API/ rate limits từ nhà cung cấp dữ liệu.

---

## 4. Dữ liệu chuẩn & kiểm soát chất lượng

### 4.1 Chuẩn hóa dữ liệu

- **Schema chuẩn hóa đề xuất**
  - `symbol`, `timestamp`, `price`, `volume`, `side` (buy/sell), `source`.
  - `timestamp` nên là UTC hoặc luôn ghi rõ timezone.

- **Suy đoán loại tài sản**
  - Sử dụng parser theo định dạng phái sinh (ví dụ VN30F1M, VN30F2024).

### 4.2 Kiểm soát chất lượng

| Hạng mục | Kiểm tra | Mục tiêu |
| --- | --- | --- |
| Missing bars | Count gap so với calendar | Không có gap ngoài giờ nghỉ |
| Clock drift | So sánh server vs exchange time | < 500ms (tuỳ SLA) |
| Outlier | Z-score/median filter | Loại bỏ spike lỗi | 
| Duplicate | Hash theo timestamp/price | Không có bản ghi trùng | 

---

## 5. Backtest tái lập

- **Snapshot dữ liệu**: lưu raw + clean để so sánh.
- **Version pipeline**: mỗi lần thay đổi transform cần version tag.
- **Config-driven**: lưu toàn bộ tham số trong YAML/JSON.
- **Deterministic**: seed random + deterministic order execution.

---

## 6. Quan sát & kiểm soát rủi ro hệ thống

### 6.1 Observability

- **Metrics**: latency, data coverage, error rate, API limit hits.
- **Logs**: log đầy đủ lỗi kết nối, retry, và trạng thái pipelines.
- **Dashboard**: realtime view về dữ liệu và lệnh.

### 6.2 Risk Control

- **Kill-switch** khi:
  - Latency vượt ngưỡng
  - Dữ liệu thiếu quá X% trong phiên
  - Lỗi kết nối liên tục
- **Position limits** theo contract, theo ngày.
- **PnL guardrails**: stop trading khi drawdown vượt ngưỡng.

---

## 7. Mở rộng từ research → paper → live

| Giai đoạn | Đặc điểm | Kết nối |
| --- | --- | --- |
| Research | Linh hoạt, thử nghiệm | Dùng chung data schema |
| Paper | Mô phỏng gần real | Dùng chung execution logic |
| Live | Ổn định, an toàn | Dùng chung risk controls |

---

## 8. Gợi ý workflow với `vnstock`

Ví dụ dùng dữ liệu lịch sử để kiểm thử pipeline:

```python
from vnstock import *

# Lấy dữ liệu lịch sử daily (example)
df = stock_historical_data('VCB', '2024-01-01', '2024-12-31', '1D')
```

Ví dụ lấy intraday cho kiểm thử chất lượng dữ liệu:

```python
from vnstock import *

stock = Vnstock().stock()
quote = stock.quote()

intraday_df = quote.intraday(symbol='VN30F1M', page_size=10_000, show_log=False)
```

> **Lưu ý**: Độ trễ và độ phủ dữ liệu thực tế cần được đánh giá từ nhà cung cấp dữ liệu.

---

## 9. Checklist triển khai (rút gọn)

- [ ] Xác thực lịch giao dịch chính thức (KRX, HOSE/HNX).
- [ ] Thiết lập NTP + kiểm tra clock drift.
- [ ] Ingest dữ liệu intraday real-time + retry logic.
- [ ] Kiểm soát chất lượng dữ liệu theo phiên.
- [ ] Backtest pipeline tái lập (snapshot + config).
- [ ] Risk controls + kill-switch.
- [ ] Observability dashboard.

---

## 10. Tài liệu tham khảo nội bộ

- `vnstock` cung cấp các hàm intraday & historical để bootstrap data pipeline.
- Hãy ưu tiên kiểm chứng quy định phiên giao dịch và lịch nghỉ lễ từ nguồn chính thức.
