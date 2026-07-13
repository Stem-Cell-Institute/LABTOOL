from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, Comment, ExperimentLog
from app.activity import log_activity

router = APIRouter(prefix="/comments")


def _get_user(request: Request, db: Session):
    uid = request.session.get("user_id")
    return db.get(User, uid) if uid else None


@router.post("/add")
def add_comment(
    request: Request,
    log_id: int = Form(...),
    content: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    log = db.get(ExperimentLog, log_id)
    if not log:
        raise HTTPException(status_code=404)
    if user.role != "admin" and not user.can_view_all and log.video.group_id != user.group_id:
        raise HTTPException(status_code=403)

    content = content.strip()
    if not content:
        return RedirectResponse(f"/logs/{log_id}", status_code=302)

    comment = Comment(log_id=log_id, user_id=user.id, content=content)
    db.add(comment)
    db.commit()
    log_activity(db, "comment",
                 f"{user.email} binh luan tren nhat ky #{log_id}",
                 user_id=user.id, target_type="log",
                 target_id=log_id, group_id=log.video.group_id)
    request.session["flash"] = "Đã thêm bình luận"
    return RedirectResponse(f"/logs/{log_id}#comments", status_code=302)


@router.post("/{comment_id}/delete")
def delete_comment(comment_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    comment = db.get(Comment, comment_id)
    if not comment:
        raise HTTPException(status_code=404)

    log_id = comment.log_id
    if user.role != "admin" and comment.user_id != user.id:
        raise HTTPException(status_code=403, detail="Chỉ có thể xóa bình luận của chính mình")

    db.delete(comment)
    db.commit()
    request.session["flash"] = "Đã xóa bình luận"
    return RedirectResponse(f"/logs/{log_id}#comments", status_code=302)
