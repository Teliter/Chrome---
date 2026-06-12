# Chrome 多开管理器

一个面向 Windows 的本地 Chrome 独立环境管理工具。每个浏览器实例使用单独的用户数据目录和固定 CDP 端口，可以保留各自的 Cookie、登录状态、书签、扩展与配置，并允许 Playwright 或其他 CDP 客户端精准连接指定实例。

项目同时保留 Python 源码版和 Windows 安装包构建脚本，便于继续开发、调试和分发。

## 主要功能

- 创建、编辑、复制、启动、关闭和删除独立 Chrome 实例
- 每个实例使用独立 Profile 和固定 CDP 调试端口
- 分组、搜索、运行状态、PID、标签页和内存监控
- 记录并显示每个浏览器上次成功打开的时间
- 支持 HTTP、HTTPS、SOCKS5 代理及带账号密码的 HTTP 代理
- 代理出口 IP、国家地区、延迟和 Google 连通性检测
- 配置 User-Agent、窗口尺寸和位置、语言、时区及地理位置
- 配置图片、通知、摄像头、麦克风和定位权限
- 配置安全 DNS、WebRTC、下载目录、颜色模式和无痕启动
- 支持 Windows 桌面窗口尺寸、位置和设备缩放比例
- 支持每个浏览器加载独立扩展目录
- 提供 Canvas、WebGL、CPU、内存和字体的页面级测试覆盖
- 查看浏览器当前 UA、语言、时区、分辨率和公网 IP
- 检查代理地区、语言、时区和设备参数的一致性
- 清除单个浏览器的缓存、Cookie 和历史记录
- 完整导出、导入和恢复单个浏览器
- 提供命令行创建、启动和关闭浏览器的接口
- 内置明文网站账号管理、CSV 导入导出功能
- 云端账号注册、登录、加密同步和网页控制台
- 企业主账号、只读子账号和浏览器/网站账号资源授权
- 新设备拉取授权账号后，在完全匹配的网站域名自动填充账号密码（不会自动提交）

## Windows 安装包

在项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\build-installer.ps1
```

生成的安装包位于 `release/`。安装版不要求用户安装 Python，程序数据默认保存在：

```text
%LOCALAPPDATA%\ChromeMultiManager
```

## 企业账号与子账号

普通注册账号默认是主账号。主账号登录网页控制台后可以：

- 创建最多 100 个子账号
- 为每个子账号分别分配浏览器和网站账号
- 禁用、启用、重置密码或删除子账号
- 随时调整子账号可见资源

子账号可以登录桌面客户端和网页控制台，但只能查看、启动和关闭被分配的浏览器。子账号不能上传同步、创建、编辑、复制、删除、导入或批量导出浏览器及网站账号。

主账号先在客户端上传同步数据，再到网页控制台完成资源分配。子账号在新设备登录客户端并点击“拉取同步”后，将获得授权的浏览器配置和网站账号。启动对应浏览器时，程序会生成本地自动填充扩展，只在完全匹配的域名填写账号和密码，不会自动点击登录。

注意：子账号拉取的网站账号会以明文保存在该 Windows 用户的数据目录中。应用内只读权限不是 Windows 文件系统或终端的强隔离；需要更强控制时，应使用独立 Windows 账号、磁盘加密和设备访问策略。

桌面客户端不会写死云端服务地址。登录或注册时需要填写服务器地址，本机开发服务可使用 `http://127.0.0.1:8787`，任意部署了兼容 API 的远程服务器均可使用，但远程连接必须使用 HTTPS。

## 运行环境

- Windows 10 或 Windows 11
- Google Chrome
- Python 3.11 或更高版本

Python 依赖：

```text
psutil
pystray
pillow
websocket-client
```

## 快速开始

1. 下载或克隆本项目。
2. 双击 `安装开发依赖.cmd` 安装 Python 依赖。
3. 双击 `打开Chrome多开管理器.cmd`。
4. 点击“新建浏览器”，可先在弹窗顶部点击“修改环境配置”，再填写名称和 CDP 端口。
5. 在列表右侧点击“启动”。

修改已有浏览器的环境参数时，先在列表中选中浏览器，再点击：

```text
更多操作 → 修改环境配置
```

“查看当前环境信息”是浏览器内的实际检测报告，只用于核对生效结果，不直接编辑配置。

环境配置窗口提供：

- 地区模板：自动填写语言、首选语言、时区和城市中心坐标
- Windows 设备模板：自动填写桌面 UA、窗口尺寸和缩放比例
- 中文下拉选项：常用窗口、语言、时区、DNS、WebRTC、定位精度和硬件参数
- 随机生成测试环境：保留已选地区和 Windows 设备，在城市附近随机坐标与其他参数
- 网络与定位、高级配置页支持滚动，不会隐藏底部选项

随机参数用于开发测试。使用代理时，仍应手动确认代理出口国家与随机地区一致。

也可以在 PowerShell 中运行：

```powershell
python -m pip install -r requirements.txt
python .\ChromeManager.pyw
```

## 命令行与自动化

新增浏览器：

```powershell
python .\browser_cli.py add --name "浏览器 A"
```

指定端口、分组和首页：

```powershell
python .\browser_cli.py add `
  --name "浏览器 B" `
  --port 9232 `
  --group "测试组" `
  --home "https://www.baidu.com"
```

