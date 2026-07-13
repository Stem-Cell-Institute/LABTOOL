@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Lab Tool - Phan tich Vlog Thi Nghiem

echo ============================================
echo   DANG CHAY CONG CU PHAN TICH VLOG THI NGHIEM
echo ============================================
echo.

python lab_tool.py

echo.
echo ============================================
echo   DA XU LY XONG. Nhan phim bat ky de dong.
echo ============================================
pause >nul
