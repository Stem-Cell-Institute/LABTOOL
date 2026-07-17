from datetime import datetime, timedelta
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Boolean,
    ForeignKey, BigInteger, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from app.database import Base


class Group(Base):
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), unique=True, nullable=False)
    description = Column(Text, default="")
    folder_path = Column(String(500), default="")  # server path for folder scanning
    created_at = Column(DateTime, default=datetime.utcnow)

    users = relationship("User", back_populates="group")
    videos = relationship("Video", back_populates="group")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    password_hash = Column(String(200), nullable=False)
    full_name = Column(String(120), default="")
    email = Column(String(200), unique=True, nullable=False, index=True)
    role = Column(String(20), default="member")  # "admin" | "member" — QUYỀN hệ thống
    member_type = Column(String(20), default="researcher")  # "researcher" | "student" | "ncs" — CHỨC DANH, độc lập với role
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=True)
    is_active = Column(Boolean, default=True)
    is_approved = Column(Boolean, default=False)   # False = chờ admin duyệt
    can_view_all = Column(Boolean, default=False)  # xem báo cáo toàn cảnh (không cần là admin)
    # NCV mặc định được; Sinh viên/NCS mặc định KHÔNG — nhưng admin cấp riêng theo từng người
    # (VD: NCS được Viện cho phép hoạt động tự do như NCV) nên đây là cờ riêng, không suy ra cứng từ member_type.
    can_create_project = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    group = relationship("Group", back_populates="users")
    videos = relationship("Video", back_populates="uploader")
    comments = relationship("Comment", back_populates="author")


class PasswordResetRequest(Base):
    """Yêu cầu đặt lại mật khẩu do người dùng gửi khi quên mật khẩu và không đăng nhập được.
    Hệ thống chưa có gửi email (không cấu hình SMTP) nên admin duyệt thủ công: tạo mật khẩu
    tạm thời rồi báo cho người dùng qua kênh khác (Zalo/gặp trực tiếp)."""
    __tablename__ = "password_reset_requests"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(String(20), default="pending")  # pending|approved|rejected
    requested_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)
    resolved_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    resolver = relationship("User", foreign_keys=[resolved_by])


class UserInvite(Base):
    """Lời mời tạo tài khoản do admin gửi cho người chưa dùng hệ thống (VD: Viện trưởng) —
    thay vì bắt họ tự đăng ký rồi chờ duyệt. Admin tạo lời mời, copy link gửi qua kênh khác
    (Zalo, tin nhắn...) vì hệ thống chưa gửi email tự động. Người được mời mở link tự đặt
    mật khẩu riêng — admin không bao giờ biết mật khẩu của họ."""
    __tablename__ = "user_invites"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    email = Column(String(200), nullable=False)
    full_name = Column(String(120), default="")
    role = Column(String(20), default="member")            # "admin" | "member"
    can_view_all = Column(Boolean, default=False)           # Viện trưởng / BGĐ
    member_type = Column(String(20), default="researcher")
    can_create_project = Column(Boolean, default=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=True)
    status = Column(String(20), default="pending")   # pending|accepted|revoked
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    expires_at = Column(DateTime, nullable=True)
    accepted_at = Column(DateTime, nullable=True)

    group = relationship("Group")
    creator = relationship("User", foreign_keys=[created_by])


class Video(Base):
    __tablename__ = "videos"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False)
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    filename = Column(String(500), nullable=False)       # server-side path
    original_name = Column(String(500), nullable=False)  # original file name
    file_size = Column(BigInteger, default=0)
    status = Column(String(20), default="pending")  # pending|processing|done|failed
    error_message = Column(Text, default="")
    source = Column(String(20), default="upload")   # "upload" | "folder"
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    report_month = Column(Integer, nullable=True)   # 1-12: kỳ báo cáo tháng
    report_year = Column(Integer, nullable=True)    # e.g., 2025
    gemini_file_name = Column(String(200), nullable=True)  # Gemini Files API name để tái dùng

    group = relationship("Group", back_populates="videos")
    uploader = relationship("User", back_populates="videos")
    log = relationship("ExperimentLog", back_populates="video", uselist=False)


