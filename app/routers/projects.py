"""Project nghiên cứu — nghiên cứu viên tạo, mời thêm thành viên, gắn nhật ký thí nghiệm vào."""
from datetime import datetime
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, Project, ProjectMember, DailyLog
from app.activity import log_activity

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


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
    projects = [m.project for m in memberships]

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "projects/list.html", {
        "user": user, "flash": flash, "projects": projects,
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
        project = db.get(Project, int(project_id))
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
                 f"Tạo đề tài nhánh '{name}' thuộc '{parent.name}', phụ trách: {owner_user.username}",
                 user_id=user.id, target_type="project", target_id=sub.id,
                 group_id=user.group_id)

    request.session["flash"] = f"Đã tạo đề tài nhánh '{name}'."
    return RedirectResponse(f"/projects/{sub.id}", status_code=302)


@router.post("/projects/{project_id}/add-member")
def add_member(project_id: int, request: Request, username: str = Form(...), db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    project = db.get(Project, project_id)
    if not project or not _can_manage_project(user, project, db):
        raise HTTPException(status_code=403)

    target = db.query(User).filter(User.username == username.strip().lower()).first()
    if not target:
        request.session["flash"] = "error:Không tìm thấy tài khoản này."
        return RedirectResponse(f"/projects/{project_id}", status_code=302)
    if _is_member(db, project_id, target.id):
        request.session["flash"] = "error:Người này đã là thành viên project."
        return RedirectResponse(f"/projects/{project_id}", status_code=302)

    db.add(ProjectMember(project_id=project_id, user_id=target.id, added_by=user.id))
    db.commit()

    log_activity(db, "add_project_member", f"Thêm {target.username} vào project '{project.name}'",
                 user_id=user.id, target_type="project", target_id=project_id,
                 group_id=user.group_id)

    request.session["flash"] = f"Đã thêm {target.full_name or target.username} vào project."
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


@router.post("/projects/{project_id}/delete")
def delete_project(project_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    project = db.get(Project, project_id)
    if not project or not _can_manage_project(user, project, db):
        raise HTTPException(status_code=403)

    # Giữ nguyên nhật ký đã gắn và các đề tài nhánh bên dưới — chỉ gỡ liên kết, không xoá
    db.query(DailyLog).filter(DailyLog.project_id == project_id).update({"project_id": None})
    db.query(Project).filter(Project.parent_id == project_id).update({"parent_id": None})
    db.delete(project)
    db.commit()

    request.session["flash"] = ("Đã xoá project. Nhật ký và đề tài nhánh đã gắn vẫn được giữ nguyên "
                                 "(không còn thuộc project nào).")
    return RedirectResponse("/projects", status_code=302)


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def view_project(project_id: int, request: Request, db: Session = Depends(get_db)):
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
    sub_projects = (
        db.query(Project)
          .filter(Project.parent_id == project_id)
          .order_by(Project.created_at.desc())
          .all()
    )

    can_manage_project = _can_manage_project(user, project, db)
    has_children = db.query(Project.id).filter(Project.parent_id == project.id).first() is not None
    can_log_here = _is_member(db, project.id, user.id) and (not has_children or can_manage_project)

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "projects/detail.html", {
        "user": user, "flash": flash, "project": project,
        "members": members, "logs": logs, "sub_projects": sub_projects,
        "can_manage_project": can_manage_project,
        "can_create_sub": can_manage_project and _can_create_project(user),
        "can_log_here": can_log_here,
    })
