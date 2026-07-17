"""Nhắn tin: DM 1-1 và nhóm chat tuỳ chọn (độc lập với project).

Khu 'Tin nhắn' riêng trên menu. Dùng polling đơn giản (client hỏi lại mỗi vài giây khi
đang mở) — hệ thống LAN nhỏ, không cần WebSocket.
"""
import os
import unicodedata
from datetime import datetime
from fastapi import APIRouter, Request, Depends, Form, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, Conversation, ConversationMember, ChatMessage, ChatFile
from app.uploads import safe_filename, file_kind, reject_reason

router = APIRouter(prefix="/messages")
from app.templating import templates

CHAT_UPLOAD_DIR = "uploads/chat"


def _get_user(request: Request, db: Session):
    uid = request.session.get("user_id")
    u = db.get(User, uid) if uid else None
    return u if (u and u.is_active) else None


def _fold(s: str) -> str:
    s = (s or "").lower().replace("đ", "d")
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _membership(db: Session, conv_id: int, user_id: int):
    return (db.query(ConversationMember)
              .filter_by(conversation_id=conv_id, user_id=user_id).first())


def _conv_title(db: Session, conv: Conversation, me_id: int) -> str:
    """Tên hiển thị: nhóm dùng title (hoặc ghép tên thành viên); DM là tên người kia."""
    if conv.type == "group":
        if conv.title:
            return conv.title
        names = [(m.user.full_name or m.user.email) for m in conv.members if m.user]
        return ", ".join(names) or "Nhóm"
    other = next((m.user for m in conv.members if m.user_id != me_id and m.user), None)
    return (other.full_name or other.email) if other else "(không rõ)"


def _unread_count(db: Session, conv_id: int, member: ConversationMember, me_id: int) -> int:
    return (db.query(func.count(ChatMessage.id))
              .filter(ChatMessage.conversation_id == conv_id,
                      ChatMessage.id > (member.last_read_id or 0),
                      ChatMessage.user_id != me_id).scalar()) or 0


def _my_conversations(db: Session, me_id: int):
    memberships = (db.query(ConversationMember)
                     .filter(ConversationMember.user_id == me_id).all())
    items = []
    for mem in memberships:
        conv = mem.conversation
        if not conv:
            continue
        last = (db.query(ChatMessage)
                  .filter(ChatMessage.conversation_id == conv.id)
                  .order_by(ChatMessage.id.desc()).first())
        items.append({
            "conv": conv,
            "title": _conv_title(db, conv, me_id),
            "last": last,
            "unread": _unread_count(db, conv.id, mem, me_id),
            "sort_key": conv.last_at or conv.created_at,
        })
    items.sort(key=lambda x: x["sort_key"], reverse=True)
    return items


def _serialize(m: ChatMessage, me_id: int) -> dict:
    from app.timeutil import vn
    return {
        "id": m.id,
        "user_id": m.user_id,
        "name": (m.user.full_name or m.user.email) if m.user else "?",
        "mine": m.user_id == me_id,
        "content": "" if m.is_deleted else m.content,
        "deleted": bool(m.is_deleted),
        "edited": m.edited_at is not None,
        "time": vn(m.created_at, "%H:%M · %d/%m"),
        # Tin đã thu hồi thì không trả tệp nữa
        "files": [] if m.is_deleted else [
            {
                "id": f.id,
                "name": f.original_name,
                "kind": f.file_type,
                "size_kb": round((f.file_size or 0) / 1024),
                "url": f"/messages/{m.conversation_id}/file/{f.id}",
            } for f in m.files
        ],
    }


# ── Trang chính ───────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
def messages_home(request: Request, c: int = None, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    convs = _my_conversations(db, user.id)

    active = None
    active_messages = []
    if c:
        mem = _membership(db, c, user.id)
        if mem:
            conv = mem.conversation
            msgs = (db.query(ChatMessage)
                      .filter(ChatMessage.conversation_id == c)
                      .order_by(ChatMessage.id.asc()).limit(300).all())
            active = {
                "conv": conv,
                "title": _conv_title(db, conv, user.id),
                "members": [(m.user.full_name or m.user.email) for m in conv.members if m.user],
                "is_group": conv.type == "group",
            }
            active_messages = [_serialize(m, user.id) for m in msgs]
            # đánh dấu đã đọc tới tin cuối
            if msgs:
                mem.last_read_id = msgs[-1].id
                db.commit()

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "messages/index.html", {
        "user": user, "convs": convs, "active": active,
        "active_messages": active_messages, "flash": flash,
        "server_now": datetime.utcnow().isoformat(),
    })


# ── Bắt đầu DM ────────────────────────────────────────────────────────────────