class ExperimentLog(Base):
    __tablename__ = "experiment_logs"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"), unique=True, nullable=False)
    title = Column(String(300), default="")
    content = Column(Text, nullable=False)
    ai_model = Column(String(60), default="gemini-2.5-flash")
    created_at = Column(DateTime, default=datetime.utcnow)

    video = relationship("Video", back_populates="log")
    comments = relationship("Comment", back_populates="log", cascade="all, delete-orphan")
    questions = relationship("VideoQuestion", back_populates="log", cascade="all, delete-orphan",
                             order_by="VideoQuestion.created_at")


class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, index=True)
    log_id = Column(Integer, ForeignKey("experiment_logs.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    log = relationship("ExperimentLog", back_populates="comments")
    author = relationship("User", back_populates="comments")


class MonthlyReport(Base):
    """Báo cáo kết quả nghiên cứu hằng tháng do nghiên cứu viên nộp.
    Không còn UniqueConstraint(user_id, report_month, report_year) ở tầng DB — ràng buộc
    "1 báo cáo/tháng" nay chỉ áp dụng ở tầng ứng dụng (results.py) và được bỏ qua cho admin
    để admin có thể nộp thử nhiều lần trong cùng tháng khi test hệ thống."""
    __tablename__ = "monthly_reports"

    id = Column(Integer, primary_key=True, index=True)
    user_id   = Column(Integer, ForeignKey("users.id"), nullable=False)
    group_id  = Column(Integer, ForeignKey("groups.id"), nullable=True)
    report_month = Column(Integer, nullable=False)   # 1-12
    report_year  = Column(Integer, nullable=False)   # e.g., 2026

    # Nội dung nghiên cứu viên tự viết (Markdown)
    content   = Column(Text, default="")
    status    = Column(String(20), default="draft")  # draft|submitted|reviewed
    submitted_at = Column(DateTime, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow)

    # Kết quả phân tích AI
    ai_analysis  = Column(Text, default="")   # Markdown đầy đủ từ Gemini
    ai_novelty   = Column(Integer, nullable=True)     # Điểm tiêu chí 1 (backward compat)
    ai_performance = Column(Integer, nullable=True)   # Điểm tiêu chí 2 (backward compat)
    ai_scores_json = Column(Text, default="")         # JSON: [{"label":"...","score":4}, ...]
    ai_verdict   = Column(String(30), default="")     # excellent|approved|warning|salary_cut
    ai_status    = Column(String(20), default="pending")  # pending|running|done|failed

    # Quyết định của quản lý (ghi đè AI nếu cần)
    manager_decision = Column(String(30), nullable=True)  # excellent|approved|warning|salary_cut
    manager_note     = Column(Text, default="")
    reviewed_by  = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_at  = Column(DateTime, nullable=True)

    author   = relationship("User", foreign_keys=[user_id])
    reviewer = relationship("User", foreign_keys=[reviewed_by])
    group    = relationship("Group")
    files    = relationship("ReportFile", back_populates="report", cascade="all, delete-orphan")


class ReportFile(Base):
    """File đính kèm cho MonthlyReport (ảnh, PDF, Excel...)."""
    __tablename__ = "report_files"

    id          = Column(Integer, primary_key=True, index=True)
    report_id   = Column(Integer, ForeignKey("monthly_reports.id"), nullable=False)
    filename    = Column(String(500), nullable=False)   # server path
    original_name = Column(String(500), nullable=False)
    file_type   = Column(String(20), default="other")   # image|pdf|doc|other
    file_size   = Column(BigInteger, default=0)
    file_hash   = Column(String(64), default="")   # SHA-256 — phát hiện tái sử dụng minh chứng cũ giữa các tháng/NCV
    phash       = Column(String(20), default="")   # perceptual hash (chỉ ảnh) — phát hiện ảnh bị crop/xoay/nén lại để né SHA-256
    uploaded_at = Column(DateTime, default=datetime.utcnow)

    report = relationship("MonthlyReport", back_populates="files")


class AICalibrationExample(Base):
    """Ví dụ hiệu chỉnh dùng để dạy AI chấm báo cáo tháng chính xác hơn theo thời gian.
    Tự động tạo khi Ban quản lý sửa quyết định khác với đề xuất AI (kèm lý do bắt buộc);
    admin cũng có thể thêm thủ công. Chỉ ví dụ is_active=True mới được đưa vào prompt."""
    __tablename__ = "ai_calibration_examples"

    id = Column(Integer, primary_key=True, index=True)
    report_id = Column(Integer, ForeignKey("monthly_reports.id"), nullable=True)
    report_month = Column(Integer, nullable=True)
    report_year  = Column(Integer, nullable=True)
    researcher_name = Column(String(150), default="")
    context_excerpt = Column(Text, default="")        # trích bối cảnh báo cáo
    ai_verdict      = Column(String(30), default="")  # AI đã đề xuất gì (rỗng nếu ví dụ thủ công)
    correct_verdict = Column(String(30), nullable=False)
    reason          = Column(Text, nullable=False)    # lý do hiệu chỉnh — tín hiệu học chính
    source          = Column(String(20), default="review_correction")  # review_correction|manual
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
    created_by      = Column(Integer, ForeignKey("users.id"), nullable=True)

    report  = relationship("MonthlyReport")
    creator = relationship("User", foreign_keys=[created_by])


class VideoQuestion(Base):
    """Câu hỏi của quản lý gửi cho AI về nội dung video báo cáo."""
    __tablename__ = "video_questions"

    id = Column(Integer, primary_key=True, index=True)
    log_id = Column(Integer, ForeignKey("experiment_logs.id"), nullable=False)
    asked_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    question = Column(Text, nullable=False)
    answer = Column(Text, default="")
    status = Column(String(20), default="pending")  # pending|done|failed
    created_at = Column(DateTime, default=datetime.utcnow)

    log = relationship("ExperimentLog", back_populates="questions")
    asker = relationship("User", foreign_keys=[asked_by])


class ReportPeriod(Base):
    """Kỳ nộp báo cáo kết quả — admin mở/đóng theo tháng."""
    __tablename__ = "report_periods"
    __table_args__ = (
        UniqueConstraint("report_month", "report_year", name="uq_period_month_year"),
    )

    id           = Column(Integer, primary_key=True)
    report_month = Column(Integer, nullable=False)
    report_year  = Column(Integer, nullable=False)
    deadline     = Column(DateTime, nullable=True)   # hạn cuối nộp
    is_open      = Column(Boolean, default=True)
    auto_closed  = Column(Boolean, default=False)    # True nếu đóng tự động qua nút
    closed_at    = Column(DateTime, nullable=True)
    closed_by    = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    created_by   = Column(Integer, ForeignKey("users.id"), nullable=True)

    closer  = relationship("User", foreign_keys=[closed_by])
    creator = relationship("User", foreign_keys=[created_by])


class SystemConfig(Base):
    """Cài đặt hệ thống dạng key-value, chỉnh sửa được qua admin UI."""
    __tablename__ = "system_config"

    key        = Column(String(100), primary_key=True)
    value      = Column(Text, default="")
    updated_at = Column(DateTime, default=datetime.utcnow)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)


class GeneralInfoMixin:
    """PHẦN I — THÔNG TIN CHUNG: các trường dùng chung cho Project và Notebook
    (trang bìa sổ tay thí nghiệm theo mẫu của Viện). Chỉ tên đề tài là bắt buộc,
    còn lại đều tuỳ chọn — điền sau cũng được."""
    topic_code      = Column(String(100), default="")   # Mã số đề tài (nếu có)
    researcher_name = Column(String(150), default="")   # Họ và tên người thực hiện
    student_id      = Column(String(100), default="")   # Mã số sinh viên/học viên/NCS
    class_info      = Column(String(200), default="")   # Lớp/Khóa/Khoa/Viện
    supervisor      = Column(String(150), default="")   # Người hướng dẫn
    co_supervisor   = Column(String(150), default="")   # Đồng hướng dẫn (nếu có)
    start_date      = Column(DateTime, nullable=True)    # Ngày bắt đầu
    end_date        = Column(DateTime, nullable=True)    # Ngày kết thúc


class Project(GeneralInfoMixin, Base):
    """Project nghiên cứu — 1 nghiên cứu viên tạo và làm chủ (owner), có thể thêm
    nhiều nghiên cứu viên khác vào làm thành viên. Nhật ký thí nghiệm của thành viên
    có thể (không bắt buộc) gắn vào 1 project để cả nhóm cùng xem tiến độ."""
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)  # = Tên đề tài/dự án
    description = Column(Text, default="")
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    parent_id = Column(Integer, ForeignKey("projects.id"), nullable=True)  # đề tài nhánh: gắn vào project cha
    created_at = Column(DateTime, default=datetime.utcnow)

    # Lưu trữ ("cất đi") — KHÁC hẳn xoá: ẩn khỏi danh sách nhưng giữ nguyên mọi thứ và mở lại
    # được bất cứ lúc nào. Nhật ký vẫn gắn nguyên vào project nên thành viên KHÔNG mất quyền xem.
    # Đây là lối đi an toàn cho việc "đề tài xong rồi, cất lại" — không cần đụng tới xoá.
    archived_at = Column(DateTime, nullable=True)
    archived_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    owner    = relationship("User", foreign_keys=[owner_id])
    parent   = relationship("Project", remote_side=[id], back_populates="children")
    children = relationship("Project", back_populates="parent")
    members  = relationship("ProjectMember", back_populates="project", cascade="all, delete-orphan")
    logs     = relationship("DailyLog", back_populates="project")

    @property
    def full_path_name(self) -> str:
        """Tên đầy đủ theo chuỗi phân cấp, từ project gốc tới nhánh nhỏ nhất.
        VD: 'Đề tài lớn › Nhánh 1 › Nhánh con 1.1'."""
        parts = [self.name]
        seen = {self.id}
        p = self.parent
        while p is not None and p.id not in seen:
            parts.append(p.name)
            seen.add(p.id)
            p = p.parent
        return " › ".join(reversed(parts))


