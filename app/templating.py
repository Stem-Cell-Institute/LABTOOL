"""Jinja2Templates dùng chung cho mọi router.

Trước đây mỗi router tự tạo một Jinja2Templates riêng — nghĩa là mỗi lần thêm bộ lọc/biến
toàn cục lại phải đăng ký 11 lần và rất dễ sót một chỗ. Gom về một chỗ để bộ lọc `vn`
(quy đổi múi giờ) chắc chắn có mặt ở mọi template.
"""
from fastapi.templating import Jinja2Templates

from app.timeutil import vn, now_local

templates = Jinja2Templates(directory="app/templates")

# {{ entry.created_at | vn('%H:%M %d/%m/%Y') }} — luôn hiển thị theo giờ địa phương
templates.env.filters["vn"] = vn
templates.env.globals["now_local"] = now_local
