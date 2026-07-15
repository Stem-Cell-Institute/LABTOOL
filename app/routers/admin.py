import os
import logging
import threading
from fastapi import APIRouter, Request, Depends, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db, SessionLocal
from app.models import User, Group, Video, ExperimentLog, SystemConfig, ReportPeriod, MonthlyReport, AICalibrationExample, PasswordResetRequest, UserInvite
from app.auth import hash_password
from app.routers.videos import _analyze_in_background
from app.activity import log_activity
from app.security import generate_temp_password, generate_invite_token

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)

ALLOWED_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


def _get_admin(request: Request, db: Session):
    uid = request.session.get("user_id")
    if not uid:
        return None
    user = db.get(User, uid)
    return user if (user and user.is_active and user.role == "admin") else None


def _get_report_viewer(request: Request, db: Session):
    """Admin hoặc người được cấp quyền xem báo cáo toàn cảnh."""
    uid = request.session.get("user_id")
    if not uid:
        return None
    user = db.get(User, uid)
    if not user or not user.is_active:
        return None
    return user if (user.role == "admin" or user.can_view_all) else None


# ── Logo ─────────────────────────────────────────────────────────────────────

@router.post("/upload-logo")
async def upload_logo(
    request: Request,
    logo: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    if not logo.content_type or not logo.content_type.startswith("image/"):
        request.session["flash"] = "error:File phải là hình ảnh (PNG, JPG, SVG...)"
        return RedirectResponse("/admin/users", status_code=302)

    os.makedirs("static", exist_ok=True)
    content = await logo.read()
    with open("static/logo.png", "wb") as f:
        f.write(content)

    log_activity(db, "upload_logo", f"Admin '{admin.email}' cap nhat logo he thong",
                 user_id=admin.id)
    request.session["flash"] = "Đã cập nhật logo hệ thống thành công"
    return RedirectResponse("/admin/users", status_code=302)


# ── Groups ──────────────────────────────────────────────────────────────────

@router.get("/groups", response_class=HTMLResponse)
def groups_page(request: Request, db: Session = Depends(get_db)):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)
    groups = db.query(Group).order_by(Group.name).all()
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "admin/groups.html", {"user": admin, "groups": groups, "flash": flash})


@router.post("/groups/create")
def create_group(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    folder_path: str = Form(""),
    db: Session = Depends(get_db),
):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    existing = db.query(Group).filter(Group.name == name).first()
    if existing:
        groups = db.query(Group).order_by(Group.name).all()
        return templates.TemplateResponse(
            request, "admin/groups.html",
            {"user": admin, "groups": groups, "error": f"Ten nhom '{name}' da ton tai"},
        )

    group = Group(name=name, description=description, folder_path=folder_path)
    db.add(group)
    db.commit()
    log_activity(db, "create_group", f"Admin tao nhom '{name}'",
                 user_id=admin.id, target_type="group", target_id=group.id)
    request.session["flash"] = f"Da tao nhom '{name}'"
    return RedirectResponse("/admin/groups", status_code=302)


@router.post("/groups/{group_id}/update")
def update_group(
    group_id: int,
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    folder_path: str = Form(""),
    db: Session = Depends(get_db),
):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)
    group = db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404)
    group.name = name
    group.description = description
    group.folder_path = folder_path
    db.commit()
    request.session["flash"] = f"Da cap nhat nhom '{name}'"
    return RedirectResponse("/admin/groups", status_code=302)


# ── Users ────────────────────────────────────────────────────────────────────

@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: Session = Depends(get_db)):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)
    pending = db.query(User).filter(User.is_approved == False).order_by(User.created_at).all()
    users = db.query(User).filter(User.is_approved == True).order_by(User.email).all()
    groups = db.query(Group).order_by(Group.name).all()
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(
        request, "admin/users.html",
        {"user": admin, "users": users, "pending": pending, "groups": groups, "flash": flash}
    )


@router.get("/users/pending-count")
def pending_count(request: Request, db: Session = Depends(get_db)):
    from fastapi.responses import JSONResponse
    admin = _get_admin(request, db)
    if not admin:
        return JSONResponse({"count": 0})
    count = db.query(User).filter(User.is_approved == False).count()
    return JSONResponse({"count": count})


@router.post("/users/{user_id}/approve")
def approve_user(
    user_id: int,
    request: Request,
    group_id: int = Form(None),
    db: Session = Depends(get_db),
):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)
    target = db.get(User, user_id)
    if not target:
        return RedirectResponse("/admin/users", status_code=302)
    target.is_approved = True
    target.group_id = group_id if group_id else None
    db.commit()
    log_activity(db, "toggle_active", f"Admin duyet tai khoan '{target.email}'",
                 user_id=admin.id, target_type="user", target_id=target.id)
    request.session["flash"] = f"Đã duyệt tài khoản '{target.email}'"
    return RedirectResponse("/admin/users", status_code=302)


@router.post("/users/{user_id}/reject")
def reject_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)
    target = db.get(User, user_id)
    if not target or target.is_approved:
        return RedirectResponse("/admin/users", status_code=302)
    email = target.email
    db.delete(target)
    db.commit()
    request.session["flash"] = f"Đã từ chối và xoá tài khoản đăng ký '{email}'"
    return RedirectResponse("/admin/users", status_code=302)


