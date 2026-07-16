import re
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, PasswordResetRequest, UserInvite
from app.auth import verify_password, hash_password
from app.activity import log_activity
from app.security import (
    is_login_locked, record_failed_login, clear_failed_logins,
    is_reg_limited, record_reg_attempt,
    is_status_check_limited, record_status_check,
    is_reset_limited, record_reset_attempt,
    check_password,
)

router = APIRouter()
from app.templating import templates

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _client_ip(request: Request) -> str:
    # KHÔNG dùng X-Forwarded-For: app chạy trực tiếp (không qua reverse proxy — xem
    # DEPLOY.md), nên header này do chính client tự gửi và có thể giả mạo tuỳ ý để
    # né khoá đăng nhập/giới hạn đăng ký theo IP.
    return request.client.host or "unknown"


# ── Đăng nhập ─────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=302)
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "login.html", {"error": None, "flash": flash})


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    ip = _client_ip(request)

    # Kiểm tra bị khoá do đăng nhập sai nhiều lần
    locked, remaining = is_login_locked(ip)
    if locked:
        mins = remaining // 60
        secs = remaining % 60
        return templates.TemplateResponse(request, "login.html", {
            "error": f"Quá nhiều lần đăng nhập sai. Vui lòng thử lại sau {mins} phút {secs} giây.",
        }, status_code=429)

    user = db.query(User).filter(User.email == email.strip().lower()).first()

    if not user or not verify_password(password, user.password_hash):
        record_failed_login(ip)
        locked2, _ = is_login_locked(ip)
        extra = " Tài khoản tạm thời bị khoá." if locked2 else ""
        return templates.TemplateResponse(request, "login.html", {
            "error": f"Email hoặc mật khẩu không đúng.{extra}",
        }, status_code=401)

    if not user.is_approved:
        return templates.TemplateResponse(request, "login.html", {
            "error": "Tài khoản đang chờ quản trị viên duyệt. Vui lòng chờ thông báo.",
            "show_check_status": True,
        }, status_code=403)

    if not user.is_active:
        return templates.TemplateResponse(request, "login.html", {
            "error": "Tài khoản đã bị vô hiệu hoá. Liên hệ quản trị viên.",
        }, status_code=403)

    clear_failed_logins(ip)
    request.session["user_id"] = user.id
    log_activity(db, "login", f"{user.email} dang nhap",
                 user_id=user.id, group_id=user.group_id)
    return RedirectResponse("/", status_code=302)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# ── Quên mật khẩu ─────────────────────────────────────────────────────────────
# Hệ thống chưa cấu hình gửi email (không SMTP) nên đây là luồng "yêu cầu admin duyệt":
# người dùng gửi yêu cầu, admin vào /admin/password-resets duyệt và tạo mật khẩu tạm,
# rồi báo cho người dùng qua kênh khác (Zalo/gặp trực tiếp).

@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "forgot_password.html", {})


@router.post("/forgot-password")
def forgot_password_submit(
    request: Request,
    email: str = Form(""),
    db: Session = Depends(get_db),
):
    ip = _client_ip(request)

    if is_reset_limited(ip):
        return templates.TemplateResponse(request, "forgot_password.html", {
            "error": "Quá nhiều yêu cầu từ thiết bị này. Vui lòng thử lại sau.",
        }, status_code=429)
    record_reset_attempt(ip)

    # Thông báo chung chung dù email có tồn tại hay không — tránh lộ email nào đã đăng ký.
    email_norm = email.strip().lower()
    if email_norm:
        user = db.query(User).filter(User.email == email_norm).first()
        if user and user.is_active:
            existing = (db.query(PasswordResetRequest)
                          .filter(PasswordResetRequest.user_id == user.id,
                                  PasswordResetRequest.status == "pending")
                          .first())
            if not existing:
                db.add(PasswordResetRequest(user_id=user.id))
                log_activity(db, "request_password_reset",
                             f"'{user.email}' yeu cau dat lai mat khau",
                             user_id=user.id)
                db.commit()

    request.session["flash"] = (
        "Nếu email tồn tại trong hệ thống, yêu cầu đặt lại mật khẩu đã được gửi tới "
        "quản trị viên. Vui lòng liên hệ Viện để nhận mật khẩu tạm thời."
    )
    return RedirectResponse("/login", status_code=302)


# ── Chấp nhận lời mời (admin mời trực tiếp, không cần đăng ký/duyệt) ──────────

