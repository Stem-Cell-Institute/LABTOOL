"""Hệ thống báo cáo kết quả nghiên cứu hằng tháng."""
import os
import re
import hashlib
import threading
import markdown as md_lib
import imagehash
from PIL import Image
from datetime import datetime
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.database import get_db, SessionLocal
from app.models import User, Group, MonthlyReport, ReportFile, SystemConfig, ReportPeriod, AICalibrationExample

router = APIRouter()
from app.templating import templates

RESULT_UPLOAD_DIR = "uploads/results"
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".tiff", ".bmp",
               ".pdf", ".xlsx", ".xls", ".csv", ".docx", ".doc", ".txt"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".tiff", ".bmp"}

VERDICT_LABEL = {
    "excellent":    ("Xuất sắc",                         "success"),
    "approved":     ("Đạt yêu cầu",                     "primary"),
    "warning":      ("Cần cải thiện",                   "warning"),
    "salary_defer": ("Xem xét điều chỉnh phụ cấp",      "dark"),
    "salary_cut":   ("Vi phạm liêm chính — Điểm 0",     "danger"),
}
MONTH_VI = ["", "Tháng 1", "Tháng 2", "Tháng 3", "Tháng 4", "Tháng 5", "Tháng 6",
            "Tháng 7", "Tháng 8", "Tháng 9", "Tháng 10", "Tháng 11", "Tháng 12"]


def _get_user(request: Request, db: Session):
    uid = request.session.get("user_id")
    return db.get(User, uid) if uid else None


def _can_manage(user: User):
    return user.role == "admin" or user.can_view_all


def _safe_filename(name: str) -> str:
    """Chỉ giữ tên file thuần (bỏ mọi thành phần thư mục/traversal) và ký tự an toàn,
    tránh path traversal khi ghép vào đường dẫn lưu trên server."""
    name = os.path.basename((name or "").replace("\\", "/"))
    name = re.sub(r'[^\w.\-() ]', '_', name).strip()
    return name or "file"


def _report_dir(user_id: int, report_id: int) -> str:
    path = os.path.join(RESULT_UPLOAD_DIR, str(user_id), str(report_id))
    os.makedirs(path, exist_ok=True)
    return path


# ── Phân tích AI background ───────────────────────────────────────────────────

