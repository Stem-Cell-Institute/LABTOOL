"""Rate limiting và login lockout — in-memory, phù hợp cho hệ thống LAN nhỏ."""
import re
import secrets
import string
import threading
from collections import defaultdict
from datetime import datetime, timedelta

_lock = threading.Lock()
_failed_logins: dict  = defaultdict(list)   # ip -> [datetime, ...]
_reg_attempts: dict   = defaultdict(list)   # ip -> [datetime, ...]
_status_checks: dict  = defaultdict(list)   # ip -> [datetime, ...]
_reset_requests: dict = defaultdict(list)   # ip -> [datetime, ...]

LOGIN_MAX     = 5    # số lần sai tối đa
LOGIN_WINDOW  = 15   # phút khoá sau khi vượt ngưỡng
REG_MAX       = 5    # số lần đăng ký tối đa
REG_WINDOW    = 60   # phút cho cửa sổ đăng ký
STATUS_MAX    = 10   # số lần kiểm tra trạng thái tối đa — chặn dò email tồn tại
STATUS_WINDOW = 15   # phút cho cửa sổ kiểm tra trạng thái
RESET_MAX     = 5    # số lần gửi yêu cầu quên mật khẩu tối đa
RESET_WINDOW  = 60   # phút cho cửa sổ yêu cầu quên mật khẩu


def _prune(lst: list, window_min: int) -> list:
    cutoff = datetime.utcnow() - timedelta(minutes=window_min)
    return [t for t in lst if t > cutoff]


# ── Login lockout ─────────────────────────────────────────────────────────────

def is_login_locked(ip: str) -> tuple[bool, int]:
    """(bị_khoá, còn_bao_nhiêu_giây)"""
    with _lock:
        attempts = _prune(_failed_logins[ip], LOGIN_WINDOW)
        _failed_logins[ip] = attempts
        if len(attempts) >= LOGIN_MAX:
            unlock_at = attempts[0] + timedelta(minutes=LOGIN_WINDOW)
            remaining = int((unlock_at - datetime.utcnow()).total_seconds())
            return True, max(remaining, 0)
        return False, 0


def record_failed_login(ip: str):
    with _lock:
        _failed_logins[ip].append(datetime.utcnow())


def clear_failed_logins(ip: str):
    with _lock:
        _failed_logins.pop(ip, None)


# ── Registration rate limit ───────────────────────────────────────────────────

def is_reg_limited(ip: str) -> bool:
    with _lock:
        attempts = _prune(_reg_attempts[ip], REG_WINDOW)
        _reg_attempts[ip] = attempts
        return len(attempts) >= REG_MAX


def record_reg_attempt(ip: str):
    with _lock:
        _reg_attempts[ip].append(datetime.utcnow())


# ── Check-status rate limit ───────────────────────────────────────────────────

def is_status_check_limited(ip: str) -> bool:
    with _lock:
        attempts = _prune(_status_checks[ip], STATUS_WINDOW)
        _status_checks[ip] = attempts
        return len(attempts) >= STATUS_MAX


def record_status_check(ip: str):
    with _lock:
        _status_checks[ip].append(datetime.utcnow())


# ── Quên mật khẩu rate limit ──────────────────────────────────────────────────

def is_reset_limited(ip: str) -> bool:
    with _lock:
        attempts = _prune(_reset_requests[ip], RESET_WINDOW)
        _reset_requests[ip] = attempts
        return len(attempts) >= RESET_MAX


def record_reset_attempt(ip: str):
    with _lock:
        _reset_requests[ip].append(datetime.utcnow())


# ── Password strength ─────────────────────────────────────────────────────────

def check_password(password: str) -> str | None:
    """None = OK, string = thông báo lỗi."""
    if len(password) < 8:
        return "Mật khẩu phải có ít nhất 8 ký tự."
    if len(password.encode("utf-8")) > 72:
        # bcrypt chỉ xử lý tối đa 72 byte — vượt quá sẽ lỗi khi hash thay vì báo rõ ràng.
        return "Mật khẩu quá dài (tối đa 72 ký tự)."
    has_upper  = bool(re.search(r'[A-Z]', password))
    has_digit  = bool(re.search(r'\d',    password))
    if not has_upper and not has_digit:
        return "Mật khẩu phải chứa ít nhất 1 chữ hoa (A-Z) hoặc 1 chữ số (0-9)."
    return None


def generate_temp_password(length: int = 10) -> str:
    """Sinh mật khẩu tạm ngẫu nhiên, luôn đạt chuẩn check_password (có hoa và số)."""
    alphabet = string.ascii_letters + string.digits
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isupper() for c in pwd) and any(c.isdigit() for c in pwd):
            return pwd


def generate_invite_token() -> str:
    return secrets.token_urlsafe(32)
