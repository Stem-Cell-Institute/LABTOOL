"""Nhật ký thí nghiệm — nghiên cứu viên tự ghi, khoá vĩnh viễn sau N ngày."""
import os
import difflib
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, Group, DailyLog, DailyLogFile, DailyLogRevision, SystemConfig, Project, ProjectMember, Notebook
from app.activity import log_activity

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

DIARY_UPLOAD_DIR = "uploads/diary"

# File "kết quả đã xử lý" — giới hạn theo định dạng xem/preview được trên web
PROCESSED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".tiff", ".bmp",
                 ".pdf", ".xlsx", ".xls", ".csv", ".docx", ".doc", ".txt"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".tiff", ".bmp"}
TEXT_PREVIEW_EXT = {".txt"}  # trình duyệt render inline được text/plain qua iframe

# File "dữ liệu thô" từ máy/thiết bị đo có thể mang bất kỳ đuôi nào (VD .fcs, .czi, .raw, .d...)
# nên KHÔNG giới hạn theo whitelist — chỉ chặn các đuôi thực thi/nguy hiểm.
DANGEROUS_EXT = {".exe", ".bat", ".cmd", ".sh", ".msi", ".dll", ".scr", ".js",
                 ".vbs", ".ps1", ".jar", ".com", ".cpl", ".gadget", ".application", ".hta", ".apk"}

DEFAULT_LOCK_DAYS = 7


def _get_user(request: Request, db: Session):
    uid = request.session.get("user_id")
    return db.get(User, uid) if uid else None


def _can_manage(user: User):
    return user.role == "admin" or user.can_view_all


def _lock_days(db: Session) -> int:
    obj = db.get(SystemConfig, "daily_log_lock_days")
    try:
        return int(obj.value) if obj and obj.value else DEFAULT_LOCK_DAYS
    except (ValueError, TypeError):
        return DEFAULT_LOCK_DAYS


def _is_locked(entry: DailyLog, lock_days: int) -> bool:
    return datetime.utcnow() - entry.created_at >= timedelta(days=lock_days)


def _can_edit(user: User, entry: DailyLog) -> bool:
    """Chủ nhật ký hoặc admin/quản lý đều được sửa — miễn là chưa khoá.
    (Thành viên cùng project chỉ được XEM, không được sửa nhật ký của người khác.)"""
    return entry.user_id == user.id or _can_manage(user)


def _is_project_member(db: Session, project_id: int, user_id: int) -> bool:
    if not project_id:
        return False
    return db.query(ProjectMember).filter_by(project_id=project_id, user_id=user_id).first() is not None


def _is_ancestor_project_owner(db: Session, project_id: int, user_id: int) -> bool:
    """True nếu user là chủ/quản lý của bất kỳ project cha nào phía trên project_id —
    chủ đề tài cha giám sát được nhật ký của mọi đề tài nhánh bên dưới, dù không phải
    thành viên trực tiếp của nhánh đó."""
    if not project_id:
        return False
    seen = set()
    p = db.get(Project, project_id)
    while p and p.parent_id and p.parent_id not in seen:
        seen.add(p.parent_id)
        parent = db.get(Project, p.parent_id)
        if not parent:
            break
        if parent.owner_id == user_id:
            return True
        p = parent
    return False


def _can_attach_project(db: Session, project_id: int, user_id: int) -> bool:
    """Được gắn nhật ký vào project này nếu là thành viên VÀ (project không có nhánh
    con, hoặc user là chủ/quản lý project đó, hoặc user là admin hệ thống) — chặn
    thành viên thường đổ nhật ký lên project cha một khi cha đã có đề tài nhánh."""
    if not _is_project_member(db, project_id, user_id):
        return False
    project = db.get(Project, project_id)
    if not project:
        return False
    has_children = db.query(Project.id).filter(Project.parent_id == project_id).first() is not None
    if not has_children:
        return True
    if project.owner_id == user_id:
        return True
    target_user = db.get(User, user_id)
    return bool(target_user and _can_manage(target_user))


