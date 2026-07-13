"""Khởi tạo database và tạo admin user mặc định."""
import os
from dotenv import load_dotenv

load_dotenv()

from app.database import engine, SessionLocal, Base
from app.models import User, Group
from app.auth import hash_password

Base.metadata.create_all(bind=engine)

db = SessionLocal()

try:
    existing = db.query(User).filter(User.email == "admin@vientebaogoc.vn").first()
    if existing:
        print("Admin user da ton tai, bo qua.")
    else:
        admin = User(
            password_hash=hash_password("admin123"),
            full_name="Quản Trị Viên",
            email="admin@vientebaogoc.vn",
            role="admin",
            group_id=None,
        )
        db.add(admin)
        db.commit()
        print("=" * 50)
        print("[OK] Database da san sang!")
        print("[OK] Admin user da duoc tao:")
        print("     Email    : admin@vientebaogoc.vn")
        print("     Password : admin123")
        print("[!]  Hay doi mat khau ngay sau khi dang nhap lan dau!")
        print("=" * 50)
finally:
    db.close()
