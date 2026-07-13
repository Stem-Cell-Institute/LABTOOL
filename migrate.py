"""Migration script — chay truoc moi lan khoi dong server de cap nhat DB cu."""
import sqlite3
import os

DB_PATH = "labtool.db"

MONTH_NAMES = ["", "Thang 1", "Thang 2", "Thang 3", "Thang 4", "Thang 5", "Thang 6",
               "Thang 7", "Thang 8", "Thang 9", "Thang 10", "Thang 11", "Thang 12"]


def run():
    if not os.path.exists(DB_PATH):
        print("[migrate] DB chua ton tai, bo qua.")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # --- videos table ---
    cols = {row[1] for row in cur.execute("PRAGMA table_info(videos)").fetchall()}

    if "report_month" not in cols:
        cur.execute("ALTER TABLE videos ADD COLUMN report_month INTEGER")
        cur.execute(
            "UPDATE videos SET report_month = CAST(strftime('%m', uploaded_at) AS INTEGER) "
            "WHERE report_month IS NULL"
        )
        print("[migrate] +report_month (backfill tu uploaded_at)")

    if "report_year" not in cols:
        cur.execute("ALTER TABLE videos ADD COLUMN report_year INTEGER")
        cur.execute(
            "UPDATE videos SET report_year = CAST(strftime('%Y', uploaded_at) AS INTEGER) "
            "WHERE report_year IS NULL"
        )
        print("[migrate] +report_year (backfill tu uploaded_at)")

    # --- users table ---
    cols_u = {row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()}

    if "can_view_all" not in cols_u:
        cur.execute("ALTER TABLE users ADD COLUMN can_view_all INTEGER DEFAULT 0")
        print("[migrate] +can_view_all (users)")

    if "is_approved" not in cols_u:
        # Tài khoản cũ (admin tạo) coi như đã được duyệt
        cur.execute("ALTER TABLE users ADD COLUMN is_approved INTEGER DEFAULT 1")
        print("[migrate] +is_approved (users, backfill=1)")

    # --- videos: gemini_file_name ---
    if "gemini_file_name" not in cols:
        cur.execute("ALTER TABLE videos ADD COLUMN gemini_file_name TEXT")
        print("[migrate] +gemini_file_name (videos)")

    # --- monthly_reports: doi group_id tu NOT NULL sang nullable ---
    cur.execute("PRAGMA foreign_keys = OFF")
    mr_cols = {row[1]: row[3] for row in cur.execute("PRAGMA table_info(monthly_reports)").fetchall()}
    # row[3] = notnull flag (1 = NOT NULL, 0 = nullable)
    if mr_cols.get("group_id", 0) == 1:  # dang NOT NULL, can sua
        cur.executescript("""
            CREATE TABLE monthly_reports_new (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                group_id INTEGER REFERENCES groups(id),
                report_month INTEGER NOT NULL,
                report_year INTEGER NOT NULL,
                content TEXT DEFAULT '',
                status VARCHAR(20) DEFAULT 'draft',
                submitted_at DATETIME,
                created_at DATETIME,
                updated_at DATETIME,
                ai_analysis TEXT DEFAULT '',
                ai_novelty INTEGER,
                ai_performance INTEGER,
                ai_verdict VARCHAR(30) DEFAULT '',
                ai_status VARCHAR(20) DEFAULT 'pending',
                manager_decision VARCHAR(30),
                manager_note TEXT DEFAULT '',
                reviewed_by INTEGER REFERENCES users(id),
                reviewed_at DATETIME,
                UNIQUE (user_id, report_month, report_year)
            );
            INSERT INTO monthly_reports_new SELECT * FROM monthly_reports;
            DROP TABLE monthly_reports;
            ALTER TABLE monthly_reports_new RENAME TO monthly_reports;
        """)
        print("[migrate] monthly_reports: group_id -> nullable")
    cur.execute("PRAGMA foreign_keys = ON")

    # --- get all existing tables ---
    existing_tables = {row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    # --- report_periods table ---
    if "report_periods" not in existing_tables:
        cur.execute("""
            CREATE TABLE report_periods (
                id           INTEGER PRIMARY KEY,
                report_month INTEGER NOT NULL,
                report_year  INTEGER NOT NULL,
                deadline     DATETIME,
                is_open      INTEGER DEFAULT 1,
                auto_closed  INTEGER DEFAULT 0,
                closed_at    DATETIME,
                closed_by    INTEGER REFERENCES users(id),
                created_at   DATETIME,
                created_by   INTEGER REFERENCES users(id),
                UNIQUE (report_month, report_year)
            )
        """)
        print("[migrate] +report_periods table")

    # --- monthly_reports: ai_scores_json ---
    mr_cols2 = {row[1] for row in cur.execute("PRAGMA table_info(monthly_reports)").fetchall()}
    if "ai_scores_json" not in mr_cols2:
        cur.execute("ALTER TABLE monthly_reports ADD COLUMN ai_scores_json TEXT DEFAULT ''")
        print("[migrate] +ai_scores_json (monthly_reports)")

    # --- monthly_reports: bo UNIQUE(user_id, report_month, report_year) ---
    # De admin nop thu nhieu bao cao trong cung 1 thang khi test he thong.
    mr_sql = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='monthly_reports'"
    ).fetchone()
    if mr_sql and "UNIQUE (user_id, report_month, report_year)" in mr_sql[0]:
        cur.execute("PRAGMA foreign_keys = OFF")
        cur.executescript("""
            CREATE TABLE monthly_reports_new (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                group_id INTEGER REFERENCES groups(id),
                report_month INTEGER NOT NULL,
                report_year INTEGER NOT NULL,
                content TEXT DEFAULT '',
                status VARCHAR(20) DEFAULT 'draft',
                submitted_at DATETIME,
                created_at DATETIME,
                updated_at DATETIME,
                ai_analysis TEXT DEFAULT '',
                ai_novelty INTEGER,
                ai_performance INTEGER,
                ai_verdict VARCHAR(30) DEFAULT '',
                ai_status VARCHAR(20) DEFAULT 'pending',
                manager_decision VARCHAR(30),
                manager_note TEXT DEFAULT '',
                reviewed_by INTEGER REFERENCES users(id),
                reviewed_at DATETIME,
                ai_scores_json TEXT DEFAULT ''
            );
            INSERT INTO monthly_reports_new SELECT * FROM monthly_reports;
            DROP TABLE monthly_reports;
            ALTER TABLE monthly_reports_new RENAME TO monthly_reports;
        """)
        cur.execute("PRAGMA foreign_keys = ON")
        print("[migrate] monthly_reports: bo UNIQUE(user_id, report_month, report_year)")

    # --- system_config table ---
    if "system_config" not in existing_tables:
        cur.execute("""
            CREATE TABLE system_config (
                key        TEXT PRIMARY KEY,
                value      TEXT DEFAULT '',
                updated_at DATETIME,
                updated_by INTEGER REFERENCES users(id)
            )
        """)
        print("[migrate] +system_config table")

    conn.commit()
    conn.close()
    print("[migrate] Hoan tat.")


if __name__ == "__main__":
    run()
