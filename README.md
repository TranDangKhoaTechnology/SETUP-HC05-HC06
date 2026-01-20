# SETUP-HC05-HC06

Tool auto-detect và cấu hình module Bluetooth Serial **HC-05 / HC-06** qua AT commands (NAME / PIN / BAUD / ROLE) + **Auto Pair MASTER/SLAVE** (HC-05 master).

> Khuyến nghị: **2x HC-05** để Auto Pair ổn định nhất. MASTER bắt buộc là **HC-05** (vì cần `ROLE=1`, `CMODE`, `BIND/PAIR/LINK`).

---

## Tính năng

### 1) Single Setup (1 thiết bị)
- Auto-detect profile AT (baud + line ending) phổ biến:
  - `38400 + CRLF` (HC-05 AT mode)
  - `9600 + NONE` (HC-06 AT mode)
  - fallback: `38400 + NONE`, `9600 + CRLF`
- Cấu hình:
  - **HC-05:** `NAME`, `PSWD/PIN`, `UART`, `ROLE`, (tuỳ chọn) đọc `ADDR?`, (tuỳ chọn) `RESET`
  - **HC-06:** `NAME`, `PIN/PSWD`, `BAUD` (bảng map), (tuỳ chọn) `ADDR?` (thường không hỗ trợ)
- Log rõ ràng: `>> command` / `<< response`, timeout, retry.

### 2) Auto Pair (MASTER/SLAVE)
Có 2 chế độ:

**(A) Mode TWO (2 cổng / 2 adapter)**
- Setup SLAVE trên `--slave-port` (lấy `ADDR?` nếu có)
- Setup MASTER trên `--master-port` và bind/pair/link tới địa chỉ SLAVE
- Nếu SLAVE không trả `ADDR?` (ví dụ HC-06), MASTER có thể scan bằng `AT+INQ` và cho chọn địa chỉ.

**(B) Mode ONE (1 cổng chung – swap)**
- Cắm **SLAVE trước** → tool setup SLAVE + đọc `AT+ADDR?` (khuyến nghị SLAVE là **HC-05**)
- Tool lưu cache địa chỉ vào `tools/.pair_cache.json`
- Tool yêu cầu **rút SLAVE, cắm MASTER vào cùng cổng** → setup MASTER + `BIND`/`PAIR`/`LINK`
- Lưu ý thực tế: nếu sau khi swap **SLAVE không còn được cấp nguồn**, các lệnh `PAIR/LINK` có thể báo lỗi (ví dụ `ERROR:(16)`) → tool vẫn có thể **BIND OK** và MASTER sẽ auto-connect khi cả 2 được cấp nguồn ở DATA mode.

### 3) GUI Tkinter
- Không cần cmd, chạy bằng nút bấm
- Chọn cổng từ dropdown + Refresh
- Tab **Pair master/slave** (ưu tiên dùng để pair)
- Tab **Setup single** có:
  - “Plan Preview” hiển thị các lệnh sắp chạy
  - Toggle bật/tắt bước (NAME/PIN/UART/ROLE/ADDR?/RESET)
  - Preset nút gợi ý **Suggest SLAVE / Suggest MASTER**
- Hỗ trợ **cuộn bằng lăn chuột** cho tab dài (scrollable).

### 4) Portable (không cần Python)
- Có thể build ra file chạy 1-click (exe/onefile) tuỳ OS bằng PyInstaller.

---

## Yêu cầu phần cứng & lưu ý AT mode

### HC-05 AT mode (bắt buộc để setup/pair)
- Giữ **KEY/EN = HIGH trước khi cấp nguồn** để vào AT mode (thường LED nháy chậm).
- Tool không thể power-cycle giúp bạn, bạn phải cắm/rút nguồn.

### HC-06 AT mode
- Thường nhận AT khi đang rảnh (chưa paired), không cần KEY/EN.

### Wiring
- RX/TX phải **cross** (TX adapter → RX module, RX adapter → TX module)
- **GND chung**
- Module RX logic **3.3V** (cẩn thận 5V).

---

## Cài & chạy (Python)

### 1) Cài dependency
```bash
pip install -r tools/requirements.txt
```

### 2) Chạy GUI
```bash
python tools/hc_setup_gui.py
```

### 3) Chạy CLI (nâng cao / script)
List ports:
```bash
python tools/hc_setup_wizard.py --list-ports
```

