# Deploy FastAPI lên Render và React lên Vercel

Kiến trúc đề xuất: Vercel phục vụ frontend; `/api/*` và `/static/*` được proxy
sang Render. Mobile gọi trực tiếp HTTPS/WSS của Render. MongoDB đặt trên Atlas;
ảnh và MP3 nằm trên Cloudinary, được web/mobile tải trực tiếp từ CDN.
Backend dùng Render Free và không cần persistent disk để giữ ảnh/audio.

## 1. Chuẩn bị MongoDB Atlas

- Tạo cluster hỗ trợ transactions (replica set), database user có quyền
  `readWrite` trên `quan4_culinary` và lấy connection string `mongodb+srv://...`.
- Thêm outbound IP của Render vào Atlas Network Access (lấy trong dashboard
  service). Nếu kết nối lỗi, kiểm tra IP allowlist và ký tự đặc biệt trong mật khẩu
  đã được URL-encode.
- Cluster mới chưa chứa dữ liệu local. Nếu cần dữ liệu đang có, dùng
  `mongodump`/`mongorestore` chuyển sang Atlas và sao chép media riêng.

## 2. Cloudinary và backend trên Render Free

Tạo tài khoản Cloudinary, lấy **Cloud name**, **API key** và **API secret** trong
Console > API Keys. Các khóa chỉ cấu hình ở backend, không đặt trong frontend,
mobile hoặc commit vào Git. Không cần tạo unsigned upload preset.

Push các thay đổi lên repository Git của bạn. Trên Render chọn **New > Blueprint**,
chọn repository và file `render.yaml` ở thư mục gốc.

Blueprint chọn `free`, không gắn disk. Render Free có thể ngủ sau 15 phút không có
traffic và khởi động chậm ở lần truy cập tiếp theo; tác vụ audio đang chạy có thể
bị ngắt khi instance dừng. Ảnh/audio đã upload thành công vẫn nằm trên Cloudinary.
Cloudinary Free và Atlas Free đều có hạn mức; theo dõi Usage để giữ trong gói Free.
Nếu service đã tồn tại ở gói trả phí, kiểm tra lại plan và disk trên Dashboard:
chỉnh file YAML không có nghĩa mọi tài nguyên đang tính phí đã tự được gỡ.

Blueprint đã cấu hình:

| Mục | Giá trị |
| --- | --- |
| Root Directory | `backend` |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1` |
| Health Check | `/health/ready` |
| Instance Type | `Free` |
| MEDIA_STORAGE | `cloudinary` |
| STATIC_DIR | `app/static` (file tạm và static đóng gói trong Git) |

Nhập các biến được hỏi:

| Biến | Cách điền |
| --- | --- |
| `MONGO_URI` | Connection string Atlas, thay username/password thật |
| `CORS_ORIGINS` | Origin Vercel chính xác, ví dụ `https://ten-du-an.vercel.app`, không có dấu `/` cuối |
| `SUPERADMIN_EMAIL` | Email tài khoản quản trị của bạn |
| `SUPERADMIN_PASSWORD` | Mật khẩu mạnh, ngẫu nhiên, ít nhất 12 ký tự |
| `FERNET_KEY` | Khóa Fernet hợp lệ theo hướng dẫn dưới |
| `CLOUDINARY_CLOUD_NAME` | Cloud name từ Cloudinary |
| `CLOUDINARY_API_KEY` | API key từ Cloudinary |
| `CLOUDINARY_API_SECRET` | API secret từ Cloudinary |

Blueprint đặt sẵn `MEDIA_STORAGE=cloudinary` và `CLOUDINARY_FOLDER=quan4`. Nếu tạo
Web Service thủ công thay vì Blueprint, bạn cần thêm hai biến này và toàn bộ các
biến môi trường production còn lại. Để phát triển local, mặc định
`MEDIA_STORAGE=local`; có thể đổi sang `cloudinary` trong `backend/.env` để thử.
Backend từ chối khởi động nếu chọn Cloudinary nhưng thiếu thông tin xác thực.

Nếu chưa có URL Vercel, tạo/import project Vercel trước để xác định production
domain, rồi điền origin đó vào Render. Với nhiều origin, phân cách bằng dấu phẩy;
không dùng `*` hoặc wildcard cho preview domain.