class Notebook(GeneralInfoMixin, Base):
    """Sổ tay thí nghiệm cá nhân — chứa 'Thông tin chung' cho nhật ký thí nghiệm
    KHÔNG thuộc project nào. 1 người có thể tạo nhiều sổ tay theo thời gian
    (VD: mỗi học kỳ/đề tài 1 sổ tay riêng, mỗi sổ có số/ngày tháng riêng)."""
    __tablename__ = "notebooks"

    id = Column(Integer, primary_key=True, index=True)
    topic_name = Column(String(300), nullable=False)  # Tên đề tài/dự án
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", foreign_keys=[owner_id])
    logs  = relationship("DailyLog", back_populates="notebook")


class ProjectMember(Base):
    """Thành viên của 1 project (owner cũng có 1 dòng ở đây để việc kiểm tra
    'có phải thành viên không' luôn nhất quán)."""
    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_user"),
    )

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    added_at   = Column(DateTime, default=datetime.utcnow)
    added_by   = Column(Integer, ForeignKey("users.id"), nullable=True)

    project = relationship("Project", back_populates="members")
    user    = relationship("User", foreign_keys=[user_id])
    adder   = relationship("User", foreign_keys=[added_by])


class ProjectMessage(Base):
    """Tin nhắn thảo luận chung giữa các thành viên của 1 project (chat nhóm).
    Chỉ người xem được project (thành viên, chủ đề tài cha, admin) mới đọc/gửi."""
    __tablename__ = "project_messages"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    content    = Column(Text, default="")     # cho phép rỗng: tin chỉ gửi ảnh/tệp
    created_at = Column(DateTime, default=datetime.utcnow)

    user  = relationship("User", foreign_keys=[user_id])
    files = relationship("ProjectMessageFile", back_populates="message", cascade="all, delete-orphan")


