@echo off
chcp 65001 >nul
title loomy2api
cd /d "%~dp0"

echo ============================================
echo   loomy2api 控制台
echo   面板: http://127.0.0.1:17890/panel
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 找不到 python，请先安装 Python 3.10+ 并加入 PATH
  pause
  exit /b 1
)

echo 正在启动...（关掉这个窗口即停止服务）
echo.
python -m loomy2api serve

echo.
echo 服务已退出。按任意键关闭窗口。
pause >nul