MEMBER_TYPES = ("researcher", "student", "ncs")


@router.post("/users/create")
def create_user(
    request: Request,
    full_name: str = Form(""),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form("member"),
    member_type: str = Form("researcher"),
    can_create_project: str = Form(None),
    group_id: int = Form(None),
    db: Session = Depends(get_db),
):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    email = email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        pending = db.query(User).filter(User.is_approved == False).order_by(User.created_at).all()
        users = db.query(User).filter(User.is_approved == True).order_by(User.email).all()
        groups = db.query(Group).order_by(Group.name).all()
        return templates.TemplateResponse(
            request, "admin/users.html",
            {"user": admin, "users": users, "pending": pending, "groups": groups,
             "error": f"Email '{email}' da ton tai"},
        )

    actual_role = "admin" if role == "admin" else "member"
    can_view = role == "vien_truong"
    actual_member_type = member_type if member_type in MEMBER_TYPES else "researcher"
    new_user = User(
        full_name=full_name,
        email=email,
        password_hash=hash_password(password),
        role=actual_role,
        member_type=actual_member_type,
        can_create_project=bool(can_create_project),
        can_view_all=can_view,
        group_id=group_id if group_id else None,
        is_approved=True,   # admin tạo thì tự động được duyệt
    )
    db.add(new_user)
    db.commit()
    log_activity(db, "create_user", f"Admin tao tai khoan '{email}' ({role})",
                 user_id=admin.id, target_type="user", target_id=new_user.id)
    request.session["flash"] = f"Da tao tai khoan '{email}'"
    return RedirectResponse("/admin/users", status_code=302)


@router.post("/users/{user_id}/update")
def update_user(
    user_id: int,
    request: Request,
    full_name: str = Form(""),
    email: str = Form(""),
    role: str = Form("member"),
    member_type: str = Form("researcher"),
    can_create_project: str = Form(None),
    group_id: int = Form(None),
    new_password: str = Form(""),
    db: Session = Depends(get_db),
):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404)

    email_norm = email.strip().lower()
    if email_norm and email_norm != target.email and db.query(User).filter(User.email == email_norm).first():
        request.session["flash"] = f"error:Email '{email_norm}' da duoc dung boi tai khoan khac."
        return RedirectResponse("/admin/users", status_code=302)

    actual_role = "admin" if role == "admin" else "member"
    can_view = role == "vien_truong"
    target.full_name = full_name
    if email_norm:
        target.email = email_norm
    target.role = actual_role
    target.member_type = member_type if member_type in MEMBER_TYPES else "researcher"
    target.can_create_project = bool(can_create_project)
    target.can_view_all = can_view
    target.group_id = group_id if group_id else None
    if new_password:
        target.password_hash = hash_password(new_password)
    db.commit()
    request.session["flash"] = f"Da cap nhat tai khoan '{target.email}'"
    return RedirectResponse("/admin/users", status_code=302)


@router.post("/users/{user_id}/toggle-admin")
def toggle_admin(user_id: int, request: Request, db: Session = Depends(get_db)):
    """Cấp hoặc thu quyền Admin nhanh — không cho tự thu quyền của chính mình."""
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404)

    if target.id == admin.id:
        request.session["flash"] = "error:Khong the tu thu quyen Admin cua chinh minh!"
        return RedirectResponse("/admin/users", status_code=302)

    if target.role == "admin":
        target.role = "member"
        msg = f"Da thu quyen Admin cua '{target.email}'"
    else:
        target.role = "admin"
        msg = f"Da cap quyen Admin cho '{target.email}'"

    db.commit()
    log_activity(db, "toggle_admin", msg,
                 user_id=admin.id, target_type="user", target_id=target.id)
    request.session["flash"] = msg
    return RedirectResponse("/admin/users", status_code=302)


@router.post("/users/{user_id}/toggle-active")
def toggle_active(user_id: int, request: Request, db: Session = Depends(get_db)):
    """Kích hoạt hoặc vô hiệu hoá tài khoản — không cho tự vô hiệu chính mình."""
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404)

    if target.id == admin.id:
        request.session["flash"] = "error:Khong the vo hieu tai khoan cua chinh minh!"
        return RedirectResponse("/admin/users", status_code=302)

    target.is_active = not target.is_active
    action = "kich hoat" if target.is_active else "vo hieu hoa"
    db.commit()
    msg = f"Da {action} tai khoan '{target.email}'"
    log_activity(db, "toggle_active", msg,
                 user_id=admin.id, target_type="user", target_id=target.id)
    request.session["flash"] = msg
    return RedirectResponse("/admin/users", status_code=302)


# ── Yêu cầu đặt lại mật khẩu ──────────────────────────────────────────────────

