"""Tiện ích ghi nhật ký hoạt động — dùng chung toàn app."""
from datetime import datetime
from sqlalchemy.orm import Session
from app.models import ActivityLog


def log_activity(
    db: Session,
    action: str,
    description: str,
    user_id: int | None = None,
    target_type: str = "",
    target_id: int | None = None,
    group_id: int | None = None,
):
    entry = ActivityLog(
        user_id=user_id,
        action=action,
        description=description,
        target_type=target_type,
        target_id=target_id,
        group_id=group_id,
    )
    db.add(entry)
    db.commit()


# Icon + label cho từng action type (dùng trong template)
ACTION_META = {
    "upload":          ("bi-cloud-upload",      "success", "Upload video"),
    "analyze_done":    ("bi-robot",              "primary", "Phân tích xong"),
    "analyze_failed":  ("bi-exclamation-circle", "danger",  "Phân tích lỗi"),
    "reanalyze":       ("bi-arrow-clockwise",    "warning", "Phân tích lại"),
    "comment":         ("bi-chat-dots",          "info",    "Bình luận"),
    "login":           ("bi-box-arrow-in-right", "secondary","Đăng nhập"),
    "delete_video":    ("bi-trash3",             "danger",  "Xóa video"),
    "delete_log":      ("bi-trash3",             "danger",  "Xóa nhật ký"),
    "toggle_admin":    ("bi-shield",             "warning", "Đổi quyền Admin"),
    "toggle_active":   ("bi-person-gear",        "secondary","Đổi trạng thái TK"),
    "scan_folder":     ("bi-folder-symlink",     "info",    "Quét thư mục"),
    "create_group":    ("bi-people-fill",        "success", "Tạo nhóm"),
    "create_user":     ("bi-person-plus",        "success", "Tạo tài khoản"),
    "diary_create":    ("bi-journal-plus",       "success", "Ghi nhật ký"),
    "diary_edit":      ("bi-pencil-square",      "info",    "Sửa nhật ký"),
    "diary_delete":    ("bi-trash3",             "danger",  "Xoá nhật ký"),
    "create_project":      ("bi-kanban-fill",    "success", "Tạo project"),
    "add_project_member":  ("bi-person-plus",    "info",    "Thêm thành viên project"),
}


def get_action_meta(action: str):
    return ACTION_META.get(action, ("bi-circle", "secondary", action))