def _analyze_report_background(report_id: int):
    db = SessionLocal()
    try:
        # Claim nguyên tử bằng UPDATE...WHERE thay vì đọc-rồi-ghi: nếu 2 request (VD nộp
        # báo cáo + admin bấm "phân tích lại") gần như đồng thời cùng khởi động luồng nền
        # cho cùng 1 report_id, chỉ luồng nào UPDATE trước mới "giành" được quyền chạy —
        # luồng còn lại thấy ai_status đã là "running" nên rowcount=0 và tự thoát, tránh
        # 2 luồng cùng ghi đè chéo kết quả phân tích của nhau.
        claimed = db.query(MonthlyReport).filter(
            MonthlyReport.id == report_id,
            MonthlyReport.ai_status != "running",
        ).update({"ai_status": "running"}, synchronize_session=False)
        db.commit()
        if not claimed:
            return

        report = db.get(MonthlyReport, report_id)
        if not report:
            return

        # Lấy báo cáo 3 tháng trước để so sánh
        previous = (
            db.query(MonthlyReport)
              .filter(
                  MonthlyReport.user_id == report.user_id,
                  MonthlyReport.status == "submitted",
                  MonthlyReport.id != report_id,
              )
              .order_by(MonthlyReport.report_year.desc(), MonthlyReport.report_month.desc())
              .limit(3).all()
        )
        prev_list = [{"month": p.report_month, "year": p.report_year, "content": p.content}
                     for p in previous]

        # File minh chứng đính kèm (ảnh, PDF, docx, xlsx, xls, csv, txt)
        evidence_paths = [f.filename for f in report.files if os.path.exists(f.filename)]

        # Đọc cài đặt AI từ DB
        import json as _json
        cfg = {r.key: r.value for r in db.query(SystemConfig).all()}

        # Ví dụ hiệu chỉnh đã được admin duyệt — dạy AI tránh lặp lại sai sót cũ
        calib = (
            db.query(AICalibrationExample)
              .filter(AICalibrationExample.is_active == True)
              .order_by(AICalibrationExample.created_at.desc())
              .limit(8).all()
        )
        calib_list = [{
            "context_excerpt": c.context_excerpt,
            "ai_verdict": c.ai_verdict,
            "correct_verdict": c.correct_verdict,
            "reason": c.reason,
        } for c in calib]

        # Phát hiện file minh chứng bị tái sử dụng từ báo cáo khác (cùng NCV tháng khác, hoặc NCV khác)
        # — so khớp bằng SHA-256, deterministic, không dựa vào AI tự nhận ra.
        system_warnings = []
        own_hashes = [f.file_hash for f in report.files if f.file_hash]
        if own_hashes:
            dupes = (
                db.query(ReportFile, MonthlyReport)
                  .join(MonthlyReport, ReportFile.report_id == MonthlyReport.id)
                  .filter(ReportFile.file_hash.in_(own_hashes), ReportFile.report_id != report.id)
                  .all()
            )
            for rf, mr in dupes:
                matched = next((f for f in report.files if f.file_hash == rf.file_hash), None)
                if not matched:
                    continue
                if mr.user_id == report.user_id:
                    system_warnings.append(
                        f"File '{matched.original_name}' trùng khớp hoàn toàn (SHA-256) với file "
                        f"'{rf.original_name}' đã nộp trong báo cáo tháng {mr.report_month}/{mr.report_year} "
                        f"của CHÍNH nghiên cứu viên này. Có khả năng đây là minh chứng cũ được tái sử dụng, "
                        f"KHÔNG phải kết quả mới của tháng này."
                    )
                else:
                    other_name = mr.author.full_name or mr.author.email if mr.author else "?"
                    system_warnings.append(
                        f"File '{matched.original_name}' trùng khớp hoàn toàn (SHA-256) với file "
                        f"'{rf.original_name}' đã nộp bởi NGHIÊN CỨU VIÊN KHÁC ({other_name}) trong báo cáo "
                        f"tháng {mr.report_month}/{mr.report_year}. Nghi vấn dùng chung/sao chép minh chứng."
                    )

        # Phát hiện ảnh RẤT GIỐNG nhưng không trùng byte tuyệt đối — bắt trường hợp crop/xoay/nén
        # lại ảnh cũ để né kiểm tra SHA-256. So bằng perceptual hash (Hamming distance), deterministic.
        own_images = [f for f in report.files if f.phash]
        if own_images:
            other_images = (
                db.query(ReportFile, MonthlyReport)
                  .join(MonthlyReport, ReportFile.report_id == MonthlyReport.id)
                  .filter(ReportFile.phash != "", ReportFile.report_id != report.id)
                  .all()
            )
            # Hamming distance / 64 bit — càng nhỏ càng giống nhau. Hiệu chỉnh thực nghiệm: nén lại
            # (~8), thu-phóng lại (~4), xoay nhẹ vài độ (~12) đều nằm trong ngưỡng này. LƯU Ý: crop
            # (cắt bớt viền ảnh) làm dịch chuyển toàn bộ cấu trúc ảnh nên thường vượt xa ngưỡng này
            # (~24+ dù chỉ cắt ~10%) — đây là giới hạn thật của perceptual hash, không bắt được ảnh
            # đã bị crop, chỉ bắt được ảnh bị nén lại/thu phóng lại/xoay nhẹ.
            PHASH_SIMILAR_THRESHOLD = 12
            reported_pairs = set()
            for own_f in own_images:
                try:
                    own_hash = imagehash.hex_to_hash(own_f.phash)
                except Exception:
                    continue
                for rf, mr in other_images:
                    if rf.file_hash == own_f.file_hash:
                        continue  # đã báo ở mức trùng khớp tuyệt đối (SHA-256) phía trên
                    pair_key = (own_f.id, rf.id)
                    if pair_key in reported_pairs:
                        continue
                    try:
                        distance = own_hash - imagehash.hex_to_hash(rf.phash)
                    except Exception:
                        continue
                    if distance <= PHASH_SIMILAR_THRESHOLD:
                        reported_pairs.add(pair_key)
                        who = (
                            "CHÍNH nghiên cứu viên này" if mr.user_id == report.user_id
                            else f"NGHIÊN CỨU VIÊN KHÁC ({mr.author.full_name or mr.author.email if mr.author else '?'})"
                        )
                        system_warnings.append(
                            f"File '{own_f.original_name}' RẤT GIỐNG (không trùng byte tuyệt đối — có thể đã bị "
                            f"crop/xoay/nén lại, độ khác biệt ảnh: {distance}/64) với file '{rf.original_name}' "
                            f"đã nộp bởi {who} trong báo cáo tháng {mr.report_month}/{mr.report_year}. Đây là "
                            f"NGHI VẤN CẦN XEM XÉT KỸ — CHƯA CHẮC CHẮN là ảnh cũ tái sử dụng nên KHÔNG tự động "
                            f"loại minh chứng, nhưng phải nêu rõ nghi vấn này và đề xuất người quản lý xác minh trực tiếp."
                        )

        from app import gemini as gem
        analysis = gem.analyze_monthly_report(
            current_content=report.content,
            researcher_name=report.author.full_name or report.author.email,
            month=report.report_month,
            year=report.report_year,
            previous_reports=prev_list,
            evidence_paths=evidence_paths,
            prompt_template=cfg.get("results_ai_prompt") or None,
            model_name=cfg.get("results_ai_model") or None,
            calibration_examples=calib_list,
            system_warnings=system_warnings,
        )

        # Trích điểm theo Quy định Viện (A×12 + B×8 − trừ, thang 100)
        scoring, verdict = _parse_ai_scores(analysis)

        report.ai_analysis    = analysis
        report.ai_scores_json = _json.dumps(scoring, ensure_ascii=False)
        report.ai_performance = scoring.get("total")
        report.ai_novelty     = scoring.get("experiment_points")
        report.ai_verdict     = verdict
        report.ai_status      = "done"
        db.commit()
    except Exception as e:
        try:
            report = db.get(MonthlyReport, report_id)
            if report:
                report.ai_status = "failed"
                report.ai_analysis = f"Lỗi phân tích: {str(e)[:500]}"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