@router.get("/password-resets", response_class=HTMLResponse)
def password_resets_page(request: Request, db: Session = Depends(get_db)):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    pending = (db.query(PasswordResetRequest)
                 .filter(PasswordResetRequest.status == "pending")
                 .order_by(PasswordResetRequest.requested_at)
                 .all())
    resolved = (db.query(PasswordResetRequest)
                  .filter(PasswordResetRequest.status != "pending")
                  .order_by(PasswordResetRequest.resolved_at.desc())
                  .limit(30).all())
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "admin/password_resets.html", {
        "user": admin, "flash": flash, "pending": pending, "resolved": resolved,
    })


@router.get("/password-resets/pending-count")
def password_resets_pending_count(request: Request, db: Session = Depends(get_db)):
    from fastapi.responses import JSONResponse
    admin = _get_admin(request, db)
    if not admin:
        return JSONResponse({"count": 0})
    count = db.query(PasswordResetRequest).filter(PasswordResetRequest.status == "pending").count()
    return JSONResponse({"count": count})


@router.post("/password-resets/{req_id}/approve")
def password_reset_approve(req_id: int, request: Request, db: Session = Depends(get_db)):
    from datetime import datetime as _dt
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    reset_req = db.get(PasswordResetRequest, req_id)
    if not reset_req or reset_req.status != "pending":
        request.session["flash"] = "error:Yeu cau khong ton tai hoac da duoc xu ly."
        return RedirectResponse("/admin/password-resets", status_code=302)

    target = db.get(User, reset_req.user_id)
    if not target:
        request.session["flash"] = "error:Tai khoan khong con ton tai."
        return RedirectResponse("/admin/password-resets", status_code=302)

    temp_password = generate_temp_password()
    target.password_hash = hash_password(temp_password)

    reset_req.status = "approved"
    reset_req.resolved_at = _dt.utcnow()
    reset_req.resolved_by = admin.id
    db.commit()

    log_activity(db, "approve_password_reset",
                 f"Admin duyet reset mat khau cho '{target.email}'",
                 user_id=admin.id, target_type="user", target_id=target.id)

    request.session["flash"] = (
        f"Da dat mat khau tam cho '{target.email}': {temp_password} "
        f"— hay bao cho ho qua kenh khac (Zalo/gap truc tiep) va yeu cau doi ngay sau khi dang nhap."
    )
    return RedirectResponse("/admin/password-resets", status_code=302)


@router.post("/password-resets/{req_id}/reject")
def password_reset_reject(req_id: int, request: Request, db: Session = Depends(get_db)):
    from datetime import datetime as _dt
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    reset_req = db.get(PasswordResetRequest, req_id)
    if not reset_req or reset_req.status != "pending":
        return RedirectResponse("/admin/password-resets", status_code=302)

    reset_req.status = "rejected"
    reset_req.resolved_at = _dt.utcnow()
    reset_req.resolved_by = admin.id
    db.commit()

    request.session["flash"] = "Da tu choi yeu cau dat lai mat khau."
    return RedirectResponse("/admin/password-resets", status_code=302)


# ── Mời người dùng ────────────────────────────────────────────────────────────
# Dành cho người sẽ không tự đăng ký (VD: Viện trưởng) — admin tạo lời mời, copy link
# gửi qua kênh khác (Zalo/tin nhắn) vì hệ thống chưa gửi email tự động. Người được mời
# mở link tự đặt mật khẩu riêng, tài khoản kích hoạt ngay không cần duyệt thêm.

INVITE_EXPIRE_DAYS = 7


@router.get("/invites", response_class=HTMLResponse)
def invites_page(request: Request, db: Session = Depends(get_db)):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    pending = (db.query(UserInvite)
                 .filter(UserInvite.status == "pending")
                 .order_by(UserInvite.created_at.desc())
                 .all())
    resolved = (db.query(UserInvite)
                  .filter(UserInvite.status != "pending")
                  .order_by(UserInvite.created_at.desc())
                  .limit(30).all())
    groups = db.query(Group).order_by(Group.name).all()
    flash = request.session.pop("flash", None)
    from datetime import datetime as _dt
    return templates.TemplateResponse(request, "admin/invites.html", {
        "user": admin, "flash": flash,
        "pending": pending, "resolved": resolved, "groups": groups,
        "now": _dt.utcnow(),
    })


@router.post("/invites/create")
def invite_create(
    request: Request,
    email: str = Form(...),
    full_name: str = Form(""),
    role: str = Form("member"),
    member_type: str = Form("researcher"),
    can_create_project: str = Form(None),
    group_id: int = Form(None),
    db: Session = Depends(get_db),
):
    from datetime import datetime as _dt, timedelta as _td

    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    email_norm = email.strip().lower()
    if db.query(User).filter(User.email == email_norm).first():
        request.session["flash"] = f"error:Email '{email_norm}' da la tai khoan trong he thong."
        return RedirectResponse("/admin/invites", status_code=302)

    existing = (db.query(UserInvite)
                  .filter(UserInvite.email == email_norm, UserInvite.status == "pending")
                  .first())
    if existing:
        request.session["flash"] = f"error:Da co loi moi dang cho '{email_norm}' xac nhan — huy loi moi cu truoc khi gui lai."
        return RedirectResponse("/admin/invites", status_code=302)

    actual_role = "admin" if role == "admin" else "member"
    can_view = role == "vien_truong"
    invite = UserInvite(
        token=generate_invite_token(),
        email=email_norm,
        full_name=full_name.strip(),
        role=actual_role,
        can_view_all=can_view,
        member_type=member_type if member_type in MEMBER_TYPES else "researcher",
        can_create_project=bool(can_create_project),
        group_id=group_id if group_id else None,
        created_by=admin.id,
        expires_at=_dt.utcnow() + _td(days=INVITE_EXPIRE_DAYS),
    )
    db.add(invite)
    db.commit()
    log_activity(db, "create_invite", f"Admin tao loi moi cho '{email_norm}'", user_id=admin.id)

    invite_url = str(request.base_url).rstrip("/") + f"/invite/{invite.token}"
    request.session["flash"] = (
        f"Da tao loi moi cho '{email_norm}'. Copy link ben duoi va gui qua kenh khac (Zalo/tin nhan): {invite_url}"
    )
    return RedirectResponse("/admin/invites", status_code=302)