class ProjectMessageFile(Base):
    """Ảnh/tệp đính kèm trong chat nhóm của project."""
    __tablename__ = "project_message_files"

    id = Column(Integer, primary_key=True, index=True)
    message_id    = Column(Integer, ForeignKey("project_messages.id"), nullable=False, index=True)
    filename      = Column(String(500), nullable=False)   # đường dẫn trên server
    original_name = Column(String(500), nullable=False)
    file_type     = Column(String(20), default="other")   # image | pdf | doc | other
    file_size     = Column(BigInteger, default=0)

    message = relationship("ProjectMessage", back_populates="files")


class ProjectChatRead(Base):
    """Mốc 'đã đọc tới đâu' của từng người trong chat nhóm của 1 project — để đếm tin chưa đọc.

    Bảng riêng (không gắn vào ProjectMember) vì chat project mở cho cả người KHÔNG phải thành
    viên trực tiếp: chủ đề tài cha giám sát và admin cũng đọc/gửi được.
    """
    __tablename__ = "project_chat_reads"
    __table_args__ = (UniqueConstraint("project_id", "user_id", name="uq_project_chat_read"),)

    id = Column(Integer, primary_key=True, index=True)
    project_id   = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    last_read_id = Column(Integer, default=0)


class ProjectDiaryRead(Base):
    """Mốc 'đã xem nhật ký tới đâu' của từng người trong 1 project — để báo có nhật ký mới.

    Tách khỏi ProjectChatRead vì là 2 loại thông báo độc lập: đọc hết tin nhắn không có nghĩa
    là đã xem nhật ký mới, và ngược lại.
    """
    __tablename__ = "project_diary_reads"
    __table_args__ = (UniqueConstraint("project_id", "user_id", name="uq_project_diary_read"),)

    id = Column(Integer, primary_key=True, index=True)
    project_id   = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    last_read_id = Column(Integer, default=0)


