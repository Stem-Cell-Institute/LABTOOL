"""Chuỗi hash chống sửa lén nhật ký thí nghiệm.

Xem docstring của IntegrityRecord (app/models.py) để hiểu mô hình và giới hạn.
Nguyên tắc: bảng integrity_records là APPEND-ONLY — chỉ thêm, không bao giờ sửa/xoá.
"""
import hashlib
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import DailyLog, IntegrityRecord


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def content_hash(entry: DailyLog) -> str:
    """Băm nội dung nhật ký theo dạng chuẩn hoá. Đưa vào MỌI trường mà việc sửa lén sẽ
    làm sai lệch hồ sơ khoa học: tác giả, tiêu đề, nội dung, ngày thí nghiệm, ngày tạo.
    Dấu '\\x1f' làm ngăn cách để không thể ghép chuỗi lừa hash (VD title='a|b' vs 'a','b')."""
    parts = [
        str(entry.id),
        str(entry.user_id),
        entry.title or "",
        entry.content or "",
        entry.experiment_date.isoformat() if entry.experiment_date else "",
        entry.created_at.isoformat() if entry.created_at else "",
    ]
    return _sha("\x1f".join(parts))


def _record_hash(prev_hash: str, log_id: int, event: str, c_hash: str, at: datetime) -> str:
    return _sha("\x1f".join([prev_hash or "", str(log_id), event, c_hash, at.isoformat()]))


def _last_record(db: Session) -> IntegrityRecord | None:
    # SessionLocal dùng autoflush=False, nên mắt xích vừa db.add() ở lần append trước sẽ KHÔNG
    # tự hiện ra trong truy vấn này -> phải flush tay, nếu không mọi mắt xích đều lấy prev_hash
    # rỗng và chuỗi không bao giờ nối được.
    db.flush()
    return db.query(IntegrityRecord).order_by(IntegrityRecord.id.desc()).first()


def append(db: Session, entry: DailyLog, event: str, actor_id: int | None = None) -> IntegrityRecord:
    """Nối 1 mắt xích cho sự kiện create/edit/delete. Gọi SAU khi entry đã có id và đã commit
    nội dung, để hash đúng trạng thái vừa lưu. KHÔNG tự commit — người gọi commit chung."""
    prev = _last_record(db)
    at = datetime.utcnow()
    c_hash = content_hash(entry)
    rec = IntegrityRecord(
        log_id=entry.id,
        event=event,
        content_hash=c_hash,
        prev_hash=prev.record_hash if prev else "",
        record_hash=_record_hash(prev.record_hash if prev else "", entry.id, event, c_hash, at),
        actor_id=actor_id,
        created_at=at,
    )
    db.add(rec)
    return rec


def seed_existing(db: Session) -> int:
    """Khởi tạo chuỗi cho các nhật ký đã có TRƯỚC khi bật tính năng này.

    Đây là 'mốc gốc': ta tin trạng thái hiện tại tại thời điểm khởi tạo, từ đó về sau mọi
    thay đổi đều để lại dấu. Không thể chứng thực ngược quá khứ — ghi rõ để không hiểu nhầm.
    Chỉ chạy khi chuỗi còn rỗng; trả về số mắt xích đã tạo.
    """
    if db.query(IntegrityRecord.id).first():
        return 0
    entries = db.query(DailyLog).order_by(DailyLog.id.asc()).all()
    for e in entries:
        append(db, e, "create", actor_id=e.user_id)
    db.commit()
    return len(entries)


def verify(db: Session) -> dict:
    """Kiểm tra toàn vẹn. Trả về báo cáo gồm 4 loại vấn đề:
      - chain_breaks : mắt xích bị sửa/xoá (chuỗi đứt)
      - modified     : nhật ký bị sửa thẳng trong DB (hash nội dung không khớp)
      - unrecorded   : nhật ký có trong DB nhưng không có mắt xích nào (chèn thẳng vào DB)
      - vanished     : có mắt xích nhưng nhật ký biến mất mà không có sự kiện 'delete'
    """
    records = db.query(IntegrityRecord).order_by(IntegrityRecord.id.asc()).all()

    chain_breaks = []
    prev_hash = ""
    for r in records:
        expected = _record_hash(prev_hash, r.log_id, r.event, r.content_hash, r.created_at)
        if r.prev_hash != prev_hash or r.record_hash != expected:
            chain_breaks.append({"seq": r.id, "log_id": r.log_id, "at": r.created_at})
        prev_hash = r.record_hash

    # mắt xích mới nhất của từng nhật ký
    latest: dict[int, IntegrityRecord] = {}
    for r in records:
        latest[r.log_id] = r

    modified, vanished = [], []
    for log_id, rec in latest.items():
        entry = db.get(DailyLog, log_id)
        if entry is None:
            if rec.event != "delete":
                vanished.append({"log_id": log_id, "last_event": rec.event, "at": rec.created_at})
            continue
        if rec.event == "delete":
            continue  # đã ghi nhận xoá nhưng bản ghi còn -> không xét nội dung
        if content_hash(entry) != rec.content_hash:
            modified.append({
                "log_id": log_id, "title": entry.title,
                "author": (entry.author.full_name or entry.author.email) if entry.author else "?",
                "recorded_at": rec.created_at,
            })

    unrecorded = []
    for e in db.query(DailyLog).order_by(DailyLog.id.asc()).all():
        if e.id not in latest:
            unrecorded.append({
                "log_id": e.id, "title": e.title,
                "author": (e.author.full_name or e.author.email) if e.author else "?",
                "created_at": e.created_at,
            })

    ok = not (chain_breaks or modified or unrecorded or vanished)
    return {
        "ok": ok,
        "total_records": len(records),
        "total_entries": db.query(DailyLog).count(),
        "latest_hash": records[-1].record_hash if records else "",
        "latest_at": records[-1].created_at if records else None,
        "chain_breaks": chain_breaks,
        "modified": modified,
        "unrecorded": unrecorded,
        "vanished": vanished,
    }