@router.post("/invites/{invite_id}/revoke")
def invite_revoke(invite_id: int, request: Request, db: Session = Depends(get_db)):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    invite = db.get(UserInvite, invite_id)
    if invite and invite.status == "pending":
        invite.status = "revoked"
        db.commit()
        request.session["flash"] = f"Da huy loi moi '{invite.email}'."
    return RedirectResponse("/admin/invites", status_code=302)


# ── Folder Scan ──────────────────────────────────────────────────────────────

def _scan_folder_background(group_id: int, folder_path: str, uploader_id: int):
    db = SessionLocal()
    try:
        for fname in os.listdir(folder_path):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in ALLOWED_EXT:
                continue
            full_path = os.path.join(folder_path, fname)
            exists = db.query(Video).filter(Video.filename == full_path).first()
            if exists:
                continue
            video = Video(
                group_id=group_id,
                uploaded_by=uploader_id,
                filename=full_path,
                original_name=fname,
                file_size=os.path.getsize(full_path),
                status="pending",
                source="folder",
            )
            db.add(video)
            db.commit()
            db.refresh(video)
            threading.Thread(target=_analyze_in_background, args=(video.id,), daemon=True).start()
    except Exception as e:
        logger.exception("Loi quet thu muc group_id=%s folder=%s", group_id, folder_path)
        try:
            log_activity(db, "scan_failed",
                         f"Loi quet thu muc '{folder_path}': {str(e)[:300]}",
                         user_id=uploader_id, target_type="group", target_id=group_id,
                         group_id=group_id)
        except Exception:
            pass
    finally:
        db.close()


@router.post("/groups/{group_id}/scan")
def scan_folder(group_id: int, request: Request, db: Session = Depends(get_db)):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    group = db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404)
    if not group.folder_path or not os.path.isdir(group.folder_path):
        request.session["flash"] = f"Thu muc '{group.folder_path}' khong ton tai hoac chua duoc cau hinh"
        return RedirectResponse("/admin/groups", status_code=302)

    threading.Thread(
        target=_scan_folder_background,
        args=(group.id, group.folder_path, admin.id),
        daemon=True,
    ).start()

    request.session["flash"] = f"Da bat dau quet thu muc '{group.folder_path}'. Qua trinh xu ly chay nen."
    return RedirectResponse("/admin/groups", status_code=302)


@router.get("", response_class=HTMLResponse)
def admin_home(request: Request, db: Session = Depends(get_db)):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/admin/stats", status_code=302)


# ── Stats Dashboard ───────────────────────────────────────────────────────────

@router.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request, db: Session = Depends(get_db)):
    from sqlalchemy import func
    from app.models import ExperimentLog, Comment, ActivityLog

    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    # System-wide counts
    total_users   = db.query(func.count(User.id)).scalar()
    total_groups  = db.query(func.count(Group.id)).scalar()
    total_videos  = db.query(func.count(Video.id)).scalar()
    total_logs    = db.query(func.count(ExperimentLog.id)).scalar()
    total_comments= db.query(func.count(Comment.id)).scalar()

    # Video by status
    status_counts = dict(
        db.query(Video.status, func.count(Video.id))
          .group_by(Video.status).all()
    )

    # Total storage (bytes)
    total_storage = db.query(func.sum(Video.file_size)).scalar() or 0

    # Per-group stats
    groups = db.query(Group).order_by(Group.name).all()
    group_stats = []
    for g in groups:
        v_count  = db.query(func.count(Video.id)).filter(Video.group_id == g.id).scalar()
        l_count  = (db.query(func.count(ExperimentLog.id))
                      .join(Video).filter(Video.group_id == g.id).scalar())
        storage  = (db.query(func.sum(Video.file_size))
                      .filter(Video.group_id == g.id).scalar() or 0)
        members  = db.query(func.count(User.id)).filter(User.group_id == g.id).scalar()
        group_stats.append({
            "group": g, "videos": v_count, "logs": l_count,
            "storage_mb": storage / 1048576, "members": members,
        })

    # Recent activity (last 15)
    recent = (db.query(ActivityLog)
                .order_by(ActivityLog.created_at.desc())
                .limit(15).all())

    from app.activity import ACTION_META
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "admin/stats.html", {
        "user": admin, "flash": flash,
        "total_users": total_users, "total_groups": total_groups,
        "total_videos": total_videos, "total_logs": total_logs,
        "total_comments": total_comments,
        "status_counts": status_counts,
        "total_storage_mb": total_storage / 1048576,
        "group_stats": group_stats,
        "recent": recent,
        "action_meta": ACTION_META,
    })


