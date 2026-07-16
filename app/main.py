import os
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.database import engine, Base, sync_schema, recover_interrupted_jobs, ensure_default_admin
from app.routers import auth, dashboard, videos, logs, comments, admin, results, diary, projects, notebooks, diary_ai

Base.metadata.create_all(bind=engine)
sync_schema()
recover_interrupted_jobs()
ensure_default_admin()

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY chưa được đặt trong .env — không thể khởi động an toàn. "
        "Xem .env.example để biết cách cấu hình."
    )

app = FastAPI(title="SCI Portal — Viện Tế Bào Gốc", docs_url=None, redoc_url=None)

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=86400 * 7)

# Tăng số này mỗi khi đẩy bản sửa quan trọng — dùng để tự kiểm tra qua /_status xem
# server đã thực sự chạy code mới sau khi deploy hay chưa, không cần nhờ ai vào xem log.
BUILD_VERSION = "2026-07-15-10"


@app.get("/_status")
def system_status():
    """Trang chẩn đoán công khai, không cần đăng nhập — dùng khi nghi ngờ server chưa
    nhận code mới sau deploy (VD: git pull xong nhưng service chưa được restart).
    Không lộ thông tin nhạy cảm — chỉ báo phiên bản code và tài khoản admin có tồn tại."""
    from datetime import datetime
    from app.database import SessionLocal, DEFAULT_ADMIN_EMAIL
    from app.models import User

    db = SessionLocal()
    try:
        admin_exists = db.query(User).filter(User.email == DEFAULT_ADMIN_EMAIL).first() is not None
    finally:
        db.close()

    return {
        "build": BUILD_VERSION,
        "server_time_utc": datetime.utcnow().isoformat() + "Z",
        "default_admin_exists": admin_exists,
    }

app.mount("/static", StaticFiles(directory="static"), name="static")
# KHÔNG mount /uploads công khai — file trong đây (video, nhật ký, minh chứng báo cáo)
# phải đi qua route có kiểm tra quyền (xem diary.py, results.py, videos.py).

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(videos.router)
app.include_router(logs.router)
app.include_router(comments.router)
app.include_router(admin.router)
app.include_router(results.router)
app.include_router(diary_ai.router)  # phải đăng ký TRƯỚC diary.router — /diary/{log_id} sẽ nuốt /diary/ask-ai nếu sau
app.include_router(diary.router)
app.include_router(projects.router)
app.include_router(notebooks.router)
