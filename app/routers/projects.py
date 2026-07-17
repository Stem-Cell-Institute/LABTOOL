"""Project nghiên cứu — nghiên cứu viên tạo, mời thêm thành viên, gắn nhật ký thí nghiệm vào."""
import os
from datetime import datetime
from fastapi import APIRouter, Request, Depends, Form, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import (User, Project, ProjectMember, DailyLog, ProjectMessage,
                        ProjectMessageFile, ProjectChatRead, ProjectDiaryRead)
from app.activity import log_activity
from app.uploads import safe_filename, file_kind, reject_reason

PROJECT_CHAT_UPLOAD_DIR = "uploads/project_chat"

router = APIRouter()
from app.templating import templates


def _get_user(request: Request, db: Session):
    uid = request.session.get("user_id")
    return db.get(User, uid) if uid else None


def _can_manage(user: User):
    return user.role == "admin" or user.can_view_all


def _can_create_project(user: User) -> bool:
    """Được tự tạo project mới (cả project gốc lẫn đề tài nhánh).
    NCV (researcher) mặc định được. Sinh viên/NCS mặc định KHÔNG — nhưng admin có thể
    cấp riêng quyền này theo từng người qua cờ user.can_create_project (VD: NCS được
    Viện cho phép hoạt động tự do như NCV, tạo và quản lý project nghiên cứu lớn)."""
    return _can_manage(user) or bool(user.can_create_project)


def _is_member(db: Session, project_id: int, user_id: int) -> bool:
    return db.query(ProjectMember).filter_by(project_id=project_id, user_id=user_id).first() is not None


def _is_ancestor_owner(db: Session, project: Project, user_id: int) -> bool:
    """True nếu user là chủ của bất kỳ project cha nào phía trên (đề tài nhánh kế thừa
    quyền giám sát từ đề tài cha, dù không có tên trong danh sách thành viên nhánh)."""
    seen = set()
    p = project
    while p.parent_id and p.parent_id not in seen:
        seen.add(p.parent_id)
        parent = db.get(Project, p.parent_id)
        if not parent:
            break
        if parent.owner_id == user_id:
            return True
        p = parent
    return False


def _can_view_project(user: User, project: Project, db: Session) -> bool:
    return (
        _can_manage(user)
        or _is_member(db, project.id, user.id)
        or _is_ancestor_owner(db, project, user.id)
    )


def _timeline_project_ids(db: Session, user: User, project_id: int) -> list[int]:
    """Các project mà dòng thời gian của trang project_id được phép gộp: chính nó + các nhánh
    con mà NGƯỜI ĐANG XEM có quyền xem. Dùng chung cho timeline, bản in và đếm nhật ký mới,
    để badge luôn khớp đúng thứ họ thực sự nhìn thấy."""
    ids = [project_id]
    for cid in _descendant_project_ids(db, project_id):
        sub = db.get(Project, cid)
        if sub and _can_view_project(user, sub, db):
            ids.append(cid)
    return ids


def _diary_unread(db: Session, user: User, project_id: int) -> int:
    """Số nhật ký mới trong project (gồm nhánh con xem được) mà người này chưa xem.
    Không tính nhật ký do chính họ viết — tự mình ghi thì không phải 'tin mới'."""
    from sqlalchemy import func
    rd = db.query(ProjectDiaryRead).filter_by(project_id=project_id, user_id=user.id).first()
    last_read = rd.last_read_id if rd else 0
    ids = _timeline_project_ids(db, user, project_id)
    return (db.query(func.count(DailyLog.id))
              .filter(DailyLog.project_id.in_(ids),
                      DailyLog.id > last_read,
                      DailyLog.user_id != user.id).scalar()) or 0


def _mark_diary_read(db: Session, user: User, project_id: int):
    """Đánh dấu đã xem tới nhật ký mới nhất — gọi khi user mở trang project (timeline hiện ngay
    trên đó nên coi như đã thấy)."""
    ids = _timeline_project_ids(db, user, project_id)
    latest = (db.query(DailyLog.id).filter(DailyLog.project_id.in_(ids))
                .order_by(DailyLog.id.desc()).first())
    if not latest:
        return
    rd = db.query(ProjectDiaryRead).filter_by(project_id=project_id, user_id=user.id).first()
    if rd:
        rd.last_read_id = max(rd.last_read_id or 0, latest[0])
    else:
        db.add(ProjectDiaryRead(project_id=project_id, user_id=user.id, last_read_id=latest[0]))
    db.commit()