# ── Activity Log ──────────────────────────────────────────────────────────────

@router.get("/activity", response_class=HTMLResponse)
def activity_page(
    request: Request,
    db: Session = Depends(get_db),
    action: str = "",
    group_id: int = None,
    email: str = "",
    limit: int = 100,
):
    from app.models import ActivityLog

    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    q = db.query(ActivityLog).order_by(ActivityLog.created_at.desc())
    if action:
        q = q.filter(ActivityLog.action == action)
    if group_id:
        q = q.filter(ActivityLog.group_id == group_id)
    if email:
        u = db.query(User).filter(User.email.ilike(f"%{email}%")).first()
        if u:
            q = q.filter(ActivityLog.user_id == u.id)
        else:
            q = q.filter(ActivityLog.user_id == -1)  # no match

    logs = q.limit(limit).all()
    groups = db.query(Group).order_by(Group.name).all()

    # Load user info for each log
    user_cache = {}
    for entry in logs:
        if entry.user_id and entry.user_id not in user_cache:
            user_cache[entry.user_id] = db.get(User, entry.user_id)

    from app.activity import ACTION_META
    return templates.TemplateResponse(request, "admin/activity.html", {
        "user": admin, "logs": logs, "user_cache": user_cache,
        "action_meta": ACTION_META, "groups": groups,
        "filter_action": action, "filter_group": group_id,
        "filter_email": email, "limit": limit,
    })


# ── Issues (failed/stuck videos) ─────────────────────────────────────────────

@router.get("/issues", response_class=HTMLResponse)
def issues_page(request: Request, db: Session = Depends(get_db)):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    failed  = db.query(Video).filter(Video.status == "failed").order_by(Video.uploaded_at.desc()).all()
    pending = db.query(Video).filter(Video.status == "pending").order_by(Video.uploaded_at.desc()).all()
    processing = db.query(Video).filter(Video.status == "processing").order_by(Video.uploaded_at.desc()).all()

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "admin/issues.html", {
        "user": admin, "flash": flash,
        "failed": failed, "pending": pending, "processing": processing,
    })


# ── Reports (monthly/yearly) ─────────────────────────────────────────────────

MONTH_VI = ["", "Tháng 1", "Tháng 2", "Tháng 3", "Tháng 4", "Tháng 5", "Tháng 6",
            "Tháng 7", "Tháng 8", "Tháng 9", "Tháng 10", "Tháng 11", "Tháng 12"]


@router.post("/users/{user_id}/toggle-view-all")
def toggle_view_all(user_id: int, request: Request, db: Session = Depends(get_db)):
    """Cấp/thu quyền xem báo cáo toàn cảnh cho người không phải admin."""
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404)

    if target.role == "admin":
        request.session["flash"] = "error:Admin da co quyen xem toan canh mac dinh!"
        return RedirectResponse("/admin/users", status_code=302)

    target.can_view_all = not target.can_view_all
    action = "cap" if target.can_view_all else "thu"
    db.commit()
    msg = f"Da {action} quyen xem bao cao toan canh cho '{target.email}'"
    log_activity(db, "toggle_admin", msg, user_id=admin.id, target_type="user", target_id=target.id)
    request.session["flash"] = msg
    return RedirectResponse("/admin/users", status_code=302)


@router.post("/users/{user_id}/toggle-create-project")
def toggle_create_project(user_id: int, request: Request, db: Session = Depends(get_db)):
    """Cấp/thu quyền tự tạo project — dùng cho Sinh viên/NCS được Viện cho phép
    hoạt động tự do như NCV (tạo và quản lý project nghiên cứu lớn của riêng họ)."""
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404)

    target.can_create_project = not target.can_create_project
    action = "cap" if target.can_create_project else "thu"
    db.commit()
    msg = f"Da {action} quyen tu tao project cho '{target.email}'"
    log_activity(db, "toggle_admin", msg, user_id=admin.id, target_type="user", target_id=target.id)
    request.session["flash"] = msg
    return RedirectResponse("/admin/users", status_code=302)