Tạo khóa Fernet bằng Python trong môi trường backend đã cài dependencies:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Nếu chuyển database đang có CCCD mã hóa, dùng lại `FERNET_KEY` cũ để đọc được dữ
liệu. Giữ khóa ổn định qua các lần deploy. Không commit `.env` hay secrets.
Blueprint tự tạo ba khóa JWT/refresh/secret riêng biệt cho service mới.

Biến tùy chọn cấu hình thêm trong Render Environment:

- `REDIS_URL`: Redis/Render Key Value; Redis không bắt buộc để khởi động, nhưng
  cache và các rate limit dùng Redis sẽ không hoạt động khi thiếu kết nối.
- `GEMINI_API_KEY`, `GOOGLE_TRANSLATION_API_KEY`: dùng AI/tính năng dịch tương ứng.
- `GOOGLE_CLIENT_ID`: Web OAuth client ID trùng cấu hình đăng nhập Google mobile.
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `FROM_EMAIL`: cấu hình SMTP
  hiện có; Render Free chặn cổng 25/465/587 nên cần dịch vụ gửi mail qua HTTPS
  nếu muốn gửi mail trên Free (chưa tích hợp trong thay đổi media).
- `GOONG_API_KEY`: tra cứu địa chỉ bằng Goong nếu cần.

Giữ **một instance, một worker** với cấu hình hiện tại để SSE trong bộ nhớ và tác
vụ nền cùng process. Sau khi deploy, mở URL thực tế Render cấp, kiểm tra
`/health/ready` và `/docs`. Không giả định tên service luôn tương ứng chính xác
`quan4-api.onrender.com`.

## 3. Frontend trên Vercel

1. Trong `frontend/vercel.json`, thay **cả hai** URL `https://quan4-api.onrender.com`
   bằng URL backend thực tế từ bước 2; giữ nguyên các phần `/api/:path*` và
   `/static/:path*`.
2. Push thay đổi, import cùng repository vào Vercel. Chọn **Root Directory =
   `frontend`**, framework **Vite**. File cấu hình dùng `npm ci`, `npm run build`,
   output `dist`.
3. Trong Vercel Environment Variables, đặt `VITE_API_BASE_URL=/api/v1` và
   `VITE_STATIC_BASE_URL=/static` cho Production. Tham khảo
   `frontend/.env.production.example`. Không nhập secrets backend vào biến `VITE_*`.
4. Deploy. Mỗi lần đổi biến `VITE_*`, redeploy frontend để build nhận giá trị mới.
5. Cập nhật `CORS_ORIGINS` trên Render đúng origin frontend cuối cùng nếu cần.

Proxy giữ request API và cookie trên origin Vercel. Giữ `COOKIE_SECURE=true`,
`COOKIE_SAMESITE=lax`, **không đặt `COOKIE_DOMAIN`**. Trỏ trực tiếp frontend
`vercel.app` sang API `onrender.com` sẽ tạo cookie khác site và có thể bị trình
duyệt chặn; cấu hình ở đây dùng proxy để tránh phụ thuộc vào cookie bên thứ ba.

## 4. Chuyển ảnh/audio cũ lên Cloudinary

File đã lưu local không tự chuyển lên Cloudinary khi đổi biến môi trường. Chạy
script từ `backend/` trên máy có file gốc và Python dependencies đã được cài:

```powershell
python -m scripts.migrate_media_to_cloudinary
```

Mặc định đây là **dry-run**: đọc database và báo số tham chiếu/file thiếu,
không upload hoặc sửa bản ghi. Kiểm tra `MONGO_URI`, `DB_NAME`, `STATIC_DIR` đúng
nguồn dữ liệu cần chuyển. Để cập nhật database Atlas đích, database đó phải có
các bản ghi tương ứng trước (ví dụ đã restore từ local).

Sau khi xác nhận database và file đúng, đặt `MEDIA_STORAGE=cloudinary` cùng ba
khóa Cloudinary trong môi trường backend, rồi chạy:

```powershell
python -m scripts.migrate_media_to_cloudinary --apply
```

