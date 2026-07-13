import os
import re
import threading
from fastapi import APIRouter, Request, Depends, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db, SessionLocal
from app.models import User, Video, ExperimentLog, Group
from app import gemini as gem
from app.activity import log_activity

router = APIRouter(prefix="/videos")
templates = Jinja2Templates(directory="app/templates")

UPLOAD_DIR = "uploads"
MAX_UPLOAD_MB = 500
ALLOWED_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


def _safe_filename(name: str) -> str:
    """Chỉ giữ tên file thuần (bỏ mọi thành phần thư mục/traversal) và ký tự an toàn,
    tránh path traversal khi ghép vào đường dẫn lưu trên server."""
    name = os.path.basename((name or "").replace("\\", "/"))
    name = re.sub(r'[^\w.\-() ]', '_', name).strip()
    return name or "file"


def _group_upload_dir(group_id: int) -> str:
    path = os.path.join(UPLOAD_DIR, str(group_id))
    os.makedirs(path, exist_ok=True)
    return path


def _analyze_in_background(video_id: int):
    """Chạy phân tích Gemini trong thread riêng, cập nhật DB khi xong."""
    db = SessionLocal()
    try:
        video = db.get(Video, video_id)
        if not video:
            return
        video.status = "processing"
        db.commit()

        content, file_name = gem.analyze_video(video.filename)

        title = ""
        for line in content.splitlines():
            stripped = line.lstrip("# ").strip()
            if stripped:
                title = stripped[:200]
                break

        log = ExperimentLog(
            video_id=video_id,
            title=title or video.original_name,
            content=content,
        )
        db.add(log)
        video.status = "done"
        video.gemini_file_name = file_name
        db.commit()
        log_activity(db, "analyze_done",
                     f"Phan tich xong: {video.original_name}",
                     user_id=video.uploaded_by, target_type="video",
                     target_id=video_id, group_id=video.group_id)
    except Exception as e:
        db.rollback()
        try:
            video = db.get(Video, video_id)
            if video:
                video.status = "failed"
                video.error_message = str(e)[:500]
                db.commit()
                log_activity(db, "analyze_failed",
                             f"Loi phan tich: {video.original_name} — {str(e)[:200]}",
                             user_id=video.uploaded_by, target_type="video",
                             target_id=video_id, group_id=video.group_id)
        except Exception:
            pass
    finally:
        db.close()


def _get_user(request: Request, db: Session):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, user_id)


@router.get("", response_class=HTMLResponse)
def video_list(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if user.role == "admin":
        group_id = request.query_params.get("group_id")
        if group_id:
            videos = db.query(Video).filter(Video.group_id == int(group_id)).order_by(Video.uploaded_at.desc()).all()
        else:
            videos = db.query(Video).order_by(Video.uploaded_at.desc()).all()
        groups = db.query(Group).order_by(Group.name).all()
        selected_group = int(group_id) if group_id else None
    else:
        videos = db.query(Video).filter(Video.group_id == user.group_id).order_by(Video.uploaded_at.desc()).all()
        groups = None
        selected_group = None

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(
        request, "videos/list.html",
        {"user": user, "videos": videos, "groups": groups,
         "selected_group": selected_group, "flash": flash},
    )


@router.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request, db: Session = Depends(get_db)):
    from datetime import datetime as _dt
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role == "member" and not user.group_id:
        request.session["flash"] = "Ban chua duoc gan vao nhom nao. Lien he admin."
        return RedirectResponse("/videos", status_code=302)
    groups = db.query(Group).order_by(Group.name).all() if user.role == "admin" else None
    now = _dt.utcnow()
    year_range = list(range(now.year - 2, now.year + 2))
    return templates.TemplateResponse(request, "videos/upload.html", {
        "user": user, "groups": groups,
        "current_month": now.month, "current_year": now.year,
        "year_range": year_range,
    })