@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    db: Session = Depends(get_db),
    year: int = None,
    group_id: int = None,
):
    from datetime import datetime as _dt
    admin = _get_report_viewer(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    now = _dt.utcnow()
    if not year:
        year = now.year

    # Year range: earliest report_year in DB → current year
    from sqlalchemy import func
    min_year = db.query(func.min(Video.report_year)).scalar() or now.year
    year_range = list(range(min(min_year, now.year - 1), now.year + 2))

    # Users to show (filter by group or all)
    groups = db.query(Group).order_by(Group.name).all()
    if group_id:
        users = (db.query(User)
                   .filter(User.group_id == group_id, User.is_active == True)
                   .order_by(User.full_name, User.email).all())
    else:
        users = (db.query(User)
                   .filter(User.is_active == True)
                   .order_by(User.group_id, User.full_name, User.email).all())

    # All videos for selected year (+ group filter)
    q = db.query(Video).filter(Video.report_year == year)
    if group_id:
        q = q.filter(Video.group_id == group_id)
    videos = q.all()

    # Build matrix: {user_id: {month: [video, ...]}}
    matrix = {u.id: {m: [] for m in range(1, 13)} for u in users}
    for v in videos:
        if v.uploaded_by in matrix and v.report_month and 1 <= v.report_month <= 12:
            matrix[v.uploaded_by][v.report_month].append(v)

    # Count totals per month (for summary row)
    month_totals = {}
    for m in range(1, 13):
        month_totals[m] = sum(len(matrix[u.id][m]) for u in users)

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "admin/reports.html", {
        "user": admin, "flash": flash,
        "year": year, "year_range": year_range,
        "groups": groups, "selected_group": group_id,
        "users": users, "matrix": matrix,
        "month_totals": month_totals,
        "months": MONTH_VI,
        "current_month": now.month, "current_year": now.year,
    })


@router.get("/reports/month", response_class=HTMLResponse)
def reports_month_detail(
    request: Request,
    db: Session = Depends(get_db),
    year: int = None,
    month: int = None,
    group_id: int = None,
):
    from datetime import datetime as _dt
    admin = _get_report_viewer(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    now = _dt.utcnow()
    year = year or now.year
    month = month or now.month

    groups = db.query(Group).order_by(Group.name).all()

    # All videos for this month/year
    q = db.query(Video).filter(Video.report_year == year, Video.report_month == month)
    if group_id:
        q = q.filter(Video.group_id == group_id)
    videos = q.order_by(Video.uploaded_at.desc()).all()

    # Users who should report (active members in groups)
    if group_id:
        all_users = (db.query(User)
                       .filter(User.group_id == group_id, User.is_active == True)
                       .order_by(User.group_id, User.email).all())
    else:
        all_users = (db.query(User)
                       .filter(User.is_active == True)
                       .order_by(User.group_id, User.email).all())

    submitted_user_ids = {v.uploaded_by for v in videos}

    # Group videos by uploader
    by_user = {}
    for v in videos:
        by_user.setdefault(v.uploaded_by, []).append(v)

    submitted_users = [u for u in all_users if u.id in submitted_user_ids]
    missing_users   = [u for u in all_users if u.id not in submitted_user_ids]

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "admin/reports_month.html", {
        "user": admin, "flash": flash,
        "year": year, "month": month,
        "month_name": MONTH_VI[month] if 1 <= month <= 12 else str(month),
        "groups": groups, "selected_group": group_id,
        "all_users": all_users, "submitted_users": submitted_users,
        "missing_users": missing_users,
        "submitted_user_ids": submitted_user_ids,
        "by_user": by_user, "videos": videos,
    })


# ── AI Settings ─────────────────────────────────────────────────────────────

GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
]

AI_CONFIG_DEFAULTS = {
    "results_ai_model":          "gemini-2.5-flash",
    "results_ai_prompt":         "",  # rỗng = dùng DEFAULT_ANALYSIS_PROMPT
}


def _get_cfg(db: Session, key: str) -> str:
    obj = db.get(SystemConfig, key)
    return obj.value if obj else AI_CONFIG_DEFAULTS.get(key, "")


def _set_cfg(db: Session, key: str, value: str, user_id: int = None):
    from datetime import datetime as _dt
    obj = db.get(SystemConfig, key)
    if obj:
        obj.value = value
        obj.updated_at = _dt.utcnow()
        obj.updated_by = user_id
    else:
        obj = SystemConfig(key=key, value=value, updated_by=user_id)
        db.add(obj)


@router.get("/ai-settings", response_class=HTMLResponse)
def ai_settings_page(request: Request, db: Session = Depends(get_db)):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    from app.gemini import DEFAULT_ANALYSIS_PROMPT
    from app.routers.results import VERDICT_LABEL
    cfg = {k: _get_cfg(db, k) for k in AI_CONFIG_DEFAULTS}
    calibration_examples = (
        db.query(AICalibrationExample)
          .order_by(AICalibrationExample.is_active.desc(), AICalibrationExample.created_at.desc())
          .all()
    )

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "admin/ai_settings.html", {
        "user": admin, "flash": flash,
        "cfg": cfg,
        "default_prompt": DEFAULT_ANALYSIS_PROMPT,
        "gemini_models": GEMINI_MODELS,
        "calibration_examples": calibration_examples,
        "verdict_label": VERDICT_LABEL,
    })


@router.post("/ai-settings")
async def ai_settings_save(request: Request, db: Session = Depends(get_db)):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    form = await request.form()

    simple_keys = [
        "results_ai_model",
        "results_ai_prompt",
    ]
    for key in simple_keys:
        _set_cfg(db, key, (form.get(key) or "").strip(), user_id=admin.id)

    db.commit()

    request.session["flash"] = "Đã lưu cài đặt AI"
    return RedirectResponse("/admin/ai-settings", status_code=302)


