import ctypes
import ctypes.wintypes
import csv
import base64
import html
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import psutil
from environment_config import (
    DEFAULT_ENVIRONMENT,
    apply_profile_preferences,
    build_chrome_arguments,
    consistency_report,
    ensure_environment_controller,
    extension_paths,
    normalize_environment,
)
try:
    import pystray
    from PIL import Image, ImageDraw, ImageTk
except ImportError:
    pystray = None
    Image = None
    ImageDraw = None
    ImageTk = None


APP_VERSION = "3.0.0-dev"
ROOT = Path(__file__).resolve().parent
MAP_FILE = ROOT / "browser-map.json"
SETTINGS_FILE = ROOT / "manager-settings.json"
PROFILES = ROOT / "profiles"
LAUNCHERS = ROOT / "launchers"
BACKUPS = ROOT / "backups"
LOG_FILE = ROOT / "manager.log"
VAULT_FILE = ROOT / "password-vault.json"
LEGACY_VAULT_FILE = ROOT / "password-vault.dat"
APP_ICON_FILE = ROOT / "chrome-manager.ico"
LOCK_PORT = 39231

BG = "#f7f6f2"
PANEL = "#ffffff"
CARD = "#ffffff"
TEXT = "#2d2a26"
MUTED = "#7c776f"
BLUE = "#cc785c"
GREEN = "#238636"
RED = "#c2413b"
PURPLE = "#7a6f9b"
BORDER = "#e5e1da"
SIDEBAR = "#f0eee8"
HOVER = "#ebe7df"

for folder in (PROFILES, LAUNCHERS, BACKUPS):
    folder.mkdir(parents=True, exist_ok=True)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default
    except Exception:
        return default


def save_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def load_vault():
    if VAULT_FILE.exists():
        return load_json(VAULT_FILE, [])
    if LEGACY_VAULT_FILE.exists():
        try:
            encrypted = base64.b64decode(LEGACY_VAULT_FILE.read_text(encoding="ascii"))
            buffer = ctypes.create_string_buffer(encrypted)
            source = DataBlob(
                len(encrypted), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
            )
            target = DataBlob()
            if not ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)
            ):
                raise ctypes.WinError()
            try:
                records = json.loads(
                    ctypes.string_at(target.pbData, target.cbData).decode("utf-8")
                )
            finally:
                ctypes.windll.kernel32.LocalFree(target.pbData)
            save_vault(records)
            LEGACY_VAULT_FILE.rename(LEGACY_VAULT_FILE.with_suffix(".dat.migrated"))
            return records
        except Exception:
            return []
    return []


def save_vault(records):
    save_json(VAULT_FILE, records)


class DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def make_app_icon(size=256):
    if not Image:
        return None
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = max(8, size // 16)
    draw.rounded_rectangle(
        (margin, margin, size - margin, size - margin),
        radius=size // 4,
        fill=BLUE,
    )
    center = size // 2
    outer = size * 3 // 10
    inner = size // 8
    draw.ellipse(
        (center - outer, center - outer, center + outer, center + outer),
        outline="white",
        width=max(8, size // 12),
    )
    draw.ellipse(
        (center - inner, center - inner, center + inner, center + inner),
        fill="white",
    )
    draw.line(
        (center, margin + size // 8, center, center - outer),
        fill="white",
        width=max(5, size // 16),
    )
    return image


def ensure_app_icon():
    if not Image:
        return
    try:
        icon = make_app_icon()
        icon.save(
            APP_ICON_FILE,
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )
    except OSError:
        pass


def set_windows_app_id():
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Codex.ChromeMultiManager.2"
        )
    except Exception:
        pass


def log(message):
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}\n")


def find_chrome():
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    return next((item for item in candidates if item.exists()), None)


def parse_proxy(value):
    value = value.strip()
    if not value:
        return None
    if "://" not in value:
        value = "http://" + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https", "socks5"):
        raise ValueError("仅支持 http、https 和 socks5 代理。")
    if not parsed.hostname or not parsed.port:
        raise ValueError("代理格式应为 协议://主机:端口。")
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": parsed.port,
        "username": urllib.parse.unquote(parsed.username or ""),
        "password": urllib.parse.unquote(parsed.password or ""),
        "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
        "full": value,
    }


def test_proxy(value):
    proxy = parse_proxy(value)
    if not proxy:
        raise ValueError("请先填写代理服务器。")
    if proxy["scheme"] == "socks5" and proxy["username"]:
        raise ValueError("Chrome 不支持 SOCKS5 用户名密码认证，请使用无认证 SOCKS5。")
    started = time.perf_counter()
    result = subprocess.run(
        [
            "curl.exe", "-sS", "--proxy", proxy["full"],
            "--connect-timeout", "7", "--max-time", "15",
            "https://ipinfo.io/json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "代理连接失败。")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("代理返回内容异常，无法读取出口 IP。") from error
    elapsed = round((time.perf_counter() - started) * 1000)
    google = subprocess.run(
        [
            "curl.exe", "-sS", "-o", "NUL", "-w", "%{http_code}",
            "--proxy", proxy["full"], "--connect-timeout", "7", "--max-time", "15",
            "https://www.google.com/generate_204",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    google_ok = google.returncode == 0 and google.stdout.strip() in ("200", "204")
    return {
        "ip": data.get("ip", "未知"),
        "country": data.get("country", "未知"),
        "region": data.get("region", ""),
        "city": data.get("city", ""),
        "org": data.get("org", ""),
        "latency": elapsed,
        "google": google_ok,
    }


def prepare_proxy_extension(profile, proxy):
    extension = profile / "ChromeManagerProxyAuth"
    shutil.rmtree(extension, ignore_errors=True)
    if not proxy or not proxy["username"]:
        return None
    if proxy["scheme"] == "socks5":
        raise ValueError("Chrome 不支持 SOCKS5 用户名密码认证。")
    extension.mkdir(parents=True, exist_ok=True)
    manifest = {
        "manifest_version": 3,
        "name": "Chrome Manager Proxy Auth",
        "version": "1.0.0",
        "permissions": ["proxy", "webRequest", "webRequestAuthProvider"],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "background.js"},
    }
    config = {
        "mode": "fixed_servers",
        "rules": {
            "singleProxy": {
                "scheme": proxy["scheme"],
                "host": proxy["host"],
                "port": proxy["port"],
            },
            "bypassList": ["localhost", "127.0.0.1"],
        },
    }
    background = (
        f"const config = {json.dumps(config, ensure_ascii=False)};\n"
        "chrome.proxy.settings.set({value: config, scope: 'regular'});\n"
        "chrome.webRequest.onAuthRequired.addListener(\n"
        f"  (details, callback) => callback({{authCredentials: "
        f"{{username: {json.dumps(proxy['username'])}, "
        f"password: {json.dumps(proxy['password'])}}}}}),\n"
        "  {urls: ['<all_urls>']},\n"
        "  ['asyncBlocking']\n"
        ");\n"
    )
    (extension / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (extension / "background.js").write_text(background, encoding="utf-8")
    return extension


def proxy_bridge_port(cdp_port):
    return 30000 + int(cdp_port)


def ensure_proxy_bridge(profile, cdp_port, proxy):
    listen_port = proxy_bridge_port(cdp_port)
    config_path = profile / "proxy-bridge.json"
    save_json(config_path, {
        "host": proxy["host"],
        "port": proxy["port"],
        "username": proxy["username"],
        "password": proxy["password"],
    })
    if not port_open(listen_port):
        subprocess.Popen(
            [
                sys.executable, str(ROOT / "proxy_forwarder.py"),
                "--listen", str(listen_port), "--config", str(config_path),
            ],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        deadline = time.time() + 5
        while time.time() < deadline and not port_open(listen_port):
            time.sleep(0.05)
    if not port_open(listen_port):
        raise ValueError("本地代理认证桥启动失败。")
    return f"http://127.0.0.1:{listen_port}"


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.18):
            return True
    except OSError:
        return False


def port_pid(port):
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == int(port):
                return conn.pid
    except Exception:
        pass
    return None


def stop_port_listener(port):
    pid = port_pid(port)
    if not pid:
        return
    try:
        process = psutil.Process(pid)
        for child in process.children(recursive=True):
            child.kill()
        process.kill()
        process.wait(timeout=3)
    except (psutil.Error, OSError):
        pass


def cdp_alive(port):
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=0.35
        ) as response:
            data = json.load(response)
            return bool(data.get("webSocketDebuggerUrl") or data.get("Browser"))
    except Exception:
        return False


def browser_stats(port):
    pid = port_pid(port)
    if not pid:
        return None, 0, 0
    memory = 0
    try:
        process = psutil.Process(pid)
        processes = [process] + process.children(recursive=True)
        memory = sum(p.memory_info().rss for p in processes if p.is_running()) / 1024 / 1024
    except Exception:
        pass
    tabs = 0
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=0.35) as response:
            tabs = sum(1 for item in json.load(response) if item.get("type") == "page")
    except Exception:
        pass
    return pid, tabs, memory


def close_windows(pid):
    user32 = ctypes.windll.user32
    wm_close = 0x0010

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _):
        value = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(value))
        if value.value == pid:
            user32.PostMessageW(hwnd, wm_close, 0, 0)
        return True

    user32.EnumWindows(callback, 0)


def safe_extract_profile(archive, destination):
    root = destination.resolve()
    for info in archive.infolist():
        path = PurePosixPath(info.filename)
        if not path.parts or path.parts[0] != "profile":
            continue
        relative = Path(*path.parts[1:])
        if not relative.parts:
            continue
        target = (root / relative).resolve()
        if root != target and root not in target.parents:
            raise ValueError(f"备份包含不安全路径：{info.filename}")
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


class SingleInstance:
    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    def acquire(self):
        try:
            self.socket.bind(("127.0.0.1", LOCK_PORT))
            self.socket.listen(1)
            return True
        except OSError:
            return False

    def notify_existing(self):
        try:
            with socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=0.5) as client:
                client.sendall(b"SHOW")
        except OSError:
            pass

    def start_listener(self, callback):
        def listen():
            while True:
                try:
                    client, _ = self.socket.accept()
                    with client:
                        if client.recv(16) == b"SHOW":
                            callback()
                except OSError:
                    return
        threading.Thread(target=listen, daemon=True).start()