def _invite_status_error(invite: UserInvite | None) -> str | None:
    from datetime import datetime as _dt
    if not invite:
        return "Lời mời không tồn tại hoặc đường link không đúng."
    if invite.status == "accepted":
        return "Lời mời này đã được sử dụng."
    if invite.status == "revoked":
        return "Lời mời này đã bị huỷ."
    if invite.expires_at and invite.expires_at < _dt.utcnow():
        return "Lời mời đã hết hạn. Vui lòng liên hệ quản trị viên để được mời lại."
    return None


@router.get("/invite/{token}", response_class=HTMLResponse)
def invite_accept_page(token: str, request: Request, db: Session = Depends(get_db)):
    invite = db.query(UserInvite).filter(UserInvite.token == token).first()
    invite_error = _invite_status_error(invite)
    if invite_error:
        return templates.TemplateResponse(request, "invite_accept.html", {"invite_error": invite_error})
    return templates.TemplateResponse(request, "invite_accept.html", {"invite": invite})


@router.post("/invite/{token}")
def invite_accept_submit(
    token: str,
    request: Request,
    full_name: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    db: Session = Depends(get_db),
):
    from datetime import datetime as _dt

    invite = db.query(UserInvite).filter(UserInvite.token == token).first()
    invite_error = _invite_status_error(invite)
    if invite_error:
        return templates.TemplateResponse(request, "invite_accept.html", {"invite_error": invite_error})

    def err(msg):
        return templates.TemplateResponse(request, "invite_accept.html",
            {"invite": invite, "error": msg, "full_name_val": full_name})

    if not full_name.strip():
        return err("Vui lòng nhập họ tên.")
    pw_err = check_password(password)
    if pw_err:
        return err(pw_err)
    if password != confirm_password:
        return err("Mật khẩu xác nhận không khớp.")
    if db.query(User).filter(User.email == invite.email).first():
        return err("Email này đã có tài khoản — vui lòng đăng nhập.")

    new_user = User(
        full_name=full_name.strip(),
        email=invite.email,
        password_hash=hash_password(password),
        role=invite.role,
        member_type=invite.member_type,
        can_create_project=invite.can_create_project,
        can_view_all=invite.can_view_all,
        group_id=invite.group_id,
        is_active=True,
        is_approved=True,
    )
    db.add(new_user)
    invite.status = "accepted"
    invite.accepted_at = _dt.utcnow()
    db.commit()
    db.refresh(new_user)

    log_activity(db, "accept_invite",
                 f"'{new_user.email}' da chap nhan loi moi va tao tai khoan",
                 user_id=new_user.id)

    request.session["user_id"] = new_user.id
    request.session["flash"] = "Chào mừng! Tài khoản của bạn đã sẵn sàng."
    return RedirectResponse("/", status_code=302)


# ── Đăng ký tài khoản mới ────────────────────────────────────────────────────

@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "register.html", {})


@router.post("/register")
def register(
    request: Request,
    # Form("") thay vì Form(...) cho các trường bắt buộc: một số phiên bản Starlette/
    # python-multipart coi field CÓ GỬI NHƯNG RỖNG là "thiếu field" và trả lỗi 422 JSON
    # thô thay vì render lại register.html — nên validate rỗng thủ công bên dưới thay vì
    # dựa vào Form(...) để đảm bảo luôn hiện đúng thông báo tiếng Việt.
    full_name: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    email: str = Form(""),
    reason: str = Form(""),
    member_type: str = Form("researcher"),
    db: Session = Depends(get_db),
):
    ip = _client_ip(request)
    member_type = member_type if member_type in ("researcher", "student", "ncs") else "researcher"
    form_data = {"full_name": full_name, "email": email, "reason": reason,
                 "member_type": member_type}

    def err(msg):
        return templates.TemplateResponse(request, "register.html",
                                          {"error": msg, "form": form_data})

    # Rate limit
    if is_reg_limited(ip):
        return err("Quá nhiều yêu cầu đăng ký từ thiết bị này. Vui lòng thử lại sau 1 giờ.")

    # Validate
    if not full_name.strip():
        return err("Vui lòng nhập họ tên đầy đủ.")
    if not email.strip():
        return err("Vui lòng nhập email — dùng để đăng nhập và nhận thông báo từ Viện.")
    if not EMAIL_RE.match(email.strip()):
        return err("Địa chỉ email không hợp lệ.")

    pw_err = check_password(password)
    if pw_err:
        return err(pw_err)
    if password != confirm_password:
        return err("Mật khẩu xác nhận không khớp.")
    email_norm = email.strip().lower()
    if db.query(User).filter(User.email == email_norm).first():
        return err(f"Email '{email}' đã được đăng ký. Vui lòng dùng email khác hoặc đăng nhập.")

    new_user = User(
        full_name=full_name.strip(),
        email=email_norm,
        password_hash=hash_password(password),
        role="member",
        member_type=member_type,
        can_create_project=(member_type == "researcher"),  # sinh viên/NCS: admin cấp riêng nếu cần
        is_active=True,
        is_approved=False,
    )
    db.add(new_user)
    db.commit()
    record_reg_attempt(ip)

    note = f" | Ghi chú: {reason.strip()}" if reason.strip() else ""
    log_activity(db, "register",
                 f"NCV '{email_norm}' dang ky tai khoan moi (cho duyet){note}",
                 user_id=new_user.id)

    request.session["flash"] = (
        "Đăng ký thành công! Tài khoản đang chờ quản trị viên duyệt. "
        "Bạn có thể kiểm tra trạng thái bên dưới."
    )
    return RedirectResponse("/login", status_code=302)