@router.post("/ai-settings/reset-prompt")
def ai_settings_reset_prompt(request: Request, db: Session = Depends(get_db)):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)
    _set_cfg(db, "results_ai_prompt", "", user_id=admin.id)
    db.commit()
    request.session["flash"] = "Đã khôi phục prompt về mặc định"
    return RedirectResponse("/admin/ai-settings", status_code=302)


@router.post("/ai-settings/calibration/add")
def calibration_add(
    request: Request,
    context_excerpt: str = Form(...),
    correct_verdict: str = Form(...),
    reason: str = Form(...),
    ai_verdict: str = Form(""),
    db: Session = Depends(get_db),
):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    ex = AICalibrationExample(
        source="manual",
        context_excerpt=context_excerpt.strip(),
        correct_verdict=correct_verdict,
        reason=reason.strip(),
        ai_verdict=ai_verdict.strip(),
        is_active=True,
        created_by=admin.id,
    )
    db.add(ex)
    db.commit()
    request.session["flash"] = "Đã thêm ví dụ hiệu chỉnh thủ công"
    return RedirectResponse("/admin/ai-settings#tab-calibration", status_code=302)


@router.post("/ai-settings/calibration/{ex_id}/toggle")
def calibration_toggle(ex_id: int, request: Request, db: Session = Depends(get_db)):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    ex = db.get(AICalibrationExample, ex_id)
    if ex:
        ex.is_active = not ex.is_active
        db.commit()
    return RedirectResponse("/admin/ai-settings#tab-calibration", status_code=302)


@router.post("/ai-settings/calibration/{ex_id}/delete")
def calibration_delete(ex_id: int, request: Request, db: Session = Depends(get_db)):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    ex = db.get(AICalibrationExample, ex_id)
    if ex:
        db.delete(ex)
        db.commit()
    request.session["flash"] = "Đã xoá ví dụ hiệu chỉnh"
    return RedirectResponse("/admin/ai-settings#tab-calibration", status_code=302)


# ── Nhật ký thí nghiệm ───────────────────────────────────────────────────────

DIARY_CONFIG_DEFAULTS = {
    "daily_log_lock_days": "7",
}


@router.get("/diary-settings", response_class=HTMLResponse)
def diary_settings_page(request: Request, db: Session = Depends(get_db)):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    cfg = {}
    for k, default in DIARY_CONFIG_DEFAULTS.items():
        obj = db.get(SystemConfig, k)
        cfg[k] = obj.value if obj and obj.value else default

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "admin/diary_settings.html", {
        "user": admin, "flash": flash, "cfg": cfg,
    })


@router.post("/diary-settings")
async def diary_settings_save(request: Request, db: Session = Depends(get_db)):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    form = await request.form()
    raw = (form.get("daily_log_lock_days") or "").strip()
    try:
        days = max(0, int(raw))
    except ValueError:
        days = int(DIARY_CONFIG_DEFAULTS["daily_log_lock_days"])

    _set_cfg(db, "daily_log_lock_days", str(days), user_id=admin.id)
    db.commit()

    request.session["flash"] = "Đã lưu cài đặt nhật ký thí nghiệm"
    return RedirectResponse("/admin/diary-settings", status_code=302)


@router.post("/issues/retry-all")
def retry_all_failed(request: Request, db: Session = Depends(get_db)):
    """Retry tất cả video failed."""
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    failed_videos = db.query(Video).filter(Video.status == "failed").all()
    count = 0
    for video in failed_videos:
        if video.log:
            db.delete(video.log)
        video.status = "pending"
        video.error_message = ""
        count += 1
    db.commit()

    for video in failed_videos:
        threading.Thread(target=_analyze_in_background, args=(video.id,), daemon=True).start()

    log_activity(db, "reanalyze",
                 f"Admin retry {count} video loi",
                 user_id=admin.id)
    request.session["flash"] = f"Da phat lai {count} video bi loi"
    return RedirectResponse("/admin/issues", status_code=302)


# ── Report Periods (mở/đóng kỳ nộp báo cáo) ────────────────────────────────

_MONTH_VI_SHORT = ["", "T1","T2","T3","T4","T5","T6","T7","T8","T9","T10","T11","T12"]


@router.get("/report-periods", response_class=HTMLResponse)
def report_periods_page(request: Request, db: Session = Depends(get_db)):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    from datetime import datetime as _dt
    now = _dt.utcnow()
    periods = (db.query(ReportPeriod)
                 .order_by(ReportPeriod.report_year.desc(), ReportPeriod.report_month.desc())
                 .all())

    # Đếm số báo cáo đã nộp cho mỗi kỳ
    from app.models import MonthlyReport as MR
    counts = {}
    for p in periods:
        counts[p.id] = (db.query(MR)
                          .filter(MR.report_month == p.report_month,
                                  MR.report_year  == p.report_year,
                                  MR.status.in_(["submitted","reviewed"]))
                          .count())

    total_users = db.query(User).filter(User.is_active == True, User.group_id != None).count()
    flash = request.session.pop("flash", None)
    year_range = list(range(now.year - 1, now.year + 2))

    return templates.TemplateResponse(request, "admin/report_periods.html", {
        "user": admin, "flash": flash,
        "periods": periods, "counts": counts,
        "total_users": total_users,
        "cur_month": now.month, "cur_year": now.year,
        "year_range": year_range,
        "month_names": MONTH_VI,
        "now": now,
    })