Script upload file được tham chiếu trong `poi`, `menu_items`,
`poi_owner_registrations`, `poi_submissions`, `poi_localizations`, rồi cập nhật URL
kể cả danh sách và `proposed_changes` lồng nhau. File gốc được giữ nguyên. Có thể
chạy lại: hash nội dung và metadata `media_assets` giúp tái sử dụng upload.
File thiếu giữ URL cũ và được đếm trong `missing`; nếu có `conflicts`, bản ghi
đã bị sửa trong lúc upload, chạy lại để xử lý phần còn lại. Đường dẫn không an
toàn sẽ dừng script; các tài liệu đã cập nhật trước đó được giữ.

Metadata Cloudinary gồm URL HTTPS, `public_id`, resource type và dung lượng được
lưu trong collection `media_assets`; không xóa collection này khi chuyển dữ liệu.
Giữ ổn định `CLOUDINARY_CLOUD_NAME` và `CLOUDINARY_FOLDER` để backend nhận diện các
URL đã lưu; khi chuyển cloud/folder cần migrate cả file và metadata tương ứng.
MP3 được upload bằng resource type `video`. Audio admin được kiểm tra quyền trước
khi chuyển hướng sang Cloudinary để phát/tải. File có URL CDN công khai như các
file `/static` trước đây; đây không phải kho lưu tài liệu riêng tư.

Không tự xóa asset khi bỏ ảnh/đổi thuyết minh vì cùng hash có thể được nhiều quán
hoặc hồ sơ dùng chung. Khi dọn Cloudinary thủ công, cần kiểm tra mọi tham chiếu và
xóa metadata tương ứng; cache Mongo không tự phát hiện file bị xóa ngoài ứng dụng.

## 5. Kiểm tra sau deploy

- Ảnh/MP3 mới tự upload lên Cloudinary trước khi trả URL hoặc đánh dấu hoàn tất.
  Nếu upload thất bại, backend báo lỗi thay vì trả URL local có thể mất sau restart.
- PMTiles chưa chuyển sang Cloudinary. Bản đồ offline cần được đóng gói trong
  source deploy hoặc triển khai storage riêng phù hợp. File bị `.gitignore`
  bỏ qua không tự xuất hiện trên cloud.
- Đăng nhập `/admin/login`, đổi mật khẩu bootstrap khi được yêu cầu. Kiểm tra
  refresh/logout và reload trực tiếp `/admin` hoặc `/owner` không bị 404.
- Thử upload ảnh, tạo audio, mở SSE monitor và nghe MP3; redeploy backend rồi xác
  nhận media còn truy cập được. SSE có thể kết nối lại khi proxy ngắt stream.
- Tích hợp Cloudinary áp dụng cho luồng tạo audio qua admin tasks và `/audio/tts`.
  Module `/localizations/on-demand` trong repo hiện vẫn còn TODO; thay đổi storage
  không hoàn thiện tính năng tạo audio tức thì đó.
- Mobile dùng `https://<backend>.onrender.com/api/v1`; WebSocket dùng WSS trực tiếp
  tới route hiện có trên Render. Proxy Vercel ở đây chỉ dành cho HTTP frontend.
- Nếu muốn seed dữ liệu mẫu, script hiện cần `data/seed` ngoài `backend`.
  Chạy `python -m scripts.seed_pois --no-demo-users` từ thư mục backend của bản
  checkout đầy đủ trên máy bạn, với biến môi trường trỏ đến Atlas đích. Chỉ thực
  hiện khi đã xác nhận đúng database và cần dữ liệu mẫu; không dùng `--reset`
  trên database đang dùng.

## Tài liệu chính thức

- [Render: FastAPI](https://render.com/docs/deploy-fastapi)
- [Render: Blueprint](https://render.com/docs/blueprint-spec)
- [Render: Persistent disks](https://render.com/docs/disks)
- [Render: Free services](https://render.com/docs/free)
- [Cloudinary: Python uploads](https://cloudinary.com/documentation/django_image_and_video_upload)
- [Cloudinary: Audio](https://cloudinary.com/documentation/audio_transformations)
- [Vercel: Vite](https://vercel.com/docs/frameworks/frontend/vite)
- [Vercel: External rewrites](https://vercel.com/docs/routing/rewrites)