Detect-only:
```bash
python tools/hc_setup_wizard.py --port COM4 --detect-only
```

Setup ví dụ:
```bash
python tools/hc_setup_wizard.py --port COM4 --module auto --name MyDevice --pin 1234 --baud 9600 --role slave
```

---

## Hướng dẫn dùng GUI

### Tab: Pair master/slave (khuyến nghị mở app là pair)
1) Chọn **Mode**
   - `two`: 2 cổng (MASTER + SLAVE)
   - `one`: 1 cổng (swap)
2) Chọn port(s)
3) Nhập:
   - Name MASTER / Name SLAVE (tuỳ chọn)
   - PIN (mặc định 1234)
   - Baud (data-mode sau setup)
4) Bấm **Pair Now**
5) Nếu mode `one`:
   - Tool chạy SLAVE phase → báo địa chỉ SLAVE
   - Tool yêu cầu swap: rút SLAVE, cắm MASTER (HC-05) vào AT mode → Continue
6) Xong:
   - Nếu `LINK` fail do SLAVE không cấp nguồn, vẫn có thể **BIND OK**
   - NEXT: cấp nguồn cả 2 ở **DATA mode (KEY/EN LOW)** → MASTER auto-connect.

### Tab: Setup single
- Chọn port + module (auto/hc05/hc06)
- Có 2 nút gợi ý:
  - **Suggest SLAVE**: set role=slave (HC-05), gợi ý name/pin, bật đọc ADDR?
  - **Suggest MASTER**: set role=master (HC-05)
- Plan Preview hiển thị lệnh sẽ chạy
- Nếu bật step mà thiếu input (PIN/NAME) tool sẽ hỏi nhập.

---

## Auto Pair bằng CLI

### Mode TWO (2 ports)
```bash
python tools/hc_setup_wizard.py pair --mode two \
  --master-port COM7 --slave-port COM5 \
  --pin 1234 --baud 9600 --name-master MASTER --name-slave SLAVE
```

### Mode ONE (1 port swap)
```bash
python tools/hc_setup_wizard.py pair --mode one \
  --port COM5 \
  --pin 1234 --baud 9600 --name-master MASTER --name-slave SLAVE
```

Gợi ý quan trọng:
- MASTER bắt buộc là HC-05 (AT+ROLE=1 / BIND/PAIR/LINK)
- Mode ONE nên dùng SLAVE là HC-05 để đọc được `AT+ADDR?`

---

## Advanced / Custom commands (Pair)
Một số firmware HC-05 clone có thể không hỗ trợ đủ ORGL/RMAAD/INIT.
Bạn có thể:
- Skip ORGL: `--no-orig`
- Skip RMAAD: `--no-rmaad`
- Advanced cho phép:
  - bật/tắt “run basic”
  - skip step không-critical
  - thêm extra commands cho MASTER/SLAVE
  - dry-run để in kế hoạch lệnh mà không gửi ra serial

---

## Giải thích lỗi thường gặp

### `ERROR:(16)` khi `AT+PAIR` hoặc `AT+LINK`
Thường xảy ra khi:
- SLAVE không ở trạng thái sẵn sàng (không powered / không data mode)
- Swap mode ONE: bạn rút SLAVE khỏi adapter và SLAVE **mất nguồn** → MASTER không thể `PAIR/LINK` ngay

✅ Cách xử lý:
- Không sao nếu `AT+BIND` OK: sau đó cấp nguồn cả 2 ở DATA mode → MASTER auto-connect.
- Nếu bạn muốn `LINK` chạy ngay, hãy đảm bảo SLAVE vẫn được cấp nguồn trong lúc MASTER chạy `LINK`.

### Detect fail / không có response
- Sai baud / line ending → thử detect-only, đảm bảo đúng AT mode (HC-05 KEY/EN HIGH trước nguồn)
- RX/TX bị đảo, thiếu GND
- Module đang paired / đang bận

---

## Build portable (tuỳ chọn)

Cài:
```bash
pip install -r tools/requirements.txt
pip install pyinstaller
```

Build GUI:
```bash
pyinstaller --onefile --name hc-setup-gui tools/hc_setup_gui.py
```

Build CLI:
```bash
pyinstaller --onefile --name hc-setup-wizard tools/hc_setup_wizard.py
```

---

## Bản quyền & License

Copyright (c) 2026 **TranDangKhoaTechnology**.