@router.post("/report-periods/open")
def open_period(
    request: Request,
    db: Session = Depends(get_db),
    report_month: int = Form(...),
    report_year:  int = Form(...),
    deadline_str: str = Form(""),
):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    if not 1 <= report_month <= 12:
        request.session["flash"] = "error:Tháng không hợp lệ."
        return RedirectResponse("/admin/report-periods", status_code=302)

    from datetime import datetime as _dt
    deadline = None
    if deadline_str:
        try:
            deadline = _dt.fromisoformat(deadline_str)
        except Exception:
            pass

    existing = (db.query(ReportPeriod)
                  .filter(ReportPeriod.report_month == report_month,
                          ReportPeriod.report_year  == report_year)
                  .first())
    if existing:
        existing.is_open    = True
        existing.deadline   = deadline
        existing.closed_at  = None
        existing.closed_by  = None
        existing.auto_closed = False
        msg = f"Đã mở lại kỳ nộp báo cáo {MONTH_VI[report_month]}/{report_year}"
    else:
        period = ReportPeriod(
            report_month=report_month,
            report_year=report_year,
            deadline=deadline,
            is_open=True,
            created_by=admin.id,
        )
        db.add(period)
        msg = f"Đã mở kỳ nộp báo cáo {MONTH_VI[report_month]}/{report_year}"

    db.commit()
    request.session["flash"] = msg
    return RedirectResponse("/admin/report-periods", status_code=302)


@router.post("/report-periods/{period_id}/close")
def close_period(period_id: int, request: Request, db: Session = Depends(get_db)):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    from datetime import datetime as _dt
    period = db.get(ReportPeriod, period_id)
    if not period:
        raise HTTPException(status_code=404)

    now = _dt.utcnow()
    period.is_open    = False
    period.closed_at  = now
    period.closed_by  = admin.id
    period.auto_closed = True

    # Lấy tất cả NCV đang hoạt động (có nhóm)
    all_users = db.query(User).filter(User.is_active == True, User.group_id != None).all()
    # Báo cáo đã có sẵn cho kỳ này, kể cả bản nháp (draft) — không được coi "có bản nháp" là
    # "đã nộp": nếu không, NCV có thể lưu 1 bản nháp rỗng rồi không bao giờ nộp chính thức để
    # né cả nhắc nhở lẫn tự động phạt ở đây.
    existing_by_user = {
        r.user_id: r for r in db.query(MonthlyReport)
            .filter(MonthlyReport.report_month == period.report_month,
                    MonthlyReport.report_year  == period.report_year)
            .all()
    }

    auto_count = 0
    for u in all_users:
        existing = existing_by_user.get(u.id)
        if existing is not None and existing.status in ("submitted", "reviewed"):
            continue  # đã nộp thật sự, không đụng vào

        if existing is not None:
            # Đang ở dạng nháp (draft) chưa từng nộp — chuyển thẳng bản ghi có sẵn thành tự động
            # "Hoãn trả lương" thay vì tạo dòng mới (tạo mới sẽ vi phạm UNIQUE user+tháng+năm).
            rpt = existing
        else:
            rpt = MonthlyReport(
                user_id=u.id,
                group_id=u.group_id,
                report_month=period.report_month,
                report_year=period.report_year,
            )
            db.add(rpt)

        rpt.content = rpt.content or ""
        rpt.status = "reviewed"
        rpt.submitted_at = now
        rpt.updated_at = now
        rpt.ai_status = "done"
        rpt.ai_verdict = "salary_defer"
        rpt.ai_scores_json = "[]"
        rpt.manager_decision = "salary_defer"
        rpt.manager_note = "Tự động: NCV chưa nộp báo cáo trong thời hạn quy định."
        rpt.reviewed_by = admin.id
        rpt.reviewed_at = now
        auto_count += 1

    db.commit()
    request.session["flash"] = (
        f"Đã đóng kỳ {MONTH_VI[period.report_month]}/{period.report_year}. "
        f"Tự động xếp loại 'Hoãn trả lương' cho {auto_count} NCV chưa nộp."
    )
    return RedirectResponse("/admin/report-periods", status_code=302)


@router.post("/report-periods/{period_id}/reopen")
def reopen_period(period_id: int, request: Request, db: Session = Depends(get_db)):
    admin = _get_admin(request, db)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    period = db.get(ReportPeriod, period_id)
    if not period:
        raise HTTPException(status_code=404)

    period.is_open    = True
    period.closed_at  = None
    period.closed_by  = None
    period.auto_closed = False
    db.commit()
    request.session["flash"] = f"Đã mở lại kỳ {MONTH_VI[period.report_month]}/{period.report_year}"
    return RedirectResponse("/admin/report-periods", status_code=302)
