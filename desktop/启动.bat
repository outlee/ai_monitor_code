@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo 正在启动 AI 节目监测...
echo 请稍等。这个黑窗口要留着，关掉它，监测窗口也会退出。
set "PY=python"
where py >nul 2>&1 && set "PY=py -3"
%PY% -c "import fastapi,uvicorn,multipart" 1>nul 2>nul
if not errorlevel 1 goto :start
echo 正在安装运行库，大约一两分钟，窗口先不要关。
%PY% -m pip install fastapi uvicorn python-multipart pywebview
if errorlevel 1 goto :fail
:start
%PY% "%~dp0launch.py"
if errorlevel 1 goto :fail
goto :eof
:fail
echo.
echo 启动失败。请把上面的文字留下，或把 desktop\launch.log 发出来。
pause