def _can_view(user: User, entry: DailyLog, db: Session) -> bool:
    return (
        _can_manage(user)
        or entry.user_id == user.id
        or _is_project_member(db, entry.project_id, user.id)
        or _is_ancestor_project_owner(db, entry.project_id, user.id)
    )


def _entry_dir(user_id: int, log_id: int) -> str:
    path = os.path.join(DIARY_UPLOAD_DIR, str(user_id), str(log_id))
    os.makedirs(path, exist_ok=True)
    return path


async def _save_uploads(entry_dir: str, entry_id: int, uploads, category: str, db: Session):
    """Lưu danh sách file upload cho 1 nhật ký. Trả về (số file lưu được, tên các file bị từ chối)
    để báo rõ cho người dùng — KHÔNG âm thầm bỏ qua file như trước."""
    rejected = []
    for upload in uploads:
        if not upload.filename:
            continue
        ext = os.path.splitext(upload.filename)[1].lower()
        if category == "processed":
            if ext not in PROCESSED_EXT:
                rejected.append(f"{upload.filename} (định dạng chưa hỗ trợ cho mục kết quả đã xử lý)")
                continue
        else:  # "raw" — chấp nhận hầu hết định dạng, chỉ chặn đuôi nguy hiểm
            if ext in DANGEROUS_EXT:
                rejected.append(f"{upload.filename} (định dạng không được phép vì lý do an toàn)")
                continue

        data = await upload.read()
        if len(data) > 50 * 1024 * 1024:  # 50MB max per file
            rejected.append(f"{upload.filename} (vượt quá 50MB)")
            continue

        save_path = os.path.join(entry_dir, upload.filename)
        base, e = os.path.splitext(upload.filename)
        counter = 1
        while os.path.exists(save_path):
            save_path = os.path.join(entry_dir, f"{base}_{counter}{e}")
            counter += 1
        with open(save_path, "wb") as f:
            f.write(data)

        file_type = "image" if ext in IMAGE_EXT else ("pdf" if ext == ".pdf" else "doc")
        db.add(DailyLogFile(
            log_id=entry_id,
            filename=save_path,
            original_name=upload.filename,
            file_type=file_type,
            category=category,
            file_size=len(data),
        ))
    return rejected


def _preview_kind(f: DailyLogFile) -> str:
    """'image' | 'pdf' | 'text' (render inline) hoặc 'none' (không preview được, chỉ tải về)."""
    if f.file_type == "image":
        return "image"
    if f.file_type == "pdf":
        return "pdf"
    ext = os.path.splitext(f.original_name)[1].lower()
    return "text" if ext in TEXT_PREVIEW_EXT else "none"


def _compute_diff(old: str, new: str):
    """Trả về danh sách (loại, nội_dung) để hiển thị diff dòng-theo-dòng trong lịch sử sửa."""
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm=""))
    result = []
    for line in diff[2:]:  # bỏ 2 dòng header --- / +++
        if line.startswith("@@"):
            result.append(("hunk", line))
        elif line.startswith("+"):
            result.append(("add", line[1:]))
        elif line.startswith("-"):
            result.append(("remove", line[1:]))
        else:
            result.append(("context", line[1:] if line.startswith(" ") else line))
    return result


# ════════════════════════════════════════════════════════════════
# RESEARCHER ROUTES
# ════════════════════════════════════════════════════════════════

@router.get("/diary", response_class=HTMLResponse)
def my_diary(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    lock_days = _lock_days(db)
    entries = (
        db.query(DailyLog)
          .filter(DailyLog.user_id == user.id)
          .order_by(DailyLog.created_at.desc())
          .all()
    )
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "diary/list.html", {
        "user": user, "flash": flash, "entries": entries,
        "lock_days": lock_days, "is_locked": lambda e: _is_locked(e, lock_days),
    })