# ── Kiểm tra trạng thái đăng ký ──────────────────────────────────────────────

@router.get("/check-status", response_class=HTMLResponse)
def check_status(request: Request, email: str = "", db: Session = Depends(get_db)):
    status = None
    if email:
        ip = _client_ip(request)
        if is_status_check_limited(ip):
            status = ("rate_limited", "Bạn đã kiểm tra quá nhiều lần. Vui lòng thử lại sau ít phút.")
            return templates.TemplateResponse(request, "check_status.html",
                                              {"email": email, "status": status})
        record_status_check(ip)
        user = db.query(User).filter(User.email == email.strip().lower()).first()
        if not user:
            status = ("not_found", "Không tìm thấy tài khoản này.")
        elif not user.is_approved:
            status = ("pending", "Tài khoản đang chờ quản trị viên duyệt.")
        elif not user.is_active:
            status = ("disabled", "Tài khoản đã bị vô hiệu hoá. Liên hệ quản trị viên.")
        else:
            status = ("approved", "Tài khoản đã được duyệt! Bạn có thể đăng nhập ngay.")
    return templates.TemplateResponse(request, "check_status.html",
                                      {"email": email, "status": status})


# ── Hồ sơ cá nhân ────────────────────────────────────────────────────────────

@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/login", status_code=302)
    user = db.get(User, user_id)
    if not user or not user.is_active:
        request.session.clear()
        return RedirectResponse("/login", status_code=302)
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "profile.html", {"user": user, "flash": flash})


@router.post("/profile")
def update_profile(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(""),
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_new_password: str = Form(""),
    db: Session = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/login", status_code=302)
    user = db.get(User, user_id)
    if not user or not user.is_active:
        request.session.clear()
        return RedirectResponse("/login", status_code=302)

    email_norm = email.strip().lower()
    if not email_norm:
        return templates.TemplateResponse(request, "profile.html",
            {"user": user, "error": "Email không được để trống — dùng để đăng nhập."})
    if not EMAIL_RE.match(email_norm):
        return templates.TemplateResponse(request, "profile.html",
            {"user": user, "error": "Địa chỉ email không hợp lệ."})
    if email_norm != user.email and db.query(User).filter(User.email == email_norm).first():
        return templates.TemplateResponse(request, "profile.html",
            {"user": user, "error": f"Email '{email}' đã được dùng bởi tài khoản khác."})

    user.full_name = full_name.strip()
    user.email = email_norm

    if new_password:
        if not current_password:
            return templates.TemplateResponse(request, "profile.html",
                {"user": user, "error": "Vui lòng nhập mật khẩu hiện tại để đổi mật khẩu."})
        if not verify_password(current_password, user.password_hash):
            return templates.TemplateResponse(request, "profile.html",
                {"user": user, "error": "Mật khẩu hiện tại không đúng."})
        pw_err = check_password(new_password)
        if pw_err:
            return templates.TemplateResponse(request, "profile.html",
                {"user": user, "error": pw_err})
        if new_password != confirm_new_password:
            return templates.TemplateResponse(request, "profile.html",
                {"user": user, "error": "Mật khẩu xác nhận không khớp."})
        user.password_hash = hash_password(new_password)

    db.commit()
    request.session["flash"] = "Đã cập nhật hồ sơ thành công."
    return RedirectResponse("/profile", status_code=302)