列出浏览器：

```powershell
python .\browser_cli.py list
```

通过配置键、名称或端口启动：

```powershell
python .\browser_cli.py start browser1
python .\browser_cli.py start 9232 --url "https://www.baidu.com"
```

关闭浏览器：

```powershell
python .\browser_cli.py stop 9232
```

启动后可通过以下地址检查 CDP：

```text
http://127.0.0.1:9232/json/version
http://127.0.0.1:9232/json/list
```

Playwright 连接示例：

```javascript
const { chromium } = require("playwright");

const browser = await chromium.connectOverCDP("http://127.0.0.1:9232");
const context = browser.contexts()[0];
const pages = context.pages();
```

## 项目结构

```text
ChromeManager.pyw          图形界面主程序
browser_cli.py             命令行和自动化接口
environment_config.py      环境模型、启动参数和一致性检查
environment_controller.py  通过 CDP 向现有及新标签页应用环境设置
proxy_forwarder.py         带认证 HTTP 代理的本地转发桥
cloud_client.py            桌面端云同步客户端
cloud_server/              注册登录 API、SQLite 存储和网页控制台
requirements.txt           Python 依赖
profiles/                  独立浏览器用户数据
backups/                   浏览器备份
launchers/                 单浏览器启动脚本
```

首次运行后程序会创建：

```text
browser-map.json       浏览器名称、端口和 Profile 映射
manager-settings.json  管理器设置
manager.log            操作日志
password-vault.json    明文网站账号库
```

这些用户数据已加入 `.gitignore`，不会默认提交到 Git。

## 云端账号与网页控制台

项目包含一个可独立部署的云端服务。开发测试时进入 `cloud_server`，双击：

```text
启动云端服务.cmd
```

或者手动运行：

```powershell
cd .\cloud_server
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8787
```

随后点击桌面管理器右上角的“云端账号”，填写：

```text
http://127.0.0.1:8787
```

注册并登录后，可以上传或拉取：

- 浏览器名称、分组、端口、代理和环境配置
- 程序设置，但不包含本地解锁密码和云端令牌
- 网站账号库

普通客户端看不到网页控制台入口。管理员在服务器端使用以下地址管理全部用户：

```text
http://127.0.0.1:8787/dashboard
```

管理员用户名默认是 `admin`。首次启动会自动生成随机密码并保存到：

```text
cloud_server/cloud-admin-password.txt
```

管理员后台可以查看全部普通用户、同步的浏览器列表和网站账号数据，并可禁用、启用、
重置密码、清空同步数据或删除用户。
普通用户令牌访问管理员接口会被拒绝。

服务器使用 `scrypt` 保存登录密码哈希，并使用 Fernet 加密完整同步快照。开发模式会在
`cloud_server/cloud-data.key` 生成数据密钥，该文件和 SQLite 数据库均已被 Git 忽略。

公网部署必须：

- 使用 HTTPS 反向代理，例如 Caddy 或 Nginx
- 设置固定的 `CHROME_MANAGER_DATA_KEY` 环境变量
- 设置 `CHROME_MANAGER_ADMIN_USERNAME` 和 `CHROME_MANAGER_ADMIN_PASSWORD`
- 备份数据库和加密密钥；丢失密钥后同步数据无法恢复
- 限制服务器访问权限，并设置防火墙、登录限速和定期更新

当前云同步不会上传 `profiles/`、Cookie、浏览器扩展和完整登录状态。这些数据体积大且非常敏感，
后续应使用独立的压缩、端到端加密、断点续传和配额模块。

## 数据迁移

完全关闭管理器和所有浏览器后，可以复制整个项目文件夹到另一台 Windows 电脑。请保留 `profiles` 和 `browser-map.json`。

部分 Chrome 登录凭据可能受 Windows DPAPI 或设备环境保护，因此换电脑后某些网站仍可能要求重新登录。

也可以使用程序内的“导出选中浏览器”和“导入为新浏览器”功能迁移单个实例。

## 安全提示

- CDP 仅绑定 `127.0.0.1`，不要将调试端口开放到公网。
- 代理账号密码和内置账号库可能以明文保存在本地。
- 云端网页控制台能显示同步的网站密码，只能通过可信 HTTPS 服务器使用。
- 不要将包含真实用户数据的 `profiles` 或配置文件上传到公开仓库。
- 使用代理前应确认代理服务商可信，并遵守网站规则和当地法律。

## 能力边界

本项目提供的是浏览器环境管理、开发测试和隐私控制能力，不是平台反检测保证：

- 无法可靠伪装 Chrome 和操作系统网络栈产生的 TLS 指纹。
- Canvas、WebGL、CPU、内存和字体覆盖只是页面级测试值。
- 虚构或固定参数可能形成不自然的环境组合，反而增加识别风险。
- 不能保证绕过店铺关联检测、模拟多个真实自然人或让平台无法识别同一设备。

## 开发自检

```powershell
python -m py_compile `
  ChromeManager.pyw `
  browser_cli.py `
  environment_config.py `
  environment_controller.py `
  proxy_forwarder.py

python .\ChromeManager.pyw --self-test
```

## 当前状态

项目仍处于开发阶段。建议在非关键账号和测试环境中验证配置，再用于长期浏览器数据。
