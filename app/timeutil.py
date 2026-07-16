"""Quy đổi múi giờ cho hiển thị.

BỐI CẢNH: toàn hệ thống LƯU giờ bằng datetime.utcnow() (giờ UTC, không kèm múi giờ), nhưng
trước đây lại đem hiển thị thẳng — nên mọi mốc giờ trên giao diện đều sớm hơn giờ Việt Nam
7 tiếng (nhật ký ghi 08:18 sáng hiện thành 01:18).

CÁCH SỬA: KHÔNG đụng vào dữ liệu đã lưu, chỉ quy đổi lúc hiển thị. Lý do bắt buộc phải vậy:
chuỗi hash toàn vẹn (app/integrity.py) băm cả created_at/experiment_date — cộng thêm giờ vào
dữ liệu cũ sẽ làm sai toàn bộ hash và mọi nhật ký cũ bị báo "sửa lén".

Việt Nam cố định UTC+7, không có giờ mùa hè, nên dùng độ lệch cố định là đủ và không cần thêm
thư viện múi giờ nào. Đặt LOCAL_UTC_OFFSET_HOURS trong .env nếu triển khai ở múi giờ khác.
"""
import os
from datetime import datetime, timedelta

try:
    OFFSET_HOURS = float(os.getenv("LOCAL_UTC_OFFSET_HOURS", "7"))
except ValueError:
    OFFSET_HOURS = 7.0

_OFFSET = timedelta(hours=OFFSET_HOURS)


def to_local(dt: datetime | None) -> datetime | None:
    """Giờ UTC đã lưu -> giờ địa phương để hiển thị."""
    return dt + _OFFSET if dt else None


def to_utc(dt: datetime | None) -> datetime | None:
    """Giờ địa phương người dùng nhập -> giờ UTC để lưu."""
    return dt - _OFFSET if dt else None


def now_local() -> datetime:
    """'Bây giờ' theo giờ địa phương — dùng khi cần so sánh với cảm nhận của người dùng
    (hôm nay là ngày nào, có phải ngày tương lai không...)."""
    return datetime.utcnow() + _OFFSET


def local_today():
    return now_local().date()


def local_date(dt: datetime | None):
    """Ngày địa phương của một mốc giờ UTC đã lưu."""
    d = to_local(dt)
    return d.date() if d else None


def vn(dt: datetime | None, fmt: str = "%H:%M %d/%m/%Y") -> str:
    """Bộ lọc Jinja: {{ entry.created_at | vn('%H:%M %d/%m/%Y') }}
    Quy đổi UTC -> giờ địa phương rồi định dạng. None -> chuỗi rỗng (không làm vỡ trang)."""
    d = to_local(dt)
    return d.strftime(fmt) if d else ""