def _parse_ai_scores(analysis: str):
    """Trích điểm theo Quy định Viện: A (1-5), B (1-5), deductions, total /100.
    Trả về: (scoring_dict, verdict_str)
    """
    import re

    # ── Kiểm tra yêu cầu tối thiểu ──────────────────────────────────────────
    min_req_fail = bool(re.search(
        r'Yêu cầu tối thiểu.*?KHÔNG ĐẠT', analysis, re.IGNORECASE | re.DOTALL
    ))

    # ── Trích điểm A và B ────────────────────────────────────────────────────
    def extract_score(label_pattern: str) -> int | None:
        m = re.search(rf'{label_pattern}\s*\(1-5\)\s*\|\s*(\d)\s*/\s*5', analysis)
        if m:
            return int(m.group(1))
        # fallback: tìm "Tiêu chí X | N/5"
        m2 = re.search(rf'{label_pattern}[^|]*\|\s*(\d)\s*/\s*5', analysis)
        return int(m2.group(1)) if m2 else None

    A = extract_score("Tiêu chí A")
    B = extract_score("Tiêu chí B")

    # ── Trích số thí nghiệm ──────────────────────────────────────────────────
    small_m = re.search(r'Số thí nghiệm Nhỏ\s*:\s*(\d+)', analysis)
    large_m = re.search(r'Số thí nghiệm Lớn\s*:\s*(\d+)', analysis)
    exp_pts_m = re.search(r'Tổng điểm thí nghiệm\s*:\s*(\d+)', analysis)

    small_count = int(small_m.group(1)) if small_m else None
    large_count = int(large_m.group(1)) if large_m else None
    experiment_points = int(exp_pts_m.group(1)) if exp_pts_m else None

    # ── Trích điểm trừ ───────────────────────────────────────────────────────
    deduct_m = re.search(r'Điểm trừ\s*:\s*(\d+)\s*điểm', analysis)
    deductions = int(deduct_m.group(1)) if deduct_m else 0
    if re.search(r'Không có điểm trừ', analysis, re.IGNORECASE):
        deductions = 0

    # ── Trích điểm tổng (ưu tiên từ dòng kết quả) ───────────────────────────
    # Trọng số: Tiêu chí A (khối lượng) × 20, Tiêu chí B (chất lượng) × 8 → thang thô tối đa 140,
    # chuẩn hóa về /100 để A chiếm ~71% trọng số thay vì cộng dồn thẳng (dễ vượt 100).
    total_m = re.search(r'=\s*(\d{1,3})\s*/\s*100', analysis)
    if total_m:
        total = int(total_m.group(1))
    elif A is not None and B is not None:
        raw = A * 20 + B * 8
        normalized = round(raw / 140 * 100)
        total = max(0, min(100, normalized - deductions))
    else:
        total = None

    # ── Xác định verdict dựa trên điểm tổng (không dùng keyword) ────────────
    if min_req_fail:
        verdict = "salary_defer"
    elif total is None:
        verdict = "approved"
    elif total >= 90:
        verdict = "excellent"
    elif total >= 70:
        verdict = "approved"
    elif total >= 50:
        verdict = "warning"
    else:
        verdict = "salary_defer"

    scoring = {
        "format":             "v2",
        "small_experiments":  small_count,
        "large_experiments":  large_count,
        "experiment_points":  experiment_points,
        "A":                  A,
        "B":                  B,
        "deductions":         deductions,
        "total":              total,
        "missing_minimums":   min_req_fail,
        "criteria_scores": [
            {"label": "Tiêu chí A — Khối lượng thí nghiệm", "score": A},
            {"label": "Tiêu chí B — Chất lượng ghi nhận",   "score": B},
        ],
    }
    return scoring, verdict