@router.post("/dm")
def start_dm(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    other = db.query(User).filter(User.email == email.strip().lower()).first()
    if not other or other.id == user.id:
        request.session["flash"] = "error:Không tìm thấy người dùng."
        return RedirectResponse("/messages", status_code=302)

    # tìm DM đã có giữa đúng 2 người này
    my_conv_ids = {m.conversation_id for m in db.query(ConversationMember)
                                                .filter_by(user_id=user.id).all()}
    existing = None
    for cid in my_conv_ids:
        conv = db.get(Conversation, cid)
        if conv and conv.type == "dm":
            member_ids = {m.user_id for m in conv.members}
            if member_ids == {user.id, other.id}:
                existing = conv
                break

    if existing:
        return RedirectResponse(f"/messages?c={existing.id}", status_code=302)

    conv = Conversation(type="dm", created_by=user.id)
    db.add(conv)
    db.flush()
    db.add(ConversationMember(conversation_id=conv.id, user_id=user.id))
    db.add(ConversationMember(conversation_id=conv.id, user_id=other.id))
    db.commit()
    return RedirectResponse(f"/messages?c={conv.id}", status_code=302)


# ── Tạo nhóm ──────────────────────────────────────────────────────────────────

@router.post("/group")
async def create_group(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    form = await request.form()
    title = (form.get("title") or "").strip()
    emails = form.getlist("members")  # danh sách email được chọn
    member_ids = {user.id}
    for e in emails:
        u = db.query(User).filter(User.email == (e or "").strip().lower()).first()
        if u:
            member_ids.add(u.id)

    if len(member_ids) < 2:
        request.session["flash"] = "error:Nhóm cần ít nhất 1 thành viên khác bạn."
        return RedirectResponse("/messages", status_code=302)

    conv = Conversation(type="group", title=title[:200], created_by=user.id)
    db.add(conv)
    db.flush()
    for uid in member_ids:
        db.add(ConversationMember(conversation_id=conv.id, user_id=uid))
    db.commit()
    request.session["flash"] = f"Đã tạo nhóm chat ({len(member_ids)} thành viên)."
    return RedirectResponse(f"/messages?c={conv.id}", status_code=302)


# ── Gửi / sửa / xoá tin ───────────────────────────────────────────────────────

@router.post("/{conv_id}/send")
async def send_message(conv_id: int, request: Request, content: str = Form(""),
                       files: list[UploadFile] = File(default=[]),
                       db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    mem = _membership(db, conv_id, user.id)
    if not mem:
        return JSONResponse({"ok": False}, status_code=403)

    text = (content or "").strip()[:4000]
    real_files = [f for f in files if f and f.filename]
    # Cho gửi tin chỉ có ảnh/tệp mà không cần chữ — nhưng không cho gửi tin rỗng hoàn toàn.
    if not text and not real_files:
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)

    m = ChatMessage(conversation_id=conv_id, user_id=user.id, content=text)
    db.add(m)
    db.flush()

    rejected = []
    if real_files:
        msg_dir = os.path.join(CHAT_UPLOAD_DIR, str(conv_id), str(m.id))
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
            db.add(ChatFile(message_id=m.id, filename=path, original_name=safe,
                            file_type=file_kind(os.path.splitext(safe)[1].lower()),
                            file_size=len(data)))

    # Tất cả tệp đều bị từ chối và cũng không có chữ -> huỷ luôn tin, đừng để lại tin rỗng.
    db.flush()
    if not text and not m.files:
        db.delete(m)
        db.commit()
        return JSONResponse({"ok": False, "error": "rejected", "rejected": rejected}, status_code=400)

    conv = db.get(Conversation, conv_id)
    conv.last_at = datetime.utcnow()
    mem.last_read_id = m.id
    db.commit()
    return JSONResponse({"ok": True, "id": m.id, "rejected": rejected})


@router.get("/{conv_id}/file/{file_id}")
def download_file(conv_id: int, file_id: int, request: Request, db: Session = Depends(get_db)):
    """Tải/xem tệp đính kèm. Bắt buộc là thành viên cuộc trò chuyện — thư mục uploads KHÔNG
    được mount công khai, nên mọi tệp đều phải đi qua đây để kiểm tra quyền."""
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _membership(db, conv_id, user.id):
        raise HTTPException(status_code=403)
    f = db.get(ChatFile, file_id)
    if not f or not f.message or f.message.conversation_id != conv_id:
        raise HTTPException(status_code=404)
    if not os.path.exists(f.filename):
        raise HTTPException(status_code=404, detail="Tệp không còn trên máy chủ.")
    return FileResponse(f.filename, filename=f.original_name)


@router.post("/{conv_id}/msg/{msg_id}/edit")
def edit_message(conv_id: int, msg_id: int, request: Request, content: str = Form(...), db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    if not _membership(db, conv_id, user.id):
        return JSONResponse({"ok": False}, status_code=403)
    m = db.get(ChatMessage, msg_id)
    if not m or m.conversation_id != conv_id or m.user_id != user.id or m.is_deleted:
        return JSONResponse({"ok": False}, status_code=403)  # chỉ sửa tin của chính mình
    text = (content or "").strip()[:4000]
    if not text:
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)
    m.content = text
    m.edited_at = datetime.utcnow()
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/{conv_id}/msg/{msg_id}/delete")
def delete_message(conv_id: int, msg_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    if not _membership(db, conv_id, user.id):
        return JSONResponse({"ok": False}, status_code=403)
    m = db.get(ChatMessage, msg_id)
    if not m or m.conversation_id != conv_id or m.user_id != user.id:
        return JSONResponse({"ok": False}, status_code=403)  # chỉ thu hồi tin của chính mình
    # Thu hồi phải xoá luôn tệp đính kèm — nếu chỉ xoá phần chữ thì ảnh/tệp vẫn tải được qua
    # link cũ, coi như chưa thu hồi gì cả.
    for f in list(m.files):
        try:
            os.remove(f.filename)
        except OSError:
            pass          # tệp đã mất trên đĩa thì thôi, vẫn phải xoá bản ghi
        db.delete(f)

    m.is_deleted = True
    m.content = ""
    m.edited_at = datetime.utcnow()   # dùng làm mốc "vừa thay đổi" để polling đồng bộ cho người khác
    db.commit()
    return JSONResponse({"ok": True})


# ── Polling & badge ───────────────────────────────────────────────────────────

@router.get("/{conv_id}/poll")
def poll_messages(conv_id: int, request: Request, after: int = 0, since: str = "", db: Session = Depends(get_db)):
    """Trả về tin MỚI (id > after) để nối vào cuối, VÀ các tin CŨ vừa bị sửa/thu hồi
    (edited_at > since) để client cập nhật tại chỗ — nhờ vậy sửa/xoá của người khác hiện
    gần như tức thì, không phải tải lại trang.

    `since` luôn là mốc thời gian do SERVER cấp (từ lần poll trước hoặc lúc render trang),
    nên không lệ thuộc đồng hồ máy client.
    """
    user = _get_user(request, db)
    if not user:
        return JSONResponse({"messages": [], "updates": []}, status_code=401)
    mem = _membership(db, conv_id, user.id)
    if not mem:
        return JSONResponse({"messages": [], "updates": []}, status_code=403)

    now = datetime.utcnow()

    msgs = (db.query(ChatMessage)
              .filter(ChatMessage.conversation_id == conv_id, ChatMessage.id > after)
              .order_by(ChatMessage.id.asc()).limit(300).all())

    updates = []
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            since_dt = None
    if since_dt and after:
        # chỉ lấy tin CŨ (id <= after) vừa thay đổi — tin mới đã nằm trong `messages` rồi
        updates = (db.query(ChatMessage)
                     .filter(ChatMessage.conversation_id == conv_id,
                             ChatMessage.id <= after,
                             ChatMessage.edited_at != None,
                             ChatMessage.edited_at > since_dt)
                     .order_by(ChatMessage.id.asc()).limit(100).all())

    if msgs:
        mem.last_read_id = max(mem.last_read_id or 0, msgs[-1].id)
        db.commit()

    return JSONResponse({
        "now": now.isoformat(),
        "messages": [_serialize(m, user.id) for m in msgs],
        "updates": [_serialize(m, user.id) for m in updates],
    })


@router.get("/unread-total")
def unread_total(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return JSONResponse({"count": 0})
    total = 0
    for mem in db.query(ConversationMember).filter_by(user_id=user.id).all():
        total += _unread_count(db, mem.conversation_id, mem, user.id)
    return JSONResponse({"count": total})


# ── Tìm người để nhắn / thêm vào nhóm ─────────────────────────────────────────

@router.get("/search-users")
def search_users(request: Request, q: str = "", db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return JSONResponse({"results": []}, status_code=401)
    qf = _fold(q.strip())
    if not qf:
        return JSONResponse({"results": []})
    results = []
    for u in db.query(User).filter(User.is_active == True).order_by(User.full_name, User.email).all():
        if u.id == user.id:
            continue
        if qf in _fold(u.full_name) or qf in _fold(u.email):
            results.append({"email": u.email, "full_name": u.full_name or "",
                            "member_type": u.member_type})
            if len(results) >= 8:
                break
    return JSONResponse({"results": results})
