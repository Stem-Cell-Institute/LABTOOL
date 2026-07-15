"""Khởi tạo database và tạo admin mặc định — chạy thủ công khi cần: python init_db.py

Lưu ý: bước này KHÔNG bắt buộc nữa — app tự làm y hệt mỗi lần khởi động
(xem ensure_default_admin trong app/database.py). Giữ file này cho ai quen quy trình cũ.
"""
from dotenv import load_dotenv

load_dotenv()

from app.database import engine, Base, ensure_default_admin, DEFAULT_ADMIN_EMAIL, DEFAULT_ADMIN_PASSWORD

Base.metadata.create_all(bind=engine)
ensure_default_admin()

print("=" * 50)
print("[OK] Database da san sang!")
print("[OK] Tai khoan admin mac dinh (neu chua ton tai thi vua duoc tao):")
print(f"     Email    : {DEFAULT_ADMIN_EMAIL}")
print(f"     Password : {DEFAULT_ADMIN_PASSWORD}")
print("[!]  Hay doi mat khau ngay sau khi dang nhap lan dau!")
print("=" * 50)