# ════════════════════════════════════════════════════════════════
# RESEARCHER ROUTES
# ════════════════════════════════════════════════════════════════

@router.get("/results", response_class=HTMLResponse)
def my_results(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    reports = (
        db.query(MonthlyReport)
          .filter(MonthlyReport.user_id == user.id)
          .order_by(MonthlyReport.report_year.desc(), MonthlyReport.report_month.desc())
          .all()
    )
    now = datetime.utcnow()
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "results/list.html", {
        "user": user, "flash": flash, "reports": reports,
        "verdict_label": VERDICT_LABEL, "months": MONTH_VI,
        "cur_month": now.month, "cur_year": now.year,
    })


@router.get("/results/new", response_class=HTMLResponse)
def new_report_form(
    request: Request,
    db: Session = Depends(get_db),
    month: int = None,
    year: int = None,
):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    now = datetime.utcnow()
    month = month or now.month
    year  = year or now.year

    # Kiểm tra đã có báo cáo tháng này chưa (admin được bỏ qua để nộp thử nhiều lần khi test)
    if user.role != "admin":
        existing = db.query(MonthlyReport).filter(
            MonthlyReport.user_id == user.id,
            MonthlyReport.report_month == month,
            MonthlyReport.report_year == year,
        ).first()
        if existing:
            if existing.status == "reviewed":
                request.session["flash"] = f"Báo cáo {MONTH_VI[month]}/{year} của bạn đã được duyệt."
                return RedirectResponse(f"/results/{existing.id}", status_code=302)
            return RedirectResponse(f"/results/{existing.id}/edit", status_code=302)

    # Kiểm tra kỳ nộp
    period = db.query(ReportPeriod).filter(
        ReportPeriod.report_month == month,
        ReportPeriod.report_year == year,
    ).first()
    period_closed = (period is not None and not period.is_open)

    year_range = list(range(now.year - 1, now.year + 2))
    return templates.TemplateResponse(request, "results/form.html", {
        "user": user, "report": None, "months": MONTH_VI,
        "sel_month": month, "sel_year": year, "year_range": year_range,
        "period": period, "period_closed": period_closed,
    })