@router.post("/upload")
async def upload_video(
    request: Request,
    file: UploadFile = File(...),
    group_id: int = Form(None),
    report_month: int = Form(None),
    report_year: int = Form(None),
    db: Session = Depends(get_db),
):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        groups = db.query(Group).order_by(Group.name).all() if user.role == "admin" else None
        return templates.TemplateResponse(
            request, "videos/upload.html",
            {"user": user, "groups": groups, "error": f"Dinh dang khong ho tro: {ext}"},
            status_code=400,
        )

    target_group_id = group_id if (user.role == "admin" and group_id) else user.group_id
    if not target_group_id:
        groups = db.query(Group).order_by(Group.name).all() if user.role == "admin" else None
        return templates.TemplateResponse(
            request, "videos/upload.html",
            {"user": user, "groups": groups, "error": "Chua chon nhom"},
            status_code=400,
        )

    upload_dir = _group_upload_dir(target_group_id)

    content = await file.read()
    size = len(content)
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        groups = db.query(Group).order_by(Group.name).all() if user.role == "admin" else None
        return templates.TemplateResponse(
            request, "videos/upload.html",
            {"user": user, "groups": groups,
             "error": f"File qua lon (toi da {MAX_UPLOAD_MB}MB). Voi video lon hon, hay dung thu muc mang."},
            status_code=400,
        )

    safe_name = _safe_filename(file.filename)
    save_path = os.path.join(upload_dir, safe_name)
    base, ext2 = os.path.splitext(safe_name)
    counter = 1
    while os.path.exists(save_path):
        save_path = os.path.join(upload_dir, f"{base}_{counter}{ext2}")
        counter += 1

    with open(save_path, "wb") as f:
        f.write(content)

    from datetime import datetime as _dt
    now = _dt.utcnow()
    video = Video(
        group_id=target_group_id,
        uploaded_by=user.id,
        filename=save_path,
        original_name=file.filename,
        file_size=size,
        status="pending",
        source="upload",
        report_month=report_month or now.month,
        report_year=report_year or now.year,
    )
    db.add(video)
    db.commit()
    db.refresh(video)

    threading.Thread(target=_analyze_in_background, args=(video.id,), daemon=True).start()

    log_activity(db, "upload",
                 f"{user.email} upload: {file.filename}",
                 user_id=user.id, target_type="video",
                 target_id=video.id, group_id=target_group_id)
    return RedirectResponse(f"/videos/{video.id}", status_code=302)


@router.get("/{video_id}", response_class=HTMLResponse)
def video_detail(video_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Khong tim thay video")
    if user.role != "admin" and video.group_id != user.group_id:
        raise HTTPException(status_code=403, detail="Khong co quyen truy cap")

    return templates.TemplateResponse(
        request, "videos/detail.html", {"user": user, "video": video}
    )


@router.get("/{video_id}/status")
def video_status(video_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        raise HTTPException(status_code=401)
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404)
    if user.role != "admin" and video.group_id != user.group_id:
        raise HTTPException(status_code=403)
    return JSONResponse({
        "status": video.status,
        "log_id": video.log.id if video.log else None,
        "error": video.error_message,
    })


@router.post("/{video_id}/reanalyze")
def reanalyze(video_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404)
    if user.role != "admin" and video.group_id != user.group_id:
        raise HTTPException(status_code=403)

    # Claim nguyên tử bằng UPDATE...WHERE thay vì đọc-rồi-ghi: nếu 2 request gần như đồng
    # thời (double-click) cùng gọi reanalyze, chỉ 1 request UPDATE được ("thắng"), request
    # kia thấy rowcount=0 và bị chặn — tránh 2 thread cùng ghi ExperimentLog, gây lỗi unique
    # constraint khiến thread thất bại đè status "done" của thread thành công thành "failed".
    claimed = db.query(Video).filter(
        Video.id == video_id,
        Video.status != "processing",
    ).update({"status": "processing", "error_message": ""}, synchronize_session=False)
    db.commit()
    if not claimed:
        request.session["flash"] = "Video đang được phân tích, vui lòng đợi."
        return RedirectResponse(f"/videos/{video.id}", status_code=302)

    if video.log:
        db.delete(video.log)
        db.commit()

    threading.Thread(target=_analyze_in_background, args=(video.id,), daemon=True).start()
    return RedirectResponse(f"/videos/{video.id}", status_code=302)
