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

## 主账号与子账号

已有普通账号升级后自动成为主账号，不会清空原有快照。主账号在 `/dashboard` 登录后可创建子账号，并按浏览器和网站账号分别授权。

子账号：

- 可登录桌面客户端与网页控制台
- 只能读取主账号分配的浏览器和网站账号
- 服务端拒绝子账号调用上传同步接口
- 不能管理其他子账号
- 被禁用或重置密码时，已有登录会话立即失效

网站账号授权使用网站、网址、用户名和所属浏览器生成稳定资源键。主账号修改这些身份字段后，原授权可能失效，需要在控制台重新勾选。

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
