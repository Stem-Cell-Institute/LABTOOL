import bcrypt
from fastapi import Request, HTTPException, Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User

# Dùng bcrypt TRỰC TIẾP, không qua passlib: passlib 1.7.4 (bản 2020, không còn bảo trì)
# hỏng với bcrypt >= 4.1 trên Python mới — lỗi "module 'bcrypt' has no attribute '__about__'"
# rồi crash khi khởi động. Hash tạo ở đây vẫn là chuẩn bcrypt $2b$, tương thích ngược với
# các mật khẩu đã lưu bằng passlib trước đó.
#
# bcrypt chỉ xử lý tối đa 72 byte — cắt bớt để không ném ValueError làm sập app; giới hạn
# này đã được báo cho người dùng ở tầng đăng ký/đổi mật khẩu (xem check_password).


def hash_password(password: str) -> str:
    pw = password.encode("utf-8")[:72]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Chưa đăng nhập")
    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Phiên đăng nhập không hợp lệ")
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Chỉ quản trị viên mới có quyền này")
    return current_user