def _descendant_project_ids(db: Session, root_id: int) -> list[int]:
    """Tất cả id đề tài nhánh con (mọi cấp) bên dưới root_id. Duyệt theo chiều rộng,
    có seen-set chống lặp vô hạn nếu dữ liệu parent_id lỡ tạo vòng."""
    ids, stack, seen = [], [root_id], {root_id}
    while stack:
        pid = stack.pop()
        for (cid,) in db.query(Project.id).filter(Project.parent_id == pid).all():
            if cid not in seen:
                seen.add(cid)
                ids.append(cid)
                stack.append(cid)
    return ids


def _can_manage_project(user: User, project: Project, db: Session) -> bool:
    """Quản lý Project, quản lý Project cha (giám sát nhánh), hoặc admin/quản lý mới được
    thêm/xoá thành viên, sửa thông tin, tạo/xoá đề tài nhánh."""
    return (
        _can_manage(user)
        or project.owner_id == user.id
        or _is_ancestor_owner(db, project, user.id)
    )


def _parse_date(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None


def _apply_general_info(project: Project, form: dict):
    project.topic_code      = (form.get("topic_code") or "").strip()
    project.researcher_name = (form.get("researcher_name") or "").strip()
    project.student_id      = (form.get("student_id") or "").strip()
    project.class_info      = (form.get("class_info") or "").strip()
    project.supervisor      = (form.get("supervisor") or "").strip()
    project.co_supervisor   = (form.get("co_supervisor") or "").strip()
    project.start_date = _parse_date(form.get("start_date"))
    project.end_date   = _parse_date(form.get("end_date"))


# ════════════════════════════════════════════════════════════════

@router.get("/projects", response_class=HTMLResponse)
def my_projects(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    memberships = (
        db.query(ProjectMember)
          .filter(ProjectMember.user_id == user.id)
          .join(Project)
          .order_by(Project.created_at.desc())
          .all()
    )
    all_projects = [m.project for m in memberships if m.project]
    # Project đã lưu trữ tách sang mục riêng — vẫn mở/xem được, chỉ là không chiếm chỗ ở
    # danh sách đang làm.
    projects = [p for p in all_projects if not p.is_archived]
    archived = [p for p in all_projects if p.is_archived]

    # Badge "có nhật ký mới" cho từng project — để thấy ngay project nào có việc mới mà không
    # phải mở lần lượt từng cái. Project đã cất đi thì không cần báo nữa.
    diary_unread = {p.id: _diary_unread(db, user, p.id) for p in projects}

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "projects/list.html", {
        "user": user, "flash": flash, "projects": projects,
        "archived": archived,
        "diary_unread": diary_unread,
        "can_create_project": _can_create_project(user),
    })


@router.get("/projects/new", response_class=HTMLResponse)
def new_project_form(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _can_create_project(user):
        request.session["flash"] = "error:Bạn chưa được cấp quyền tự tạo project — hãy liên hệ nghiên cứu viên hướng dẫn hoặc admin để được thêm vào/cấp quyền."
        return RedirectResponse("/projects", status_code=302)
    return templates.TemplateResponse(request, "projects/form.html", {"user": user, "project": None})


@router.get("/projects/{project_id}/edit", response_class=HTMLResponse)
def edit_project_form(project_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    project = db.get(Project, project_id)
    if not project or not _can_manage_project(user, project, db):
        raise HTTPException(status_code=403)
    return templates.TemplateResponse(request, "projects/form.html", {"user": user, "project": project})


@router.post("/projects/save")
async def save_project(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    form = await request.form()
    name = (form.get("name") or "").strip()
    description = (form.get("description") or "").strip()
    project_id = form.get("project_id")

    if not name:
        request.session["flash"] = "error:Tên đề tài/dự án không được để trống."
        back = f"/projects/{project_id}/edit" if project_id else "/projects/new"
        return RedirectResponse(back, status_code=302)

    if project_id:
        try:
            project_id_int = int(project_id)
        except ValueError:
            raise HTTPException(status_code=400)
        project = db.get(Project, project_id_int)
        if not project or not _can_manage_project(user, project, db):
            raise HTTPException(status_code=403)
        project.name = name
        project.description = description
        _apply_general_info(project, form)
        db.commit()
        request.session["flash"] = f"Đã cập nhật project '{name}'."
        return RedirectResponse(f"/projects/{project.id}", status_code=302)

    if not _can_create_project(user):
        raise HTTPException(status_code=403, detail="Bạn chưa được cấp quyền tự tạo project.")

    project = Project(name=name, description=description, owner_id=user.id)
    _apply_general_info(project, form)
    db.add(project)
    db.flush()
    db.add(ProjectMember(project_id=project.id, user_id=user.id, added_by=user.id))
    db.commit()
    db.refresh(project)

    log_activity(db, "create_project", f"Tạo project '{name}'",
                 user_id=user.id, target_type="project", target_id=project.id,
                 group_id=user.group_id)

    request.session["flash"] = f"Đã tạo project '{name}'."
    return RedirectResponse(f"/projects/{project.id}", status_code=302)


@router.get("/projects/{project_id}/sub-projects/new", response_class=HTMLResponse)
def new_sub_project_form(project_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    parent = db.get(Project, project_id)
    if not parent or not _can_manage_project(user, parent, db):
        raise HTTPException(status_code=403)
    if not _can_create_project(user):
        request.session["flash"] = "error:Bạn chưa được cấp quyền tự tạo đề tài nhánh — hãy nhờ nghiên cứu viên hướng dẫn tạo giúp."
        return RedirectResponse(f"/projects/{project_id}", status_code=302)

    members = (
        db.query(ProjectMember)
          .filter(ProjectMember.project_id == project_id)
          .join(User, ProjectMember.user_id == User.id)
          .order_by(User.full_name)
          .all()
    )
    return templates.TemplateResponse(request, "projects/sub_form.html", {
        "user": user, "parent": parent, "members": members,
    })


@router.post("/projects/{project_id}/sub-projects")
async def create_sub_project(project_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    parent = db.get(Project, project_id)
    if not parent or not _can_manage_project(user, parent, db):
        raise HTTPException(status_code=403)
    if not _can_create_project(user):
        raise HTTPException(status_code=403, detail="Bạn chưa được cấp quyền tự tạo đề tài nhánh.")

    form = await request.form()
    name = (form.get("name") or "").strip()
    description = (form.get("description") or "").strip()
    owner_user_id = form.get("owner_user_id")

    if not name:
        request.session["flash"] = "error:Tên đề tài nhánh không được để trống."
        return RedirectResponse(f"/projects/{project_id}/sub-projects/new", status_code=302)

    if not owner_user_id or not _is_member(db, project_id, int(owner_user_id)):
        request.session["flash"] = "error:Người phụ trách nhánh phải là thành viên của đề tài cha."
        return RedirectResponse(f"/projects/{project_id}/sub-projects/new", status_code=302)

    owner_id = int(owner_user_id)
    sub = Project(name=name, description=description, owner_id=owner_id, parent_id=project_id)
    db.add(sub)
    db.flush()
    db.add(ProjectMember(project_id=sub.id, user_id=owner_id, added_by=user.id))
    db.commit()
    db.refresh(sub)

    owner_user = db.get(User, owner_id)
    log_activity(db, "create_project",
                 f"Tạo đề tài nhánh '{name}' thuộc '{parent.name}', phụ trách: {owner_user.email}",
                 user_id=user.id, target_type="project", target_id=sub.id,
                 group_id=user.group_id)

    request.session["flash"] = f"Đã tạo đề tài nhánh '{name}'."
    return RedirectResponse(f"/projects/{sub.id}", status_code=302)


def _fold(s: str) -> str:
    """Bỏ dấu + chữ thường để tìm kiếm không phân biệt hoa/thường và KHÔNG phân biệt dấu
    tiếng Việt: gõ 'truong sinh' vẫn khớp 'Nguyễn Trường Sinh'. 'đ' NFD không tự tách nên
    thay tay."""
    import unicodedata
    s = (s or "").lower().replace("đ", "d")
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


@router.get("/projects/{project_id}/search-users")
def search_users(project_id: int, request: Request, q: str = "", db: Session = Depends(get_db)):
    """Gợi ý tài khoản để thêm vào project — tìm theo HỌ TÊN hoặc EMAIL (bỏ dấu, không phân
    biệt hoa/thường). Chỉ người quản lý project mới gọi được. Loại người đã là thành viên."""
    from fastapi.responses import JSONResponse
    user = _get_user(request, db)
    if not user:
        return JSONResponse({"results": []}, status_code=401)
    project = db.get(Project, project_id)
    if not project or not _can_manage_project(user, project, db):
        return JSONResponse({"results": []}, status_code=403)

    qf = _fold(q.strip())
    if not qf:
        return JSONResponse({"results": []})

    member_ids = {uid for (uid,) in db.query(ProjectMember.user_id)
                                       .filter(ProjectMember.project_id == project_id).all()}
    results = []
    for u in db.query(User).order_by(User.full_name, User.email).all():
        if u.id in member_ids:
            continue
        if qf in _fold(u.full_name) or qf in _fold(u.email):
            results.append({
                "email": u.email,
                "full_name": u.full_name or "",
                "member_type": u.member_type,
                "is_approved": bool(u.is_approved),
                "is_active": bool(u.is_active),
            })
            if len(results) >= 8:
                break
    return JSONResponse({"results": results})


@router.post("/projects/{project_id}/add-member")
def add_member(project_id: int, request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    project = db.get(Project, project_id)
    if not project or not _can_manage_project(user, project, db):
        raise HTTPException(status_code=403)

    target = db.query(User).filter(User.email == email.strip().lower()).first()
    if not target:
        request.session["flash"] = "error:Không tìm thấy tài khoản này."
        return RedirectResponse(f"/projects/{project_id}", status_code=302)
    if _is_member(db, project_id, target.id):
        request.session["flash"] = "error:Người này đã là thành viên project."
        return RedirectResponse(f"/projects/{project_id}", status_code=302)

    db.add(ProjectMember(project_id=project_id, user_id=target.id, added_by=user.id))
    db.commit()

    log_activity(db, "add_project_member", f"Thêm {target.email} vào project '{project.name}'",
                 user_id=user.id, target_type="project", target_id=project_id,
                 group_id=user.group_id)

    # Thêm được không có nghĩa là người đó dùng được ngay: đăng nhập còn bị chặn nếu chưa
    # duyệt (is_approved) hoặc đã bị vô hiệu hoá (is_active). Báo rõ để người quản lý project
    # không tưởng nhầm là xong — tránh đúng tình huống "thêm vào project được mà họ vẫn không
    # vào được hệ thống".
    name = target.full_name or target.email
    if not target.is_approved:
        request.session["flash"] = (
            f"Đã thêm {name} vào project. ⚠️ Tài khoản này CHƯA được quản trị viên duyệt nên "
            f"chưa đăng nhập được — cần duyệt trong mục Quản trị › Tài khoản thì họ mới truy cập được project."
        )
    elif not target.is_active:
        request.session["flash"] = (
            f"Đã thêm {name} vào project. ⚠️ Tài khoản này đang bị vô hiệu hoá nên chưa đăng nhập được "
            f"cho tới khi được kích hoạt lại."
        )
    else:
        request.session["flash"] = f"Đã thêm {name} vào project."
    return RedirectResponse(f"/projects/{project_id}", status_code=302)


@router.post("/projects/{project_id}/transfer-owner/{user_id}")
def transfer_owner(project_id: int, user_id: int, request: Request, db: Session = Depends(get_db)):
    """Chuyển quyền quản lý project cho một thành viên khác (bàn giao khi nghỉ việc, chuyển
    nhóm, hết vai trò...).

    Người quản lý CŨ vẫn ở lại làm thành viên thường — không tự động đá ra, vì nhật ký họ đã
    ghi vẫn thuộc project và họ thường vẫn cần xem tiếp.
    """
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    project = db.get(Project, project_id)
    if not project or not _can_manage_project(user, project, db):
        raise HTTPException(status_code=403)

    if user_id == project.owner_id:
        request.session["flash"] = "error:Người này đã là quản lý project rồi."
        return RedirectResponse(f"/projects/{project_id}", status_code=302)

    # Bắt buộc là thành viên sẵn có: tránh trao quyền nhầm cho người ngoài project.
    if not _is_member(db, project_id, user_id):
        request.session["flash"] = "error:Chỉ chuyển quyền cho người đã là thành viên project."
        return RedirectResponse(f"/projects/{project_id}", status_code=302)

    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404)

    old_owner = db.get(User, project.owner_id)
    project.owner_id = user_id
    db.commit()

    old_name = (old_owner.full_name or old_owner.email) if old_owner else "?"
    new_name = target.full_name or target.email
    log_activity(db, "transfer_project_owner",
                 f"Chuyen quyen quan ly project '{project.name}' tu '{old_name}' sang '{new_name}'",
                 user_id=user.id, target_type="project", target_id=project_id,
                 group_id=user.group_id)

    msg = f"Đã chuyển quyền quản lý project cho {new_name}."
    # Người vừa trao quyền thường mất luôn quyền quản lý ngay sau thao tác này (trừ khi họ còn
    # là admin hoặc chủ đề tài cha) -> nói rõ, tránh ngạc nhiên khi thấy các nút quản lý biến mất.
    if not _can_manage_project(user, project, db):
        msg += " Bạn không còn quyền quản lý project này nữa."
    request.session["flash"] = msg
    return RedirectResponse(f"/projects/{project_id}", status_code=302)


@router.post("/projects/{project_id}/remove-member/{user_id}")
def remove_member(project_id: int, user_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    project = db.get(Project, project_id)
    if not project or not _can_manage_project(user, project, db):
        raise HTTPException(status_code=403)

    if user_id == project.owner_id:
        request.session["flash"] = "error:Không thể xoá quản lý project. Hãy xoá cả project nếu muốn."
        return RedirectResponse(f"/projects/{project_id}", status_code=302)

    m = db.query(ProjectMember).filter_by(project_id=project_id, user_id=user_id).first()
    if m:
        db.delete(m)
        db.commit()

    request.session["flash"] = "Đã xoá thành viên khỏi project."
    return RedirectResponse(f"/projects/{project_id}", status_code=302)


# ── Chat nhóm project ─────────────────────────────────────────────────────────
# Thảo luận chung giữa các thành viên project. Ai xem được project (thành viên, chủ đề
# tài cha, admin) thì đọc/gửi được — cùng phạm vi với _can_view_project. Dùng polling
# đơn giản (client hỏi lại mỗi vài giây) — hệ thống LAN nhỏ, không cần WebSocket.

def _chat_unread(db: Session, project_id: int, user_id: int) -> int:
    """Số tin trong chat project mà user chưa đọc (không tính tin do chính họ gửi)."""
    from sqlalchemy import func
    rd = db.query(ProjectChatRead).filter_by(project_id=project_id, user_id=user_id).first()
    last_read = rd.last_read_id if rd else 0
    return (db.query(func.count(ProjectMessage.id))
              .filter(ProjectMessage.project_id == project_id,
                      ProjectMessage.id > last_read,
                      ProjectMessage.user_id != user_id).scalar()) or 0


def _mark_chat_read(db: Session, project_id: int, user_id: int):
    """Đánh dấu đã đọc tới tin mới nhất — gọi khi user đang thực sự mở khung chat."""
    latest = (db.query(ProjectMessage.id)
                .filter(ProjectMessage.project_id == project_id)
                .order_by(ProjectMessage.id.desc()).first())
    if not latest:
        return
    rd = db.query(ProjectChatRead).filter_by(project_id=project_id, user_id=user_id).first()
    if rd:
        rd.last_read_id = max(rd.last_read_id or 0, latest[0])
    else:
        db.add(ProjectChatRead(project_id=project_id, user_id=user_id, last_read_id=latest[0]))
    db.commit()


@router.get("/projects/{project_id}/messages")
def get_messages(project_id: int, request: Request, after: int = 0, db: Session = Depends(get_db)):
    from fastapi.responses import JSONResponse
    from app.timeutil import vn
    user = _get_user(request, db)
    if not user:
        return JSONResponse({"messages": []}, status_code=401)
    project = db.get(Project, project_id)
    if not project or not _can_view_project(user, project, db):
        return JSONResponse({"messages": []}, status_code=403)

    q = db.query(ProjectMessage).filter(ProjectMessage.project_id == project_id)
    if after:
        q = q.filter(ProjectMessage.id > after)
    msgs = q.order_by(ProjectMessage.id.asc()).limit(300).all()

    # Đang mở khung chat = đã đọc tới tin mới nhất.
    _mark_chat_read(db, project_id, user.id)

    return JSONResponse({
        "me": user.id,
        "messages": [{
            "id": m.id,
            "user_id": m.user_id,
            "name": (m.user.full_name or m.user.email) if m.user else "?",
            "content": m.content,
            "time": vn(m.created_at, "%H:%M · %d/%m"),   # quy đổi ra giờ địa phương, không hiện UTC
            "files": [{
                "id": f.id,
                "name": f.original_name,
                "kind": f.file_type,
                "size_kb": round((f.file_size or 0) / 1024),
                "url": f"/projects/{project_id}/messages/file/{f.id}",
            } for f in m.files],
        } for m in msgs],
    })


@router.get("/projects/{project_id}/messages/file/{file_id}")
def download_chat_file(project_id: int, file_id: int, request: Request, db: Session = Depends(get_db)):
    """Tải/xem tệp trong chat nhóm project. Cùng phạm vi quyền với việc đọc chat: phải xem
    được project. Thư mục uploads KHÔNG mount công khai nên mọi tệp đều qua đây."""
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    project = db.get(Project, project_id)
    if not project or not _can_view_project(user, project, db):
        raise HTTPException(status_code=403)
    f = db.get(ProjectMessageFile, file_id)
    if not f or not f.message or f.message.project_id != project_id:
        raise HTTPException(status_code=404)
    if not os.path.exists(f.filename):
        raise HTTPException(status_code=404, detail="Tệp không còn trên máy chủ.")
    return FileResponse(f.filename, filename=f.original_name)


@router.get("/projects/{project_id}/messages/unread-count")
def chat_unread_count(project_id: int, request: Request, db: Session = Depends(get_db)):
    from fastapi.responses import JSONResponse
    user = _get_user(request, db)
    if not user:
        return JSONResponse({"count": 0}, status_code=401)
    project = db.get(Project, project_id)
    if not project or not _can_view_project(user, project, db):
        return JSONResponse({"count": 0}, status_code=403)
    return JSONResponse({"count": _chat_unread(db, project_id, user.id)})


@router.post("/projects/{project_id}/messages")
async def post_message(project_id: int, request: Request, content: str = Form(""),
                       files: list[UploadFile] = File(default=[]),
                       db: Session = Depends(get_db)):
    from fastapi.responses import JSONResponse
    user = _get_user(request, db)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    project = db.get(Project, project_id)
    if not project or not _can_view_project(user, project, db):
        return JSONResponse({"ok": False}, status_code=403)

    text = (content or "").strip()[:4000]   # chặn tin nhắn quá dài
    real_files = [f for f in files if f and f.filename]
    # Cho gửi tin chỉ có ảnh/tệp, nhưng không cho tin rỗng hoàn toàn.
    if not text and not real_files:
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)

    m = ProjectMessage(project_id=project_id, user_id=user.id, content=text)
    db.add(m)
    db.flush()

    rejected = []
    if real_files:
        msg_dir = os.path.join(PROJECT_CHAT_UPLOAD_DIR, str(project_id), str(m.id))
        os.makedirs(msg_dir, exist_ok=True)
        for up in real_files:
            data = await up.read()
            why = reject_reason(up.filename, len(data))
            if why:
                rejected.append(why)
                continue
            safe = safe_filename(up.filename)
            path = os.path.join(msg_dir, safe)
            with open(path, "wb") as fh:
                fh.write(data)
            db.add(ProjectMessageFile(message_id=m.id, filename=path, original_name=safe,
                                      file_type=file_kind(os.path.splitext(safe)[1].lower()),
                                      file_size=len(data)))

    db.flush()
    # Mọi tệp đều bị từ chối và cũng không có chữ -> huỷ luôn tin, đừng để lại tin rỗng.
    if not text and not m.files:
        db.delete(m)
        db.commit()
        return JSONResponse({"ok": False, "error": "rejected", "rejected": rejected}, status_code=400)

    db.commit()
    db.refresh(m)
    return JSONResponse({"ok": True, "id": m.id, "rejected": rejected})


@router.post("/projects/{project_id}/leave")
def leave_project(project_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404)
    if project.owner_id == user.id:
        request.session["flash"] = "error:Quản lý project không thể tự rời — hãy xoá project nếu muốn."
        return RedirectResponse(f"/projects/{project_id}", status_code=302)

    m = db.query(ProjectMember).filter_by(project_id=project_id, user_id=user.id).first()
    if m:
        db.delete(m)
        db.commit()

    request.session["flash"] = "Bạn đã rời project."
    return RedirectResponse("/projects", status_code=302)


# ── Lưu trữ / mở lại project ──────────────────────────────────────────────────
# "Cất đi" thay cho "phá huỷ": ẩn khỏi danh sách nhưng giữ NGUYÊN mọi thứ (nhật ký vẫn gắn vào
# project nên thành viên không mất quyền xem) và mở lại được bất cứ lúc nào. Đây mới là thứ
# người dùng thực sự cần khi "đề tài xong rồi" — nên họ tự làm được, không cần phiền admin.

@router.post("/projects/{project_id}/archive")
def archive_project(project_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    project = db.get(Project, project_id)
    if not project or not _can_manage_project(user, project, db):
        raise HTTPException(status_code=403)

    if not project.is_archived:
        project.archived_at = datetime.utcnow()
        project.archived_by = user.id
        db.commit()
        log_activity(db, "archive_project", f"Luu tru project '{project.name}'",
                     user_id=user.id, target_type="project", target_id=project_id,
                     group_id=user.group_id)
    request.session["flash"] = (
        f"Đã lưu trữ project '{project.name}'. Mọi nhật ký và dữ liệu vẫn được giữ nguyên — "
        f"mở lại bất cứ lúc nào ở mục “Đã lưu trữ”."
    )
    return RedirectResponse("/projects", status_code=302)


@router.post("/projects/{project_id}/unarchive")
def unarchive_project(project_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    project = db.get(Project, project_id)
    if not project or not _can_manage_project(user, project, db):
        raise HTTPException(status_code=403)

    project.archived_at = None
    project.archived_by = None
    db.commit()
    log_activity(db, "archive_project", f"Mo lai project '{project.name}' tu luu tru",
                 user_id=user.id, target_type="project", target_id=project_id,
                 group_id=user.group_id)
    request.session["flash"] = f"Đã mở lại project '{project.name}'."
    return RedirectResponse(f"/projects/{project_id}", status_code=302)


@router.post("/projects/{project_id}/delete")
def delete_project(project_id: int, request: Request, db: Session = Depends(get_db)):
    """Xoá THẬT — chỉ admin/quản lý. Người dùng thường dùng 'Lưu trữ' ở trên.

    Lý do siết: xoá project gỡ nhật ký ra khỏi project, mà nhật ký đã quá hạn khoá thì KHÔNG
    thể gắn lại (route sửa từ chối bản đã khoá) -> thành viên mất quyền xem nhau VĨNH VIỄN.
    Đây là con đường để một người sắp nghỉ việc phá vỡ không gian làm việc chung của cả nhóm.
    """
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404)
    if not _can_manage(user):
        request.session["flash"] = (
            "error:Chỉ quản trị viên mới xoá được project. Nếu đề tài đã xong, hãy dùng "
            "“Lưu trữ project” — giữ nguyên toàn bộ nhật ký và mở lại được bất cứ lúc nào."
        )
        return RedirectResponse(f"/projects/{project_id}", status_code=302)

    from sqlalchemy import func
    from app.models import ProjectMessageFile, ProjectChatRead as _PCR, ProjectDiaryRead as _PDR

    name = project.name
    detached = db.query(func.count(DailyLog.id)).filter(DailyLog.project_id == project_id).scalar() or 0

    try:
        # Dọn dữ liệu chỉ thuộc riêng project này. Bắt buộc phải làm TRƯỚC khi xoá project:
        # SQLite bật kiểm tra khoá ngoại nên còn dòng nào trỏ tới project là DELETE sẽ nổ 500.
        msg_ids = [row[0] for row in db.query(ProjectMessage.id)
                                       .filter(ProjectMessage.project_id == project_id).all()]
        if msg_ids:
            for f in db.query(ProjectMessageFile).filter(ProjectMessageFile.message_id.in_(msg_ids)).all():
                try:
                    os.remove(f.filename)
                except OSError:
                    pass
            db.query(ProjectMessageFile).filter(ProjectMessageFile.message_id.in_(msg_ids)).delete(synchronize_session=False)
            db.query(ProjectMessage).filter(ProjectMessage.project_id == project_id).delete(synchronize_session=False)
        db.query(_PCR).filter(_PCR.project_id == project_id).delete(synchronize_session=False)
        db.query(_PDR).filter(_PDR.project_id == project_id).delete(synchronize_session=False)

        # Giữ nguyên nhật ký đã gắn và các đề tài nhánh bên dưới — chỉ gỡ liên kết, không xoá
        db.query(DailyLog).filter(DailyLog.project_id == project_id).update({"project_id": None})
        db.query(Project).filter(Project.parent_id == project_id).update({"parent_id": None})
        db.delete(project)
        db.commit()
    except Exception:
        db.rollback()
        import logging
        logging.getLogger(__name__).exception("Loi khi xoa project id=%s", project_id)
        request.session["flash"] = "error:Không xoá được project do còn dữ liệu liên quan. Hãy báo quản trị viên."
        return RedirectResponse(f"/projects/{project_id}", status_code=302)

    import shutil
    shutil.rmtree(os.path.join(PROJECT_CHAT_UPLOAD_DIR, str(project_id)), ignore_errors=True)

    # Xoá project là hành động phá huỷ và khó đảo ngược -> phải truy vết được ai làm, lúc nào.
    log_activity(db, "delete_project",
                 f"Xoa project '{name}' (id={project_id}) — go lien ket {detached} nhat ky",
                 user_id=user.id, target_type="project", target_id=project_id,
                 group_id=user.group_id)

    request.session["flash"] = ("Đã xoá project. Nhật ký và đề tài nhánh đã gắn vẫn được giữ nguyên "
                                 "(không còn thuộc project nào).")
    return RedirectResponse("/projects", status_code=302)


@router.get("/projects/{project_id}/export", response_class=HTMLResponse)
def export_project_diary(project_id: int, request: Request, db: Session = Depends(get_db)):
    """Bản in nhật ký của cả project (gồm các đề tài nhánh mà người xem được phép xem).
    Xếp TĂNG DẦN theo ngày thí nghiệm — đọc như cuốn sổ tay của cả nhóm."""
    import markdown as md_lib

    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    project = db.get(Project, project_id)
    if not project or not _can_view_project(user, project, db):
        raise HTTPException(status_code=403)

    ids = [project_id]
    for cid in _descendant_project_ids(db, project_id):
        sub = db.get(Project, cid)
        if sub and _can_view_project(user, sub, db):
            ids.append(cid)

    entries = (db.query(DailyLog)
                 .filter(DailyLog.project_id.in_(ids))
                 .order_by(DailyLog.experiment_date.asc(), DailyLog.created_at.asc())
                 .all())
    for e in entries:
        e.content_html = md_lib.markdown(e.content or "", extensions=["tables", "fenced_code", "nl2br"])

    return templates.TemplateResponse(request, "diary/export.html", {
        "user": user,
        "doc_title": f"Nhật ký thí nghiệm — {project.name}",
        "doc_subtitle": project.full_path_name + (" (gồm cả đề tài nhánh)" if len(ids) > 1 else ""),
        "entries": entries,
        "show_author": True,
        "range_label": None,
        "exported_at": datetime.utcnow(),
        "back_url": f"/projects/{project_id}",
    })


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def view_project(project_id: int, request: Request, db: Session = Depends(get_db), sort: str = "new"):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404)
    if not _can_view_project(user, project, db):
        raise HTTPException(status_code=403)

    members = (
        db.query(ProjectMember)
          .filter(ProjectMember.project_id == project_id)
          .join(User, ProjectMember.user_id == User.id)
          .order_by(User.full_name)
          .all()
    )
    logs = (
        db.query(DailyLog)
          .filter(DailyLog.project_id == project_id)
          .order_by(DailyLog.created_at.desc())
          .limit(200)
          .all()
    )

    # Timeline tổng quan: gộp nhật ký của project NÀY + các đề tài nhánh con mà người đang
    # xem được phép xem (chủ project cha thấy hết nhánh dưới; thành viên thường chỉ thấy nhánh
    # mình tham gia) — không lộ nhật ký nhánh mà họ vốn không có quyền xem.
    timeline_ids = _timeline_project_ids(db, user, project_id)
    from app.routers.diary import diary_order, _newest_first
    timeline_logs = (
        db.query(DailyLog)
          .filter(DailyLog.project_id.in_(timeline_ids))
          .order_by(*diary_order(_newest_first(sort)))
          .limit(300)
          .all()
    )
    sub_projects = (
        db.query(Project)
          .filter(Project.parent_id == project_id)
          .order_by(Project.created_at.desc())
          .all()
    )

    can_manage_project = _can_manage_project(user, project, db)
    has_children = db.query(Project.id).filter(Project.parent_id == project.id).first() is not None
    # Project đã lưu trữ = "đã cất đi", không ghi thêm nhật ký mới vào nữa (mở lại thì ghi tiếp được)
    can_log_here = (_is_member(db, project.id, user.id)
                    and (not has_children or can_manage_project)
                    and not project.is_archived)

    # Đếm nhật ký mới TRƯỚC khi đánh dấu đã xem, để lần vào này vẫn thấy "có N nhật ký mới";
    # lần sau quay lại mới hết.
    diary_unread = _diary_unread(db, user, project.id)
    _mark_diary_read(db, user, project.id)

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "projects/detail.html", {
        "user": user, "flash": flash, "project": project,
        "members": members, "logs": logs, "timeline_logs": timeline_logs,
        "sub_projects": sub_projects,
        "chat_unread": _chat_unread(db, project.id, user.id),
        "diary_unread": diary_unread,
        "can_manage_project": can_manage_project,
        "can_create_sub": can_manage_project and _can_create_project(user),
        "can_log_here": can_log_here,
    })
