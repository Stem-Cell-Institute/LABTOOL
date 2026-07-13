# Hướng dẫn Deploy — SCI Portal

## Yêu cầu

- **Python 3.10+** (tải tại python.org)
- **pip** (đi kèm Python)
- **Google Gemini API Key** (liên hệ quản trị hệ thống để lấy key)

---

## Các bước deploy (Windows Server)

### 1. Lấy source code

```bash
git clone https://github.com/<your-org>/labtool.git
cd labtool
```

### 2. Cài dependencies

```bash
pip install -r requirements.txt
```

### 3. Tạo file cấu hình `.env`

Sao chép file mẫu rồi điền giá trị thực:

```bash
copy .env.example .env
```

Mở `.env` và điền:

```
GOOGLE_API_KEY=<Gemini API key do quản trị cung cấp>
SECRET_KEY=<chuỗi ngẫu nhiên dài, ví dụ: abc123xyz!@#...>
```

> **Lưu ý:** `SECRET_KEY` dùng để mã hoá session. Đặt một chuỗi bất kỳ dài ~32 ký tự là đủ.

### 4. Khởi tạo / cập nhật database

```bash
python migrate.py
```

Lệnh này tạo file `labtool.db` và tài khoản admin mặc định:
- Email: `admin@vientebaogoc.vn`
- Password: `admin123` → **đổi ngay sau khi đăng nhập lần đầu** (vào Hồ sơ để đổi cả email lẫn mật khẩu)

### 5. Chạy ứng dụng

```bash
python run.py
```

Truy cập tại: `http://<IP-server>:8000`

---

## Chạy như một dịch vụ Windows (tự khởi động lại)

Dùng **NSSM** (tải tại nssm.cc):

```bash
nssm install SCI-Portal "C:\Python310\python.exe" "C:\labtool\run.py"
nssm set SCI-Portal AppDirectory "C:\labtool"
nssm start SCI-Portal
```

---

## Cấu trúc thư mục sau khi deploy

```
labtool/
├── .env              ← file bí mật, KHÔNG commit lên git
├── labtool.db        ← database SQLite, backup định kỳ
├── uploads/          ← file upload của người dùng, backup định kỳ
└── ...
```

> **Backup:** Chỉ cần backup 2 thứ: `labtool.db` và thư mục `uploads/`

---

## Cập nhật phiên bản mới

```bash
git pull
python migrate.py
# restart service SCI-Portal (hoặc Ctrl+C rồi python run.py lại)
```