@router.get("/results/{report_id}/edit", response_class=HTMLResponse)
def edit_report_form(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    report = db.get(MonthlyReport, report_id)
    if not report or report.user_id != user.id:
        raise HTTPException(status_code=403)
    if report.status == "reviewed":
        request.session["flash"] = "error:Báo cáo đã được duyệt, không thể chỉnh sửa"
        return RedirectResponse(f"/results/{report_id}", status_code=302)

    period = db.query(ReportPeriod).filter(
        ReportPeriod.report_month == report.report_month,
        ReportPeriod.report_year == report.report_year,
    ).first()
    period_closed = (period is not None and not period.is_open)

    now = datetime.utcnow()
    year_range = list(range(now.year - 1, now.year + 2))
    return templates.TemplateResponse(request, "results/form.html", {
        "user": user, "report": report, "months": MONTH_VI,
        "sel_month": report.report_month, "sel_year": report.report_year,
        "year_range": year_range,
        "period": period, "period_closed": period_closed,
    })


@router.post("/results/save")
async def save_report(
    request: Request,
    content: str = Form(...),
    report_month: int = Form(...),
    report_year: int = Form(...),
    report_id: int = Form(None),
    action: str = Form("draft"),   # "draft" | "submit"
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    now = datetime.utcnow()

    if report_id:
        report = db.get(MonthlyReport, report_id)
        if not report or report.user_id != user.id:
            raise HTTPException(status_code=403)
    else:
        # Kiểm tra trùng tháng/năm (admin được bỏ qua để nộp thử nhiều lần khi test)
        existing = None
        if user.role != "admin":
            existing = db.query(MonthlyReport).filter(
                MonthlyReport.user_id == user.id,
                MonthlyReport.report_month == report_month,
                MonthlyReport.report_year == report_year,
            ).first()
        if existing:
            report = existing
        else:
            report = MonthlyReport(
                user_id=user.id,
                group_id=user.group_id,
                report_month=report_month,
                report_year=report_year,
            )
            db.add(report)
            db.flush()

    # Chặn nộp khi kỳ đã đóng (chỉ áp dụng khi action=submit) — PHẢI kiểm tra theo tháng/năm
    # THẬT của report (report.report_month/report.report_year), không dùng report_month/report_year
    # từ form: nếu không, ai đó có thể gửi report_id của báo cáo tháng đã đóng kỳ kèm form khai
    # tháng/năm khác (đang mở) để lách qua điều kiện kiểm tra này.
    if action == "submit":
        period = db.query(ReportPeriod).filter(
            ReportPeriod.report_month == report.report_month,
            ReportPeriod.report_year == report.report_year,
        ).first()
        if period and not period.is_open:
            request.session["flash"] = (
                f"error:Kỳ nộp báo cáo {MONTH_VI[report.report_month]}/{report.report_year} "
                f"đã đóng, không thể nộp"
            )
            return RedirectResponse(f"/results/{report.id}", status_code=302)

    if report.status == "reviewed":
        # Chặn ở server, không chỉ ở giao diện — báo cáo đã duyệt không được sửa nội dung hay
        # đính kèm thêm file nữa (nếu không, quyết định của quản lý/AI sẽ không còn khớp với
        # nội dung thực tế đã đổi sau khi duyệt).
        request.session["flash"] = "error:Báo cáo đã được duyệt, không thể chỉnh sửa nội dung hay đính kèm thêm file."
        return RedirectResponse(f"/results/{report.id}", status_code=302)

    report.content = content.strip()
    report.updated_at = now
    was_submitted = False

    if action == "submit" and report.status != "reviewed":
        was_submitted = report.status == "submitted"
        report.status = "submitted"
        report.submitted_at = now
        # Reset AI để phân tích lại nội dung mới
        report.ai_status = "pending"
        report.ai_analysis = ""
        report.ai_verdict = ""
        report.ai_scores_json = ""
        report.ai_novelty = None
        report.ai_performance = None
        report.manager_decision = None

    db.commit()
    db.refresh(report)

    # Lưu file đính kèm
    report_dir = _report_dir(user.id, report.id)
    for upload in files:
        if not upload.filename:
            continue
        ext = os.path.splitext(upload.filename)[1].lower()
        if ext not in ALLOWED_EXT:
            continue
        data = await upload.read()
        if len(data) > 50 * 1024 * 1024:  # 50MB max per file
            continue
        safe_name = _safe_filename(upload.filename)
        save_path = os.path.join(report_dir, safe_name)
        base, e = os.path.splitext(safe_name)
        counter = 1
        while os.path.exists(save_path):
            save_path = os.path.join(report_dir, f"{base}_{counter}{e}")
            counter += 1
        with open(save_path, "wb") as f:
            f.write(data)
        file_type = "image" if ext in IMAGE_EXT else ("pdf" if ext == ".pdf" else "doc")
        phash_value = ""
        if file_type == "image":
            try:
                phash_value = str(imagehash.phash(Image.open(save_path)))
            except Exception:
                pass  # ảnh lỗi/định dạng không đọc được — bỏ qua phash, vẫn giữ SHA-256
        rf = ReportFile(
            report_id=report.id,
            filename=save_path,
            original_name=upload.filename,
            file_type=file_type,
            file_size=len(data),
            file_hash=hashlib.sha256(data).hexdigest(),
            phash=phash_value,
        )
        db.add(rf)
    db.commit()

    if action == "submit":
        threading.Thread(target=_analyze_report_background, args=(report.id,), daemon=True).start()
        if was_submitted:
            request.session["flash"] = f"Đã nộp lại báo cáo {MONTH_VI[report.report_month]}/{report.report_year}. Hệ thống đang phân tích lại..."
        else:
            request.session["flash"] = f"Đã nộp báo cáo {MONTH_VI[report.report_month]}/{report.report_year}. Hệ thống đang phân tích..."
    else:
        request.session["flash"] = "Đã lưu bản nháp"

    return RedirectResponse(f"/results/{report.id}", status_code=302)


@router.get("/results/{report_id}/file/{file_id}")
def download_file(report_id: int, file_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    report = db.get(MonthlyReport, report_id)
    if not report:
        raise HTTPException(status_code=404)
    if not _can_manage(user) and report.user_id != user.id:
        raise HTTPException(status_code=403)
    rf = db.get(ReportFile, file_id)
    if not rf or rf.report_id != report_id or not os.path.exists(rf.filename):
        raise HTTPException(status_code=404)
    return FileResponse(rf.filename, filename=rf.original_name)


@router.post("/results/{report_id}/delete-file/{file_id}")
def delete_file(report_id: int, file_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    report = db.get(MonthlyReport, report_id)
    if not report or report.user_id != user.id:
        raise HTTPException(status_code=403)
    if report.status == "reviewed":
        request.session["flash"] = "error:Báo cáo đã được duyệt, không thể xoá tệp đính kèm"
        return RedirectResponse(f"/results/{report_id}", status_code=302)
    rf = db.get(ReportFile, file_id)
    if rf and rf.report_id == report_id:
        try:
            os.remove(rf.filename)
        except Exception:
            pass
        db.delete(rf)
        db.commit()
    return RedirectResponse(f"/results/{report_id}/edit", status_code=302)


# ════════════════════════════════════════════════════════════════
# MANAGER / ADMIN ROUTES  (must come before /{report_id} param routes)
# ════════════════════════════════════════════════════════════════

@router.get("/results/overview", response_class=HTMLResponse)
def results_overview(
    request: Request,
    db: Session = Depends(get_db),
    year: int = None,
    group_id: int = None,
):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _can_manage(user):
        return RedirectResponse("/results", status_code=302)

    now = datetime.utcnow()
    year = year or now.year

    min_year = db.query(func.min(MonthlyReport.report_year)).scalar() or now.year
    year_range = list(range(min(min_year, now.year - 1), now.year + 2))

    groups = db.query(Group).order_by(Group.name).all()
    if group_id:
        users = db.query(User).filter(User.group_id == group_id, User.is_active == True).order_by(User.full_name).all()
    else:
        users = db.query(User).filter(User.is_active == True).order_by(User.group_id, User.full_name).all()

    # Lấy tất cả báo cáo năm đó
    q = db.query(MonthlyReport).filter(MonthlyReport.report_year == year)
    if group_id:
        q = q.filter(MonthlyReport.group_id == group_id)
    all_reports = q.all()

    # Build matrix: {user_id: {month: report}}
    matrix = {u.id: {} for u in users}
    for r in all_reports:
        if r.user_id in matrix:
            matrix[r.user_id][r.report_month] = r

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "results/overview.html", {
        "user": user, "flash": flash,
        "year": year, "year_range": year_range,
        "groups": groups, "selected_group": group_id,
        "users": users, "matrix": matrix,
        "verdict_label": VERDICT_LABEL, "months": MONTH_VI,
        "cur_month": now.month, "cur_year": now.year,
    })


@router.get("/results/monthly", response_class=HTMLResponse)
def results_monthly(
    request: Request,
    db: Session = Depends(get_db),
    year: int = None,
    month: int = None,
    group_id: int = None,
):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _can_manage(user):
        return RedirectResponse("/results", status_code=302)

    now = datetime.utcnow()
    year  = year or now.year
    month = month or now.month
    groups = db.query(Group).order_by(Group.name).all()

    q = db.query(MonthlyReport).filter(
        MonthlyReport.report_year == year,
        MonthlyReport.report_month == month,
    )
    if group_id:
        q = q.filter(MonthlyReport.group_id == group_id)
    reports = q.order_by(MonthlyReport.group_id).all()

    # Người chưa nộp — chỉ tính "đã nộp" nếu status là submitted/reviewed, KHÔNG tính report
    # còn ở dạng nháp (draft) là đã nộp, để tránh lọt người để báo cáo ở dạng nháp mãi mãi
    # nhằm né cả nhắc nhở lẫn phạt tự động khi đóng kỳ.
    if group_id:
        all_members = db.query(User).filter(User.group_id == group_id, User.is_active == True).all()
    else:
        all_members = db.query(User).filter(User.is_active == True).all()
    submitted_user_ids = {r.user_id for r in reports if r.status in ("submitted", "reviewed")}
    missing_users = [u for u in all_members if u.id not in submitted_user_ids]

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "results/monthly.html", {
        "user": user, "flash": flash,
        "year": year, "month": month,
        "month_name": MONTH_VI[month] if 1 <= month <= 12 else str(month),
        "groups": groups, "selected_group": group_id,
        "reports": reports, "missing_users": missing_users,
        "verdict_label": VERDICT_LABEL,
    })


@router.post("/results/{report_id}/review")
def review_report(
    report_id: int,
    request: Request,
    manager_decision: str = Form(...),
    manager_note: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _can_manage(user):
        raise HTTPException(status_code=403)

    report = db.get(MonthlyReport, report_id)
    if not report:
        raise HTTPException(status_code=404)

    if report.user_id == user.id:
        # Chống xung đột lợi ích: không cho tự duyệt báo cáo của chính mình, kể cả admin/can_view_all.
        request.session["flash"] = (
            "error:Không thể tự duyệt báo cáo của chính mình — cần một quản lý khác xác nhận để tránh xung đột lợi ích."
        )
        return RedirectResponse(f"/results/{report_id}", status_code=302)

    manager_note = manager_note.strip()
    is_correction = bool(report.ai_verdict) and manager_decision != report.ai_verdict
    if is_correction and not manager_note:
        request.session["flash"] = (
            "error:Quyết định của bạn khác với đề xuất của hệ thống — vui lòng ghi rõ lý do "
            "(lý do này giúp huấn luyện AI chính xác hơn cho các lần chấm sau)."
        )
        return RedirectResponse(f"/results/{report_id}", status_code=302)

    report.manager_decision = manager_decision
    report.manager_note = manager_note
    report.reviewed_by = user.id
    report.reviewed_at = datetime.utcnow()
    report.status = "reviewed"

    if is_correction:
        _upsert_calibration_example(db, report, manager_decision, manager_note, user.id)

    db.commit()

    label = VERDICT_LABEL.get(manager_decision, ("Đã duyệt", "secondary"))[0]
    request.session["flash"] = f"Đã duyệt: {label} — {report.author.full_name or report.author.email}"
    return RedirectResponse(f"/results/{report_id}", status_code=302)


def _upsert_calibration_example(db: Session, report: MonthlyReport, correct_verdict: str, reason: str, created_by: int):
    """Lưu lại lần Ban quản lý sửa quyết định khác với AI thành ví dụ hiệu chỉnh.
    Mỗi báo cáo chỉ giữ 1 ví dụ mới nhất — nếu BQL đổi quyết định nhiều lần thì cập nhật thay vì tạo trùng.
    """
    ex = db.query(AICalibrationExample).filter(AICalibrationExample.report_id == report.id).first()
    if not ex:
        ex = AICalibrationExample(report_id=report.id, source="review_correction", created_by=created_by)
        db.add(ex)
    ex.report_month = report.report_month
    ex.report_year = report.report_year
    ex.researcher_name = report.author.full_name or report.author.email
    ex.context_excerpt = (report.content or "")[:800]
    ex.ai_verdict = report.ai_verdict
    ex.correct_verdict = correct_verdict
    ex.reason = reason
    ex.is_active = True


@router.post("/results/{report_id}/reanalyze")
def reanalyze_report(report_id: int, request: Request, db: Session = Depends(get_db)):
    """Xoá kết quả phân tích cũ và kích hoạt phân tích lại (chỉ admin/can_view_all)."""
    user = _get_user(request, db)
    if not user or user.role != "admin":
        raise HTTPException(status_code=403)

    report = db.get(MonthlyReport, report_id)
    if not report:
        raise HTTPException(status_code=404)

    if report.ai_status == "running":
        # Đang có 1 luồng phân tích thật sự chạy cho báo cáo này — không reset, tránh
        # 2 luồng cùng ghi đè kết quả (xem thêm claim nguyên tử trong _analyze_report_background).
        request.session["flash"] = "Báo cáo đang được phân tích, vui lòng đợi."
        return RedirectResponse(f"/results/{report_id}", status_code=302)

    report.ai_status      = "pending"
    report.ai_analysis    = ""
    report.ai_verdict     = ""
    report.ai_scores_json = ""
    report.ai_novelty     = None
    report.ai_performance = None
    db.commit()

    import threading
    t = threading.Thread(target=_analyze_report_background, args=(report_id,), daemon=True)
    t.start()

    request.session["flash"] = "Đã kích hoạt phân tích lại. Trang sẽ tự cập nhật khi xong."
    return RedirectResponse(f"/results/{report_id}", status_code=302)


@router.get("/results/period-status")
def current_period_status(request: Request, db: Session = Depends(get_db)):
    from fastapi.responses import JSONResponse
    user = _get_user(request, db)
    if not user:
        raise HTTPException(status_code=401)
    now = datetime.utcnow()
    period = db.query(ReportPeriod).filter(
        ReportPeriod.report_month == now.month,
        ReportPeriod.report_year == now.year,
    ).first()
    if not period:
        return JSONResponse({"status": "no_period"})
    return JSONResponse({
        "status": "open" if period.is_open else "closed",
        "deadline": period.deadline.isoformat() if period.deadline else None,
    })


# ════════════════════════════════════════════════════════════════
# INDIVIDUAL REPORT ROUTES (must come after named routes above)
# ════════════════════════════════════════════════════════════════

@router.get("/results/{report_id}", response_class=HTMLResponse)
def view_report(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    report = db.get(MonthlyReport, report_id)
    if not report:
        raise HTTPException(status_code=404)
    if not _can_manage(user) and report.user_id != user.id:
        raise HTTPException(status_code=403)

    import json as _json
    content_html = md_lib.markdown(report.content, extensions=["tables", "fenced_code", "nl2br"]) if report.content else ""
    ai_html = md_lib.markdown(report.ai_analysis, extensions=["tables", "fenced_code", "nl2br"]) if report.ai_analysis else ""
    ai_scoring = None
    ai_scores = []
    try:
        raw = _json.loads(report.ai_scores_json) if report.ai_scores_json else None
        if isinstance(raw, dict) and raw.get("format") == "v2":
            ai_scoring = raw
            ai_scores = raw.get("criteria_scores", [])
        elif isinstance(raw, list):
            ai_scores = raw  # định dạng cũ
    except Exception:
        pass

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "results/detail.html", {
        "user": user, "report": report, "flash": flash,
        "content_html": content_html, "ai_html": ai_html,
        "ai_scores": ai_scores, "ai_scoring": ai_scoring,
        "verdict_label": VERDICT_LABEL, "months": MONTH_VI,
        # Không cho thấy giao diện tự duyệt trên báo cáo của chính mình, kể cả admin/can_view_all
        # (chống xung đột lợi ích — enforce lại ở server trong review_report()).
        "can_manage": _can_manage(user) and report.user_id != user.id,
    })


@router.get("/results/{report_id}/ai-status")
def ai_status(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if not user:
        raise HTTPException(status_code=401)
    report = db.get(MonthlyReport, report_id)
    if not report:
        raise HTTPException(status_code=404)
    if not _can_manage(user) and report.user_id != user.id:
        raise HTTPException(status_code=403)
    from fastapi.responses import JSONResponse
    return JSONResponse({"status": report.ai_status, "verdict": report.ai_verdict})
