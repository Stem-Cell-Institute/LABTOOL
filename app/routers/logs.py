import markdown
import threading
from datetime import datetime
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.database import get_db, SessionLocal
from app.models import User, ExperimentLog, Video, Group, VideoQuestion

router = APIRouter(prefix="/logs")
from app.templating import templates


def _get_user(request: Request, db: Session):
    uid = request.session.get("user_id")
    return db.get(User, uid) if uid else None


def _can_view_all(user: User) -> bool:
    return user.role == "admin" or user.can_view_all


def _base_query(db: Session, user: User):
    q = db.query(ExperimentLog).join(Video)
    if not _can_view_all(user):
        q = q.filter(Video.group_id == user.group_id)
    return q


def _check_log_access(user: User, log: ExperimentLog):
    if _can_view_all(user):
        return True
    return log.video.group_id == user.group_id


# ── Danh sách nhật ký ────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
def log_list(
    request: Request,
    db: Session = Depends(get_db),
    q: str = "",
    group_id: int = None,
    from_date: str = "",
    to_date: str = "",
):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    query = _base_query(db, user)

    if q:
        pattern = f"%{q}%"
        query = query.filter(
            or_(ExperimentLog.title.ilike(pattern), ExperimentLog.content.ilike(pattern))
        )
    if group_id and _can_view_all(user):
        query = query.filter(Video.group_id == group_id)
    if from_date:
        try:
            query = query.filter(ExperimentLog.created_at >= datetime.strptime(from_date, "%Y-%m-%d"))
        except ValueError:
            pass
    if to_date:
        try:
            query = query.filter(ExperimentLog.created_at <= datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59))
        except ValueError:
            pass

    logs = query.order_by(ExperimentLog.created_at.desc()).all()
    groups = db.query(Group).order_by(Group.name).all() if _can_view_all(user) else None

    return templates.TemplateResponse(
        request, "logs/list.html",
        {
            "user": user, "logs": logs,
            "q": q, "groups": groups, "selected_group": group_id,
            "from_date": from_date, "to_date": to_date,
        },
    )


# ── Chi tiết nhật ký ─────────────────────────────────────────────────────────

@router.get("/{log_id}", response_class=HTMLResponse)
def log_detail(log_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    log = db.get(ExperimentLog, log_id)
    if not log:
        raise HTTPException(status_code=404)
    if not _check_log_access(user, log):
        raise HTTPException(status_code=403)

    content_html = markdown.markdown(log.content, extensions=["tables", "fenced_code", "nl2br"])

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(
        request, "logs/detail.html",
        {"user": user, "log": log, "content_html": content_html, "flash": flash},
    )


# ── Hỏi AI về video ──────────────────────────────────────────────────────────

def _ask_in_background(question_id: int):
    db = SessionLocal()
    try:
        vq = db.get(VideoQuestion, question_id)
        if not vq:
            return
        video = vq.log.video
        # Build history from previous questions in this log
        prev = db.query(VideoQuestion).filter(
            VideoQuestion.log_id == vq.log_id,
            VideoQuestion.status == "done",
            VideoQuestion.id != question_id,
        ).order_by(VideoQuestion.created_at).all()
        history = "\n".join(f"H: {p.question}\nA: {p.answer}" for p in prev[-3:])  # last 3

        from app import gemini as gem
        answer, new_file_name = gem.ask_about_video(
            video_path=video.filename,
            question=vq.question,
            gemini_file_name=video.gemini_file_name,
            history=history,
        )
        vq.answer = answer
        vq.status = "done"
        if new_file_name:
            video.gemini_file_name = new_file_name
        db.commit()
    except Exception as e:
        try:
            vq = db.get(VideoQuestion, question_id)
            if vq:
                vq.answer = f"Lỗi: {str(e)[:300]}"
                vq.status = "failed"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


@router.post("/{log_id}/ask")
def ask_question(
    log_id: int,
    request: Request,
    question: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    log = db.get(ExperimentLog, log_id)
    if not log:
        raise HTTPException(status_code=404)
    if not _check_log_access(user, log):
        raise HTTPException(status_code=403)

    question = question.strip()
    if not question:
        return RedirectResponse(f"/logs/{log_id}#ask-ai", status_code=302)

    vq = VideoQuestion(log_id=log_id, asked_by=user.id, question=question, status="pending")
    db.add(vq)
    db.commit()
    db.refresh(vq)

    threading.Thread(target=_ask_in_background, args=(vq.id,), daemon=True).start()
    return RedirectResponse(f"/logs/{log_id}#ask-ai", status_code=302)


@router.get("/{log_id}/question/{qid}/status")
def question_status(log_id: int, qid: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        raise HTTPException(status_code=401)
    log = db.get(ExperimentLog, log_id)
    if not log:
        raise HTTPException(status_code=404)
    if not _check_log_access(user, log):
        raise HTTPException(status_code=403)
    vq = db.get(VideoQuestion, qid)
    if not vq or vq.log_id != log_id:
        raise HTTPException(status_code=404)
    return JSONResponse({"status": vq.status, "answer": vq.answer})


# ── Export ───────────────────────────────────────────────────────────────────

@router.get("/{log_id}/export/md")
def export_md(log_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        raise HTTPException(status_code=401)
    log = db.get(ExperimentLog, log_id)
    if not log:
        raise HTTPException(status_code=404)
    if not _check_log_access(user, log):
        raise HTTPException(status_code=403)

    return Response(
        content=log.content,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="nhatky_{log_id}.md"'},
    )


@router.get("/{log_id}/export/pdf")
def export_pdf(log_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        raise HTTPException(status_code=401)
    log = db.get(ExperimentLog, log_id)
    if not log:
        raise HTTPException(status_code=404)
    if not _check_log_access(user, log):
        raise HTTPException(status_code=403)

    try:
        from weasyprint import HTML
        from app.timeutil import vn as _vn   # bản in cũng phải là giờ địa phương, không phải UTC
        content_html = markdown.markdown(log.content, extensions=["tables", "fenced_code", "nl2br"])
        html_content = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<style>
  body {{ font-family: Arial, sans-serif; margin: 2cm; line-height: 1.6; color: #333; }}
  h1 {{ color: #1a5276; border-bottom: 2px solid #1a5276; padding-bottom: 8px; }}
  h2 {{ color: #1f618d; margin-top: 1.5em; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
  th, td {{ border: 1px solid #ccc; padding: 8px 12px; }}
  th {{ background: #eaf4fb; }}
  code {{ background: #f4f4f4; padding: 2px 5px; border-radius: 3px; font-size: 0.9em; }}
  .meta {{ color: #666; font-size: 0.9em; margin-bottom: 1em; }}
</style>
</head><body>
<p class="meta">Nhom: {log.video.group.name} | Ngay: {_vn(log.created_at, '%d/%m/%Y %H:%M')} | AI: {log.ai_model}</p>
{content_html}
</body></html>"""
        pdf_bytes = HTML(string=html_content).write_pdf()
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="nhatky_{log_id}.pdf"'},
        )
    except (ImportError, OSError) as e:
        # OSError: weasyprint ĐÃ cài nhưng thiếu thư viện hệ thống (libpango/gobject...) —
        # trường hợp này hay gặp và trước đây lọt xuống thành lỗi 500 khó hiểu.
        raise HTTPException(
            status_code=501,
            detail=f"Máy chủ chưa cài đủ thư viện để xuất PDF (weasyprint): {e}",
        )