# ── Nhắn tin (DM 1-1 + nhóm chat tuỳ chọn) ─────────────────────────────────────
# Độc lập với project: người dùng nhắn riêng nhau hoặc tự lập nhóm gồm thành viên bất kỳ.
# Khác với ProjectMessage (chat gắn cứng vào 1 project) ở trên.

class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    type = Column(String(10), default="dm")          # "dm" (1-1) | "group"
    title = Column(String(200), default="")          # tên nhóm; DM để trống (suy ra từ 2 người)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_at = Column(DateTime, default=datetime.utcnow)  # thời điểm tin cuối — để sắp xếp danh sách

    members = relationship("ConversationMember", back_populates="conversation", cascade="all, delete-orphan")


class ConversationMember(Base):
    __tablename__ = "conversation_members"
    __table_args__ = (UniqueConstraint("conversation_id", "user_id", name="uq_conv_user"),)

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    last_read_id = Column(Integer, default=0)         # id tin nhắn cuối đã đọc — tính số chưa đọc
    joined_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("Conversation", back_populates="members")
    user = relationship("User", foreign_keys=[user_id])


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    edited_at = Column(DateTime, nullable=True)       # có giá trị nếu đã sửa
    is_deleted = Column(Boolean, default=False)       # xoá mềm — hiện "đã thu hồi"

    user  = relationship("User", foreign_keys=[user_id])
    files = relationship("ChatFile", back_populates="message", cascade="all, delete-orphan")


class ChatFile(Base):
    """Ảnh/tệp đính kèm trong tin nhắn. Tách bảng riêng để 1 tin gửi được nhiều tệp."""
    __tablename__ = "chat_files"

    id = Column(Integer, primary_key=True, index=True)
    message_id    = Column(Integer, ForeignKey("chat_messages.id"), nullable=False, index=True)
    filename      = Column(String(500), nullable=False)   # đường dẫn trên server
    original_name = Column(String(500), nullable=False)
    file_type     = Column(String(20), default="other")   # image | pdf | doc | other
    file_size     = Column(BigInteger, default=0)

    message = relationship("ChatMessage", back_populates="files")


