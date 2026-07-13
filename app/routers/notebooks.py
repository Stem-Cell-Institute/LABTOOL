"""Sổ tay thí nghiệm cá nhân — chứa Thông tin chung cho nhật ký thí nghiệm không thuộc project."""
from datetime import datetime
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, Notebook, DailyLog

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _get_user(request: Request, db: Session):
    uid = request.session.get("user_id")
    return db.get(User, uid) if uid else None


def _can_manage(user: User):
    return user.role == "admin" or user.can_view_all


def _can_edit_notebook(user: User, nb: Notebook) -> bool:
    return nb.owner_id == user.id or _can_manage(user)


def _parse_date(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None


def _apply_general_info(nb, form: dict):
    nb.topic_code      = (form.get("topic_code") or "").strip()
    nb.researcher_name = (form.get("researcher_name") or "").strip()
    nb.student_id      = (form.get("student_id") or "").strip()
    nb.class_info      = (form.get("class_info") or "").strip()
    nb.supervisor      = (form.get("supervisor") or "").strip()
    nb.co_supervisor    = (form.get("co_supervisor") or "").strip()
    nb.start_date = _parse_date(form.get("start_date"))
    nb.end_date   = _parse_date(form.get("end_date"))


@router.get("/notebooks", response_class=HTMLResponse)
def my_notebooks(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    notebooks = (
        db.query(Notebook)
          .filter(Notebook.owner_id == user.id)
          .order_by(Notebook.created_at.desc())
          .all()
    )
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "notebooks/list.html", {
        "user": user, "flash": flash, "notebooks": notebooks,
    })


@router.get("/notebooks/new", response_class=HTMLResponse)
def new_notebook_form(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "notebooks/form.html", {"user": user, "notebook": None})


@router.get("/notebooks/{notebook_id}/edit", response_class=HTMLResponse)
def edit_notebook_form(notebook_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    nb = db.get(Notebook, notebook_id)
    if not nb or not _can_edit_notebook(user, nb):
        raise HTTPException(status_code=403)
    return templates.TemplateResponse(request, "notebooks/form.html", {"user": user, "notebook": nb})


@router.post("/notebooks/save")
async def save_notebook(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    form = await request.form()
    topic_name = (form.get("topic_name") or "").strip()
    notebook_id = form.get("notebook_id")

    if not topic_name:
        request.session["flash"] = "error:Tên đề tài/dự án không được để trống."
        back = f"/notebooks/{notebook_id}/edit" if notebook_id else "/notebooks/new"
        return RedirectResponse(back, status_code=302)

    if notebook_id:
        nb = db.get(Notebook, int(notebook_id))
        if not nb or not _can_edit_notebook(user, nb):
            raise HTTPException(status_code=403)
        nb.topic_name = topic_name
    else:
        nb = Notebook(topic_name=topic_name, owner_id=user.id)
        db.add(nb)
        db.flush()

    _apply_general_info(nb, form)
    db.commit()
    db.refresh(nb)

    request.session["flash"] = f"Đã lưu sổ tay '{topic_name}'."
    return RedirectResponse(f"/notebooks/{nb.id}", status_code=302)


@router.post("/notebooks/{notebook_id}/delete")
def delete_notebook(notebook_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    nb = db.get(Notebook, notebook_id)
    if not nb or not _can_edit_notebook(user, nb):
        raise HTTPException(status_code=403)

    # Giữ nguyên các nhật ký đã gắn — chỉ gỡ liên kết, không xoá nội dung
    db.query(DailyLog).filter(DailyLog.notebook_id == notebook_id).update({"notebook_id": None})
    db.delete(nb)
    db.commit()

    request.session["flash"] = "Đã xoá sổ tay. Nhật ký đã gắn vẫn được giữ nguyên."
    return RedirectResponse("/notebooks", status_code=302)


@router.get("/notebooks/{notebook_id}", response_class=HTMLResponse)
def view_notebook(notebook_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    nb = db.get(Notebook, notebook_id)
    if not nb:
        raise HTTPException(status_code=404)
    if nb.owner_id != user.id and not _can_manage(user):
        raise HTTPException(status_code=403)

    logs = (
        db.query(DailyLog)
          .filter(DailyLog.notebook_id == notebook_id)
          .order_by(DailyLog.created_at.desc())
          .limit(200)
          .all()
    )

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "notebooks/detail.html", {
        "user": user, "flash": flash, "notebook": nb, "logs": logs,
        "can_edit": _can_edit_notebook(user, nb),
        "is_owner": nb.owner_id == user.id,  # sổ tay là riêng tư — chỉ chủ mới ghi được nhật ký vào đây
    })
