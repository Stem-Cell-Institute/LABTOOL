"""Nhật ký thí nghiệm — nghiên cứu viên tự ghi, khoá vĩnh viễn sau N ngày."""
import os
import re
import difflib
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, Group, DailyLog, DailyLogFile, DailyLogRevision, SystemConfig, Project, ProjectMember, Notebook
from app.activity import log_activity
from app.timeutil import local_today, to_utc

router = APIRouter()
from app.templating import templates

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


def diary_order(newest_first: bool):
    """Thứ tự dòng thời gian: theo NGÀY THÍ NGHIỆM trước, rồi mới tới lúc ghi.
    Dùng chung cho /diary, /diary/overview và trang project để 3 nơi luôn nhất quán."""
    if newest_first:
        return (DailyLog.experiment_date.desc(), DailyLog.created_at.desc())
    return (DailyLog.experiment_date.asc(), DailyLog.created_at.asc())


def _newest_first(sort: str) -> bool:
    """?sort=old -> cũ nhất trước. Mặc định (không có tham số) là mới nhất trước."""
    return sort != "old"


def _fold(s: str) -> str:
    """Bỏ dấu + chữ thường để tìm kiếm không phân biệt hoa/thường và KHÔNG phân biệt dấu:
    gõ 'te bao goc' vẫn khớp 'tế bào gốc'. 'đ' không tự tách khi chuẩn hoá NFD nên thay tay."""
    import unicodedata
    s = (s or "").lower().replace("đ", "d")
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _parse_date_only(raw: str):
    """'YYYY-MM-DD' -> datetime đầu ngày; rỗng/sai -> None (không lọc)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None


def _parse_exp_date(raw: str, now: datetime) -> datetime:
    """Ngày thực hiện thí nghiệm từ form (YYYY-MM-DD, là ngày theo giờ ĐỊA PHƯƠNG của người
    dùng). Rỗng/sai định dạng -> thời điểm hiện tại. KHÔNG cho chọn ngày tương lai — nhật ký
    là ghi việc đã làm, không phải kế hoạch.

    Trả về mốc UTC (như mọi cột thời gian khác) để lúc hiển thị quy đổi ngược lại ra đúng
    ngày người dùng đã chọn.
    """
    from app.timeutil import to_utc, local_today

    raw = (raw or "").strip()
    if not raw:
        return now
    try:
        d = datetime.strptime(raw, "%Y-%m-%d")   # 00:00 ngày địa phương
    except ValueError:
        return now
    if d.date() > local_today():
        return now
    return to_utc(d)


def _can_view(user: User, entry: DailyLog, db: Session) -> bool:
    return (
        _can_manage(user)
        or entry.user_id == user.id
        or _is_project_member(db, entry.project_id, user.id)
        or _is_ancestor_project_owner(db, entry.project_id, user.id)
    )


def _safe_filename(name: str) -> str:
    """Chỉ giữ tên file thuần (bỏ mọi thành phần thư mục/traversal) và ký tự an toàn,
    tránh path traversal khi ghép vào đường dẫn lưu trên server."""
    name = os.path.basename((name or "").replace("\\", "/"))
    name = re.sub(r'[^\w.\-() ]', '_', name).strip()
    return name or "file"


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

        safe_name = _safe_filename(upload.filename)
        save_path = os.path.join(entry_dir, safe_name)
        base, e = os.path.splitext(safe_name)
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
def my_diary(request: Request, db: Session = Depends(get_db), q: str = "",
             from_date: str = "", to_date: str = "", sort: str = "new"):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    lock_days = _lock_days(db)
    query = db.query(DailyLog).filter(DailyLog.user_id == user.id)

    # Lọc theo khoảng NGÀY LÀM THÍ NGHIỆM (làm ở SQL cho nhẹ)
    d_from = _parse_date_only(from_date)
    d_to = _parse_date_only(to_date)
    # Người dùng chọn ngày theo giờ địa phương, còn cột lưu UTC -> phải quy đổi mốc lọc,
    # nếu không sẽ lệch 7 tiếng ở hai đầu khoảng (lọt/sót bản ghi ghi vào sáng sớm hay tối muộn).
    if d_from:
        query = query.filter(DailyLog.experiment_date >= to_utc(d_from))
    if d_to:
        query = query.filter(DailyLog.experiment_date < to_utc(d_to + timedelta(days=1)))

    entries = query.order_by(*diary_order(_newest_first(sort))).all()

    # Tìm chữ: lọc bằng Python để KHÔNG phân biệt dấu (gõ "te bao" vẫn ra "tế bào").
    # SQLite không có sẵn so sánh bỏ dấu; số bản ghi mỗi người ở mức vài trăm nên hoàn toàn ổn.
    qf = _fold(q.strip())
    if qf:
        entries = [e for e in entries if qf in _fold(e.title) or qf in _fold(e.content)]

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "diary/list.html", {
        "user": user, "flash": flash, "entries": entries,
        "lock_days": lock_days, "is_locked": lambda e: _is_locked(e, lock_days),
        "q": q, "from_date": from_date, "to_date": to_date,
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
        if not p or p.is_archived:
            continue          # project đã cất đi thì không gắn nhật ký mới vào nữa
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


def _render_markdown(entries):
    """Gắn sẵn content_html cho từng entry để template in ra nội dung đã render markdown."""
    import markdown as md_lib
    for e in entries:
        e.content_html = md_lib.markdown(
            e.content or "", extensions=["tables", "fenced_code", "nl2br"]
        )
    return entries


@router.get("/diary/export", response_class=HTMLResponse)
def export_my_diary(request: Request, db: Session = Depends(get_db),
                    q: str = "", from_date: str = "", to_date: str = ""):
    """Bản in sổ tay nhật ký của chính mình — dùng đúng bộ lọc của trang /diary.
    Xếp TĂNG DẦN theo ngày thí nghiệm để đọc như một cuốn sổ tay giấy."""
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    query = db.query(DailyLog).filter(DailyLog.user_id == user.id)
    d_from = _parse_date_only(from_date)
    d_to = _parse_date_only(to_date)
    # Người dùng chọn ngày theo giờ địa phương, còn cột lưu UTC -> phải quy đổi mốc lọc,
    # nếu không sẽ lệch 7 tiếng ở hai đầu khoảng (lọt/sót bản ghi ghi vào sáng sớm hay tối muộn).
    if d_from:
        query = query.filter(DailyLog.experiment_date >= to_utc(d_from))
    if d_to:
        query = query.filter(DailyLog.experiment_date < to_utc(d_to + timedelta(days=1)))
    entries = query.order_by(DailyLog.experiment_date.asc(), DailyLog.created_at.asc()).all()

    qf = _fold(q.strip())
    if qf:
        entries = [e for e in entries if qf in _fold(e.title) or qf in _fold(e.content)]

    range_label = None
    if d_from or d_to:
        range_label = f"{d_from.strftime('%d/%m/%Y') if d_from else '…'} – {d_to.strftime('%d/%m/%Y') if d_to else '…'}"

    return templates.TemplateResponse(request, "diary/export.html", {
        "user": user,
        "doc_title": "Sổ tay nhật ký thí nghiệm",
        "doc_subtitle": (user.full_name or user.email) + (f" · Lọc: “{q}”" if q else ""),
        "entries": _render_markdown(entries),
        "show_author": False,
        "range_label": range_label,
        "exported_at": datetime.utcnow(),
        "back_url": "/diary",
    })


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
        "today_str": local_today().strftime("%Y-%m-%d"),
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
        "today_str": local_today().strftime("%Y-%m-%d"),
    })


@router.post("/diary/save")
async def save_entry(
    request: Request,
    content: str = Form(...),
    title: str = Form(""),
    log_id: int = Form(None),
    attach_to: str = Form(""),
    experiment_date: str = Form(""),
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
    exp_date = _parse_exp_date(experiment_date, now)

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
        entry.experiment_date = exp_date

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
            experiment_date=exp_date,
        )
        db.add(entry)
        db.flush()
        action = "diary_create"

    # Nối mắt xích chuỗi toàn vẹn SAU khi nội dung đã ở trạng thái cuối (entry đã có id nhờ
    # flush ở trên) — để hash đúng thứ vừa lưu. Xem app/integrity.py.
    from app import integrity
    integrity.append(db, entry, "create" if not log_id else "edit", actor_id=user.id)

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


@router.get("/diary/{log_id}/file/{file_id}")
def download_file(log_id: int, file_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    entry = db.get(DailyLog, log_id)
    if not entry or not _can_view(user, entry, db):
        raise HTTPException(status_code=403)
    rf = db.get(DailyLogFile, file_id)
    if not rf or rf.log_id != log_id or not os.path.exists(rf.filename):
        raise HTTPException(status_code=404)
    return FileResponse(rf.filename, filename=rf.original_name)


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

    # Ghi mắt xích 'delete' TRƯỚC khi xoá — để chuỗi phân biệt được "xoá hợp lệ qua ứng dụng"
    # với "biến mất khỏi DB không rõ lý do" (dấu hiệu bị xoá lén bằng SQL).
    from app import integrity
    integrity.append(db, entry, "delete", actor_id=user.id)

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
    sort: str = "new",
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
                  User.email.ilike(like),
                  Group.name.ilike(like),
                  Project.name.ilike(like),
                  Notebook.topic_name.ilike(like),
              ))
        )
    entries = query.order_by(*diary_order(_newest_first(sort))).limit(300).all()

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