class DailyLog(Base):
    """Nhật ký thí nghiệm — nghiên cứu viên tự ghi, nhiều bản/ngày.
    Bị khoá vĩnh viễn (kể cả với admin) sau N ngày kể từ lúc tạo, theo cấu hình
    'daily_log_lock_days' (SystemConfig), để đảm bảo tính liêm chính khoa học.
    """
    __tablename__ = "daily_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id  = Column(Integer, ForeignKey("users.id"), nullable=False)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)  # tuỳ chọn — không bắt buộc
    notebook_id = Column(Integer, ForeignKey("notebooks.id"), nullable=True)  # tuỳ chọn — nhật ký đơn lẻ, không thuộc project
    title    = Column(String(300), default="")
    content  = Column(Text, nullable=False)

    # Ngày THỰC HIỆN thí nghiệm — khác created_at (lúc bấm lưu). NCV thường làm xong rồi tối
    # hoặc hôm sau mới ghi, nên timeline phải theo ngày làm THẬT mới đúng. Giữ cả 2 mốc:
    # chênh lệch giữa chúng chính là thông tin liêm chính (ghi muộn bao lâu).
    experiment_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)  # người sửa lần cuối (có thể khác author)

    @property
    def log_date(self):
        """Ngày dùng để hiển thị/nhóm theo dòng thời gian — ưu tiên ngày thí nghiệm,
        lùi về created_at cho bản ghi cũ chưa có dữ liệu này."""
        return self.experiment_date or self.created_at

    @property
    def logged_late_days(self) -> int:
        """Số ngày ghi muộn so với ngày làm thí nghiệm (0 nếu ghi trong ngày).
        So theo NGÀY ĐỊA PHƯƠNG: cả 2 mốc lưu bằng UTC, nếu so ngày UTC thì việc ghi lúc
        7-12h tối giờ VN đã sang ngày UTC khác -> báo 'ghi muộn 1 ngày' oan."""
        if not self.experiment_date:
            return 0
        from app.timeutil import local_date
        return max(0, (local_date(self.created_at) - local_date(self.experiment_date)).days)

    @property
    def day_label(self) -> str:
        """Nhãn ngày cho dòng thời gian — kèm 'Hôm nay/Hôm qua' để định vị nhanh, nhưng LUÔN
        giữ ngày cụ thể bên cạnh để không mơ hồ khi in ra hay đọc lại sau này."""
        from app.timeutil import local_date, local_today
        d = local_date(self.log_date)
        today = local_today()
        if d == today:
            return f"Hôm nay — {d.strftime('%d/%m/%Y')}"
        if d == today - timedelta(days=1):
            return f"Hôm qua — {d.strftime('%d/%m/%Y')}"
        return d.strftime("%d/%m/%Y")

    author      = relationship("User", foreign_keys=[user_id])
    last_editor = relationship("User", foreign_keys=[updated_by])
    group       = relationship("Group")
    project     = relationship("Project", back_populates="logs")
    notebook    = relationship("Notebook", back_populates="logs")
    files       = relationship("DailyLogFile", back_populates="log", cascade="all, delete-orphan")
    revisions   = relationship("DailyLogRevision", back_populates="log", cascade="all, delete-orphan",
                               order_by="DailyLogRevision.edited_at.desc()")


