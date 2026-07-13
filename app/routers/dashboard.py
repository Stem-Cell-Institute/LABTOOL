from calendar import monthrange
from datetime import datetime
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, distinct
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, Video, ExperimentLog, Group, MonthlyReport, DailyLog

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

MONTH_VI = ["", "Tháng 1", "Tháng 2", "Tháng 3", "Tháng 4", "Tháng 5", "Tháng 6",
            "Tháng 7", "Tháng 8", "Tháng 9", "Tháng 10", "Tháng 11", "Tháng 12"]

VERDICT_LABEL = {
    "excellent":    ("Xuất sắc",               "success"),
    "approved":     ("Đạt yêu cầu",            "primary"),
    "warning":      ("Cần cải thiện",          "warning"),
    "salary_defer": ("Hoãn trả lương tháng này", "dark"),
    "salary_cut":   ("Xem xét trừ lương",      "danger"),
}


def _video_report_stats(db: Session, m: int, y: int):
    """Thống kê báo cáo video tháng hiện tại theo nhóm."""
    groups = db.query(Group).order_by(Group.name).all()
    result = []
    total_submitted = total_members = 0
    for g in groups:
        members = (db.query(func.count(User.id))
                     .filter(User.group_id == g.id, User.is_active == True).scalar()) or 0
        submitted = (db.query(func.count(distinct(Video.uploaded_by)))
                       .filter(Video.group_id == g.id,
                               Video.report_month == m,
                               Video.report_year == y).scalar()) or 0
        pct = int(submitted / members * 100) if members else 0
        result.append({"group": g, "submitted": submitted, "total": members, "pct": pct})
        total_submitted += submitted
        total_members += members
    return result, total_submitted, total_members


def _result_report_stats(db: Session, m: int, y: int):
    """Thống kê báo cáo kết quả NCV tháng hiện tại theo nhóm."""
    groups = db.query(Group).order_by(Group.name).all()
    result = []
    total_submitted = total_members = 0
    for g in groups:
        members = (db.query(func.count(User.id))
                     .filter(User.group_id == g.id, User.is_active == True).scalar()) or 0
        submitted = (db.query(func.count(MonthlyReport.id))
                       .filter(MonthlyReport.group_id == g.id,
                               MonthlyReport.report_month == m,
                               MonthlyReport.report_year == y,
                               MonthlyReport.status.in_(["submitted", "reviewed"])).scalar()) or 0
        pct = int(submitted / members * 100) if members else 0
        result.append({"group": g, "submitted": submitted, "total": members, "pct": pct})
        total_submitted += submitted
        total_members += members
    return result, total_submitted, total_members


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/login", status_code=302)

    user = db.get(User, user_id)
    if not user or not user.is_active:
        request.session.clear()
        return RedirectResponse("/login", status_code=302)
    flash = request.session.pop("flash", None)
    now = datetime.utcnow()
    cur_month, cur_year = now.month, now.year

    is_overview = (user.role == "admin" or user.can_view_all)

    ctx = {
        "user": user, "flash": flash,
        "cur_month": cur_month, "cur_year": cur_year,
        "month_name": MONTH_VI[cur_month],
        "verdict_label": VERDICT_LABEL,
        "is_overview": is_overview,
    }

    if is_overview:
        total_videos = db.query(func.count(Video.id)).scalar()
        total_logs   = db.query(func.count(ExperimentLog.id)).scalar()
        total_groups = db.query(func.count(Group.id)).scalar()
        total_users  = db.query(func.count(User.id)).filter(User.is_active == True, User.role == "member").scalar()
        recent_videos = db.query(Video).order_by(Video.uploaded_at.desc()).limit(8).all()

        vid_group, vid_submitted, vid_total = _video_report_stats(db, cur_month, cur_year)
        res_group, res_submitted, res_total = _result_report_stats(db, cur_month, cur_year)

        # Báo cáo kết quả chưa duyệt
        pending_review = db.query(func.count(MonthlyReport.id)).filter(
            MonthlyReport.status == "submitted"
        ).scalar() or 0

        # Nhật ký thí nghiệm — phục vụ admin/quản lý kiểm tra liêm chính khoa học
        month_start = datetime(cur_year, cur_month, 1)
        month_end = datetime(cur_year, cur_month, monthrange(cur_year, cur_month)[1], 23, 59, 59)
        diary_total = db.query(func.count(DailyLog.id)).scalar() or 0
        diary_month = db.query(func.count(DailyLog.id)).filter(
            DailyLog.created_at >= month_start, DailyLog.created_at <= month_end
        ).scalar() or 0
        diary_recent = (db.query(DailyLog)
                           .order_by(DailyLog.created_at.desc())
                           .limit(5).all())

        ctx.update({
            "total_videos": total_videos, "total_logs": total_logs,
            "total_groups": total_groups, "total_users": total_users,
            "recent_videos": recent_videos,
            "vid_group": vid_group, "vid_submitted": vid_submitted, "vid_total": vid_total,
            "res_group": res_group, "res_submitted": res_submitted, "res_total": res_total,
            "pending_review": pending_review,
            "diary_total": diary_total, "diary_month": diary_month, "diary_recent": diary_recent,
        })

    else:
        gid = user.group_id
        total_videos = (db.query(func.count(Video.id))
                          .filter(Video.group_id == gid).scalar())
        total_logs   = (db.query(func.count(ExperimentLog.id))
                          .join(Video).filter(Video.group_id == gid).scalar())
        recent_videos = (db.query(Video)
                           .filter(Video.group_id == gid)
                           .order_by(Video.uploaded_at.desc()).limit(5).all())

        # VidNote: báo cáo video tháng này
        my_video_report = db.query(Video).filter(
            Video.uploaded_by == user.id,
            Video.report_month == cur_month,
            Video.report_year == cur_year,
        ).first()

        # Kết quả NCV: báo cáo văn bản tháng này
        my_result_report = db.query(MonthlyReport).filter(
            MonthlyReport.user_id == user.id,
            MonthlyReport.report_month == cur_month,
            MonthlyReport.report_year == cur_year,
        ).first()

        # Tiến độ nhóm — video
        group_members = (db.query(func.count(User.id))
                           .filter(User.group_id == gid, User.is_active == True).scalar()) or 0
        group_vid_submitted = (db.query(func.count(distinct(Video.uploaded_by)))
                                 .filter(Video.group_id == gid,
                                         Video.report_month == cur_month,
                                         Video.report_year == cur_year).scalar()) or 0
        group_vid_pct = int(group_vid_submitted / group_members * 100) if group_members else 0

        # Tiến độ nhóm — kết quả
        group_res_submitted = (db.query(func.count(MonthlyReport.id))
                                 .filter(MonthlyReport.group_id == gid,
                                         MonthlyReport.report_month == cur_month,
                                         MonthlyReport.report_year == cur_year,
                                         MonthlyReport.status.in_(["submitted", "reviewed"])).scalar()) or 0
        group_res_pct = int(group_res_submitted / group_members * 100) if group_members else 0

        ctx.update({
            "total_videos": total_videos, "total_logs": total_logs,
            "recent_videos": recent_videos,
            "my_video_report": my_video_report,
            "my_result_report": my_result_report,
            "group_members": group_members,
            "group_vid_submitted": group_vid_submitted, "group_vid_pct": group_vid_pct,
            "group_res_submitted": group_res_submitted, "group_res_pct": group_res_pct,
        })

    return templates.TemplateResponse(request, "dashboard.html", ctx)