class EnvironmentDialog(tk.Toplevel):
    def __init__(self, parent, environment=None):
        super().__init__(parent)
        self.result = None
        self.title("浏览器环境配置")
        self.configure(bg=BG)
        self.transient(parent)
        self.grab_set()
        self.geometry("720x680")
        self.minsize(640, 560)
        env = normalize_environment(environment)

        shell = tk.Frame(self, bg=BG, padx=16, pady=16)
        shell.pack(fill="both", expand=True)
        card = tk.Frame(shell, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="both", expand=True)
        header = ttk.Frame(card, style="Panel.TFrame", padding=(22, 16))
        header.pack(fill="x")
        ttk.Label(header, text="浏览器环境配置", style="DialogTitle.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="用于开发测试与隐私控制。部分覆盖不能改变 TLS 或全部浏览器指纹。",
            style="PanelMuted.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        footer = ttk.Frame(card, style="Panel.TFrame", padding=(18, 12))
        footer.pack(side="bottom", fill="x")
        ttk.Button(footer, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(footer, text="确定保存", command=self.save).pack(side="right", padx=8)

        notebook = ttk.Notebook(card)
        notebook.pack(fill="both", expand=True, padx=18, pady=(4, 12))
        tabs = {}
        for key, title in (
            ("basic", "基础"),
            ("permissions", "权限"),
            ("privacy", "隐私"),
            ("advanced", "高级"),
        ):
            tabs[key] = ttk.Frame(notebook, style="Panel.TFrame", padding=18)
            notebook.add(tabs[key], text=title)

        self.vars = {}

        def entry(tab, label, key, value, width=28):
            row = ttk.Frame(tab, style="Panel.TFrame")
            row.pack(fill="x", pady=5)
            ttk.Label(row, text=label, background=PANEL, width=20).pack(side="left")
            variable = tk.StringVar(value=str(value))
            self.vars[key] = variable
            ttk.Entry(row, textvariable=variable, width=width).pack(
                side="left", fill="x", expand=True
            )

        def choice(tab, label, key, value, values):
            row = ttk.Frame(tab, style="Panel.TFrame")
            row.pack(fill="x", pady=5)
            ttk.Label(row, text=label, background=PANEL, width=20).pack(side="left")
            variable = tk.StringVar(value=value)
            self.vars[key] = variable
            ttk.Combobox(
                row, textvariable=variable, values=values, state="readonly"
            ).pack(side="left", fill="x", expand=True)

        def check(tab, label, key, value):
            variable = tk.BooleanVar(value=bool(value))
            self.vars[key] = variable
            ttk.Checkbutton(tab, text=label, variable=variable).pack(anchor="w", pady=5)

        entry(tabs["basic"], "User-Agent", "user_agent", env["user_agent"])
        entry(tabs["basic"], "窗口宽度", "window_width", env["window_width"])
        entry(tabs["basic"], "窗口高度", "window_height", env["window_height"])
        entry(tabs["basic"], "窗口 X 坐标", "window_x", env["window_x"])
        entry(tabs["basic"], "窗口 Y 坐标", "window_y", env["window_y"])
        entry(tabs["basic"], "浏览器语言", "language", env["language"])
        entry(
            tabs["basic"], "首选语言请求头", "accept_languages",
            env["accept_languages"],
        )
        entry(tabs["basic"], "时区", "timezone", env["timezone"])
        choice(
            tabs["basic"], "颜色模式", "color_mode", env["color_mode"],
            ("system", "light", "dark"),
        )
        check(tabs["basic"], "移动设备测试模式", "mobile_mode", env["mobile_mode"])
        check(tabs["basic"], "触摸事件模式", "touch_mode", env["touch_mode"])
        entry(
            tabs["basic"], "设备缩放比例", "device_scale_factor",
            env["device_scale_factor"],
        )

        for label, key in (
            ("允许加载图片", "images"),
            ("允许通知", "notifications"),
            ("允许摄像头", "camera"),
            ("允许麦克风", "microphone"),
            ("允许地理位置", "geolocation_permission"),
        ):
            check(tabs["permissions"], label, key, env[key])
        entry(tabs["permissions"], "下载目录", "download_dir", env["download_dir"])
        check(
            tabs["permissions"], "禁用密码保存提示",
            "disable_password_prompt", env["disable_password_prompt"],
        )
        check(
            tabs["permissions"], "禁用翻译提示",
            "disable_translate", env["disable_translate"],
        )
        check(tabs["permissions"], "无痕启动", "incognito", env["incognito"])

        choice(
            tabs["privacy"], "WebRTC 策略", "webrtc_policy",
            env["webrtc_policy"],
            ("default", "default_public_interface_only", "disable_non_proxied_udp"),
        )
        choice(
            tabs["privacy"], "DNS 模式", "dns_mode", env["dns_mode"],
            ("system", "disable_async", "secure", "custom"),
        )
        entry(tabs["privacy"], "安全 DNS 模板", "dns_template", env["dns_template"])
        check(tabs["privacy"], "启用地理位置模拟", "geo_enabled", env["geo_enabled"])
        entry(tabs["privacy"], "纬度", "latitude", env["latitude"])
        entry(tabs["privacy"], "经度", "longitude", env["longitude"])
        entry(tabs["privacy"], "定位精度（米）", "accuracy", env["accuracy"])
        ttk.Label(
            tabs["privacy"],
            text="以下为隐私测试覆盖，不保证绕过检测，并可能造成环境不一致：",
            style="PanelMuted.TLabel",
        ).pack(anchor="w", pady=(14, 5))
        check(tabs["privacy"], "Canvas 隐私扰动", "privacy_canvas", env["privacy_canvas"])
        check(tabs["privacy"], "WebGL 信息隐藏", "privacy_webgl", env["privacy_webgl"])
        check(tabs["privacy"], "硬件信息最小化", "privacy_hardware", env["privacy_hardware"])
        check(tabs["privacy"], "字体枚举限制", "privacy_fonts", env["privacy_fonts"])
        entry(
            tabs["advanced"], "模拟 CPU 线程数", "hardware_concurrency",
            env["hardware_concurrency"],
        )
        entry(
            tabs["advanced"], "模拟设备内存（GB）", "device_memory",
            env["device_memory"],
        )
        entry(
            tabs["advanced"], "WebGL 厂商", "webgl_vendor",
            env["webgl_vendor"],
        )
        entry(
            tabs["advanced"], "WebGL 渲染器", "webgl_renderer",
            env["webgl_renderer"],
        )

        entry(
            tabs["advanced"], "独立扩展目录（;分隔）", "extension_paths",
            ";".join(env["extension_paths"]),
        )
        ttk.Label(
            tabs["advanced"],
            text=(
                "TLS 指纹、真实显卡/CPU/内存和操作系统网络栈无法由普通 Chrome "
                "启动参数可靠改写。此处不会提供“保证不关联”的承诺。"
            ),
            style="PanelMuted.TLabel",
            wraplength=600,
            justify="left",
        ).pack(anchor="w", pady=12)

    def save(self):
        result = normalize_environment(DEFAULT_ENVIRONMENT)
        integer_keys = (
            "window_width", "window_height", "window_x", "window_y",
            "hardware_concurrency",
        )
        float_keys = ("latitude", "longitude", "accuracy", "device_scale_factor")
        try:
            for key, variable in self.vars.items():
                value = variable.get()
                if key in integer_keys:
                    value = int(value)
                elif key in float_keys:
                    value = float(value)
                elif key == "extension_paths":
                    value = [item.strip() for item in value.split(";") if item.strip()]
                result[key] = value
        except ValueError:
            messagebox.showerror("格式错误", "窗口、坐标、经纬度和缩放比例必须是数字。")
            return
        if result["window_width"] < 320 or result["window_height"] < 320:
            messagebox.showerror("窗口尺寸错误", "窗口宽度和高度不能小于 320。")
            return
        try:
            result["device_memory"] = float(result["device_memory"])
        except ValueError:
            messagebox.showerror("格式错误", "模拟设备内存必须是数字。")
            return
        if result["hardware_concurrency"] < 1 or result["device_memory"] <= 0:
            messagebox.showerror("硬件参数错误", "CPU 线程和设备内存必须大于 0。")
            return
        self.result = result
        self.destroy()


class BrowserDialog(tk.Toplevel):
    def __init__(self, parent, title, record=None, default_port=9231):
        super().__init__(parent)
        self.result = None
        self.title(title)
        self.configure(bg=BG)
        self.transient(parent)
        self.grab_set()
        self.resizable(True, True)
        screen_h = self.winfo_screenheight()
        height = min(680, max(500, screen_h - 100))
        self.geometry(f"570x{height}")
        self.minsize(500, 480)

        data = record or {}
        self.environment = normalize_environment(data.get("environment"))
        shell = tk.Frame(self, bg=BG, padx=18, pady=18)
        shell.pack(fill="both", expand=True)
        card = tk.Frame(shell, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="both", expand=True)
        header = ttk.Frame(card, style="Panel.TFrame", padding=(24, 20))
        header.pack(fill="x")
        ttk.Label(header, text=title, style="DialogTitle.TLabel").pack(anchor="w")
        ttk.Label(header, text="填写独立浏览器信息，保存后立即同步到管理列表。",
                  style="PanelMuted.TLabel").pack(anchor="w", pady=(4, 0))

        button_bar = ttk.Frame(card, style="Panel.TFrame", padding=(22, 16))
        button_bar.pack(side="bottom", fill="x")
        ttk.Button(button_bar, text="取消", style="Primary.TButton",
                   command=self.destroy).pack(side="right")
        ttk.Button(button_bar, text="确定保存", style="Primary.TButton",
                   command=self.save).pack(side="right", padx=(0, 8))

        holder = ttk.Frame(card, style="Panel.TFrame")
        holder.pack(fill="both", expand=True)
        canvas = tk.Canvas(holder, bg=PANEL, highlightthickness=0)
        scrollbar = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.form = ttk.Frame(canvas, style="Panel.TFrame", padding=(24, 10, 24, 20))
        window = canvas.create_window((0, 0), window=self.form, anchor="nw")
        self.form.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.bind_all("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))
        self.bind("<Destroy>", lambda _: canvas.unbind_all("<MouseWheel>"))

        fields = [
            ("名称", "name", data.get("name", "")),
            ("分组", "group", data.get("group", "默认分组")),
            ("CDP 端口", "port", str(data.get("port", default_port))),
            ("启动首页", "home", data.get("home", "about:blank")),
            ("代理服务器（可留空）", "proxy", data.get("proxy", "")),
            ("备注", "note", data.get("note", "")),
            ("定时启动（HH:MM，可留空）", "schedule", data.get("schedule", "")),
        ]
        self.vars = {}
        for label, key, value in fields:
            ttk.Label(self.form, text=label, background=PANEL).pack(anchor="w", pady=(9, 4))
            variable = tk.StringVar(value=value)
            self.vars[key] = variable
            ttk.Entry(self.form, textvariable=variable).pack(fill="x", ipady=6)
            if key == "proxy":
                proxy_row = ttk.Frame(self.form, style="Panel.TFrame")
                proxy_row.pack(fill="x", pady=(7, 2))
                ttk.Button(
                    proxy_row, text="测试代理", style="Primary.TButton",
                    command=self.run_proxy_test,
                ).pack(side="left")
                self.proxy_result = ttk.Label(
                    proxy_row, text="支持 http://用户:密码@IP:端口",
                    style="PanelMuted.TLabel",
                )
                self.proxy_result.pack(side="left", padx=10)
        self.auto_start = tk.BooleanVar(value=data.get("auto_start", False))
        ttk.Checkbutton(self.form, text="管理器启动时自动打开此浏览器",
                        variable=self.auto_start).pack(anchor="w", pady=(14, 8))
        ttk.Button(
            self.form, text="配置浏览器环境", style="Primary.TButton",
            command=self.edit_environment,
        ).pack(anchor="w", pady=(4, 8))
        self.bind("<Control-Return>", lambda _: self.save())
        self.bind("<Escape>", lambda _: self.destroy())
        self.after(50, self.focus_force)

    def edit_environment(self):
        dialog = EnvironmentDialog(self, self.environment)
        self.wait_window(dialog)
        if dialog.result:
            self.environment = dialog.result

    def run_proxy_test(self):
        value = self.vars["proxy"].get().strip()
        self.proxy_result.config(text="正在检测...")
        self.update_idletasks()
        try:
            result = test_proxy(value)
            location = " / ".join(
                item for item in (
                    result["country"], result["region"], result["city"]
                ) if item
            )
            self.proxy_result.config(
                text=(
                    f"出口：{result['ip']} · {result['latency']} ms · "
                    f"Google：{'可访问' if result['google'] else '不可访问'}"
                ),
                foreground=GREEN if result["google"] else RED,
            )
            messagebox.showinfo(
                "代理测试结果",
                f"出口 IP：{result['ip']}\n"
                f"国家/地区：{location or '未知'}\n"
                f"网络：{result['org'] or '未知'}\n"
                f"延迟：{result['latency']} ms\n"
                f"Google：{'可以访问' if result['google'] else '无法访问'}",
                parent=self,
            )
        except ValueError as error:
            self.proxy_result.config(text="测试失败", foreground=RED)
            messagebox.showerror("代理测试失败", str(error), parent=self)

    def save(self):
        name = self.vars["name"].get().strip()
        if not name:
            messagebox.showerror("名称不能为空", "请输入浏览器名称。", parent=self)
            return
        try:
            port = int(self.vars["port"].get())
            if not 1024 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror("端口错误", "请输入 1024 到 65535 之间的端口。", parent=self)
            return
        schedule = self.vars["schedule"].get().strip()
        proxy_value = self.vars["proxy"].get().strip()
        if proxy_value:
            try:
                proxy = parse_proxy(proxy_value)
                if proxy["scheme"] == "socks5" and proxy["username"]:
                    raise ValueError("Chrome 不支持 SOCKS5 用户名密码认证。")
            except ValueError as error:
                messagebox.showerror("代理格式错误", str(error), parent=self)
                return
        if schedule:
            try:
                datetime.strptime(schedule, "%H:%M")
            except ValueError:
                messagebox.showerror("时间格式错误", "定时启动请使用 HH:MM，例如 08:30。", parent=self)
                return
        self.result = {key: variable.get().strip() for key, variable in self.vars.items()}
        self.result["port"] = port
        self.result["auto_start"] = self.auto_start.get()
        self.result["environment"] = self.environment
        self.destroy()


class VaultDialog(tk.Toplevel):
    def __init__(self, parent, browsers, record=None):
        super().__init__(parent)
        self.result = None
        self.title("账号信息")
        self.configure(bg=BG)
        self.transient(parent)
        self.grab_set()
        self.resizable(True, True)
        screen_h = self.winfo_screenheight()
        height = min(680, max(500, screen_h - 120))
        self.geometry(f"580x{height}")
        self.minsize(520, 480)

        data = record or {}
        shell = tk.Frame(self, bg=BG, padx=18, pady=18)
        shell.pack(fill="both", expand=True)
        card = tk.Frame(shell, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="both", expand=True)
        header = ttk.Frame(card, style="Panel.TFrame", padding=(24, 20))
        header.pack(fill="x")
        ttk.Label(header, text="保存网站账号", style="DialogTitle.TLabel").pack(anchor="w")
        ttk.Label(
            header, text="账号和密码将直接以明文保存，请妥善保管程序文件夹。",
            style="PanelMuted.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        footer = ttk.Frame(card, style="Panel.TFrame", padding=(24, 16))
        footer.pack(side="bottom", fill="x")
        ttk.Button(footer, text="取消", style="Primary.TButton",
                   command=self.destroy).pack(side="right")
        ttk.Button(footer, text="确定保存", style="Primary.TButton",
                   command=self.save).pack(side="right", padx=(0, 8))

        holder = ttk.Frame(card, style="Panel.TFrame")
        holder.pack(fill="both", expand=True)
        canvas = tk.Canvas(holder, bg=PANEL, highlightthickness=0)
        scrollbar = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        form = ttk.Frame(canvas, style="Panel.TFrame", padding=(24, 8, 24, 16))
        window = canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind(
            "<Configure>",
            lambda _: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(window, width=event.width),
        )
        canvas.bind_all(
            "<MouseWheel>",
            lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"),
        )
        self.bind("<Destroy>", lambda _: canvas.unbind_all("<MouseWheel>"))
        self.vars = {}
        fields = (
            ("网站名称", "site", data.get("site", "")),
            ("网站地址", "url", data.get("url", "https://")),
            ("登录账号", "username", data.get("username", "")),
            ("登录密码", "password", data.get("password", "")),
            ("备注", "note", data.get("note", "")),
        )
        for label, key, value in fields:
            ttk.Label(form, text=label, background=PANEL).pack(anchor="w", pady=(8, 4))
            variable = tk.StringVar(value=value)
            self.vars[key] = variable
            entry = ttk.Entry(form, textvariable=variable, show="*" if key == "password" else "")
            entry.pack(fill="x", ipady=5)

        ttk.Label(form, text="所属浏览器", background=PANEL).pack(anchor="w", pady=(8, 4))
        browser_values = ["未指定"] + [f"{key} · {item['name']}" for key, item in browsers.items()]
        current_key = data.get("browser_key", "")
        current = next(
            (value for value in browser_values if value.startswith(f"{current_key} ·")),
            "未指定",
        )
        self.browser_var = tk.StringVar(value=current)
        ttk.Combobox(
            form, textvariable=self.browser_var, values=browser_values,
            state="readonly",
        ).pack(fill="x", ipady=4)

        self.bind("<Escape>", lambda _: self.destroy())
        self.bind("<Control-Return>", lambda _: self.save())

    def save(self):
        site = self.vars["site"].get().strip()
        if not site:
            messagebox.showerror("缺少网站名称", "请输入网站名称。", parent=self)
            return
        browser_value = self.browser_var.get()
        browser_key = "" if browser_value == "未指定" else browser_value.split(" · ", 1)[0]
        self.result = {key: value.get().strip() for key, value in self.vars.items()}
        self.result["browser_key"] = browser_key
        self.destroy()


class App:
    def __init__(self, root):
        self.root = root
        self.map = load_json(MAP_FILE, {})
        self.map_mtime = MAP_FILE.stat().st_mtime_ns if MAP_FILE.exists() else 0
        self.settings = load_json(
            SETTINGS_FILE,
            {"quick_urls": [], "minimize_to_tray": True, "password_hash": ""},
        )
        self.vault = load_vault()
        self.last_schedule_minute = ""
        self.tray = None
        self.configure_style()
        if not self.unlock():
            root.destroy()
            return
        self.build()
        self.refresh()
        self.start_tray()
        self.auto_start()
        self.tick()

    def configure_style(self):
        self.root.title(f"Chrome 多开管理器 {APP_VERSION}")
        self.root.geometry("1380x780")
        self.root.minsize(1120, 650)
        self.root.configure(bg=BG)
        self.apply_window_icon()
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=CARD,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Title.TLabel", background=PANEL, font=("Microsoft YaHei UI", 20, "bold"),
                        foreground=TEXT)
        style.configure("Section.TLabel", background=BG, font=("Microsoft YaHei UI", 15, "bold"),
                        foreground=TEXT)
        style.configure("DialogTitle.TLabel", background=PANEL, foreground=TEXT,
                        font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED,
                        font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Muted.TLabel", foreground=MUTED,
                        font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TEntry", padding=8, fieldbackground=PANEL, foreground=TEXT)
        style.configure("TCombobox", padding=7, fieldbackground=PANEL, foreground=TEXT,
                        arrowcolor=MUTED)
        style.configure("Treeview", background=CARD, foreground=TEXT,
                        fieldbackground=CARD, rowheight=46, borderwidth=0,
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Treeview.Heading", background="#f4f2ed", foreground="#57534e",
                        bordercolor=BORDER, relief="flat",
                        font=("Microsoft YaHei UI", 10, "bold"), padding=(8, 11))
        style.map("Treeview", background=[("selected", "#f2dfd8")],
                  foreground=[("selected", TEXT)])
        style.configure("TButton", padding=(12, 8), background=BLUE, foreground="white",
                        bordercolor=BLUE, relief="flat",
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.map("TButton", background=[("active", "#b9684f")],
                  foreground=[("active", "white")])
        style.configure("Primary.TButton", background=BLUE, foreground="white",
                        bordercolor=BLUE)
        style.map("Primary.TButton", background=[("active", "#b9684f")])
        style.configure("Success.TButton", background="#eef7ef", foreground=GREEN,
                        bordercolor=BLUE)
        style.configure("Danger.TButton", background=BLUE, foreground="white",
                        bordercolor=BLUE)
        style.configure("Purple.TButton", background=BLUE, foreground="white",
                        bordercolor=BLUE)
        style.configure("Success.TButton", background=BLUE, foreground="white",
                        bordercolor=BLUE)
        style.configure("Icon.TButton", padding=(9, 7), background=BLUE,
                        foreground="white", bordercolor=BLUE)
        for button_style in ("Success.TButton", "Danger.TButton", "Purple.TButton", "Icon.TButton"):
            style.map(button_style, background=[("active", "#b9684f")],
                      foreground=[("active", "white")])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 8), background=PANEL, foreground=MUTED)
        style.map("TNotebook.Tab", background=[("selected", BLUE)], foreground=[("selected", "white")])
        style.configure("Hidden.TNotebook", background=BG, borderwidth=0, tabmargins=0)
        style.layout("Hidden.TNotebook.Tab", [])

    def create_app_icon(self, size=64):
        return make_app_icon(size)

    def apply_window_icon(self):
        if APP_ICON_FILE.exists():
            try:
                self.root.iconbitmap(default=str(APP_ICON_FILE))
            except tk.TclError:
                pass
            self.root.update_idletasks()
            try:
                load_image = ctypes.windll.user32.LoadImageW
                load_image.restype = ctypes.wintypes.HANDLE
                flags = 0x0010
                self.native_big_icon = load_image(
                    None, str(APP_ICON_FILE), 1, 32, 32, flags
                )
                self.native_small_icon = load_image(
                    None, str(APP_ICON_FILE), 1, 16, 16, flags
                )
                hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
                if not hwnd:
                    hwnd = self.root.winfo_id()
                ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 1, self.native_big_icon)
                ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 0, self.native_small_icon)
            except Exception:
                pass
        if not ImageTk:
            return
        icon = self.create_app_icon(64)
        if icon:
            self.window_icon = ImageTk.PhotoImage(icon)
            self.root.iconphoto(True, self.window_icon)

    def unlock(self):
        password_hash = self.settings.get("password_hash", "")
        if not password_hash:
            return True
        value = simpledialog.askstring("管理器已锁定", "请输入管理器密码：", show="*", parent=self.root)
        if value and hashlib.sha256(value.encode()).hexdigest() == password_hash:
            return True
        messagebox.showerror("密码错误", "密码不正确。")
        return False

    def build(self):
        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill="both", expand=True)

        sidebar = tk.Frame(shell, bg=SIDEBAR, width=176, highlightthickness=1,
                           highlightbackground=BORDER)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = tk.Frame(sidebar, bg=SIDEBAR)
        brand.pack(fill="x", padx=18, pady=(22, 26))
        tk.Label(brand, text="C", bg=BLUE, fg="white", width=2, height=1,
                 font=("Segoe UI", 15, "bold")).pack(side="left")
        tk.Label(brand, text="  Chrome\n  Manager", bg=SIDEBAR, fg=TEXT,
                 justify="left", font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")

        content = tk.Frame(shell, bg=BG)
        content.pack(side="left", fill="both", expand=True)
        header = ttk.Frame(content, style="Panel.TFrame", padding=(24, 17))
        header.pack(fill="x")
        left = ttk.Frame(header, style="Panel.TFrame")
        left.pack(side="left")
        ttk.Label(left, text="独立浏览器", style="Title.TLabel").pack(anchor="w")
        ttk.Label(left, text="管理本地独立 Chrome 环境，并允许 Codex 通过 CDP 精准控制",
                  style="PanelMuted.TLabel").pack(anchor="w", pady=(3, 0))
        self.summary = ttk.Label(header, text="", foreground=GREEN,
                                 background=PANEL, font=("Microsoft YaHei UI", 10, "bold"))
        self.summary.pack(side="right")

        self.notebook = ttk.Notebook(content, style="Hidden.TNotebook")
        self.notebook.pack(fill="both", expand=True, padx=22, pady=18)
        self.main_tab = ttk.Frame(self.notebook)
        self.password_tab = ttk.Frame(self.notebook)
        self.log_tab = ttk.Frame(self.notebook)
        self.settings_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.main_tab, text="浏览器管理")
        self.notebook.add(self.password_tab, text="密码管理")
        self.notebook.add(self.log_tab, text="操作日志")
        self.notebook.add(self.settings_tab, text="程序设置")
        self.notebook.bind("<<NotebookTabChanged>>", self.on_page_changed)

        self.nav_buttons = []
        nav_items = (
            ("浏览器", 0), ("密码管理", 1),
            ("操作日志", 2), ("程序设置", 3),
        )
        for text, index in nav_items:
            button = tk.Button(
                sidebar, text=text, anchor="w", relief="flat", borderwidth=0,
                bg=SIDEBAR, fg=TEXT, activebackground=HOVER, activeforeground=TEXT,
                font=("Microsoft YaHei UI", 11, "bold"), padx=22, pady=12,
                command=lambda target=index: self.select_page(target),
            )
            button.pack(fill="x", padx=9, pady=2)
            self.nav_buttons.append(button)
        tk.Label(sidebar, text=f"开发版  {APP_VERSION}", bg=SIDEBAR, fg=MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(side="bottom", pady=18)

        self.build_main()
        self.build_passwords()
        self.build_logs()
        self.build_settings()
        self.select_page(0)

    def select_page(self, index):
        self.notebook.select(index)
        for position, button in enumerate(self.nav_buttons):
            active = position == index
            button.configure(
                bg="#e7e2d9" if active else SIDEBAR,
                fg=BLUE if active else TEXT,
                font=("Microsoft YaHei UI", 11, "bold"),
            )
        self.root.after_idle(self.reposition_page_buttons)

    def on_page_changed(self, _event=None):
        self.root.after_idle(self.reposition_page_buttons)

    def reposition_page_buttons(self):
        if hasattr(self, "action_buttons"):
            self.position_action_buttons()
        if hasattr(self, "vault_action_buttons"):
            self.position_vault_buttons()

    def build_main(self):
        self.search_var = tk.StringVar()
        self.group_filter_var = tk.StringVar(value="全部分组")
        self.status_filter_var = tk.StringVar(value="全部状态")

        filters = ttk.Frame(self.main_tab)
        filters.pack(fill="x", pady=(0, 10))
        self.group_filter = ttk.Combobox(
            filters, textvariable=self.group_filter_var, state="readonly", width=16
        )
        self.group_filter.pack(side="left", padx=(0, 8))
        self.group_filter.bind("<<ComboboxSelected>>", lambda _: self.refresh())
        ttk.Entry(filters, textvariable=self.search_var, width=34).pack(side="left", padx=(0, 8))
        self.status_filter = ttk.Combobox(
            filters, textvariable=self.status_filter_var, state="readonly", width=13,
            values=("全部状态", "运行中", "未启动", "端口占用"),
        )
        self.status_filter.pack(side="left", padx=(0, 8))
        self.status_filter.bind("<<ComboboxSelected>>", lambda _: self.refresh())
        ttk.Button(filters, text="筛选", command=self.refresh).pack(side="left", padx=(0, 6))
        ttk.Button(filters, text="重置条件", command=self.reset_filters).pack(side="left")
        ttk.Button(filters, text="刷新", command=self.refresh,
                   style="Icon.TButton").pack(side="right")

        toolbar = ttk.Frame(self.main_tab)
        toolbar.pack(fill="x", pady=(0, 12))
        ttk.Button(toolbar, text="＋ 新建浏览器", command=self.create,
                   style="Primary.TButton").pack(side="left", padx=(0, 8))
        ttk.Button(toolbar, text="关闭选中", command=self.stop_selected,
                   style="Danger.TButton").pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="更多操作", command=self.more_menu).pack(side="left")
        ttk.Button(toolbar, text="全部启动", command=self.start_all).pack(side="right")

        columns = ("name", "group", "port", "status", "pid", "tabs", "memory", "home", "proxy", "action")
        table = ttk.Frame(self.main_tab, style="Panel.TFrame")
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            table, columns=columns, show="headings", selectmode="extended"
        )
        headers = {
            "name": "浏览器名称", "action": "操作", "group": "分组", "port": "CDP 端口",
            "status": "设备状态", "pid": "进程 PID", "tabs": "标签页", "memory": "内存",
            "home": "启动首页", "proxy": "代理配置",
        }
        widths = {
            "name": 110, "group": 80, "port": 70, "status": 75, "pid": 70,
            "tabs": 55, "memory": 70, "home": 150, "proxy": 100, "action": 220,
        }
        for column in columns:
            self.tree.heading(column, text=headers[column], anchor="center")
            self.tree.column(
                column, width=widths[column], minwidth=widths[column],
                anchor="center", stretch=column in ("home", "proxy"),
            )
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.on_tree_scroll)
        self.tree_scrollbar = vertical
        self.tree.configure(yscrollcommand=self.on_tree_yview)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self.on_tree_double_click)
        self.tree.bind("<Configure>", lambda _: self.position_action_buttons())
        self.tree.bind("<MouseWheel>", lambda _: self.root.after_idle(self.position_action_buttons))
        self.tree.tag_configure("running", foreground=GREEN)
        self.tree.tag_configure("occupied", foreground=RED)
        self.action_buttons = {}
        ttk.Label(self.main_tab, text="点击列表中的“启动”可打开对应浏览器；按 Ctrl 或 Shift 可多选。",
                  style="Muted.TLabel").pack(anchor="w", pady=8)

    def on_tree_yview(self, first, last):
        self.tree_scrollbar.set(first, last)
        self.root.after_idle(self.position_action_buttons)

    def on_tree_scroll(self, *args):
        self.tree.yview(*args)
        self.root.after_idle(self.position_action_buttons)

    def position_action_buttons(self):
        if not hasattr(self, "action_buttons"):
            return
        for key, button_group in self.action_buttons.items():
            box = self.tree.bbox(key, "action")
            if not box:
                button_group.place_forget()
                continue
            x, y, width, height = box
            button_group.place(
                x=x + 5, y=y + 6, width=max(204, width - 10), height=max(32, height - 12)
            )

    def start_row(self, key):
        record = self.map.get(key)
        if not record or port_open(record["port"]):
            return
        self.tree.selection_set(key)
        self.start_browser(record)
        self.root.after(1200, self.refresh)

    def edit_row(self, key):
        if key not in self.map:
            return
        self.tree.selection_set(key)
        self.edit()

    def delete_row(self, key):
        if key not in self.map:
            return
        self.tree.selection_set(key)
        self.delete()

    def on_tree_double_click(self, event):
        row = self.tree.identify_row(event.y)
        if row and row in self.map:
            self.tree.selection_set(row)
            self.start_browser(self.map[row])
            self.root.after(1200, self.refresh)

    def reset_filters(self):
        self.search_var.set("")
        self.group_filter_var.set("全部分组")
        self.status_filter_var.set("全部状态")
        self.refresh()

    def build_passwords(self):
        info = tk.Frame(
            self.password_tab, bg="#f1ede5", highlightthickness=1,
            highlightbackground=BORDER, padx=16, pady=12,
        )
        info.pack(fill="x", pady=(0, 12))
        tk.Label(
            info,
            text="注意：账号与密码以明文保存在 password-vault.json。"
                 "请勿把程序文件夹或导出的 CSV 发给其他人。"
                 "本程序不会自动读取或解密 Chrome 已保存密码。",
            bg="#f1ede5", fg=TEXT, anchor="w",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(fill="x")

        actions = ttk.Frame(self.password_tab)
        actions.pack(fill="x", pady=(0, 10))
        for text, command in (
            ("新增账号", self.add_vault_entry),
            ("复制账号", self.copy_vault_username),
            ("复制密码", self.copy_vault_password),
            ("导入 Chrome CSV", self.import_chrome_csv),
            ("导出完整 CSV", self.export_vault_csv),
            ("导出 Google CSV", self.export_google_csv),
            ("打开 Google 导入页", self.open_chrome_passwords),
        ):
            ttk.Button(
                actions, text=text, command=command, style="Primary.TButton"
            ).pack(side="left", padx=(0, 7))

        columns = ("site", "url", "username", "password", "browser", "note", "action")
        self.vault_tree = ttk.Treeview(
            self.password_tab, columns=columns, show="headings", selectmode="browse"
        )
        headers = {
            "site": "网站", "url": "网址", "username": "账号", "password": "密码",
            "browser": "所属浏览器", "note": "备注", "action": "操作",
        }
        widths = {
            "site": 100, "url": 180, "username": 125, "password": 125,
            "browser": 105, "note": 145, "action": 220,
        }
        for column in columns:
            self.vault_tree.heading(column, text=headers[column], anchor="center")
            self.vault_tree.column(
                column, width=widths[column], anchor="center",
                stretch=column in ("url", "note"), minwidth=widths[column],
            )
        self.vault_tree.pack(fill="both", expand=True)
        self.vault_tree.bind("<Double-1>", lambda _: self.edit_vault_entry())
        self.vault_tree.bind("<Configure>", lambda _: self.position_vault_buttons())
        self.vault_tree.bind(
            "<MouseWheel>", lambda _: self.root.after_idle(self.position_vault_buttons)
        )
        self.vault_action_buttons = {}
        self.refresh_vault()

    def selected_vault_index(self):
        selected = self.vault_tree.selection()
        if not selected:
            messagebox.showinfo("请选择账号", "请先选择一条账号记录。")
            return None
        return int(selected[0])

    def refresh_vault(self):
        if not hasattr(self, "vault_tree"):
            return
        for button_group in getattr(self, "vault_action_buttons", {}).values():
            button_group.destroy()
        self.vault_action_buttons = {}
        self.vault_tree.delete(*self.vault_tree.get_children())
        for index, record in enumerate(self.vault):
            browser = self.map.get(record.get("browser_key", ""), {}).get("name", "未指定")
            key = str(index)
            self.vault_tree.insert(
                "", "end", iid=key,
                values=(
                    record.get("site", ""), record.get("url", ""),
                    record.get("username", ""), record.get("password", ""),
                    browser, record.get("note", ""), "",
                ),
            )
            button_group = tk.Frame(self.vault_tree, bg=CARD)
            for text, command in (
                ("打开", lambda row=key: self.open_vault_row(row)),
                ("编辑", lambda row=key: self.edit_vault_row(row)),
                ("删除", lambda row=key: self.delete_vault_row(row)),
            ):
                tk.Button(
                    button_group, text=text, command=command, relief="flat",
                    borderwidth=0, bg=BLUE, fg="white",
                    activebackground="#b9684f", activeforeground="white",
                    font=("Microsoft YaHei UI", 9, "bold"), cursor="hand2",
                ).pack(side="left", fill="both", expand=True, padx=2)
            self.vault_action_buttons[key] = button_group
        self.root.after_idle(self.position_vault_buttons)

    def position_vault_buttons(self):
        if not hasattr(self, "vault_action_buttons"):
            return
        for key, button_group in self.vault_action_buttons.items():
            box = self.vault_tree.bbox(key, "action")
            if not box:
                button_group.place_forget()
                continue
            x, y, width, height = box
            button_group.place(
                x=x + 5, y=y + 6, width=max(204, width - 10), height=max(32, height - 12)
            )

    def open_vault_row(self, key):
        if not self.vault_tree.exists(key):
            return
        self.vault_tree.selection_set(key)
        self.open_vault_site()

    def edit_vault_row(self, key):
        if not self.vault_tree.exists(key):
            return
        self.vault_tree.selection_set(key)
        self.edit_vault_entry()

    def delete_vault_row(self, key):
        if not self.vault_tree.exists(key):
            return
        self.vault_tree.selection_set(key)
        self.delete_vault_entry()

    def add_vault_entry(self):
        dialog = VaultDialog(self.root, self.map)
        self.root.wait_window(dialog)
        if dialog.result:
            self.vault.append(dialog.result)
            save_vault(self.vault)
            self.refresh_vault()

    def edit_vault_entry(self):
        index = self.selected_vault_index()
        if index is None:
            return
        dialog = VaultDialog(self.root, self.map, self.vault[index])
        self.root.wait_window(dialog)
        if dialog.result:
            self.vault[index] = dialog.result
            save_vault(self.vault)
            self.refresh_vault()

    def delete_vault_entry(self):
        index = self.selected_vault_index()
        if index is None:
            return
        if messagebox.askyesno("删除账号", f"确定删除 [{self.vault[index].get('site', '')}]？"):
            self.vault.pop(index)
            save_vault(self.vault)
            self.refresh_vault()

    def copy_vault_value(self, field, label):
        index = self.selected_vault_index()
        if index is None:
            return
        value = self.vault[index].get(field, "")
        if not value:
            messagebox.showinfo("没有内容", f"这条记录没有保存{label}。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        messagebox.showinfo("已复制", f"{label}已复制到剪贴板。")

    def copy_vault_username(self):
        self.copy_vault_value("username", "账号")

    def copy_vault_password(self):
        self.copy_vault_value("password", "密码")

    def open_vault_site(self):
        index = self.selected_vault_index()
        if index is None:
            return
        record = self.vault[index]
        url = record.get("url", "").strip()
        if not url:
            messagebox.showinfo("没有网址", "这条记录没有保存网站地址。")
            return
        browser = self.map.get(record.get("browser_key", ""))
        if browser:
            self.start_browser(browser, url)
        else:
            os.startfile(url)

    def export_vault_csv(self):
        if not self.vault:
            messagebox.showinfo("没有账号", "账号库中还没有可导出的记录。")
            return
        path = filedialog.asksaveasfilename(
            title="导出完整账号 CSV",
            initialfile="账号密码明文导出.csv",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv")],
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["网站", "网址", "账号", "密码", "所属浏览器", "备注"])
            for record in self.vault:
                browser = self.map.get(record.get("browser_key", ""), {}).get("name", "")
                writer.writerow([
                    record.get("site", ""), record.get("url", ""),
                    record.get("username", ""), record.get("password", ""),
                    browser, record.get("note", ""),
                ])
        messagebox.showwarning(
            "导出完成",
            f"明文账号密码已导出到：\n{path}\n\n该文件包含明文密码，请妥善保管。",
        )

    def import_chrome_csv(self):
        paths = filedialog.askopenfilenames(
            title="选择一个或多个 Chrome 密码 CSV",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not paths:
            return

        existing = {
            (record.get("url", "").strip().lower(), record.get("username", "").strip().lower()): index
            for index, record in enumerate(self.vault)
        }
        added = 0
        updated = 0
        skipped = 0
        for path in paths:
            try:
                with open(path, "r", newline="", encoding="utf-8-sig") as handle:
                    reader = csv.DictReader(handle)
                    fieldnames = {name.lower().strip() for name in (reader.fieldnames or [])}
                    if not {"url", "username", "password"}.issubset(fieldnames):
                        skipped += 1
                        continue
                    for row in reader:
                        normalized = {
                            str(key).lower().strip(): (value or "").strip()
                            for key, value in row.items() if key is not None
                        }
                        url = normalized.get("url", "")
                        username = normalized.get("username", "")
                        if not url and not username:
                            continue
                        record = {
                            "site": normalized.get("name", "") or url,
                            "url": url,
                            "username": username,
                            "password": normalized.get("password", ""),
                            "note": normalized.get("note", ""),
                            "browser_key": "",
                        }
                        identity = (url.lower(), username.lower())
                        if identity in existing:
                            index = existing[identity]
                            browser_key = self.vault[index].get("browser_key", "")
                            self.vault[index].update(record)
                            self.vault[index]["browser_key"] = browser_key
                            updated += 1
                        else:
                            existing[identity] = len(self.vault)
                            self.vault.append(record)
                            added += 1
            except (OSError, UnicodeError, csv.Error):
                skipped += 1

        save_vault(self.vault)
        self.refresh_vault()
        messagebox.showinfo(
            "导入完成",
            f"新增 {added} 条，更新 {updated} 条。"
            + (f"\n有 {skipped} 个文件格式不正确或无法读取。" if skipped else ""),
        )

    def export_google_csv(self):
        if not self.vault:
            messagebox.showinfo("没有账号", "账号库中还没有可导出的记录。")
            return
        path = filedialog.asksaveasfilename(
            title="导出 Google Password Manager CSV",
            initialfile="google-passwords-import.csv",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv")],
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["url", "username", "password"])
            writer.writeheader()
            for record in self.vault:
                writer.writerow({
                    "url": record.get("url", ""),
                    "username": record.get("username", ""),
                    "password": record.get("password", ""),
                })
        messagebox.showwarning(
            "Google CSV 已生成",
            f"导入文件已保存到：\n{path}\n\n"
            "接下来选择一条已指定浏览器的账号，再点击“打开 Google 导入页”手动导入。"
            "导入完成后建议删除该 CSV。",
        )

    def open_chrome_passwords(self):
        index = self.selected_vault_index()
        if index is None:
            return
        browser_key = self.vault[index].get("browser_key", "")
        browser = self.map.get(browser_key)
        if not browser:
            messagebox.showinfo("未指定浏览器", "请先编辑账号并选择所属浏览器。")
            return
        self.start_browser(browser, "chrome://password-manager/settings")

    def build_quick(self):
        row = ttk.Frame(self.quick_tab)
        row.pack(fill="x", pady=(0, 10))
        ttk.Button(row, text="新增快捷网址", style="Primary.TButton",
                   command=self.add_quick).pack(side="left")
        ttk.Button(row, text="删除选中", command=self.remove_quick).pack(side="left", padx=6)
        self.quick_tree = ttk.Treeview(self.quick_tab, columns=("name", "url"),
                                       show="headings", selectmode="extended")
        self.quick_tree.heading("name", text="名称")
        self.quick_tree.heading("url", text="网址")
        self.quick_tree.column("name", width=220)
        self.quick_tree.column("url", width=700)
        self.quick_tree.pack(fill="both", expand=True)
        ttk.Button(self.quick_tab, text="在选中浏览器打开", style="Purple.TButton",
                   command=self.open_quick).pack(anchor="e", pady=10)
        self.refresh_quick()

    def build_logs(self):
        self.log_text = tk.Text(self.log_tab, bg=CARD, fg=TEXT, insertbackground=TEXT,
                                relief="flat", font=("Consolas", 10))
        self.log_text.pack(fill="both", expand=True)
        row = ttk.Frame(self.log_tab)
        row.pack(fill="x", pady=8)
        ttk.Button(row, text="刷新日志", command=self.refresh_logs).pack(side="left")
        ttk.Button(row, text="清空日志", style="Danger.TButton",
                   command=self.clear_logs).pack(side="left", padx=6)
        self.refresh_logs()

    def build_settings(self):
        box = ttk.Frame(self.settings_tab, style="Panel.TFrame", padding=22)
        box.pack(fill="x")
        self.tray_var = tk.BooleanVar(value=self.settings.get("minimize_to_tray", True))
        ttk.Checkbutton(
            box,
            text="关闭主窗口时最小化到系统托盘",
            variable=self.tray_var,
            command=self.save_settings,
        ).pack(anchor="w", pady=5)
        self.run_var = tk.BooleanVar(value=self.is_run_at_startup())
        ttk.Checkbutton(box, text="Windows 登录后自动启动管理器",
                        variable=self.run_var, command=self.toggle_run_startup).pack(anchor="w", pady=5)
        row = ttk.Frame(box, style="Panel.TFrame")
        row.pack(fill="x", pady=12)
        ttk.Button(row, text="设置/修改密码", command=self.set_password).pack(side="left")
        ttk.Button(row, text="清除密码", command=self.clear_password).pack(side="left", padx=6)
        ttk.Button(row, text="导出全部配置", command=self.export_config).pack(side="left", padx=6)
        ttk.Button(row, text="导入配置", command=self.import_config).pack(side="left", padx=6)
        ttk.Button(row, text="检查 Chrome 版本", command=self.chrome_version).pack(side="left", padx=6)
        ttk.Label(box, text=f"版本：{APP_VERSION}\n数据目录：{ROOT}",
                  style="PanelMuted.TLabel").pack(anchor="w", pady=12)

    def selected(self, single=False):
        keys = list(self.tree.selection())
        if not keys:
            messagebox.showinfo("请选择", "请先选择浏览器。")
            return []
        if single and len(keys) != 1:
            messagebox.showinfo("只能选择一个", "此操作一次只能选择一个浏览器。")
            return []
        return keys

    def next_index(self):
        numbers = [int(key[7:]) for key in self.map
                   if key.startswith("browser") and key[7:].isdigit()]
        for path in PROFILES.glob("browser-*"):
            suffix = path.name.removeprefix("browser-")
            if suffix.isdigit():
                numbers.append(int(suffix))
        return max(numbers, default=0) + 1

    def next_port(self, extra_used=None):
        used = {int(record["port"]) for record in self.map.values()}
        used.update(extra_used or set())
        port = 9231
        while port in used or port_open(port):
            port += 1
        return port

    def profile_path(self, record):
        return ROOT / record["profile"]

    def save_map(self):
        save_json(MAP_FILE, self.map)
        self.map_mtime = MAP_FILE.stat().st_mtime_ns

    def reload_external_map(self):
        if not MAP_FILE.exists():
            return
        current_mtime = MAP_FILE.stat().st_mtime_ns
        if current_mtime == self.map_mtime:
            return
        incoming = load_json(MAP_FILE, None)
        if isinstance(incoming, dict):
            self.map = incoming
            self.map_mtime = current_mtime
            log("检测到外部配置变更，已自动重新加载 browser-map.json")

    def port_conflict(self, port, exclude=None):
        configured = any(key != exclude and int(record["port"]) == int(port)
                         for key, record in self.map.items())
        if configured or port_open(port):
            messagebox.showerror("端口冲突", f"端口 {port} 已被配置或正在使用。")
            return True
        return False

    def clear_launchers(self, key):
        for path in LAUNCHERS.glob(f"启动-{key}-*.cmd"):
            path.unlink(missing_ok=True)

    def write_launcher(self, key, record):
        self.clear_launchers(key)
        safe_name = "".join("_" if char in '<>:"/\\|?*' else char for char in record["name"])
        launcher = LAUNCHERS / f"启动-{key}-{safe_name}.cmd"
        content = (
            '@echo off\r\n'
            'cd /d "%~dp0.."\r\n'
            f'python browser_cli.py start {key}\r\n'
        )
        launcher.write_text(content, encoding="utf-8-sig")

    def create(self):
        index = self.next_index()
        dialog = BrowserDialog(self.root, "新建独立浏览器", default_port=self.next_port())
        self.root.wait_window(dialog)
        if not dialog.result or self.port_conflict(dialog.result["port"]):
            return
        key = f"browser{index}"
        record = dialog.result
        record["profile"] = f"profiles/browser-{index}"
        record["created_at"] = datetime.now().isoformat(timespec="seconds")
        self.profile_path(record).mkdir(parents=True, exist_ok=True)
        self.map[key] = record
        self.save_map()
        self.write_launcher(key, record)
        log(f"创建浏览器：{record['name']}，端口 {record['port']}")
        self.refresh()

    def edit(self):
        keys = self.selected(single=True)
        if not keys:
            return
        key = keys[0]
        old = self.map[key].copy()
        dialog = BrowserDialog(self.root, "编辑浏览器", old, old["port"])
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        if dialog.result["port"] != old["port"] and self.port_conflict(dialog.result["port"], key):
            return
        dialog.result["profile"] = old["profile"]
        dialog.result["created_at"] = old.get("created_at", "")
        self.map[key] = dialog.result
        self.save_map()
        self.write_launcher(key, dialog.result)
        log(f"编辑浏览器：{dialog.result['name']}")
        self.refresh()
        restart_changes = (
            dialog.result.get("proxy", "") != old.get("proxy", "")
            or normalize_environment(dialog.result.get("environment"))
            != normalize_environment(old.get("environment"))
        )
        if restart_changes and port_open(old["port"]):
            messagebox.showwarning(
                "需要重启浏览器",
                "代理或浏览器环境配置已经保存，但部分启动参数和权限需要重启后生效。\n\n"
                "请先关闭该独立浏览器，再重新启动。",
            )

    def clone(self):
        keys = self.selected(single=True)
        if not keys:
            return
        source_key = keys[0]
        source = self.map[source_key]
        if port_open(source["port"]):
            messagebox.showwarning("请先关闭", "复制前请关闭源浏览器。")
            return
        name = simpledialog.askstring("复制浏览器", "新浏览器名称：",
                                      initialvalue=f"{source['name']} 副本")
        if not name:
            return
        index = self.next_index()
        record = source.copy()
        record.update(name=name, port=self.next_port(), profile=f"profiles/browser-{index}",
                      auto_start=False, schedule="",
                      created_at=datetime.now().isoformat(timespec="seconds"))
        source_path = self.profile_path(source)
        target_path = self.profile_path(record)

        def ignore_locks(_, names):
            ignored = {"lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"}
            return [name for name in names if name in ignored or name.startswith("Singleton")]

        try:
            if source_path.exists():
                shutil.copytree(source_path, target_path, dirs_exist_ok=True,
                                ignore=ignore_locks)
            key = f"browser{index}"
            self.map[key] = record
            self.save_map()
            self.write_launcher(key, record)
            log(f"复制浏览器：{source['name']} -> {name}")
            self.refresh()
        except Exception as exc:
            messagebox.showerror("复制失败", str(exc))

    def delete(self):
        keys = self.selected()
        if not keys:
            return
        if any(port_open(self.map[key]["port"]) for key in keys):
            messagebox.showwarning("请先关闭", "删除前请关闭选中的浏览器。")
            return
        remove_data = messagebox.askyesnocancel(
            "删除浏览器",
            "选择“是”：删除配置和全部浏览数据。\n"
            "选择“否”：只删除配置，保留 profile。\n"
            "选择“取消”：不执行。",
        )
        if remove_data is None:
            return
        for key in keys:
            record = self.map.pop(key)
            self.clear_launchers(key)
            if remove_data:
                shutil.rmtree(self.profile_path(record), ignore_errors=True)
            log(f"删除浏览器：{record['name']}，删除数据={remove_data}")
        self.save_map()
        self.refresh()

    def start_browser(self, record, url=None):
        chrome = find_chrome()
        if not chrome:
            messagebox.showerror("未找到 Chrome", "请先安装 Google Chrome。")
            return False
        if port_open(record["port"]) and not cdp_alive(record["port"]):
            messagebox.showerror(
                "端口被其他程序占用",
                f"端口 {record['port']} 正在监听，但不是可控制的 Chrome。"
            )
            return False
        if cdp_alive(record["port"]):
            if url:
                subprocess.Popen([str(chrome), f"--user-data-dir={self.profile_path(record)}", url])
            return True
        profile = self.profile_path(record)
        profile.mkdir(parents=True, exist_ok=True)
        environment = normalize_environment(record.get("environment"))
        apply_profile_preferences(profile, environment)
        arguments = [
            str(chrome),
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={record['port']}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=http://127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        arguments.extend(build_chrome_arguments(environment))
        extensions = extension_paths(profile, environment)
        if extensions:
            extension_value = ",".join(str(path) for path in extensions)
            arguments.append(f"--load-extension={extension_value}")
        if record.get("proxy"):
            try:
                proxy = parse_proxy(record["proxy"])
                extension = prepare_proxy_extension(profile, proxy)
            except ValueError as error:
                messagebox.showerror("代理配置错误", str(error))
                return False
            if extension:
                bridge = ensure_proxy_bridge(profile, record["port"], proxy)
                arguments.append(f"--proxy-server={bridge}")
            else:
                arguments.append(f"--proxy-server={proxy['server']}")
        arguments.append(url or record.get("home") or "about:blank")
        subprocess.Popen(arguments, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        deadline = time.time() + 12
        while time.time() < deadline and not cdp_alive(record["port"]):
            self.root.update()
            time.sleep(0.08)
        if cdp_alive(record["port"]):
            ensure_environment_controller(
                ROOT, profile, record["port"], environment
            )
        log(f"启动浏览器：{record['name']}，端口 {record['port']}")
        return True

    def start_selected(self):
        for key in self.selected():
            self.start_browser(self.map[key])
        self.root.after(1200, self.refresh)

    def start_all(self):
        for record in self.map.values():
            if not port_open(record["port"]):
                self.start_browser(record)
        self.root.after(1500, self.refresh)

    def stop_selected(self):
        for key in self.selected():
            record = self.map[key]
            if port_open(record["port"]) and not cdp_alive(record["port"]):
                messagebox.showerror(
                    "拒绝关闭",
                    f"端口 {record['port']} 不是 Chrome CDP，管理器不会结束该进程。",
                )
                continue
            pid = port_pid(record["port"])
            if not pid:
                continue
            if not messagebox.askyesno("确认关闭", f"关闭 [{record['name']}]？\n请先保存网页内容。"):
                continue
            close_windows(pid)
            deadline = time.time() + 5
            while time.time() < deadline and port_open(record["port"]):
                self.root.update()
                time.sleep(0.12)
            if port_open(record["port"]) and messagebox.askyesno(
                "强制关闭", f"{record['name']} 仍在运行，是否强制结束？"
            ):
                try:
                    process = psutil.Process(pid)
                    for child in process.children(recursive=True):
                        child.kill()
                    process.kill()
                except Exception:
                    pass
            stop_port_listener(30000 + int(record["port"]))
            stop_port_listener(40000 + int(record["port"]))
            log(f"关闭浏览器：{record['name']}")
        self.refresh()

    def open_url(self):
        keys = self.selected()
        if not keys:
            return
        url = simpledialog.askstring("打开网页", "请输入网址：",
                                     initialvalue="https://www.baidu.com")
        if not url:
            return
        if "://" not in url:
            url = "https://" + url
        for key in keys:
            self.start_browser(self.map[key], url)

    def backup(self):
        keys = self.selected()
        if not keys:
            return
        for key in keys:
            record = self.map[key]
            if port_open(record["port"]):
                messagebox.showwarning("请先关闭", f"请先关闭 {record['name']} 再备份。")
                continue
            target = filedialog.asksaveasfilename(
                title=f"备份 {record['name']}",
                defaultextension=".zip",
                initialdir=BACKUPS,
                initialfile=f"{record['name']}-{datetime.now():%Y%m%d-%H%M}.zip",
                filetypes=[("ZIP", "*.zip")],
            )
            if not target:
                continue
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("browser-record.json",
                                 json.dumps(record, ensure_ascii=False, indent=2))
                profile = self.profile_path(record)
                if profile.exists():
                    for file in profile.rglob("*"):
                        if file.is_file() and not file.name.startswith("Singleton") and file.name != "lockfile":
                            archive.write(file, "profile/" + file.relative_to(profile).as_posix())
            log(f"备份浏览器：{record['name']} -> {target}")
        messagebox.showinfo("备份完成", "备份处理完成。")

    def restore(self):
        keys = self.selected(single=True)
        if not keys:
            return
        record = self.map[keys[0]]
        if port_open(record["port"]):
            messagebox.showwarning("请先关闭", "恢复前请关闭浏览器。")
            return
        source = filedialog.askopenfilename(title="选择备份", filetypes=[("ZIP", "*.zip")])
        if not source or not messagebox.askyesno("确认恢复", "恢复会覆盖当前 profile，是否继续？"):
            return
        profile = self.profile_path(record)
        staging = ROOT / f".restore-{int(time.time() * 1000)}"
        old = None
        try:
            staging.mkdir(parents=True)
            with zipfile.ZipFile(source) as archive:
                safe_extract_profile(archive, staging)
            old = ROOT / f".restore-old-{int(time.time() * 1000)}"
            if profile.exists():
                profile.rename(old)
            staging.rename(profile)
            shutil.rmtree(old, ignore_errors=True)
            log(f"恢复浏览器：{record['name']} <- {source}")
            messagebox.showinfo("恢复完成", "浏览器数据已恢复。")
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            if old and old.exists() and not profile.exists():
                old.rename(profile)
            messagebox.showerror("恢复失败", str(exc))

    def import_browser(self):
        source = filedialog.askopenfilename(
            title="导入独立浏览器",
            filetypes=[("浏览器备份 ZIP", "*.zip")],
        )
        if not source:
            return
        staging = ROOT / f".import-{int(time.time() * 1000)}"
        index = self.next_index()
        target_profile = PROFILES / f"browser-{index}"
        try:
            with zipfile.ZipFile(source) as archive:
                try:
                    record = json.loads(
                        archive.read("browser-record.json").decode("utf-8-sig")
                    )
                except (KeyError, UnicodeError, json.JSONDecodeError) as error:
                    raise ValueError("该文件不是有效的独立浏览器备份。") from error
                if not isinstance(record, dict):
                    raise ValueError("浏览器配置格式错误。")
                staging.mkdir(parents=True)
                safe_extract_profile(archive, staging)
            name = simpledialog.askstring(
                "导入浏览器",
                "新浏览器名称：",
                initialvalue=f"{record.get('name', '导入浏览器')} 导入",
                parent=self.root,
            )
            if not name:
                shutil.rmtree(staging, ignore_errors=True)
                return
            record.update(
                name=name.strip(),
                port=self.next_port(),
                profile=f"profiles/browser-{index}",
                auto_start=False,
                schedule="",
                created_at=datetime.now().isoformat(timespec="seconds"),
                environment=normalize_environment(record.get("environment")),
            )
            if target_profile.exists():
                shutil.rmtree(target_profile)
            staging.rename(target_profile)
            key = f"browser{index}"
            self.map[key] = record
            self.save_map()
            self.write_launcher(key, record)
            self.refresh()
            log(f"导入独立浏览器：{record['name']} <- {source}")
            messagebox.showinfo(
                "导入完成",
                f"已创建 [{record['name']}]，CDP 端口为 {record['port']}。",
            )
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(target_profile, ignore_errors=True)
            messagebox.showerror("导入失败", str(exc))

    def clear_browser_data(self):
        keys = self.selected()
        if not keys:
            return
        running = [
            self.map[key]["name"]
            for key in keys
            if port_open(self.map[key]["port"])
        ]
        if running:
            messagebox.showwarning(
                "请先关闭浏览器",
                "以下浏览器仍在运行：\n" + "\n".join(running),
            )
            return
        if not messagebox.askyesno(
            "清除浏览数据",
            "将清除选中浏览器的缓存、Cookie 和历史记录。\n"
            "网站登录状态可能失效，此操作不能撤销。是否继续？",
        ):
            return
        relative_dirs = (
            "Default/Cache",
            "Default/Code Cache",
            "Default/GPUCache",
            "Default/Service Worker/CacheStorage",
            "Default/Service Worker/ScriptCache",
            "ShaderCache",
            "GrShaderCache",
        )
        relative_files = (
            "Default/Cookies",
            "Default/Cookies-journal",
            "Default/Network/Cookies",
            "Default/Network/Cookies-journal",
            "Default/History",
            "Default/History-journal",
            "Default/Archived History",
            "Default/Visited Links",
        )
        for key in keys:
            profile = self.profile_path(self.map[key]).resolve()
            for relative in relative_dirs:
                target = (profile / relative).resolve()
                if profile == target or profile in target.parents:
                    shutil.rmtree(target, ignore_errors=True)
            for relative in relative_files:
                target = (profile / relative).resolve()
                if profile in target.parents:
                    target.unlink(missing_ok=True)
            log(f"清除浏览数据：{self.map[key]['name']}")
        messagebox.showinfo("清除完成", "缓存、Cookie 和历史记录已清除。")

    def environment_report_html(self, record):
        profile = self.profile_path(record)
        report = profile / "chrome-manager-environment-report.html"
        title = html.escape(record.get("name", "浏览器环境"))
        content = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - 环境信息</title>
<style>
body{{font-family:"Microsoft YaHei UI",sans-serif;background:#f7f6f2;color:#2d2a26;margin:0;padding:28px}}
.card{{max-width:980px;margin:auto;background:white;border:1px solid #e5e1da;border-radius:14px;padding:26px}}
h1{{margin-top:0}} table{{border-collapse:collapse;width:100%}}td{{padding:11px;border-bottom:1px solid #eee}}
td:first-child{{font-weight:700;width:220px}}.warn{{background:#fff2ed;padding:13px;border-radius:8px}}
</style></head><body><div class="card">
<h1>{title} · 当前环境</h1>
<p class="warn">这是环境实测页面。隐私覆盖仅用于测试，不能证明平台无法识别设备或账号关联。</p>
<table id="result"><tr><td>检测状态</td><td>正在读取...</td></tr></table>
<script>
const rows = [];
const add=(name,value)=>rows.push([name,String(value ?? '')]);
add('User-Agent', navigator.userAgent);
add('浏览器语言', navigator.language);
add('首选语言', (navigator.languages||[]).join(', '));
add('时区', Intl.DateTimeFormat().resolvedOptions().timeZone);
add('窗口尺寸', `${{window.outerWidth}} × ${{window.outerHeight}}`);
add('页面尺寸', `${{window.innerWidth}} × ${{window.innerHeight}}`);
add('屏幕分辨率', `${{screen.width}} × ${{screen.height}}`);
add('设备像素比例', devicePixelRatio);
add('触摸点数', navigator.maxTouchPoints);
add('CPU 线程', navigator.hardwareConcurrency);
add('设备内存', navigator.deviceMemory || '浏览器未公开');
add('颜色模式', matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
const render=()=>document.querySelector('#result').innerHTML=rows.map(
  row=>`<tr><td>${{row[0]}}</td><td>${{row[1].replaceAll('&','&amp;').replaceAll('<','&lt;')}}</td></tr>`
).join('');
render();
fetch('https://api.ipify.org?format=json').then(r=>r.json()).then(data=>{{
  add('公网 IP', data.ip); render();
}}).catch(()=>{{add('公网 IP','读取失败，请检查网络或代理');render();}});
if (navigator.geolocation) navigator.geolocation.getCurrentPosition(
  p=>{{add('地理位置',`${{p.coords.latitude}}, ${{p.coords.longitude}} (±${{p.coords.accuracy}}m)`);render();}},
  e=>{{add('地理位置',`不可用：${{e.message}}`);render();}},
  {{timeout:5000}}
);
</script></div></body></html>"""
        report.write_text(content, encoding="utf-8")
        return report

    def view_environment(self):
        keys = self.selected(single=True)
        if not keys:
            return
        record = self.map[keys[0]]
        report = self.environment_report_html(record)
        self.start_browser(record, report.as_uri())

    def edit_selected_environment(self):
        keys = self.selected(single=True)
        if not keys:
            return
        key = keys[0]
        record = self.map[key]
        before = normalize_environment(record.get("environment"))
        dialog = EnvironmentDialog(self.root, before)
        self.root.wait_window(dialog)
        if not dialog.result or dialog.result == before:
            return
        record["environment"] = dialog.result
        self.save_map()
        log(f"修改浏览器环境：{record['name']}")
        self.refresh()
        if port_open(record["port"]):
            messagebox.showwarning(
                "需要重启浏览器",
                "环境配置已经保存。\n\n"
                "当前浏览器正在运行，窗口、代理、权限、DNS、扩展等部分配置"
                "需要关闭并重新启动后才会完整生效。",
            )
        else:
            messagebox.showinfo(
                "保存成功",
                "环境配置已经保存，下次启动该浏览器时生效。",
            )

    def check_environment(self):
        keys = self.selected(single=True)
        if not keys:
            return
        record = self.map[keys[0]]
        proxy_info = None
        proxy_error = ""
        if record.get("proxy"):
            try:
                proxy_info = test_proxy(record["proxy"])
            except ValueError as error:
                proxy_error = str(error)
        result = consistency_report(record.get("environment"), proxy_info)
        lines = []
        if proxy_info:
            lines.append(
                f"代理出口：{proxy_info['ip']} · {proxy_info['country']} · "
                f"{proxy_info['latency']} ms"
            )
        elif record.get("proxy"):
            lines.append(f"代理检测失败：{proxy_error}")
        else:
            lines.append("代理：未配置")
        lines.append("")
        if result["issues"]:
            lines.append("需要修正：")
            lines.extend(f"• {item}" for item in result["issues"])
            lines.append("")
        lines.append("风险与提示：")
        lines.extend(f"• {item}" for item in result["warnings"])
        messagebox.showinfo("环境一致性检查", "\n".join(lines))

    def more_menu(self):
        menu = tk.Menu(self.root, tearoff=0, bg=CARD, fg=TEXT)
        menu.add_command(label="修改环境配置", command=self.edit_selected_environment)
        menu.add_command(label="查看当前环境信息", command=self.view_environment)
        menu.add_command(label="环境一致性检查", command=self.check_environment)
        menu.add_separator()
        menu.add_command(label="导出选中浏览器", command=self.backup)
        menu.add_command(label="导入为新浏览器", command=self.import_browser)
        menu.add_command(label="恢复到选中浏览器", command=self.restore)
        menu.add_command(label="清除缓存、Cookie 和历史记录", command=self.clear_browser_data)
        menu.add_separator()
        menu.add_command(label="复制 Codex 操作指令", command=self.copy_codex)
        menu.add_command(label="打开数据目录", command=self.open_profiles)
        menu.add_command(label="打开启动器目录", command=lambda: os.startfile(LAUNCHERS))
        menu.add_command(label="在 Chrome 中检查更新", command=self.open_update)
        menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def copy_codex(self):
        keys = self.selected()
        if not keys:
            return
        prompts = []
        for key in keys:
            record = self.map[key]
            port = record["port"]
            root_path = str(ROOT)
            prompts.append(
                f"请控制本地独立浏览器 [{record['name']}]。\n"
                f"浏览器配置键：{key}\n"
                f"CDP 调试端口：{port}\n"
                f"管理器目录：{root_path}\n\n"
                f"先检测 http://127.0.0.1:{port}/json/version 是否可访问。\n"
                "如果无法访问，说明浏览器尚未启动。请直接使用终端执行以下命令启动，"
                "不要要求我手动点击管理器：\n"
                f"Set-Location -LiteralPath '{root_path}'\n"
                f"python .\\browser_cli.py start {key}\n\n"
                f"启动后轮询 http://127.0.0.1:{port}/json/version，最多等待 20 秒。"
                "端口就绪后再访问 "
                f"http://127.0.0.1:{port}/json/list 读取现有标签页。\n"
                f"随后必须直接连接 CDP 地址：http://127.0.0.1:{port}\n"
                "使用 Playwright 的 connectOverCDP、agent-browser 的 CDP 连接参数，"
                "或其他等价 CDP 客户端连接现有浏览器。\n"
                "不要使用 Chrome 扩展会话来猜测浏览器，不要新建临时浏览器，"
                "不要连接其他端口，也不要仅根据窗口标题判断实例。\n"
                "本次只需要完成连接和控制验证，不执行其他网页任务。"
                "连接成功后复用现有登录状态，确认能够读取并控制现有标签页，"
                "然后回复“已连接并可以控制该浏览器”。\n"
                "保留 Chrome 和现有标签页，不要关闭浏览器。"
            )
        self.root.clipboard_clear()
        self.root.clipboard_append("\n\n".join(prompts))
        messagebox.showinfo("已复制", "Codex 操作指令已复制到剪贴板。")

    def open_profiles(self):
        keys = self.selected(single=True)
        if keys:
            path = self.profile_path(self.map[keys[0]])
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)

    def open_update(self):
        keys = self.selected(single=True)
        if keys:
            self.start_browser(self.map[keys[0]], "chrome://settings/help")

    def add_quick(self):
        name = simpledialog.askstring("新增快捷网址", "名称：")
        if not name:
            return
        url = simpledialog.askstring("新增快捷网址", "网址：", initialvalue="https://")
        if not url:
            return
        self.settings.setdefault("quick_urls", []).append({"name": name, "url": url})
        self.save_settings()
        self.refresh_quick()

    def remove_quick(self):
        selected = self.quick_tree.selection()
        if not selected:
            return
        for index in sorted((int(item) for item in selected), reverse=True):
            self.settings["quick_urls"].pop(index)
        self.save_settings()
        self.refresh_quick()

    def refresh_quick(self):
        if not hasattr(self, "quick_tree"):
            return
        self.quick_tree.delete(*self.quick_tree.get_children())
        for index, item in enumerate(self.settings.get("quick_urls", [])):
            self.quick_tree.insert("", "end", iid=str(index), values=(item["name"], item["url"]))

    def open_quick(self):
        browser_keys = list(self.tree.selection())
        sites = self.quick_tree.selection()
        if not browser_keys or not sites:
            messagebox.showinfo(
                "请选择",
                "请先在“浏览器管理”页选择浏览器，再在“快捷网址”页选择网址。",
            )
            return
        for key in browser_keys:
            for site in sites:
                url = self.settings["quick_urls"][int(site)]["url"]
                self.start_browser(self.map[key], url)

    def export_config(self):
        target = filedialog.asksaveasfilename(
            defaultextension=".json",
            initialfile="Chrome多开配置.json",
            filetypes=[("JSON", "*.json")],
        )
        if target:
            save_json(Path(target), {"browsers": self.map, "settings": self.settings})
            log(f"导出配置：{target}")

    def import_config(self):
        source = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not source:
            return
        data = load_json(Path(source), {})
        incoming = data.get("browsers", data)
        if not isinstance(incoming, dict):
            messagebox.showerror("导入失败", "配置格式无效。")
            return
        if not messagebox.askyesno(
            "导入配置",
            "导入会为冲突端口和 profile 自动分配新值，不会删除现有数据。是否继续？",
        ):
            return
        used_ports = {int(record["port"]) for record in self.map.values()}
        used_profiles = {str(record["profile"]).lower() for record in self.map.values()}
        for _, original in incoming.items():
            if not isinstance(original, dict) or "name" not in original:
                continue
            record = original.copy()
            index = self.next_index()
            port = int(record.get("port", self.next_port(used_ports)))
            if port in used_ports or port_open(port):
                port = self.next_port(used_ports)
            used_ports.add(port)
            profile = str(record.get("profile", f"profiles/browser-{index}")).replace("\\", "/")
            profile_parts = PurePosixPath(profile).parts
            if (
                Path(profile).is_absolute()
                or ".." in profile_parts
                or not profile_parts
                or profile_parts[0].lower() != "profiles"
            ):
                profile = f"profiles/browser-{index}"
            if profile.lower() in used_profiles:
                profile = f"profiles/browser-{index}"
            used_profiles.add(profile.lower())
            record["port"] = port
            record["profile"] = profile
            record.setdefault("group", "导入")
            record.setdefault("home", "about:blank")
            record.setdefault("proxy", "")
            record.setdefault("note", "")
            record.setdefault("schedule", "")
            record.setdefault("auto_start", False)
            key = f"browser{index}"
            self.map[key] = record
            self.profile_path(record).mkdir(parents=True, exist_ok=True)
            self.write_launcher(key, record)
        self.settings.update(data.get("settings", {}))
        self.save_map()
        self.save_settings()
        self.refresh()
        log(f"导入配置：{source}")

    def set_password(self):
        value = simpledialog.askstring("设置密码", "请输入新密码：", show="*")
        if value:
            confirm = simpledialog.askstring("确认密码", "请再次输入密码：", show="*")
            if confirm != value:
                messagebox.showerror("密码不一致", "两次输入的密码不一致。")
                return
            self.settings["password_hash"] = hashlib.sha256(value.encode()).hexdigest()
            self.save_settings()
            messagebox.showinfo("设置成功", "密码将在下次启动时生效。")

    def clear_password(self):
        self.settings["password_hash"] = ""
        self.save_settings()
        messagebox.showinfo("已清除", "管理器密码已清除。")

    def save_settings(self):
        if hasattr(self, "tray_var"):
            self.settings["minimize_to_tray"] = self.tray_var.get()
        save_json(SETTINGS_FILE, self.settings)

    def startup_command(self):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        return f'"{pythonw}" "{Path(__file__).resolve()}"'

    def is_run_at_startup(self):
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
            ) as key:
                winreg.QueryValueEx(key, "ChromeMultiManager")
                return True
        except Exception:
            return False

    def toggle_run_startup(self):
        import winreg
        path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
            if self.run_var.get():
                winreg.SetValueEx(key, "ChromeMultiManager", 0, winreg.REG_SZ,
                                  self.startup_command())
            else:
                try:
                    winreg.DeleteValue(key, "ChromeMultiManager")
                except FileNotFoundError:
                    pass

    def chrome_version(self):
        chrome = find_chrome()
        if not chrome:
            messagebox.showerror("未找到 Chrome", "没有找到 Chrome。")
            return
        escaped_chrome = str(chrome).replace("'", "''")
        command = f"(Get-Item -LiteralPath '{escaped_chrome}').VersionInfo.ProductVersion"
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        version = result.stdout.strip() or "无法读取版本"
        messagebox.showinfo("Chrome 版本", f"{version}\n\n{chrome}")

    def refresh_logs(self):
        if not hasattr(self, "log_text"):
            return
        self.log_text.delete("1.0", "end")
        if LOG_FILE.exists():
            content = LOG_FILE.read_text(encoding="utf-8", errors="replace")
            self.log_text.insert("end", content[-100000:])

    def clear_logs(self):
        if messagebox.askyesno("清空日志", "确定清空操作日志？"):
            LOG_FILE.write_text("", encoding="utf-8")
            self.refresh_logs()

    def auto_start(self):
        for record in self.map.values():
            if record.get("auto_start") and not port_open(record["port"]):
                self.start_browser(record)

    def check_schedule(self):
        current = datetime.now().strftime("%H:%M")
        if current == self.last_schedule_minute:
            return
        self.last_schedule_minute = current
        for record in self.map.values():
            if record.get("schedule") == current and not port_open(record["port"]):
                self.start_browser(record)

    def refresh(self):
        if not hasattr(self, "tree"):
            return
        self.reload_external_map()
        groups = sorted({record.get("group", "").strip() for record in self.map.values()
                         if record.get("group", "").strip()})
        group_values = ("全部分组", *groups)
        self.group_filter.configure(values=group_values)
        if self.group_filter_var.get() not in group_values:
            self.group_filter_var.set("全部分组")
        query = self.search_var.get().strip().lower()
        wanted_group = self.group_filter_var.get()
        wanted_status = self.status_filter_var.get()
        selected = self.tree.selection()
        for button_group in getattr(self, "action_buttons", {}).values():
            button_group.destroy()
        self.action_buttons = {}
        self.tree.delete(*self.tree.get_children())
        running = 0
        visible = 0
        for key, record in sorted(self.map.items(), key=lambda item: int(item[1]["port"])):
            is_open = port_open(record["port"])
            is_cdp = cdp_alive(record["port"]) if is_open else False
            pid, tabs, memory = browser_stats(record["port"]) if is_cdp else (None, 0, 0)
            status = "运行中" if is_cdp else ("端口占用" if is_open else "未启动")
            running += bool(is_cdp)
            searchable = " ".join((
                record.get("name", ""), record.get("group", ""), str(record.get("port", "")),
                record.get("home", ""), record.get("proxy", ""),
            )).lower()
            if query and query not in searchable:
                continue
            if wanted_group != "全部分组" and record.get("group", "") != wanted_group:
                continue
            if wanted_status != "全部状态" and status != wanted_status:
                continue
            visible += 1
            self.tree.insert(
                "", "end", iid=key,
                values=(
                    record["name"], record.get("group", ""), record["port"], status,
                    pid or "", tabs, f"{memory:.0f} MB" if memory else "",
                    record.get("home", ""), record.get("proxy", ""), "",
                ),
                tags=("running" if is_cdp else "occupied" if is_open else "stopped",),
            )
            button_group = tk.Frame(self.tree, bg=CARD)
            button_specs = (
                (
                    "已启动" if is_cdp else ("不可用" if is_open else "启动"),
                    lambda browser_key=key: self.start_row(browser_key),
                    "disabled" if is_open else "normal",
                ),
                ("编辑", lambda browser_key=key: self.edit_row(browser_key), "normal"),
                ("删除", lambda browser_key=key: self.delete_row(browser_key), "normal"),
            )
            for text, command, state in button_specs:
                tk.Button(
                    button_group,
                    text=text,
                    command=command,
                    state=state,
                    relief="flat",
                    borderwidth=0,
                    bg=BLUE,
                    fg="white",
                    disabledforeground=MUTED,
                    activebackground="#b9684f",
                    activeforeground="white",
                    font=("Microsoft YaHei UI", 9, "bold"),
                    cursor="hand2" if state == "normal" else "arrow",
                    padx=6,
                ).pack(side="left", fill="both", expand=True, padx=2)
            self.action_buttons[key] = button_group
        for key in selected:
            if self.tree.exists(key):
                self.tree.selection_add(key)
        suffix = f"  ·  当前显示 {visible} 个" if visible != len(self.map) else ""
        self.summary.config(text=f"共 {len(self.map)} 个  ·  运行中 {running} 个{suffix}")
        self.root.after_idle(self.position_action_buttons)

    def tick(self):
        if not self.root.winfo_exists():
            return
        self.refresh()
        self.check_schedule()
        self.root.after(4000, self.tick)

    def start_tray(self):
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        if not pystray:
            return
        image = Image.new("RGB", (64, 64), PANEL)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((8, 8, 56, 56), 10, fill=BLUE)
        draw.ellipse((20, 20, 44, 44), fill="white")
        menu = pystray.Menu(
            pystray.MenuItem("显示管理器", lambda: self.root.after(0, self.show_window)),
            pystray.MenuItem("全部启动", lambda: self.root.after(0, self.start_all)),
            pystray.MenuItem("退出管理器", lambda: self.root.after(0, self.quit_app)),
        )
        self.tray = pystray.Icon("ChromeMultiManager", image, "Chrome 多开管理器", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_close(self):
        if self.settings.get("minimize_to_tray", True) and self.tray:
            self.root.withdraw()
        else:
            self.quit_app()

    def quit_app(self):
        if self.tray:
            self.tray.stop()
        self.root.destroy()


def self_test():
    assert find_chrome() is not None
    environment = normalize_environment({
        "window_width": 900,
        "window_height": 700,
        "language": "en-US",
        "timezone": "America/New_York",
        "privacy_hardware": True,
        "hardware_concurrency": 6,
        "device_memory": 12,
    })
    arguments = build_chrome_arguments(environment)
    assert "--window-size=900,700" in arguments
    assert "--lang=en-US" in arguments
    assert consistency_report(environment)["warnings"]
    test_root = ROOT / ".selftest"
    shutil.rmtree(test_root, ignore_errors=True)
    test_root.mkdir()
    profile = test_root / "profile"
    apply_profile_preferences(profile, environment)
    preferences = load_json(profile / "Default" / "Preferences", {})
    assert preferences["intl"]["accept_languages"]
    assert preferences["profile"]["default_content_setting_values"]["notifications"] == 2
    archive_path = test_root / "safe.zip"
    destination = test_root / "restore"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("profile/Default/test.txt", "ok")
    with zipfile.ZipFile(archive_path) as archive:
        safe_extract_profile(archive, destination)
    assert (destination / "Default/test.txt").read_text() == "ok"
    bad_path = test_root / "bad.zip"
    with zipfile.ZipFile(bad_path, "w") as archive:
        archive.writestr("profile/../../outside.txt", "bad")
    failed = False
    try:
        with zipfile.ZipFile(bad_path) as archive:
            safe_extract_profile(archive, destination)
    except ValueError:
        failed = True
    assert failed
    shutil.rmtree(test_root)
    print("SELFTEST_OK")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
        raise SystemExit(0)
    instance = SingleInstance()
    if not instance.acquire():
        instance.notify_existing()
        raise SystemExit(0)
    set_windows_app_id()
    ensure_app_icon()
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.1)
    except Exception:
        pass
    app = App(root)
    instance.start_listener(lambda: root.after(0, app.show_window))
    root.mainloop()
