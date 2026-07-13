import os
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.database import engine, Base, sync_schema
from app.routers import auth, dashboard, videos, logs, comments, admin, results, diary, projects, notebooks, diary_ai

Base.metadata.create_all(bind=engine)
sync_schema()

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production-vienbao-2025")

app = FastAPI(title="VidNote — Viện Tế Bào Gốc", docs_url=None, redoc_url=None)

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=86400 * 7)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

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