def _my_projects(db: Session, user_id: int):
    """Danh sách project để chọn gắn nhật ký vào.
    Một khi project đã có đề tài nhánh, CHỈ chủ/quản lý project đó (hoặc admin hệ thống)
    mới được gắn thẳng vào project cha — thành viên thường chỉ thấy các nhánh mình tham gia,
    ép công việc phải ghi vào đúng nhánh thay vì đổ hết lên project cha."""
    memberships = (
        db.query(ProjectMember)
          .filter(ProjectMember.user_id == user_id)
          .join(Project)
          .order_by(Project.name)
          .all()
    )
    target_user = db.get(User, user_id)
    is_system_manager = bool(target_user and _can_manage(target_user))

    result = []
    for m in memberships:
        p = m.project
        has_children = db.query(Project.id).filter(Project.parent_id == p.id).first() is not None
        if has_children and p.owner_id != user_id and not is_system_manager:
            continue
        result.append(p)
    return result


def _my_notebooks(db: Session, user_id: int):
    return (
        db.query(Notebook)
          .filter(Notebook.owner_id == user_id)
          .order_by(Notebook.topic_name)
          .all()
    )


def _parse_attach_to(raw: str):
    """'project:3' -> (3, None) | 'notebook:5' -> (None, 5) | '' -> (None, None)"""
    raw = (raw or "").strip()
    if not raw or ":" not in raw:
        return None, None
    kind, _, val = raw.partition(":")
    try:
        val = int(val)
    except ValueError:
        return None, None
    if kind == "project":
        return val, None
    if kind == "notebook":
        return None, val
    return None, None


@router.get("/diary/new", response_class=HTMLResponse)
def new_entry_form(request: Request, db: Session = Depends(get_db), attach_to: str = ""):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # Vào từ nút "ghi nhật ký mới" bên trong 1 project/sổ tay cụ thể (?attach_to=project:X
    # hoặc notebook:Y) -> tự gán sẵn, không bắt chọn lại thủ công. Vào từ nav trên cùng
    # (không có tham số này) -> vẫn hiện dropdown chọn như bình thường.
    preselect = None
    pid, nid = _parse_attach_to(attach_to)
    if pid and _can_attach_project(db, pid, user.id):
        project = db.get(Project, pid)
        if project:
            preselect = {"value": f"project:{pid}", "label": project.full_path_name}
    elif nid and db.query(Notebook).filter_by(id=nid, owner_id=user.id).first():
        notebook = db.get(Notebook, nid)
        if notebook:
            preselect = {"value": f"notebook:{nid}", "label": notebook.topic_name}

    return templates.TemplateResponse(request, "diary/form.html", {
        "user": user, "entry": None, "locked": False, "lock_days": _lock_days(db),
        "my_projects": _my_projects(db, user.id), "my_notebooks": _my_notebooks(db, user.id),
        "preselect": preselect,
    })