class IntegrityRecord(Base):
    """Chuỗi hash chống sửa lén nhật ký (append-only, không bao giờ sửa/xoá dòng nào).

    VÌ SAO CẦN: khoá-sau-N-ngày chỉ chặn ở tầng ứng dụng. Bất kỳ ai vào được server/DB đều
    sửa thẳng bảng daily_logs bằng SQL mà không để lại dấu vết. Mỗi lần tạo/sửa/xoá nhật ký
    qua ứng dụng, ta ghi thêm 1 mắt xích ở đây:
      - content_hash: băm nội dung nhật ký tại thời điểm đó
      - prev_hash   : record_hash của mắt xích ngay trước → nối thành chuỗi
      - record_hash : băm chính mắt xích này (gồm cả prev_hash)
    Sửa lén nội dung trong DB -> content_hash không khớp khi kiểm tra.
    Xoá/sửa mắt xích -> chuỗi đứt, phát hiện được.

    GIỚI HẠN THẬT: người có quyền DB + đọc được mã nguồn vẫn có thể tính lại TOÀN BỘ chuỗi để
    che dấu vết, vì không có khoá bí mật/neo ngoài hệ thống. Cách khắc phục rẻ tiền: định kỳ
    ghi lại 'hash mới nhất' ra nơi ngoài server (in ra, gửi email, sổ giấy) — sau này đối chiếu.
    """
    __tablename__ = "integrity_records"

    id = Column(Integer, primary_key=True, index=True)          # cũng là số thứ tự mắt xích
    log_id       = Column(Integer, nullable=False, index=True)  # KHÔNG dùng ForeignKey: nhật ký bị xoá thì mắt xích vẫn phải còn
    event        = Column(String(10), nullable=False)           # create | edit | delete
    content_hash = Column(String(64), nullable=False)
    prev_hash    = Column(String(64), default="")
    record_hash  = Column(String(64), nullable=False)
    actor_id     = Column(Integer, nullable=True)               # ai gây ra thay đổi
    created_at   = Column(DateTime, default=datetime.utcnow)


class DailyLogRevision(Base):
    """Lịch sử chỉnh sửa DailyLog — mỗi lần sửa lưu lại nội dung TRƯỚC và SAU khi sửa,
    ai sửa, khi nào — để admin/nghiên cứu viên tra cứu ai đã đổi những gì trước lúc khoá."""
    __tablename__ = "daily_log_revisions"

    id = Column(Integer, primary_key=True, index=True)
    log_id    = Column(Integer, ForeignKey("daily_logs.id"), nullable=False)
    edited_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    edited_at = Column(DateTime, default=datetime.utcnow)

    prev_title   = Column(String(300), default="")
    prev_content = Column(Text, default="")
    new_title    = Column(String(300), default="")
    new_content  = Column(Text, default="")

    log    = relationship("DailyLog", back_populates="revisions")
    editor = relationship("User")


class DailyLogFile(Base):
    """File đính kèm cho DailyLog (ảnh, PDF, Excel...)."""
    __tablename__ = "daily_log_files"

    id          = Column(Integer, primary_key=True, index=True)
    log_id      = Column(Integer, ForeignKey("daily_logs.id"), nullable=False)
    filename    = Column(String(500), nullable=False)   # server path
    original_name = Column(String(500), nullable=False)
    file_type   = Column(String(20), default="other")   # image|pdf|doc|other
    category    = Column(String(20), default="other")   # raw|processed|other — dữ liệu thô hay kết quả đã xử lý
    file_size   = Column(BigInteger, default=0)
    uploaded_at = Column(DateTime, default=datetime.utcnow)

    log = relationship("DailyLog", back_populates="files")


class ActivityLog(Base):
    """Ghi lại mọi hành động quan trọng trong hệ thống để admin giám sát."""
    __tablename__ = "activity_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)   # None = system
    action = Column(String(50), nullable=False, index=True)
    # upload | analyze_done | analyze_failed | comment | login | reanalyze | delete | toggle_admin | toggle_active
    target_type = Column(String(30), default="")   # video | log | user | group
    target_id = Column(Integer, nullable=True)
    description = Column(String(500), default="")
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    user = relationship("User", foreign_keys=[user_id])


class DiaryAIQuestion(Base):
    """Câu hỏi ADMIN gửi cho AI để tổng hợp/phân tích TẤT CẢ nhật ký thí nghiệm cùng lúc
    (giống NotebookLM) — chỉ admin hệ thống (không phải quản lý/can_view_all) mới dùng được."""
    __tablename__ = "diary_ai_questions"

    id = Column(Integer, primary_key=True, index=True)
    asked_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    question = Column(Text, nullable=False)
    answer = Column(Text, default="")
    status = Column(String(20), default="pending")  # pending|done|failed
    entry_count = Column(Integer, default=0)  # số nhật ký đã đưa vào ngữ cảnh khi hỏi
    created_at = Column(DateTime, default=datetime.utcnow)

    asker = relationship("User", foreign_keys=[asked_by])
