# Chrome Manager Cloud

## 本地开发

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8787
```

打开 `http://127.0.0.1:8787/dashboard`。

这是服务器管理员后台，不是普通用户控制台。普通用户只能通过桌面客户端注册、登录和同步。
管理员可查看全部用户数据，并执行禁用、启用、重置密码、清空同步数据和删除用户。

首次启动会创建管理员：

```text
用户名：admin
密码：cloud-admin-password.txt 文件中的随机密码
```

## 环境变量

```text
CHROME_MANAGER_DB        SQLite 数据库路径
CHROME_MANAGER_DATA_KEY  生产环境数据加密密钥
CHROME_MANAGER_KEY_FILE  开发环境密钥文件路径
CHROME_MANAGER_ADMIN_USERNAME       管理员用户名
CHROME_MANAGER_ADMIN_PASSWORD       管理员密码
CHROME_MANAGER_ADMIN_PASSWORD_FILE  自动生成的管理员密码文件
```

生产环境应设置高强度随机 `CHROME_MANAGER_DATA_KEY`，并通过 Caddy 或 Nginx 提供 HTTPS。
不要公开提交 `cloud.db`、`cloud-data.key` 或任何备份文件。

## Docker

```powershell
docker build -t chrome-manager-cloud .
docker run -d --name chrome-manager-cloud `
  -p 127.0.0.1:8787:8787 `
  -v chrome-manager-data:/data `
  -e CHROME_MANAGER_DATA_KEY="请替换为高强度随机密钥" `
  -e CHROME_MANAGER_ADMIN_USERNAME="admin" `
  -e CHROME_MANAGER_ADMIN_PASSWORD="请替换为高强度管理员密码" `
  chrome-manager-cloud
```

Docker 端口仍只绑定本机。再使用 Caddy 或 Nginx 将 HTTPS 域名反向代理到
`http://127.0.0.1:8787`。
