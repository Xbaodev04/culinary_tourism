# Thống kê truy cập du khách

Flutter gửi sự kiện đến `POST /api/v1/analytics/collect` sau khi khách đồng ý.
Không cần đăng nhập. Mã thiết bị là UUID lưu trên trình duyệt, không phải IP
hay danh tính người dùng. Đổi trình duyệt, dùng ẩn danh hoặc xóa dữ liệu có thể
được tính là thiết bị khác. Các tab mở đồng thời có thể tạo phiên riêng.

- Thiết bị: số mã thiết bị riêng biệt có hoạt động trong kỳ, không cộng các tổng theo ngày.
- Phiên: cặp thiết bị/phiên riêng biệt trong kỳ; phiên mới sau 30 phút không hoạt động.
  Reload trong thời hạn giữ nguyên mã phiên; backend loại trùng sự kiện mở phiên.
- QR: số lần mở trang chi tiết từ liên kết `?source=qr` hoặc máy quét trong app.
  Đây là lượt mở liên kết, không xác minh camera đã quét mã. Reload trang QR
  là lượt mở trang mới nhưng không phải thiết bị hoặc phiên mới.
- Audio: tận dụng `audio_started` hiện có, chỉ ghi khi thực sự bắt đầu phát.

Giữ hàng đợi tối đa 500 sự kiện và gửi lại với cùng ID khi mất mạng. Từ chối
chia sẻ không tạo mã thiết bị; tắt chia sẻ xóa mã và hàng đợi trên máy. Khách
từng chỉ đồng ý thống kê audio được hỏi lại khi mở rộng sang thống kê truy cập;
lựa chọn từ chối trước đây được giữ nguyên. Bản ghi đã nhận tự hết hạn sau
90 ngày qua TTL index MongoDB.

Admin xem `traffic.totals` và `traffic.daily` trong `/admin/dashboard/stats`;
chủ quán nhận số liệu giới hạn theo POI của mình trong `/owner/analytics`.
API chỉ trả tổng số, không trả mã thiết bị hoặc phiên. Bản đầu không bổ sung
đếm online qua heartbeat, fingerprint hay lọc bot nâng cao.

## Triển khai

Deploy backend Render, frontend quản trị Vercel và Flutter Web cùng phiên bản.
Không cần biến môi trường mới; index được tạo khi backend khởi động. QR mới
tự có `source=qr`; QR cũ vẫn mở quán nhưng cần tạo/in lại để phân biệt nguồn QR.
Số liệu bắt đầu tích lũy từ khi deploy, không suy ra thiết bị từ sự kiện cũ thiếu ID.
