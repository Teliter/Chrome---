@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
echo.
echo 管理后台: http://127.0.0.1:8787/dashboard
echo 管理员用户名默认为 admin
echo 首次启动后，随机密码保存在 cloud-admin-password.txt
echo.
python -m uvicorn app:app --host 127.0.0.1 --port 8787
pause