@router.get("/diary/{log_id}/edit", response_class=HTMLResponse)
def edit_entry_form(log_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    entry = db.get(DailyLog, log_id)
    if not entry or not _can_edit(user, entry):
        raise HTTPException(status_code=403)

    lock_days = _lock_days(db)
    if _is_locked(entry, lock_days):
        request.session["flash"] = "error:Nhật ký này đã bị khoá, không thể chỉnh sửa."
        return RedirectResponse(f"/diary/{log_id}", status_code=302)

    return templates.TemplateResponse(request, "diary/form.html", {
        "user": user, "entry": entry, "locked": False, "lock_days": lock_days,
        "my_projects": _my_projects(db, entry.user_id), "my_notebooks": _my_notebooks(db, entry.user_id),
    })


@router.post("/diary/save")
async def save_entry(
    request: Request,
    content: str = Form(...),
    title: str = Form(""),
    log_id: int = Form(None),
    attach_to: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    raw_files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    lock_days = _lock_days(db)
    now = datetime.utcnow()
    project_id, notebook_id = _parse_attach_to(attach_to)

    if log_id:
        entry = db.get(DailyLog, log_id)
        if not entry or not _can_edit(user, entry):
            raise HTTPException(status_code=403)
        if _is_locked(entry, lock_days):
            request.session["flash"] = "error:Nhật ký này đã bị khoá, không thể chỉnh sửa."
            return RedirectResponse(f"/diary/{log_id}", status_code=302)

        # project_id/notebook_id chỉ hợp lệ nếu TÁC GIẢ của nhật ký (không phải người đang sửa) sở hữu/là thành viên
        if project_id and not _can_attach_project(db, project_id, entry.user_id):
            project_id = None
        if notebook_id and not db.query(Notebook).filter_by(id=notebook_id, owner_id=entry.user_id).first():
            notebook_id = None
        entry.project_id = project_id
        entry.notebook_id = notebook_id

        new_title = title.strip()
        new_content = content.strip()
        if new_title != entry.title or new_content != entry.content:
            db.add(DailyLogRevision(
                log_id=entry.id, edited_by=user.id, edited_at=now,
                prev_title=entry.title, prev_content=entry.content,
                new_title=new_title, new_content=new_content,
            ))
            entry.title = new_title
            entry.content = new_content
            entry.updated_at = now
            entry.updated_by = user.id
        action = "diary_edit"
    else:
        if project_id and not _can_attach_project(db, project_id, user.id):
            project_id = None
        if notebook_id and not db.query(Notebook).filter_by(id=notebook_id, owner_id=user.id).first():
            notebook_id = None
        entry = DailyLog(
            user_id=user.id,
            group_id=user.group_id,
            notebook_id=notebook_id,
            project_id=project_id,
            title=title.strip(),
            content=content.strip(),
        )
        db.add(entry)
        db.flush()
        action = "diary_create"

    db.commit()
    db.refresh(entry)

    entry_dir = _entry_dir(user.id, entry.id)
    rejected = await _save_uploads(entry_dir, entry.id, files, "processed", db)
    rejected += await _save_uploads(entry_dir, entry.id, raw_files, "raw", db)
    db.commit()

    log_activity(db, action, f"{'Cập nhật' if log_id else 'Tạo'} nhật ký thí nghiệm",
                 user_id=user.id, target_type="daily_log", target_id=entry.id,
                 group_id=user.group_id)

    if rejected:
        request.session["flash"] = ("error:Đã lưu nhật ký, nhưng " + str(len(rejected)) +
                                     " file bị từ chối: " + "; ".join(rejected))
    else:
        request.session["flash"] = "Đã lưu nhật ký."
    return RedirectResponse(f"/diary/{entry.id}", status_code=302)


@router.post("/diary/{log_id}/delete-file/{file_id}")
def delete_file(log_id: int, file_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    entry = db.get(DailyLog, log_id)
    if not entry or not _can_edit(user, entry):
        raise HTTPException(status_code=403)

    lock_days = _lock_days(db)
    if _is_locked(entry, lock_days):
        request.session["flash"] = "error:Nhật ký này đã bị khoá, không thể xoá tệp đính kèm."
        return RedirectResponse(f"/diary/{log_id}", status_code=302)

    rf = db.get(DailyLogFile, file_id)
    if rf and rf.log_id == log_id:
        try:
            os.remove(rf.filename)
        except Exception:
            pass
        db.delete(rf)
        db.commit()
    return RedirectResponse(f"/diary/{log_id}/edit", status_code=302)


@router.post("/diary/{log_id}/delete")
def delete_entry(log_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    entry = db.get(DailyLog, log_id)
    if not entry or entry.user_id != user.id:
        raise HTTPException(status_code=403)

    lock_days = _lock_days(db)
    if _is_locked(entry, lock_days):
        request.session["flash"] = "error:Nhật ký này đã bị khoá, không thể xoá."
        return RedirectResponse(f"/diary/{log_id}", status_code=302)

    for rf in entry.files:
        try:
            os.remove(rf.filename)
        except Exception:
            pass
    db.delete(entry)
    db.commit()

    log_activity(db, "diary_delete", "Xoá nhật ký thí nghiệm",
                 user_id=user.id, target_type="daily_log", target_id=log_id,
                 group_id=user.group_id)

    request.session["flash"] = "Đã xoá nhật ký."
    return RedirectResponse("/diary", status_code=302)


# ════════════════════════════════════════════════════════════════
# MANAGER / ADMIN ROUTES  (must come before /{log_id} param routes)
# ════════════════════════════════════════════════════════════════

@router.get("/diary/overview", response_class=HTMLResponse)
def diary_overview(
    request: Request,
    db: Session = Depends(get_db),
    group_id: int = None,
    user_id: int = None,
    q: str = "",
):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _can_manage(user):
        return RedirectResponse("/diary", status_code=302)

    lock_days = _lock_days(db)
    groups = db.query(Group).order_by(Group.name).all()
    q = (q or "").strip()

    query = db.query(DailyLog)
    if group_id:
        query = query.filter(DailyLog.group_id == group_id)
    if user_id:
        query = query.filter(DailyLog.user_id == user_id)
    if q:
        like = f"%{q}%"
        query = (
            query
              .outerjoin(User, DailyLog.user_id == User.id)
              .outerjoin(Group, DailyLog.group_id == Group.id)
              .outerjoin(Project, DailyLog.project_id == Project.id)
              .outerjoin(Notebook, DailyLog.notebook_id == Notebook.id)
              .filter(or_(
                  DailyLog.title.ilike(like),
                  DailyLog.content.ilike(like),
                  User.full_name.ilike(like),
                  User.username.ilike(like),
                  Group.name.ilike(like),
                  Project.name.ilike(like),
                  Notebook.topic_name.ilike(like),
              ))
        )
    entries = query.order_by(DailyLog.created_at.desc()).limit(300).all()

    if group_id:
        members = db.query(User).filter(User.group_id == group_id, User.is_active == True).order_by(User.full_name).all()
    else:
        members = db.query(User).filter(User.is_active == True).order_by(User.full_name).all()

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "diary/overview.html", {
        "user": user, "flash": flash,
        "groups": groups, "selected_group": group_id,
        "members": members, "selected_user": user_id,
        "entries": entries, "lock_days": lock_days, "search_q": q,
        "is_locked": lambda e: _is_locked(e, lock_days),
    })


# ════════════════════════════════════════════════════════════════
# INDIVIDUAL ENTRY ROUTES (must come after named routes above)
# ════════════════════════════════════════════════════════════════

@router.get("/diary/{log_id}", response_class=HTMLResponse)
def view_entry(log_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    entry = db.get(DailyLog, log_id)
    if not entry:
        raise HTTPException(status_code=404)
    if not _can_view(user, entry, db):
        raise HTTPException(status_code=403)

    import markdown as md_lib
    content_html = md_lib.markdown(entry.content, extensions=["tables", "fenced_code", "nl2br"]) if entry.content else ""

    lock_days = _lock_days(db)
    locked = _is_locked(entry, lock_days)
    unlock_at = entry.created_at + timedelta(days=lock_days)
    days_remaining = max(0, (unlock_at - datetime.utcnow()).days)

    revisions = []
    for rev in entry.revisions:
        revisions.append({
            "rev": rev,
            "title_changed": rev.prev_title != rev.new_title,
            "diff": _compute_diff(rev.prev_content, rev.new_content),
        })

    files_view = [{"f": f, "preview": _preview_kind(f)} for f in entry.files]

    can_manage = _can_manage(user)
    is_author = entry.user_id == user.id
    if can_manage:
        back_url = "/diary/overview"
    elif is_author:
        back_url = "/diary"
    elif entry.project_id:
        back_url = f"/projects/{entry.project_id}"
    else:
        back_url = "/diary"

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "diary/detail.html", {
        "user": user, "entry": entry, "flash": flash,
        "content_html": content_html,
        "locked": locked, "unlock_at": unlock_at, "lock_days": lock_days,
        "days_remaining": days_remaining,
        "can_edit": _can_edit(user, entry),
        "can_manage": can_manage,
        "show_author": not is_author,
        "back_url": back_url,
        "revisions": revisions,
        "files_view": files_view,
    })
