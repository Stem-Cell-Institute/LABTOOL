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
