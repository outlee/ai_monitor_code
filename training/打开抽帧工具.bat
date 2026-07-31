@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 启动视频抽帧可视化工具...
python video_to_frames_gui.py
if errorlevel 1 (
  echo.
  echo 若提示没有 python，请先安装 Python 并勾选 Add to PATH
  pause
)
