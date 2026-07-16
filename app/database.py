import os
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./labtool.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

# Enable WAL mode for SQLite (better concurrent read performance)
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def set_wal_mode(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
        dbapi_conn.execute("PRAGMA foreign_keys=ON")


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def sync_schema():
    """Tự thêm cột còn thiếu vào bảng đã tồn tại.

    Dự án này không dùng Alembic — Base.metadata.create_all() chỉ tạo bảng MỚI,
    không tự ALTER bảng đã có sẵn khi model thêm cột. Gọi hàm này sau create_all()
    ở mỗi lần khởi động app để không ai phải nhớ chạy ALTER TABLE thủ công.

    Cột mới được backfill bằng giá trị `default=` khai báo trong model (nếu là giá
    trị tĩnh, VD default=True/"researcher") ngay sau khi ALTER TABLE — tránh để dữ
    liệu cũ bị NULL rồi âm thầm đổi hành vi ở nơi code đọc cột đó (VD một cờ quyền
    boolean NULL bị coi là falsy, vô tình khoá quyền của toàn bộ tài khoản có từ
    trước khi thêm cột).

    Giới hạn: chỉ xử lý được cột MỚI. Đổi kiểu cột, đổi tên cột, hay xoá cột vẫn
    cần can thiệp thủ công — những thay đổi đó hiếm và rủi ro cao nên cố tình
    không tự động hoá.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # bảng mới toàn bộ — create_all() đã lo
            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                col_type = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}'))
                # ASCII-only: console Windows mac dinh dung cp1252, print() tieng Viet co dau se crash
                print(f"[sync_schema] added missing column: {table.name}.{col.name}")

                default = col.default
                if default is not None and getattr(default, "is_scalar", False):
                    conn.execute(
                        text(f'UPDATE "{table.name}" SET "{col.name}" = :val WHERE "{col.name}" IS NULL'),
                        {"val": default.arg},
                    )
                    print(f"[sync_schema] backfilled default for {table.name}.{col.name}")


def recover_interrupted_jobs():
    """Đánh dấu lại các job phân tích AI (video, báo cáo) bị kẹt ở trạng thái "đang xử lý"
    do server tắt/khởi động lại giữa chừng lần chạy trước.

    Trạng thái "processing"/"running" chỉ được cập nhật tiếp bởi chính luồng nền
    (threading.Thread) đã tạo ra nó — nếu tiến trình chết giữa chừng (deploy, crash, mất
    điện...), luồng đó biến mất cùng tiến trình và không ai cập nhật lại nữa, khiến bản ghi
    kẹt "đang xử lý" vĩnh viễn và không route nào cho phép chạy lại (để tránh 2 luồng đá
    nhau). Gọi hàm này mỗi lần khởi động, TRƯỚC khi có request nào tới, để chuyển các bản
    ghi kẹt từ lần chạy trước thành "lỗi" — cho phép admin bấm Retry/Phân tích lại bình
    thường qua giao diện thay vì phải sửa thẳng database.
    """
    from app.models import Video, MonthlyReport  # import trễ để tránh vòng lặp import với models.py

    db = SessionLocal()
    try:
        stuck_videos = db.query(Video).filter(Video.status == "processing").all()
        for v in stuck_videos:
            v.status = "failed"
            v.error_message = "Bị gián đoạn do hệ thống khởi động lại — nhấn Retry để phân tích lại."

        stuck_reports = db.query(MonthlyReport).filter(MonthlyReport.ai_status == "running").all()
        for r in stuck_reports:
            r.ai_status = "failed"
            r.ai_analysis = "Bị gián đoạn do hệ thống khởi động lại — nhấn 'Phân tích lại' để thử lại."

        if stuck_videos or stuck_reports:
            db.commit()
            print(f"[recover] Da danh dau {len(stuck_videos)} video va {len(stuck_reports)} "
                  f"bao cao bi ket 'dang xu ly' thanh 'loi' do restart truoc do.")
    finally:
        db.close()


# ── Tài khoản admin mặc định ─────────────────────────────────────────────────
# Deploy mới hoàn toàn = database trống (labtool.db nằm trong .gitignore, không theo
# codebase lên Git) — nếu không có ai tạo sẵn tài khoản admin thì không cách nào đăng
# nhập vào hệ thống lần đầu. Hàm này chạy mỗi lần khởi động: CHƯA có tài khoản với
# email này thì tạo mới; ĐÃ có rồi thì không đụng gì cả — đặc biệt là không ghi đè
# mật khẩu, để admin đổi mật khẩu riêng xong không bị reset ngược mỗi lần restart.
#
# [!] Đổi mật khẩu NGAY sau lần đăng nhập đầu tiên (menu góc phải → Hồ sơ) — mật khẩu
#     mặc định này nằm dạng đọc được trong mã nguồn, ai xem code đều biết.
DEFAULT_ADMIN_EMAIL = "ntsinh0409@gmail.com"
DEFAULT_ADMIN_PASSWORD = "Admin123"


def backfill_experiment_dates():
    """Bản ghi nhật ký tạo trước khi có trường 'ngày thí nghiệm' sẽ để trống — lấp bằng
    created_at (giả định ghi trong ngày làm) để timeline và sắp xếp không vướng giá trị rỗng.
    Idempotent: chạy lại các lần sau không đụng gì vì không còn dòng nào trống.
    """
    from app.models import DailyLog

    db = SessionLocal()
    try:
        rows = db.query(DailyLog).filter(DailyLog.experiment_date == None).all()
        if not rows:
            return
        for r in rows:
            r.experiment_date = r.created_at
        db.commit()
        print(f"[backfill] Da dat experiment_date = created_at cho {len(rows)} nhat ky cu.")
    finally:
        db.close()


def seed_integrity_chain():
    """Khởi tạo chuỗi hash toàn vẹn cho nhật ký đã có sẵn (chỉ chạy lần đầu, khi chuỗi rỗng).
    Từ mốc này trở đi mọi tạo/sửa/xoá qua ứng dụng đều để lại mắt xích. Xem app/integrity.py."""
    from app import integrity

    db = SessionLocal()
    try:
        n = integrity.seed_existing(db)
        if n:
            print(f"[integrity] Da khoi tao chuoi hash cho {n} nhat ky co san.")
    finally:
        db.close()


def ensure_default_admin():
    from app.models import User
    from app.auth import hash_password

    db = SessionLocal()
    try:
        if db.query(User).filter(User.email == DEFAULT_ADMIN_EMAIL).first():
            return  # tai khoan da ton tai — khong dung vao mat khau
        db.add(User(
            email=DEFAULT_ADMIN_EMAIL,
            password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),
            full_name="Quản Trị Viên",
            role="admin",
            member_type="researcher",
            is_active=True,
            is_approved=True,
            can_create_project=True,
        ))
        db.commit()
        print(f"[init] Da tao tai khoan admin mac dinh '{DEFAULT_ADMIN_EMAIL}' "
              f"— hay doi mat khau ngay sau lan dang nhap dau tien!")
    finally:
        db.close()
