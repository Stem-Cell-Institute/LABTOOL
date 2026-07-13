from datetime import datetime
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
    username = Column(String(60), unique=True, nullable=False, index=True)
    password_hash = Column(String(200), nullable=False)
    full_name = Column(String(120), default="")
    email = Column(String(200), default="")
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

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)  # người sửa lần cuối (có thể khác author)

    author      = relationship("User", foreign_keys=[user_id])
    last_editor = relationship("User", foreign_keys=[updated_by])
    group       = relationship("Group")
    project     = relationship("Project", back_populates="logs")
    notebook    = relationship("Notebook", back_populates="logs")
    files       = relationship("DailyLogFile", back_populates="log", cascade="all, delete-orphan")
    revisions   = relationship("DailyLogRevision", back_populates="log", cascade="all, delete-orphan",
                               order_by="DailyLogRevision.edited_at.desc()")


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
