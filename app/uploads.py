"""Quy tắc an toàn dùng chung cho tệp đính kèm trong chat (khu Tin nhắn và chat nhóm project).

Để MỘT chỗ duy nhất: nếu mỗi nơi tự chép một bản danh sách đuôi nguy hiểm, chỉ cần sau này bổ
sung đuôi mới vào một bản là bản kia thành lỗ hổng.
"""
import os
import re

MAX_FILE_BYTES = 25 * 1024 * 1024   # 25MB/tệp — chat không phải nơi lưu dữ liệu thô (đã có nhật ký lo)

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
DOC_EXT = {".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt", ".ppt", ".pptx"}

# Chặn các đuôi thực thi — người nhận rất dễ bấm mở tệp do đồng nghiệp gửi mà không nghi ngờ.
DANGEROUS_EXT = {".exe", ".bat", ".cmd", ".sh", ".msi", ".dll", ".scr", ".js",
                 ".vbs", ".ps1", ".jar", ".com", ".cpl", ".gadget", ".application", ".hta", ".apk"}


def safe_filename(name: str) -> str:
    """Chỉ giữ tên tệp thuần, bỏ mọi thành phần thư mục — chặn path traversal khi ghép đường dẫn."""
    name = os.path.basename((name or "").replace("\\", "/"))
    name = re.sub(r'[^\w.\-() ]', '_', name).strip()
    return name or "file"


def file_kind(ext: str) -> str:
    """Phân loại để giao diện biết cách hiển thị: ảnh xem tại chỗ, còn lại hiện thẻ tải về."""
    if ext in IMAGE_EXT:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in DOC_EXT:
        return "doc"
    return "other"


def reject_reason(filename: str, size: int) -> str | None:
    """None = tệp hợp lệ. Ngược lại trả về lý do để báo thẳng cho người gửi."""
    ext = os.path.splitext(filename)[1].lower()
    if ext in DANGEROUS_EXT:
        return f"{filename} (định dạng không được phép vì lý do an toàn)"
    if size > MAX_FILE_BYTES:
        return f"{filename} (vượt quá {MAX_FILE_BYTES // (1024 * 1024)}MB)"
    return None
