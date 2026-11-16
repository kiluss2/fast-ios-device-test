# fastSimulatorTest & fastDeviceTest

Bạn chỉ cần gõ lệnh và mô tả bằng lời. Agent tự viết flow hoặc bấm từng bước, chạy, rồi báo
PASS/FAIL kèm ảnh chụp.

## Cài đặt (máy mới)

Chỉ cần có sẵn Xcode 26 hoặc 27. Không cần Homebrew hay xcodegen: AXe đã đóng kèm trong gói.

**Cách nhanh nhất:** nhờ AI cài: "làm theo SETUP.md trong https://github.com/kiluss2/fast-ios-device-test".
[SETUP.md](SETUP.md) có từng bước, lệnh kiểm tra, kết quả mong đợi và cách sửa từng lỗi.

**Tự cài:**

1. Mở Terminal, chạy:
   ```bash
   git clone https://github.com/kiluss2/fast-ios-device-test.git ~/fast-ios-device-test
   ~/fast-ios-device-test/install.sh --link
   ~/fast-ios-device-test/install.sh --check          # dòng cuối phải là CHECK OK
   ```
2. Thoát rồi mở lại Claude Code.

Cập nhật bản mới: `git -C ~/fast-ios-device-test pull && ~/fast-ios-device-test/install.sh --link`.
Giữ nguyên thư mục `~/fast-ios-device-test` vì skill và lệnh trỏ vào đó. Không dùng git thì tải
zip ở trang [Releases](https://github.com/kiluss2/fast-ios-device-test/releases), giải nén và chạy
`install.sh` trong đó (không thêm cờ).

## iPhone thật

Mỗi máy cần (chủ máy bật một lần):

- Mở khoá và cắm vào Mac (hoặc chung Wi‑Fi sau khi đã ghép đôi), đã bấm "Tin cậy máy tính này".
- Cài đặt › Quyền riêng tư & Bảo mật › Chế độ nhà phát triển: bật.
- Có development profile chứa máy đó: thường chỉ cần đăng nhập Xcode và chạy một app bất kỳ lên
  máy một lần. Tool không cần đăng nhập Apple ID.
- Lần đầu iOS có thể hỏi bật Cài đặt › Nhà phát triển › Enable UI Automation.

Kiểm tra một máy: `fastDeviceTest check --device <tên>`: báo thiếu gì và cách sửa. Máy bị khoá
giữa chừng thì tool báo ngay sau vài giây: mở khoá bằng tay rồi chạy lại. Đặt Tự động khoá dài hơn
(ví dụ 5 phút) trên máy test thì ít gặp hơn.

## Lệnh

| Lệnh | Dùng khi |
| --- | --- |
| `/fastSimulatorTest <mô tả>` | Test trên Simulator (mặc định, nhanh) |
| `/fastDeviceTest <mô tả>` | Test trên iPhone thật, kể cả Settings / Shortcuts / màn hình chính |
| `/fastUIReview <session>` | Xem lại video đã quay |
| (tự động) | Mỗi lần chạy flow PASS, và lần `--before` fail đúng bước kiểm tra, được chép ra `~/Desktop/<project> <ticket>/`: ảnh ghép có chú thích và video nén trong `evidence/` (kéo cả folder này vào PR), ảnh lẻ trong `source/`. Merge PR xong thì tự xoá folder đó |
| `fastEvidenceSheet <spec.json>` | Dựng lại ảnh ghép sau khi sửa `source/sheet.json` (đổi thứ tự, chú thích, ảnh) |
| `fastUIMap <bundle id> [--find <chữ> \| --path-to <màn>]` | Bản đồ màn hình các lần chạy trước đã ghi: selector, đường đi; `--path-to` in sẵn một lệnh đi thẳng tới màn đó |
| `fastDeviceTest check --device <tên>` | Kiểm tra một iPhone trước khi chạy |

Agent tự chọn giữa viết flow (để chạy lại, chạy nhiều máy, quay video) và bấm từng bước bằng
`do` (để dò màn mới). Việc dò dài có thể giao cho subagent `ui-explorer` (Haiku, đang thử nghiệm).

## Ví dụ

```
/fastSimulatorTest mở tab Lyrics ở trạng thái premium, chụp màn hình
/fastSimulatorTest màn Home, chạy cả sáng và tối, có quay video
/fastDeviceTest trên iPhone của tôi: mở Home, kiểm tra không hiện paywall
/fastDeviceTest trên iPhone, có quay video: mở paywall rồi đóng
/fastDeviceTest bấm giữ widget ngoài màn hình chính, mở Edit Widget
```

## Cần biết

- Cử chỉ hỗ trợ: bấm, bấm giữ, chạm đúp, kéo-thả, vuốt (cả vuốt từ mép), gõ chữ, phím
  (xoá, Return, mũi tên, Cmd+…), nút Home/âm lượng, thanh trượt; riêng iPhone thêm pinch, xoay hai
  ngón, xoay ngang/dọc, bánh xe chọn giờ, chạm hai ngón. Simulator không làm được pinch, xoay,
  bánh xe; iPhone không lắc máy được: tool báo rõ thay vì làm sai.
- Thời gian: một bước `do` khoảng 2 giây trên iPhone, 3 giây trên Simulator; một flow trên
  iPhone khoảng 7 giây khi runner đang chạy (lần đầu ~15–20 giây).
- Runner trên iPhone tự tắt sau 5 phút rảnh; agent luôn `stop` khi xong. Xong hẳn việc thì agent
  chạy `fastDeviceTest lock` (tắt runner rồi khoá màn hình, cho máy để Auto-Lock "Never") và
  `fastSimulatorTest shutdown` (tắt các Simulator đang bật cho nhẹ máy). Máy đang bị người khác
  dùng thì tool báo "busy", không giành máy.
- Nhiều bước gộp một lệnh mà có bước nguy hiểm (Xoá, Thanh toán, Gửi, Đăng xuất…) thì tool từ chối,
  trừ khi agent ghi rõ `"confirm": true`.
- Trạng thái cài sẵn (premium, đã xong onboarding…) tự bị xoá sau khi test trên Simulator; trên
  iPhone, chúng chỉ là tham số khi mở app, không ghi vào máy.
- Quay video được cả Simulator lẫn iPhone thật (iPhone dùng tính năng quay của XCTest, không cần
  Control Center hay QuickTime); video nén nhẹ, khoảng 1–2 MB cho 20 giây.
- Bàn phím tiếng Việt (Telex) trên máy sẽ biến chữ agent gõ; số và phím thì không sao.

Có gì mới: xem [CHANGELOG.md](CHANGELOG.md).
