# Chrome 多开管理器

一个面向 Windows 的本地 Chrome 独立环境管理工具。每个浏览器实例使用单独的用户数据目录和固定 CDP 端口，可以保留各自的 Cookie、登录状态、书签、扩展与配置，并允许 Codex、Playwright 或其他 CDP 客户端精准连接指定实例。

当前项目是便于继续开发和调试的 Python 源码版，暂未打包为 EXE。

## 主要功能

- 创建、编辑、复制、启动、关闭和删除独立 Chrome 实例
- 每个实例使用独立 Profile 和固定 CDP 调试端口
- 分组、搜索、运行状态、PID、标签页和内存监控
- 支持 HTTP、HTTPS、SOCKS5 代理及带账号密码的 HTTP 代理
- 代理出口 IP、国家地区、延迟和 Google 连通性检测
- 配置 User-Agent、窗口尺寸和位置、语言、时区及地理位置
- 配置图片、通知、摄像头、麦克风和定位权限
- 配置安全 DNS、WebRTC、下载目录、颜色模式和无痕启动
- 支持移动设备、触摸事件和设备缩放测试模式
- 支持每个浏览器加载独立扩展目录
- 提供 Canvas、WebGL、CPU、内存和字体的页面级测试覆盖
- 查看浏览器当前 UA、语言、时区、分辨率和公网 IP
- 检查代理地区、语言、时区和设备参数的一致性
- 清除单个浏览器的缓存、Cookie 和历史记录
- 完整导出、导入和恢复单个浏览器
- 提供 Codex/命令行创建、启动和关闭浏览器的接口
- 内置明文网站账号管理、CSV 导入导出功能

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
4. 点击“新建浏览器”，填写名称和 CDP 端口。
5. 在列表右侧点击“启动”。

也可以在 PowerShell 中运行：

```powershell
python -m pip install -r requirements.txt
python .\ChromeManager.pyw
```

## Codex 与命令行

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
browser_cli.py             Codex 和命令行接口
environment_config.py      环境模型、启动参数和一致性检查
environment_controller.py  通过 CDP 向现有及新标签页应用环境设置
proxy_forwarder.py         带认证 HTTP 代理的本地转发桥
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

## 数据迁移

完全关闭管理器和所有浏览器后，可以复制整个项目文件夹到另一台 Windows 电脑。请保留 `profiles` 和 `browser-map.json`。

部分 Chrome 登录凭据可能受 Windows DPAPI 或设备环境保护，因此换电脑后某些网站仍可能要求重新登录。

也可以使用程序内的“导出选中浏览器”和“导入为新浏览器”功能迁移单个实例。

## 安全提示

- CDP 仅绑定 `127.0.0.1`，不要将调试端口开放到公网。
- 代理账号密码和内置账号库可能以明文保存在本地。
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
