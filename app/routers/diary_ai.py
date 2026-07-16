"""Hỏi AI tổng hợp toàn bộ nhật ký thí nghiệm — kiểu NotebookLM. Chỉ admin hệ thống."""
import threading
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from app.database import get_db, SessionLocal
from app.models import User, DailyLog, DiaryAIQuestion

router = APIRouter()
from app.templating import templates

MAX_ENTRIES = 500  # số nhật ký gần nhất đưa vào ngữ cảnh khi tổng hợp


def _get_user(request: Request, db: Session):
    uid = request.session.get("user_id")
    return db.get(User, uid) if uid else None


def _is_admin(user: User) -> bool:
    """CHỈ admin hệ thống — không mở cho quản lý/can_view_all, khác với các trang khác trong module này."""
    return bool(user) and user.role == "admin"


def _ask_diary_ai_background(question_id: int):
    db = SessionLocal()
    try:
        dq = db.get(DiaryAIQuestion, question_id)
        if not dq:
            return

        entries = (
            db.query(DailyLog)
              .order_by(DailyLog.created_at.desc())
              .limit(MAX_ENTRIES)
              .all()
        )

        prev = (
            db.query(DiaryAIQuestion)
              .filter(DiaryAIQuestion.status == "done", DiaryAIQuestion.id != question_id)
              .order_by(DiaryAIQuestion.created_at.desc())
              .limit(3)
              .all()
        )
        history = "\n".join(f"H: {p.question}\nA: {p.answer}" for p in reversed(prev))

        from app import gemini as gem
        answer = gem.ask_about_diary_entries(dq.question, entries, history=history)
        dq.answer = answer
        dq.status = "done"
        dq.entry_count = len(entries)
        db.commit()
    except Exception as e:
        try:
            dq = db.get(DiaryAIQuestion, question_id)
            if dq:
                dq.answer = f"Lỗi: {str(e)[:300]}"
                dq.status = "failed"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


@router.get("/diary/ask-ai", response_class=HTMLResponse)
def ask_ai_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _is_admin(user):
        raise HTTPException(status_code=403)

    total_entries = db.query(DailyLog).count()
    questions = (
        db.query(DiaryAIQuestion)
          .order_by(DiaryAIQuestion.created_at.desc())
          .limit(50)
          .all()
    )

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "diary/ask_ai.html", {
        "user": user, "flash": flash,
        "questions": questions, "total_entries": total_entries, "max_entries": MAX_ENTRIES,
    })


@router.post("/diary/ask-ai")
def ask_ai_submit(request: Request, question: str = Form(...), db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _is_admin(user):
        raise HTTPException(status_code=403)

    question = question.strip()
    if not question:
        return RedirectResponse("/diary/ask-ai", status_code=302)

    dq = DiaryAIQuestion(asked_by=user.id, question=question, status="pending")
    db.add(dq)
    db.commit()
    db.refresh(dq)

    threading.Thread(target=_ask_diary_ai_background, args=(dq.id,), daemon=True).start()
    return RedirectResponse("/diary/ask-ai", status_code=302)


@router.get("/diary/ask-ai/{qid}/status")
def ask_ai_status(qid: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user or not _is_admin(user):
        raise HTTPException(status_code=401)
    dq = db.get(DiaryAIQuestion, qid)
    if not dq:
        raise HTTPException(status_code=404)
    return JSONResponse({"status": dq.status, "answer": dq.answer, "entry_count": dq.entry_count})
