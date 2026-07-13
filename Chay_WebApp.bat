@echo off
chcp 65001 >nul
cd /d "%~dp0"
title SCI Portal - Vien Te Bao Goc

echo ================================================
echo   SCI PORTAL - VIEN TE BAO GOC
echo ================================================
echo.

REM Lan dau tien: khoi tao database
if not exist labtool.db (
    echo [*] Lan dau chay - dang khoi tao database...
    python init_db.py
    echo.
)

echo [*] Kiem tra cap nhat co so du lieu...
python migrate.py
echo.

echo [*] Dang khoi dong server...
echo [*] Sau khi thay "Application startup complete", mo trinh duyet tai:
echo.
echo        http://localhost:8000
echo.
echo [*] De dung server: nhan Ctrl+C
echo ================================================
echo.

python run.py

pause
