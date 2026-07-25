import ctypes
import ctypes.wintypes
import csv
import base64
import html
import hashlib
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.parse
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

import psutil
import websocket
from cloud_client import (
    CloudError,
    account_status as cloud_account_status,
    download_snapshot,
    login as cloud_login_request,
    logout as cloud_logout_request,
    normalize_server_url,
    register as cloud_register_request,
    upload_snapshot,
)
from environment_config import (
    DEFAULT_ENVIRONMENT,
    apply_profile_preferences,
    build_chrome_arguments,
    consistency_report,
    ensure_environment_controller,
    environment_controller_port,
    extension_paths,
    normalize_environment,
    prepare_autofill_extension,
)
try:
    import pystray
    from PIL import Image, ImageDraw, ImageTk
except ImportError:
    pystray = None
    Image = None
    ImageDraw = None
    ImageTk = None


APP_VERSION = "3.3.3"
UPDATE_REPOSITORY = "Teliter/Chrome---"
UPDATE_API_URL = (
    f"https://api.github.com/repos/{UPDATE_REPOSITORY}/releases/latest"
)
UPDATE_LATEST_URL = f"https://github.com/{UPDATE_REPOSITORY}/releases/latest"
FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ROOT = (
    Path(os.environ.get("LOCALAPPDATA", Path.home()))
    / "ChromeMultiManager"
    if FROZEN
    else Path(__file__).resolve().parent
)
MAP_FILE = ROOT / "browser-map.json"
SETTINGS_FILE = ROOT / "manager-settings.json"
PROFILES = ROOT / "profiles"
LAUNCHERS = ROOT / "launchers"
BACKUPS = ROOT / "backups"
LOG_FILE = ROOT / "manager.log"
VAULT_FILE = ROOT / "password-vault.json"
LEGACY_VAULT_FILE = ROOT / "password-vault.dat"
WELCOME_FILE = ROOT / "ChromeManager-welcome.html"
WELCOME_HOME_LABEL = "软件欢迎页"
APP_ICON_FILE = RESOURCE_ROOT / "chrome-manager.ico"
LOCK_PORT = 39231
MAX_CDP_PORT = 25535
PROXY_BRIDGE_FALLBACK_START = 56000
PROXY_BRIDGE_FALLBACK_END = 60999

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
_DIALOG_ROOT = None

for folder in (PROFILES, LAUNCHERS, BACKUPS):
    folder.mkdir(parents=True, exist_ok=True)


def enable_windows_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def system_tk_scaling():
    if sys.platform == "win32":
        try:
            dpi = int(ctypes.windll.user32.GetDpiForSystem())
            if dpi > 0:
                return max(1.0, min(3.0, dpi / 72.0))
        except Exception:
            pass
    return None


def system_display_info(window=None):
    width = window.winfo_screenwidth() if window else 1280
    height = window.winfo_screenheight() if window else 800
    work_width, work_height = width, height
    dpi = 96
    if sys.platform == "win32":
        try:
            work = ctypes.wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(work), 0
            ):
                work_width = work.right - work.left
                work_height = work.bottom - work.top
        except Exception:
            pass
        try:
            dpi = int(ctypes.windll.user32.GetDpiForSystem()) or 96
        except Exception:
            pass
    recommended_width = max(960, min(1600, int(work_width * 0.86)))
    recommended_height = max(640, min(960, int(work_height * 0.84)))
    return {
        "work_width": work_width,
        "work_height": work_height,
        "dpi": dpi,
        "scale_percent": round(dpi * 100 / 96),
        "recommended_width": min(recommended_width, work_width),
        "recommended_height": min(recommended_height, work_height),
    }


def center_window(window, parent=None):
    window.update_idletasks()
    width = max(window.winfo_width(), window.winfo_reqwidth())
    height = max(window.winfo_height(), window.winfo_reqheight())
    work_left = 0
    work_top = 0
    work_right = window.winfo_screenwidth()
    work_bottom = window.winfo_screenheight()
    if sys.platform == "win32":
        try:
            work = ctypes.wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(work), 0
            ):
                work_left, work_top = work.left, work.top
                work_right, work_bottom = work.right, work.bottom
        except Exception:
            pass
    anchor = parent if parent and parent.winfo_exists() else None
    if anchor and anchor.winfo_viewable():
        x = anchor.winfo_rootx() + (anchor.winfo_width() - width) // 2
        y = anchor.winfo_rooty() + (anchor.winfo_height() - height) // 2
    else:
        x = work_left + (work_right - work_left - width) // 2
        y = work_top + (work_bottom - work_top - height) // 2
    y -= min(90, max(36, height // 12))
    bottom_margin = 56
    top_margin = 18
    x = max(work_left, min(x, max(work_left, work_right - width)))
    y = max(
        work_top + top_margin,
        min(y, max(work_top + top_margin, work_bottom - height - bottom_margin)),
    )
    window.geometry(f"+{x}+{y}")


def normalize_home_value(value):
    value = str(value or "").strip()
    if value == WELCOME_HOME_LABEL:
        return ""
    return "" if value.lower() == "about:blank" else value


def display_home_value(value):
    return normalize_home_value(value) or WELCOME_HOME_LABEL


def storage_home_value(value):
    value = str(value or "").strip()
    return "" if value == WELCOME_HOME_LABEL else normalize_home_value(value)


def ensure_welcome_page():
    ROOT.mkdir(parents=True, exist_ok=True)
    content = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Chrome 多开管理器</title>
  <style>
    :root {
      color-scheme: light;
      font-family: "Microsoft YaHei UI", "Microsoft YaHei", Arial, sans-serif;
      background: #f7f6f2;
      color: #2d2a26;
    }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
    }
    main {
      width: min(760px, calc(100vw - 48px));
      padding: 44px 48px;
      background: #fff;
      border: 1px solid #e5e1da;
      box-shadow: 0 18px 50px rgba(45, 42, 38, 0.08);
    }
    h1 {
      margin: 0 0 14px;
      font-size: 30px;
      font-weight: 700;
      letter-spacing: 0;
    }
    p {
      margin: 0;
      color: #6f6960;
      font-size: 15px;
      line-height: 1.8;
    }
    ul {
      margin: 28px 0 0;
      padding: 0;
      list-style: none;
      display: grid;
      gap: 12px;
    }
    li {
      padding: 12px 14px;
      background: #fbfaf8;
      border: 1px solid #eee9e2;
      color: #3a352f;
      font-size: 14px;
    }
  </style>
</head>
<body>
  <main>
    <h1>Chrome 多开管理器</h1>
    <p>欢迎使用本软件。这里会为每个浏览器保存独立资料、账号信息、代理与环境配置，方便你按不同用途启动和管理 Chrome。</p>
    <ul>
      <li>在“编辑浏览器”里可以为当前浏览器添加网站账号，并选择某个网站作为启动首页。</li>
      <li>没有设置启动首页时，会默认打开这个欢迎页面。</li>
      <li>密码管理中的账号可复制、打开网页并尝试填充，不会自动提交表单。</li>
    </ul>
  </main>
</body>
</html>
"""
    if not WELCOME_FILE.exists() or WELCOME_FILE.read_text(encoding="utf-8") != content:
        WELCOME_FILE.write_text(content, encoding="utf-8")
    return WELCOME_FILE


def default_start_url():
    return ensure_welcome_page().as_uri()


def resolve_start_url(record, url=None):
    home = normalize_home_value(record.get("home", ""))
    return normalize_home_value(url) or home or default_start_url()


def reorder_mapping_key(mapping, source_key, target_key, after=False):
    """Move one dictionary entry while preserving the displayed list order."""
    keys = list(mapping)
    if (
        source_key not in mapping
        or target_key not in mapping
        or source_key == target_key
    ):
        return False
    records = dict(mapping)
    keys.remove(source_key)
    target_index = keys.index(target_key)
    keys.insert(target_index + int(after), source_key)
    mapping.clear()
    mapping.update((key, records[key]) for key in keys)
    return True


def configure_dialog_parent(root):
    global _DIALOG_ROOT
    _DIALOG_ROOT = root
    for module, names in (
        (
            messagebox,
            (
                "showinfo", "showwarning", "showerror",
                "askquestion", "askokcancel", "askyesno",
                "askyesnocancel", "askretrycancel",
            ),
        ),
        (simpledialog, ("askstring", "askinteger", "askfloat")),
        (
            filedialog,
            (
                "askopenfilename", "askopenfilenames", "asksaveasfilename",
                "askdirectory",
            ),
        ),
    ):
        for name in names:
            original = getattr(module, name)
            if getattr(original, "_chrome_manager_wrapped", False):
                continue

            def wrapped(*args, _original=original, **kwargs):
                if "parent" not in kwargs and _DIALOG_ROOT:
                    kwargs["parent"] = _DIALOG_ROOT
                return _original(*args, **kwargs)

            wrapped._chrome_manager_wrapped = True
            setattr(module, name, wrapped)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default
    except Exception:
        return default


def version_tuple(value):
    parts = []
    for item in str(value or "").strip().lstrip("vV").split("."):
        digits = "".join(char for char in item if char.isdigit())
        parts.append(int(digits or 0))
    return tuple((parts + [0, 0, 0])[:3])


def latest_release_fallback(timeout=20):
    request = urllib.request.Request(
        UPDATE_LATEST_URL,
        headers={"User-Agent": f"ChromeMultiManager/{APP_VERSION}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        page_url = response.geturl()
    tag = urllib.parse.unquote(
        PurePosixPath(urllib.parse.urlparse(page_url).path).name
    ).strip()
    if not tag or tag == "latest":
        raise RuntimeError("无法识别 GitHub 最新版本。")
    version = tag.lstrip("vV")
    asset_name = f"ChromeMultiManager-Setup-{version}.exe"
    download_url = (
        f"https://github.com/{UPDATE_REPOSITORY}/releases/download/"
        f"{urllib.parse.quote(tag)}/{asset_name}"
    )
    digest = ""
    try:
        checksum_request = urllib.request.Request(
            download_url + ".sha256",
            headers={"User-Agent": f"ChromeMultiManager/{APP_VERSION}"},
        )
        with urllib.request.urlopen(
            checksum_request, timeout=timeout
        ) as checksum_response:
            checksum = checksum_response.read().decode("ascii", "ignore").strip()
        candidate = checksum.split()[0].lower()
        if len(candidate) == 64 and all(
            char in "0123456789abcdef" for char in candidate
        ):
            digest = "sha256:" + candidate
    except Exception:
        pass
    return {
        "version": version,
        "tag": tag,
        "name": tag,
        "notes": "已通过 GitHub 备用通道检测到新版本。",
        "page_url": page_url,
        "asset_name": asset_name,
        "download_url": download_url,
        "size": 0,
        "digest": digest,
    }


def latest_release_info(timeout=20):
    request = urllib.request.Request(
        UPDATE_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"ChromeMultiManager/{APP_VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            release = json.loads(response.read().decode("utf-8"))
    except Exception as error:
        try:
            return latest_release_fallback(timeout=timeout)
        except Exception as fallback_error:
            raise RuntimeError(
                f"无法检查新版本：{error}；备用通道：{fallback_error}"
            ) from fallback_error
    tag = str(release.get("tag_name", "")).strip()
    asset = next(
        (
            item for item in release.get("assets", [])
            if str(item.get("name", "")).lower().endswith(".exe")
            and "setup" in str(item.get("name", "")).lower()
        ),
        None,
    )
    if not tag or not asset or not asset.get("browser_download_url"):
        raise RuntimeError("最新版本没有可用的 Windows 安装包。")
    return {
        "version": tag.lstrip("vV"),
        "tag": tag,
        "name": release.get("name") or tag,
        "notes": str(release.get("body", "")).strip(),
        "page_url": release.get("html_url", ""),
        "asset_name": asset.get("name", "ChromeMultiManager-Setup.exe"),
        "download_url": asset["browser_download_url"],
        "size": int(asset.get("size", 0)),
        "digest": str(asset.get("digest", "")),
    }


def download_release_installer(release, progress=None):
    target_dir = Path(tempfile.gettempdir()) / "ChromeMultiManagerUpdate"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / release["asset_name"]
    partial = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(
        release["download_url"],
        headers={"User-Agent": f"ChromeMultiManager/{APP_VERSION}"},
    )
    digest = hashlib.sha256()
    downloaded = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            total = int(
                response.headers.get("Content-Length")
                or release["size"]
                or 0
            )
            with partial.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    if progress:
                        progress(downloaded, total)
        expected = release["digest"].removeprefix("sha256:").strip().lower()
        actual = digest.hexdigest().lower()
        if expected and actual != expected:
            raise RuntimeError("安装包校验失败，文件可能不完整或已被修改。")
        os.replace(partial, target)
        return target, actual
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def save_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def format_open_time(value):
    if not value:
        return "从未打开"
    try:
        moment = datetime.fromisoformat(str(value))
        return moment.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value).replace("T", " ")[:16]


def normalize_web_url(value):
    url = str(value or "").strip()
    if not url:
        raise ValueError("网址不能为空。")
    if (
        len(url) >= 3
        and url[0].isalpha()
        and url[1] == ":"
        and url[2] in ("/", "\\")
    ) or url.startswith(("/", "\\")):
        raise ValueError("仅支持 http 或 https 网站地址。")
    if "://" not in url:
        url = "https://" + url
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("仅支持 http 或 https 网站地址。")
    return url


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
            "ChromeMultiManager.Desktop.3"
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


def proxy_bridge_config_path(profile):
    return Path(profile) / "proxy-bridge.json"


def _process_cmdline(pid):
    try:
        return psutil.Process(pid).cmdline()
    except (psutil.Error, OSError):
        return []


def _process_matches(pid, markers):
    if not markers:
        return True
    haystack = "\n".join(str(part).lower() for part in _process_cmdline(pid))
    return all(str(marker).lower() in haystack for marker in markers)


def proxy_bridge_markers(config_path):
    return ["proxy", str(config_path)]


def environment_controller_markers(config_path):
    return ["environment", str(config_path)]


def proxy_bridge_candidates(cdp_port):
    cdp_port = int(cdp_port)
    seen = set()

    def add(port):
        if 1024 <= port <= 65535 and port not in seen:
            seen.add(port)
            yield port

    yield from add(30000 + cdp_port)
    size = PROXY_BRIDGE_FALLBACK_END - PROXY_BRIDGE_FALLBACK_START + 1
    start = PROXY_BRIDGE_FALLBACK_START + (cdp_port % size)
    for offset in range(size):
        port = PROXY_BRIDGE_FALLBACK_START + (
            (start - PROXY_BRIDGE_FALLBACK_START + offset) % size
        )
        yield from add(port)


def proxy_bridge_port(cdp_port, config_path=None):
    markers = proxy_bridge_markers(config_path) if config_path else None
    if config_path:
        saved_port = load_json(config_path, {}).get("listen_port")
        try:
            saved_port = int(saved_port)
        except (TypeError, ValueError):
            saved_port = None
        if saved_port and saved_port != LOCK_PORT:
            pid = port_pid(saved_port)
            if not pid or _process_matches(pid, markers):
                return saved_port
    for candidate in proxy_bridge_candidates(cdp_port):
        if candidate == LOCK_PORT:
            continue
        pid = port_pid(candidate)
        if not pid or _process_matches(pid, markers):
            return candidate
    raise RuntimeError("No available proxy bridge port")


def ensure_proxy_bridge(profile, cdp_port, proxy):
    config_path = proxy_bridge_config_path(profile)
    listen_port = proxy_bridge_port(cdp_port, config_path)
    save_json(config_path, {
        "host": proxy["host"],
        "port": proxy["port"],
        "username": proxy["username"],
        "password": proxy["password"],
        "listen_port": listen_port,
    })
    markers = proxy_bridge_markers(config_path)
    if not port_open(listen_port):
        if FROZEN:
            command = [
                sys.executable,
                "--proxy-forwarder",
                "--listen",
                str(listen_port),
                "--config",
                str(config_path),
            ]
        else:
            command = [
                sys.executable,
                str(RESOURCE_ROOT / "proxy_forwarder.py"),
                "--listen",
                str(listen_port),
                "--config",
                str(config_path),
            ]
        subprocess.Popen(
            command,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        deadline = time.time() + 5
        while time.time() < deadline and not port_open(listen_port):
            time.sleep(0.05)
    if not port_open(listen_port) or not _process_matches(port_pid(listen_port), markers):
        raise ValueError("本地代理认证桥启动失败。")
    return f"http://127.0.0.1:{listen_port}"


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.18):
            return True
    except OSError:
        return False


def port_pid(port):
    return listener_pid_map().get(int(port))


def listener_pid_map():
    listeners = {}
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr:
                listeners[int(conn.laddr.port)] = conn.pid
    except Exception:
        pass
    return listeners


def stop_port_listener(port, markers=None):
    pid = port_pid(port)
    if not pid:
        return
    if not _process_matches(pid, markers):
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


def cdp_json(port, path, timeout=1.0):
    with urllib.request.urlopen(
        f"http://127.0.0.1:{int(port)}{path}", timeout=timeout
    ) as response:
        return json.load(response)


def cdp_open_tab(port, url):
    quoted = urllib.parse.quote(url, safe="")
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}/json/new?{quoted}",
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=2.0) as response:
            return json.load(response)
    except Exception:
        return cdp_json(port, f"/json/new?{quoted}", timeout=2.0)


def cdp_pages(port):
    try:
        pages = cdp_json(port, "/json/list", timeout=1.0)
    except Exception:
        return []
    return [
        page for page in pages
        if page.get("type") == "page" and page.get("webSocketDebuggerUrl")
    ]


def send_cdp_command(connection, command_id, method, params=None):
    payload = {"id": command_id, "method": method}
    if params:
        payload["params"] = params
    connection.send(json.dumps(payload))
    deadline = time.time() + 3
    while time.time() < deadline:
        response = json.loads(connection.recv())
        if response.get("id") == command_id:
            return response
    return {}


def autofill_vault_script(username, password):
    payload = json.dumps(
        {"username": str(username), "password": str(password)},
        ensure_ascii=False,
    )
    return f"""
(() => {{
  const entry = {payload};
  const setValue = (element, value) => {{
    if (!element) return false;
    element.focus();
    const setter = Object.getOwnPropertyDescriptor(
      element instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype,
      'value'
    ).set;
    setter.call(element, value);
    element.dispatchEvent(new Event('input', {{bubbles: true}}));
    element.dispatchEvent(new Event('change', {{bubbles: true}}));
    return true;
  }};
  const visible = element => {{
    if (!element || element.disabled || element.readOnly) return false;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden';
  }};
  const fill = () => {{
    const passwords = [...document.querySelectorAll('input[type="password"]')]
      .filter(visible);
    if (!passwords.length) return false;
    const passwordInput = passwords[0];
    const scope = passwordInput.form || passwordInput.closest('form') || document;
    const usernames = [...scope.querySelectorAll(
      'input[autocomplete="username"],input[type="email"],'
      + 'input[name*="user" i],input[name*="email" i],input[name*="login" i],'
      + 'input[id*="user" i],input[id*="email" i],input[type="text"],input:not([type])'
    )].filter(input => input !== passwordInput && visible(input));
    setValue(usernames[0], entry.username);
    setValue(passwordInput, entry.password);
    return true;
  }};
  if (fill()) return true;
  let attempts = 0;
  const timer = setInterval(() => {{
    attempts += 1;
    if (fill() || attempts >= 30) clearInterval(timer);
  }}, 500);
  return false;
}})();
""".strip()


def apply_vault_autofill(port, page_url, username, password):
    script = autofill_vault_script(username, password)
    target = None
    normalized = page_url.rstrip("/")
    deadline = time.time() + 8
    while time.time() < deadline and not target:
        for page in cdp_pages(port):
            url = str(page.get("url", "")).rstrip("/")
            if url == normalized or url.startswith(normalized):
                target = page
                break
        if not target:
            time.sleep(0.25)
    if not target:
        pages = cdp_pages(port)
        target = pages[0] if pages else None
    if not target:
        return False
    connection = websocket.create_connection(
        target["webSocketDebuggerUrl"],
        timeout=3,
        origin="http://127.0.0.1",
        suppress_origin=True,
    )
    try:
        send_cdp_command(connection, 1, "Runtime.evaluate", {
            "expression": script,
            "awaitPromise": True,
        })
        send_cdp_command(connection, 2, "Page.addScriptToEvaluateOnNewDocument", {
            "source": script,
        })
        return True
    finally:
        connection.close()


def browser_stats(port, pid=None):
    pid = pid or port_pid(port)
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


def win_file_version(path):
    version = ctypes.windll.version
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        raise ctypes.WinError()
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise ctypes.WinError()
    pointer = ctypes.c_void_p()
    length = ctypes.wintypes.UINT()
    if not version.VerQueryValueW(
        buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)
    ):
        raise ctypes.WinError()

    class FixedFileInfo(ctypes.Structure):
        _fields_ = [
            ("signature", ctypes.wintypes.DWORD),
            ("struct_version", ctypes.wintypes.DWORD),
            ("file_version_ms", ctypes.wintypes.DWORD),
            ("file_version_ls", ctypes.wintypes.DWORD),
            ("product_version_ms", ctypes.wintypes.DWORD),
            ("product_version_ls", ctypes.wintypes.DWORD),
            ("file_flags_mask", ctypes.wintypes.DWORD),
            ("file_flags", ctypes.wintypes.DWORD),
            ("file_os", ctypes.wintypes.DWORD),
            ("file_type", ctypes.wintypes.DWORD),
            ("file_subtype", ctypes.wintypes.DWORD),
            ("file_date_ms", ctypes.wintypes.DWORD),
            ("file_date_ls", ctypes.wintypes.DWORD),
        ]

    info = ctypes.cast(pointer, ctypes.POINTER(FixedFileInfo)).contents
    return (
        info.file_version_ms >> 16,
        info.file_version_ms & 0xFFFF,
        info.file_version_ls >> 16,
        info.file_version_ls & 0xFFFF,
    )


def safe_extract_profile(archive, destination):
    root = destination.resolve()
    for info in archive.infolist():
        path = PurePosixPath(info.filename)
        if not path.parts or path.parts[0] != "profile":
            continue
        relative = Path(*path.parts[1:])
        if not relative.parts:
            continue
        if any(
            part in (".", "..")
            or ":" in part
            or "\x00" in part
            for part in relative.parts
        ):
            raise ValueError(f"备份包含不安全路径：{info.filename}")
        target = (root / relative).resolve()
        if root != target and root not in target.parents:
            raise ValueError(f"备份包含不安全路径：{info.filename}")
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def safe_profile_path(record):
    raw = str(record.get("profile", "")).replace("\\", "/")
    parts = PurePosixPath(raw).parts
    if (
        len(parts) < 2
        or parts[0].lower() != "profiles"
        or any(part in (".", "..") or ":" in part or "\x00" in part for part in parts)
    ):
        raise ValueError(f"不安全的浏览器数据目录：{raw or '空路径'}")
    root = PROFILES.resolve()
    target = (ROOT / Path(*parts)).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"浏览器数据目录超出 profiles：{raw}")
    return target


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


REGION_PRESETS = {
    "中国大陆 · 上海": {
        "language": "zh-CN", "accept_languages": "zh-CN,zh,en-US,en",
        "timezone": "Asia/Shanghai", "latitude": 31.2304, "longitude": 121.4737,
    },
    "中国香港": {
        "language": "zh-HK", "accept_languages": "zh-HK,zh-TW,zh,en",
        "timezone": "Asia/Hong_Kong", "latitude": 22.3193, "longitude": 114.1694,
    },
    "日本 · 东京": {
        "language": "ja-JP", "accept_languages": "ja-JP,ja,en-US,en",
        "timezone": "Asia/Tokyo", "latitude": 35.6762, "longitude": 139.6503,
    },
    "新加坡": {
        "language": "en-SG", "accept_languages": "en-SG,en-US,en",
        "timezone": "Asia/Singapore", "latitude": 1.3521, "longitude": 103.8198,
    },
    "马来西亚 · 吉隆坡": {
        "language": "ms-MY", "accept_languages": "ms-MY,ms,en-US,en",
        "timezone": "Asia/Kuala_Lumpur", "latitude": 3.1390, "longitude": 101.6869,
    },
    "越南 · 胡志明市": {
        "language": "vi-VN", "accept_languages": "vi-VN,vi,en-US,en",
        "timezone": "Asia/Ho_Chi_Minh", "latitude": 10.8231, "longitude": 106.6297,
    },
    "泰国 · 曼谷": {
        "language": "th-TH", "accept_languages": "th-TH,th,en-US,en",
        "timezone": "Asia/Bangkok", "latitude": 13.7563, "longitude": 100.5018,
    },
    "菲律宾 · 马尼拉": {
        "language": "en-PH", "accept_languages": "en-PH,en-US,en,fil",
        "timezone": "Asia/Manila", "latitude": 14.5995, "longitude": 120.9842,
    },
    "美国 · 纽约": {
        "language": "en-US", "accept_languages": "en-US,en",
        "timezone": "America/New_York", "latitude": 40.7128, "longitude": -74.0060,
    },
    "美国 · 洛杉矶": {
        "language": "en-US", "accept_languages": "en-US,en",
        "timezone": "America/Los_Angeles", "latitude": 34.0522, "longitude": -118.2437,
    },
    "英国 · 伦敦": {
        "language": "en-GB", "accept_languages": "en-GB,en-US,en",
        "timezone": "Europe/London", "latitude": 51.5074, "longitude": -0.1278,
    },
    "印度尼西亚 · 雅加达": {
        "language": "id-ID", "accept_languages": "id-ID,id,en-US,en",
        "timezone": "Asia/Jakarta", "latitude": -6.2088, "longitude": 106.8456,
    },
}

DEVICE_PRESETS = {
    "Windows 桌面 · Chrome 默认": {
        "user_agent": "", "window_width": 1280, "window_height": 800,
        "mobile_mode": False, "touch_mode": False, "device_scale_factor": 1.0,
    },
    "Windows 桌面 · 笔记本": {
        "user_agent": "", "window_width": 1366, "window_height": 768,
        "mobile_mode": False, "touch_mode": False, "device_scale_factor": 1.0,
    },
    "Windows 桌面 · 宽屏": {
        "user_agent": "", "window_width": 1440, "window_height": 900,
        "mobile_mode": False, "touch_mode": False, "device_scale_factor": 1.0,
    },
    "Windows 桌面 · 全高清": {
        "user_agent": "", "window_width": 1600, "window_height": 900,
        "mobile_mode": False, "touch_mode": False, "device_scale_factor": 1.0,
    },
    "Windows 桌面 · 紧凑窗口": {
        "user_agent": "", "window_width": 1100, "window_height": 700,
        "mobile_mode": False, "touch_mode": False, "device_scale_factor": 1.0,
    },
    "Windows 桌面 · 1536 宽屏": {
        "user_agent": "", "window_width": 1536, "window_height": 864,
        "mobile_mode": False, "touch_mode": False, "device_scale_factor": 1.0,
    },
    "Windows 桌面 · 1920 全高清": {
        "user_agent": "", "window_width": 1920, "window_height": 1080,
        "mobile_mode": False, "touch_mode": False, "device_scale_factor": 1.0,
    },
}


class EnvironmentDialog(tk.Toplevel):
    def __init__(self, parent, environment=None):
        super().__init__(parent)
        self.withdraw()
        self.result = None
        self.title("浏览器环境配置")
        self.configure(bg=BG)
        self.transient(parent)
        display = system_display_info(parent)
        dialog_width = min(780, max(700, display["work_width"] - 80))
        dialog_height = min(720, max(600, display["work_height"] - 80))
        self.geometry(f"{dialog_width}x{dialog_height}")
        self.minsize(min(700, dialog_width), min(600, dialog_height))
        env = normalize_environment(environment)
        current_device_name = (
            f"当前电脑推荐 · {display['recommended_width']}×"
            f"{display['recommended_height']} · 跟随系统 "
            f"{display['scale_percent']}%"
        )
        self.device_presets = {
            current_device_name: {
                "user_agent": "",
                "window_width": display["recommended_width"],
                "window_height": display["recommended_height"],
                "mobile_mode": False,
                "touch_mode": False,
                "device_scale_factor": 0.0,
            },
            **DEVICE_PRESETS,
        }

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
            ("preset", "快速选择"),
            ("basic", "基础"),
            ("permissions", "权限"),
            ("privacy", "网络与定位"),
            ("advanced", "高级"),
        ):
            page = ttk.Frame(notebook, style="Panel.TFrame")
            canvas = tk.Canvas(page, bg=PANEL, highlightthickness=0)
            scrollbar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)
            content = ttk.Frame(canvas, style="Panel.TFrame", padding=18)
            window = canvas.create_window((0, 0), window=content, anchor="nw")
            content.bind(
                "<Configure>",
                lambda _event, current=canvas: current.configure(
                    scrollregion=current.bbox("all")
                ),
            )
            canvas.bind(
                "<Configure>",
                lambda event, current=canvas, item=window: current.itemconfigure(
                    item, width=event.width
                ),
            )
            canvas.bind(
                "<MouseWheel>",
                lambda event, current=canvas: current.yview_scroll(
                    int(-event.delta / 120), "units"
                ),
            )
            tabs[key] = content
            notebook.add(page, text=title)

        self.vars = {}
        self.choice_maps = {}

        def entry(tab, label, key, value, width=28):
            row = ttk.Frame(tab, style="Panel.TFrame")
            row.pack(fill="x", pady=5)
            ttk.Label(row, text=label, background=PANEL, width=20).pack(side="left")
            variable = tk.StringVar(value=str(value))
            self.vars[key] = variable
            ttk.Entry(row, textvariable=variable, width=width).pack(
                side="left", fill="x", expand=True
            )

        def number_input(tab, label, key, value, minimum, maximum, increment=1):
            row = ttk.Frame(tab, style="Panel.TFrame")
            row.pack(fill="x", pady=5)
            ttk.Label(row, text=label, background=PANEL, width=20).pack(side="left")
            variable = tk.StringVar(value=str(value))
            self.vars[key] = variable
            ttk.Spinbox(
                row,
                textvariable=variable,
                from_=minimum,
                to=maximum,
                increment=increment,
                width=18,
            ).pack(side="left", fill="x", expand=True)

        def friendly_choice(tab, label, key, value, options, editable=False):
            row = ttk.Frame(tab, style="Panel.TFrame")
            row.pack(fill="x", pady=5)
            ttk.Label(row, text=label, background=PANEL, width=20).pack(side="left")
            mapping = dict(options)
            reverse = {item: name for name, item in mapping.items()}
            display = tk.StringVar(value=reverse.get(value, str(value)))
            self.vars[key] = display
            self.choice_maps[key] = mapping
            ttk.Combobox(
                row,
                textvariable=display,
                values=tuple(mapping),
                state="normal" if editable else "readonly",
            ).pack(side="left", fill="x", expand=True)

        def check(tab, label, key, value):
            variable = tk.BooleanVar(value=bool(value))
            self.vars[key] = variable
            ttk.Checkbutton(tab, text=label, variable=variable).pack(anchor="w", pady=5)

        ttk.Label(
            tabs["preset"],
            text="先选常用模板，程序会自动填写 Windows 桌面环境、语言、时区、定位和窗口参数。",
            style="PanelMuted.TLabel",
            wraplength=650,
        ).pack(anchor="w", pady=(0, 12))
        preset_row = ttk.Frame(tabs["preset"], style="Panel.TFrame")
        preset_row.pack(fill="x", pady=6)
        ttk.Label(
            preset_row, text="地区模板", background=PANEL, width=20
        ).pack(side="left")
        self.region_preset = tk.StringVar(value="请选择地区")
        region_combo = ttk.Combobox(
            preset_row, textvariable=self.region_preset,
            values=tuple(REGION_PRESETS), state="readonly",
        )
        region_combo.pack(side="left", fill="x", expand=True)
        region_combo.bind("<<ComboboxSelected>>", self.apply_region_preset)

        device_row = ttk.Frame(tabs["preset"], style="Panel.TFrame")
        device_row.pack(fill="x", pady=6)
        ttk.Label(
            device_row, text="设备模板", background=PANEL, width=20
        ).pack(side="left")
        self.device_preset = tk.StringVar(value="请选择设备")
        device_combo = ttk.Combobox(
            device_row, textvariable=self.device_preset,
            values=tuple(self.device_presets), state="readonly",
        )
        device_combo.pack(side="left", fill="x", expand=True)
        device_combo.bind("<<ComboboxSelected>>", self.apply_device_preset)

        random_row = ttk.Frame(tabs["preset"], style="Panel.TFrame")
        random_row.pack(fill="x", pady=(18, 4))
        ttk.Button(
            random_row,
            text="随机生成测试环境",
            style="Primary.TButton",
            command=self.randomize_environment,
        ).pack(side="left")
        ttk.Label(
            random_row,
            text="随机结果会保持地区、语言、时区、定位和设备参数基本一致。",
            style="PanelMuted.TLabel",
        ).pack(side="left", padx=12)

        ttk.Label(
            tabs["preset"],
            text=(
                "建议：代理国家、地区模板和时区保持一致。本项目当前只提供 "
                "Windows 桌面 Chrome 环境，不再生成手机参数。"
            ),
            style="PanelMuted.TLabel",
            wraplength=650,
            justify="left",
        ).pack(anchor="w", pady=18)

        friendly_choice(
            tabs["basic"], "User-Agent", "user_agent", env["user_agent"],
            (
                ("跟随本机 Chrome（推荐）", ""),
                ("Windows Chrome 149", (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/149.0.0.0 Safari/537.36"
                )),
            ),
            editable=True,
        )
        friendly_choice(
            tabs["basic"], "窗口尺寸", "_window_size",
            f"{env['window_width']}x{env['window_height']}",
            (
                (
                    f"当前电脑推荐 · {display['recommended_width']} × "
                    f"{display['recommended_height']}",
                    f"{display['recommended_width']}x{display['recommended_height']}",
                ),
                ("1024 × 768（小屏）", "1024x768"),
                ("1100 × 700（紧凑）", "1100x700"),
                ("1280 × 720（宽屏）", "1280x720"),
                ("1280 × 800（常用）", "1280x800"),
                ("1366 × 768（笔记本）", "1366x768"),
                ("1440 × 900（桌面）", "1440x900"),
                ("1536 × 864（高分屏）", "1536x864"),
                ("1600 × 900（宽屏）", "1600x900"),
                ("1920 × 1080（全高清）", "1920x1080"),
            ),
        )
        number_input(tabs["basic"], "窗口 X 坐标", "window_x", env["window_x"], 0, 5000, 10)
        number_input(tabs["basic"], "窗口 Y 坐标", "window_y", env["window_y"], 0, 3000, 10)
        friendly_choice(
            tabs["basic"], "浏览器语言", "language", env["language"],
            (
                ("简体中文（中国）", "zh-CN"), ("繁体中文（香港）", "zh-HK"),
                ("繁体中文（台湾）", "zh-TW"), ("英语（美国）", "en-US"),
                ("英语（英国）", "en-GB"), ("英语（新加坡）", "en-SG"),
                ("日语（日本）", "ja-JP"), ("印尼语", "id-ID"),
                ("马来语", "ms-MY"), ("英语（菲律宾）", "en-PH"),
                ("泰语", "th-TH"), ("越南语", "vi-VN"),
            ),
        )
        friendly_choice(
            tabs["basic"], "首选语言请求头", "accept_languages", env["accept_languages"],
            (
                ("中文优先", "zh-CN,zh,en-US,en"),
                ("香港繁体中文优先", "zh-HK,zh-TW,zh,en"),
                ("美国英语优先", "en-US,en"),
                ("英国英语优先", "en-GB,en-US,en"),
                ("日语优先", "ja-JP,ja,en-US,en"),
                ("印尼语优先", "id-ID,id,en-US,en"),
                ("马来语优先", "ms-MY,ms,en-US,en"),
                ("越南语优先", "vi-VN,vi,en-US,en"),
                ("泰语优先", "th-TH,th,en-US,en"),
                ("菲律宾英语优先", "en-PH,en-US,en,fil"),
            ),
        )
        friendly_choice(
            tabs["basic"], "时区", "timezone", env["timezone"],
            (
                ("中国大陆 · 上海", "Asia/Shanghai"),
                ("中国香港", "Asia/Hong_Kong"), ("中国台湾", "Asia/Taipei"),
                ("日本 · 东京", "Asia/Tokyo"), ("新加坡", "Asia/Singapore"),
                ("印度尼西亚 · 雅加达", "Asia/Jakarta"),
                ("马来西亚 · 吉隆坡", "Asia/Kuala_Lumpur"),
                ("越南 · 胡志明市", "Asia/Ho_Chi_Minh"),
                ("泰国 · 曼谷", "Asia/Bangkok"),
                ("菲律宾 · 马尼拉", "Asia/Manila"),
                ("美国 · 纽约", "America/New_York"),
                ("美国 · 芝加哥", "America/Chicago"),
                ("美国 · 洛杉矶", "America/Los_Angeles"),
                ("英国 · 伦敦", "Europe/London"),
                ("德国 · 柏林", "Europe/Berlin"),
                ("澳大利亚 · 悉尼", "Australia/Sydney"),
            ),
        )
        friendly_choice(
            tabs["basic"], "颜色模式", "color_mode", env["color_mode"],
            (
                ("跟随系统", "system"), ("浅色模式", "light"), ("深色模式", "dark"),
            ),
        )
        friendly_choice(
            tabs["basic"], "设备缩放比例", "device_scale_factor",
            str(env["device_scale_factor"]),
            (
                (
                    f"跟随当前电脑（系统 {display['scale_percent']}%，推荐）",
                    "0.0",
                ),
                ("80%（显示更多内容）", "0.8"),
                ("90%（稍小）", "0.9"),
                ("100%（保留原有效果）", "1.0"),
                ("110%（稍大）", "1.1"),
                ("125%（高分屏）", "1.25"),
                ("150%（高分屏）", "1.5"),
                ("175%", "1.75"),
                ("200%", "2.0"),
            ),
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

        friendly_choice(
            tabs["privacy"], "WebRTC 策略", "webrtc_policy",
            env["webrtc_policy"],
            (
                ("限制非代理 UDP（推荐）", "disable_non_proxied_udp"),
                ("仅默认公网网卡", "default_public_interface_only"),
                ("Chrome 默认", "default"),
            ),
        )
        friendly_choice(
            tabs["privacy"], "DNS 模式", "dns_mode", env["dns_mode"],
            (
                ("跟随系统 DNS", "system"),
                ("关闭 Chrome 异步 DNS", "disable_async"),
                ("Cloudflare 安全 DNS", "secure"),
                ("自定义 DoH 地址", "custom"),
            ),
        )
        friendly_choice(
            tabs["privacy"], "安全 DNS 服务", "dns_template", env["dns_template"],
            (
                ("Cloudflare", "https://cloudflare-dns.com/dns-query"),
                ("Google", "https://dns.google/dns-query"),
                ("Quad9", "https://dns.quad9.net/dns-query"),
                ("阿里公共 DNS", "https://dns.alidns.com/dns-query"),
            ),
            editable=True,
        )
        check(tabs["privacy"], "启用地理位置模拟", "geo_enabled", env["geo_enabled"])
        friendly_choice(
            tabs["privacy"], "定位精度", "accuracy", str(env["accuracy"]),
            (
                ("精确 · 20 米", "20"), ("城市级 · 100 米", "100"),
                ("大致区域 · 1000 米", "1000"),
            ),
        )
        number_input(
            tabs["privacy"], "纬度（-90 到 90）", "latitude",
            env["latitude"], -90, 90, 0.0001,
        )
        number_input(
            tabs["privacy"], "经度（-180 到 180）", "longitude",
            env["longitude"], -180, 180, 0.0001,
        )
        ttk.Label(
            tabs["privacy"],
            text="以下为隐私测试覆盖，不保证绕过检测，并可能造成环境不一致：",
            style="PanelMuted.TLabel",
        ).pack(anchor="w", pady=(14, 5))
        check(tabs["privacy"], "Canvas 隐私扰动", "privacy_canvas", env["privacy_canvas"])
        check(tabs["privacy"], "WebGL 信息隐藏", "privacy_webgl", env["privacy_webgl"])
        check(tabs["privacy"], "硬件信息最小化", "privacy_hardware", env["privacy_hardware"])
        check(tabs["privacy"], "字体枚举限制", "privacy_fonts", env["privacy_fonts"])
        friendly_choice(
            tabs["advanced"], "模拟 CPU 线程数", "hardware_concurrency",
            str(env["hardware_concurrency"]),
            (("2 线程", "2"), ("4 线程（常用）", "4"), ("6 线程", "6"),
             ("8 线程", "8"), ("12 线程", "12"), ("16 线程", "16")),
        )
        friendly_choice(
            tabs["advanced"], "模拟设备内存（GB）", "device_memory",
            str(env["device_memory"]),
            (("2 GB", "2"), ("4 GB", "4"), ("8 GB（常用）", "8"),
             ("12 GB", "12"), ("16 GB", "16"), ("32 GB", "32")),
        )
        friendly_choice(
            tabs["advanced"], "WebGL 厂商", "webgl_vendor",
            env["webgl_vendor"],
            (
                ("保持隐私占位值", "Privacy Protected"),
                ("Intel", "Google Inc. (Intel)"),
                ("NVIDIA", "Google Inc. (NVIDIA)"),
                ("AMD", "Google Inc. (AMD)"),
            ),
            editable=True,
        )
        friendly_choice(
            tabs["advanced"], "WebGL 渲染器", "webgl_renderer",
            env["webgl_renderer"],
            (
                ("保持隐私占位值", "Privacy Protected Renderer"),
                ("Intel UHD Graphics 620", "ANGLE (Intel, Intel(R) UHD Graphics 620, D3D11)"),
                ("NVIDIA GTX 1660", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660, D3D11)"),
                ("AMD Radeon Graphics", "ANGLE (AMD, AMD Radeon Graphics, D3D11)"),
            ),
            editable=True,
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
        self.after_idle(lambda: self.show_centered(parent))

    def show_centered(self, parent):
        center_window(self, parent)
        self.deiconify()
        self.lift()
        self.grab_set()
        self.focus_force()

    def set_field_value(self, key, value):
        variable = self.vars.get(key)
        if variable is None:
            return
        mapping = self.choice_maps.get(key)
        if mapping:
            display = next(
                (name for name, actual in mapping.items() if str(actual) == str(value)),
                str(value),
            )
            variable.set(display)
        else:
            variable.set(value)

    def apply_region_preset(self, _event=None):
        preset = REGION_PRESETS.get(self.region_preset.get())
        if not preset:
            return
        for key, value in preset.items():
            self.set_field_value(key, value)
        self.set_field_value("geo_enabled", True)
        self.set_field_value("geolocation_permission", True)

    def apply_device_preset(self, _event=None):
        preset = self.device_presets.get(self.device_preset.get())
        if not preset:
            return
        for key, value in preset.items():
            if key in ("window_width", "window_height"):
                continue
            self.set_field_value(key, value)
        self.set_field_value("mobile_mode", False)
        self.set_field_value("touch_mode", False)
        self.set_field_value(
            "_window_size",
            f"{preset['window_width']}x{preset['window_height']}",
        )

    def randomize_environment(self):
        selected_region = self.region_preset.get()
        selected_device = self.device_preset.get()
        region_name = (
            selected_region
            if selected_region in REGION_PRESETS
            else random.choice(tuple(REGION_PRESETS))
        )
        device_name = (
            selected_device
            if selected_device in self.device_presets
            else random.choice(tuple(self.device_presets))
        )
        self.region_preset.set(region_name)
        self.device_preset.set(device_name)
        self.apply_region_preset()
        self.apply_device_preset()

        region = REGION_PRESETS[region_name]
        self.set_field_value(
            "latitude",
            round(float(region["latitude"]) + random.uniform(-0.035, 0.035), 6),
        )
        self.set_field_value(
            "longitude",
            round(float(region["longitude"]) + random.uniform(-0.035, 0.035), 6),
        )
        self.set_field_value("window_x", random.choice((40, 80, 120, 160, 220)))
        self.set_field_value("window_y", random.choice((40, 70, 100, 140)))
        self.set_field_value("color_mode", random.choice(("system", "light", "dark")))
        self.set_field_value("accuracy", random.choice(("20", "100", "1000")))
        self.set_field_value("hardware_concurrency", random.choice(
            ("4", "6", "8", "12", "16")
        ))
        self.set_field_value("device_memory", random.choice(
            ("4", "8", "12", "16")
        ))
        self.set_field_value(
            "webrtc_policy",
            "disable_non_proxied_udp",
        )
        self.set_field_value("dns_mode", random.choice(("system", "secure")))
        self.set_field_value(
            "dns_template",
            random.choice((
                "https://cloudflare-dns.com/dns-query",
                "https://dns.google/dns-query",
                "https://dns.quad9.net/dns-query",
            )),
        )
        messagebox.showinfo(
            "随机环境已生成",
            f"地区：{region_name}\n设备：{device_name}（Windows 桌面）\n\n"
            "已在所选城市附近小范围随机定位坐标和定位精度。\n"
            "请检查代理出口国家是否与该地区一致，再保存配置。",
            parent=self,
        )

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
                if key in self.choice_maps:
                    value = self.choice_maps[key].get(value, value)
                if key == "_window_size":
                    width, height = str(value).lower().replace("×", "x").split("x", 1)
                    result["window_width"] = int(width.strip())
                    result["window_height"] = int(height.strip())
                    continue
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
        if not -90 <= result["latitude"] <= 90:
            messagebox.showerror("纬度错误", "纬度必须在 -90 到 90 之间。")
            return
        if not -180 <= result["longitude"] <= 180:
            messagebox.showerror("经度错误", "经度必须在 -180 到 180 之间。")
            return
        result["mobile_mode"] = False
        result["touch_mode"] = False
        self.result = result
        self.destroy()


class BrowserDialog(tk.Toplevel):
    def __init__(
        self,
        parent,
        title,
        record=None,
        default_port=9231,
        browser_key="",
        vault=None,
    ):
        super().__init__(parent)
        self.withdraw()
        self.result = None
        self.vault_result = None
        self.browser_key = browser_key
        self.title(title)
        self.configure(bg=BG)
        self.transient(parent)
        self.resizable(True, True)
        display = system_display_info(parent)
        height = min(700, max(560, display["work_height"] - 100))
        width = min(760, max(680, display["work_width"] - 120))
        self.geometry(f"{width}x{height}")
        self.minsize(650, 540)

        data = record or {}
        self.environment = normalize_environment(data.get("environment"))
        self.account_records = [
            item.copy()
            for item in (vault or [])
            if item.get("browser_key", "") == browser_key
        ]
        shell = tk.Frame(self, bg=BG, padx=14, pady=14)
        shell.pack(fill="both", expand=True)
        card = tk.Frame(shell, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="both", expand=True)
        header = ttk.Frame(card, style="Panel.TFrame", padding=(22, 15))
        header.pack(fill="x")
        ttk.Label(header, text=title, style="DialogTitle.TLabel").pack(anchor="w")
        ttk.Label(header, text="填写独立浏览器信息，保存后立即同步到管理列表。",
                  style="PanelMuted.TLabel").pack(anchor="w", pady=(4, 0))

        environment_card = tk.Frame(
            card,
            bg="#f7f3ee",
            highlightthickness=1,
            highlightbackground=BORDER,
            padx=14,
            pady=10,
        )
        environment_card.pack(fill="x", padx=20, pady=(0, 8))
        environment_text = tk.Frame(environment_card, bg="#f7f3ee")
        environment_text.pack(side="left", fill="x", expand=True)
        tk.Label(
            environment_text,
            text="浏览器环境",
            bg="#f7f3ee",
            fg=TEXT,
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(anchor="w")
        self.environment_summary = tk.Label(
            environment_text,
            bg="#f7f3ee",
            fg=MUTED,
            justify="left",
            anchor="w",
            font=("Microsoft YaHei UI", 9),
        )
        self.environment_summary.pack(anchor="w", pady=(4, 0))
        ttk.Button(
            environment_card,
            text="修改环境配置",
            style="Primary.TButton",
            command=self.edit_environment,
        ).pack(side="right", padx=(12, 0))
        self.update_environment_summary()

        button_bar = ttk.Frame(card, style="Panel.TFrame", padding=(20, 12))
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
        self.form = ttk.Frame(canvas, style="Panel.TFrame", padding=(20, 8, 20, 16))
        window = canvas.create_window((0, 0), window=self.form, anchor="nw")
        self.form.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.bind_all("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))
        self.bind("<Destroy>", lambda _: canvas.unbind_all("<MouseWheel>"))

        self.form.columnconfigure(0, weight=1)
        self.form.columnconfigure(1, weight=1)
        self.vars = {}

        def add_field(label, key, value, row, column=0, columnspan=1):
            field = ttk.Frame(self.form, style="Panel.TFrame")
            field.grid(
                row=row,
                column=column,
                columnspan=columnspan,
                sticky="ew",
                padx=(0, 8) if column == 0 and columnspan == 1 else (8, 0)
                if column == 1 else 0,
                pady=(5, 3),
            )
            ttk.Label(field, text=label, background=PANEL).pack(
                anchor="w", pady=(0, 4)
            )
            variable = tk.StringVar(value=value)
            self.vars[key] = variable
            entry = ttk.Entry(field, textvariable=variable)
            entry.pack(fill="x")
            return entry

        first_entry = add_field("名称", "name", data.get("name", ""), 0, 0)
        add_field("分组", "group", data.get("group", "默认分组"), 0, 1)
        add_field(
            "CDP 端口", "port", str(data.get("port", default_port)), 1, 0
        )
        home_field = ttk.Frame(self.form, style="Panel.TFrame")
        home_field.grid(
            row=1,
            column=1,
            sticky="ew",
            padx=(8, 0),
            pady=(5, 3),
        )
        ttk.Label(home_field, text="启动首页", background=PANEL).pack(
            anchor="w", pady=(0, 4)
        )
        self.vars["home"] = tk.StringVar(
            value=display_home_value(data.get("home", ""))
        )
        self.home_combo = ttk.Combobox(
            home_field,
            textvariable=self.vars["home"],
            style="Filter.TCombobox",
            values=self.home_choices(),
        )
        self.home_combo.pack(fill="x")
        add_field(
            "代理服务器（可留空）", "proxy", data.get("proxy", ""), 2, 0, 2
        )
        proxy_row = ttk.Frame(self.form, style="Panel.TFrame")
        proxy_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(3, 5))
        self.proxy_test_button = ttk.Button(
            proxy_row, text="测试代理", style="Primary.TButton",
            command=self.run_proxy_test,
        )
        self.proxy_test_button.pack(side="left")
        self.proxy_result = ttk.Label(
            proxy_row, text="支持 http://用户:密码@IP:端口",
            style="PanelMuted.TLabel",
        )
        self.proxy_result.pack(side="left", padx=10)
        add_field("备注", "note", data.get("note", ""), 4, 0, 2)
        add_field(
            "定时启动（HH:MM，可留空）",
            "schedule",
            data.get("schedule", ""),
            5,
            0,
        )
        self.auto_start = tk.BooleanVar(value=data.get("auto_start", False))
        ttk.Checkbutton(
            self.form,
            text="管理器启动时自动打开此浏览器",
            variable=self.auto_start,
        ).grid(row=5, column=1, sticky="w", padx=(16, 0), pady=(30, 3))

        account_box = ttk.Frame(self.form, style="Panel.TFrame")
        account_box.grid(row=6, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        self.form.rowconfigure(6, weight=1)
        account_header = ttk.Frame(account_box, style="Panel.TFrame")
        account_header.pack(fill="x", pady=(0, 6))
        ttk.Label(
            account_header,
            text="网页账号",
            background=PANEL,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side="left")
        ttk.Button(
            account_header,
            text="新增网页账号",
            style="Primary.TButton",
            command=self.add_account,
        ).pack(side="right", padx=(6, 0))
        account_columns = ("site", "url", "username", "password", "note", "action")
        self.account_tree = ttk.Treeview(
            account_box,
            columns=account_columns,
            show="headings",
            height=5,
            selectmode="browse",
        )
        account_headers = {
            "site": "网站", "url": "网址", "username": "账号",
            "password": "密码", "note": "备注", "action": "操作",
        }
        account_widths = {
            "site": 70, "url": 160, "username": 90,
            "password": 105, "note": 70, "action": 180,
        }
        self.account_tree_min_widths = account_widths
        self.account_tree_width_weights = {
            "site": 0.5, "url": 1.8, "username": 0.85,
            "password": 0.9, "note": 0.65, "action": 0.95,
        }
        for column in account_columns:
            self.account_tree.heading(column, text=account_headers[column], anchor="center")
            self.account_tree.column(
                column,
                width=account_widths[column],
                minwidth=account_widths[column],
                anchor="center",
                stretch=False,
            )
        account_scroll = ttk.Scrollbar(
            account_box, orient="vertical", command=self.on_account_tree_scroll
        )
        self.account_tree_scrollbar = account_scroll
        self.account_tree.configure(yscrollcommand=self.on_account_tree_yview)
        self.account_tree.pack(side="left", fill="both", expand=True)
        account_scroll.pack(side="right", fill="y")
        self.account_tree.bind("<Double-1>", lambda _: self.edit_account())
        self.account_tree.bind("<Configure>", self.on_account_tree_configure)
        self.account_tree.bind(
            "<MouseWheel>", lambda _: self.after_idle(self.position_account_buttons)
        )
        self.account_action_buttons = {}
        self.refresh_accounts()
        self.bind("<Control-Return>", lambda _: self.save())
        self.bind("<Escape>", lambda _: self.destroy())

        def initialize_dialog():
            canvas.yview_moveto(0)
            center_window(self, parent)
            self.deiconify()
            self.lift()
            self.grab_set()
            first_entry.focus_set()

        self.after_idle(initialize_dialog)

    def home_choices(self):
        choices = [WELCOME_HOME_LABEL]
        for record in self.account_records:
            url = record.get("url", "").strip()
            if url and url not in choices:
                choices.append(url)
        return choices

    def refresh_home_choices(self):
        if hasattr(self, "home_combo"):
            self.home_combo.configure(values=self.home_choices())

    def current_browser_options(self):
        name = self.vars.get("name").get().strip() if self.vars.get("name") else ""
        return {self.browser_key: {"name": name or "当前浏览器"}}

    def refresh_accounts(self):
        if not hasattr(self, "account_tree"):
            return
        for button_group in getattr(self, "account_action_buttons", {}).values():
            button_group.destroy()
        self.account_action_buttons = {}
        self.account_tree.delete(*self.account_tree.get_children())
        for index, record in enumerate(self.account_records):
            key = str(index)
            self.account_tree.insert(
                "",
                "end",
                iid=key,
                values=(
                    record.get("site", ""),
                    record.get("url", ""),
                    record.get("username", ""),
                    record.get("password", ""),
                    record.get("note", ""),
                    "",
                ),
            )
            button_group = tk.Frame(self.account_tree, bg=CARD)
            for text, command in (
                ("编辑", lambda row=key: self.edit_account_row(row)),
                ("启动页", lambda row=key: self.use_account_as_home_row(row)),
                ("删除", lambda row=key: self.delete_account_row(row)),
            ):
                tk.Button(
                    button_group,
                    text=text,
                    command=command,
                    relief="flat",
                    borderwidth=0,
                    bg=BLUE,
                    fg="white",
                    activebackground="#b9684f",
                    activeforeground="white",
                    font=("Microsoft YaHei UI", 9, "bold"),
                    cursor="hand2",
                ).pack(side="left", fill="both", expand=True, padx=2)
            self.account_action_buttons[key] = button_group
        self.refresh_home_choices()
        self.resize_account_tree_columns()
        self.after_idle(self.position_account_buttons)

    def on_account_tree_configure(self, _event=None):
        pending = getattr(self, "account_tree_resize_job", None)
        if pending:
            self.after_cancel(pending)
        self.account_tree_resize_job = self.after(60, self.resize_account_tree_columns)

    def resize_account_tree_columns(self):
        self.account_tree_resize_job = None
        if not hasattr(self, "account_tree") or not self.account_tree.winfo_exists():
            return
        available = max(0, self.account_tree.winfo_width() - 4)
        minimum_total = sum(self.account_tree_min_widths.values())
        extra = max(0, available - minimum_total)
        weight_total = sum(self.account_tree_width_weights.values())
        for column in self.account_tree["columns"]:
            width = self.account_tree_min_widths[column]
            if extra:
                width += round(
                    extra * self.account_tree_width_weights[column] / weight_total
                )
            self.account_tree.column(column, width=width)
        self.position_account_buttons()

    def on_account_tree_yview(self, first, last):
        self.account_tree_scrollbar.set(first, last)
        self.after_idle(self.position_account_buttons)

    def on_account_tree_scroll(self, *args):
        self.account_tree.yview(*args)
        self.after_idle(self.position_account_buttons)

    def position_account_buttons(self):
        if not hasattr(self, "account_action_buttons"):
            return
        for key, button_group in self.account_action_buttons.items():
            box = self.account_tree.bbox(key, "action")
            if not box:
                button_group.place_forget()
                continue
            x, y, width, height = box
            button_group.place(
                x=x + 4,
                y=y + 5,
                width=max(168, width - 8),
                height=max(30, height - 10),
            )

    def selected_account_index(self):
        selected = self.account_tree.selection()
        if not selected:
            messagebox.showinfo("请选择账号", "请先选择一条网页账号。", parent=self)
            return None
        return int(selected[0])

    def edit_account_dialog(self, record=None):
        dialog = VaultDialog(
            self,
            self.current_browser_options(),
            record,
            fixed_browser_key=self.browser_key,
        )
        self.wait_window(dialog)
        if not dialog.result:
            return None
        result = dialog.result
        result["browser_key"] = self.browser_key
        return result

    def add_account(self):
        result = self.edit_account_dialog()
        if not result:
            return
        self.account_records.append(result)
        self.refresh_accounts()

    def select_account_row(self, key):
        if not self.account_tree.exists(key):
            return None
        self.account_tree.selection_set(key)
        return int(key)

    def edit_account_row(self, key):
        index = self.select_account_row(key)
        if index is None:
            return
        self.edit_account_at(index)

    def delete_account_row(self, key):
        index = self.select_account_row(key)
        if index is None:
            return
        self.delete_account(index)

    def use_account_as_home_row(self, key):
        index = self.select_account_row(key)
        if index is None:
            return
        self.use_account_as_home(index)

    def edit_account(self):
        index = self.selected_account_index()
        if index is None:
            return
        self.edit_account_at(index)

    def edit_account_at(self, index):
        result = self.edit_account_dialog(self.account_records[index])
        if not result:
            return
        self.account_records[index] = result
        self.refresh_accounts()

    def delete_account(self, index=None):
        if index is None:
            index = self.selected_account_index()
        if index is None:
            return
        site = self.account_records[index].get("site", "")
        if not messagebox.askyesno("删除账号", f"确定删除 [{site}]？", parent=self):
            return
        removed = self.account_records.pop(index)
        if self.vars["home"].get().strip() == removed.get("url", "").strip():
            self.vars["home"].set(WELCOME_HOME_LABEL)
        self.refresh_accounts()

    def use_account_as_home(self, index=None):
        if index is None:
            index = self.selected_account_index()
        if index is None:
            return
        url = self.account_records[index].get("url", "").strip()
        if not url:
            messagebox.showinfo("没有网址", "这条网页账号没有保存网址。", parent=self)
            return
        self.vars["home"].set(url)

    def edit_environment(self):
        dialog = EnvironmentDialog(self, self.environment)
        self.wait_window(dialog)
        if dialog.result:
            self.environment = dialog.result
            self.update_environment_summary()

    def update_environment_summary(self):
        env = normalize_environment(self.environment)
        user_agent = env["user_agent"] or "跟随 Chrome 默认值"
        if len(user_agent) > 48:
            user_agent = user_agent[:45] + "..."
        scale = float(env["device_scale_factor"])
        scale_text = "跟随系统缩放" if scale <= 0 else f"{scale * 100:g}%"
        self.environment_summary.config(
            text=(
                f"{env['language']} · {env['timezone']} · "
                f"{env['window_width']}×{env['window_height']} · {scale_text}\n"
                f"UA：{user_agent}"
            )
        )

    def run_proxy_test(self):
        value = self.vars["proxy"].get().strip()
        if not value:
            messagebox.showerror("代理测试失败", "请先填写代理服务器。", parent=self)
            return
        self.proxy_result.config(text="正在检测...")
        self.proxy_test_button.configure(state="disabled")

        def worker():
            try:
                result = test_proxy(value)
            except ValueError as error:
                message = str(error)
                self.after(
                    0,
                    lambda: self.finish_proxy_test(error_message=message),
                )
                return
            self.after(0, lambda: self.finish_proxy_test(result=result))

        threading.Thread(target=worker, daemon=True).start()

    def finish_proxy_test(self, result=None, error_message=""):
        if not self.winfo_exists():
            return
        self.proxy_test_button.configure(state="normal")
        if error_message:
            self.proxy_result.config(text="测试失败", foreground=RED)
            messagebox.showerror("代理测试失败", error_message, parent=self)
            return
        try:
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
        except Exception as error:
            self.proxy_result.config(text="测试失败", foreground=RED)
            messagebox.showerror("代理测试失败", str(error), parent=self)

    def save(self):
        name = self.vars["name"].get().strip()
        if not name:
            messagebox.showerror("名称不能为空", "请输入浏览器名称。", parent=self)
            return
        try:
            port = int(self.vars["port"].get())
            if not 1024 <= port <= MAX_CDP_PORT:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "端口错误",
                f"请输入 1024 到 {MAX_CDP_PORT} 之间的端口。",
                parent=self,
            )
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
        self.result["home"] = storage_home_value(self.result.get("home", ""))
        self.result["port"] = port
        self.result["auto_start"] = self.auto_start.get()
        self.result["environment"] = self.environment
        self.vault_result = []
        for account in self.account_records:
            item = account.copy()
            item["browser_key"] = self.browser_key
            self.vault_result.append(item)
        self.destroy()


class VaultDialog(tk.Toplevel):
    def __init__(self, parent, browsers, record=None, fixed_browser_key=None):
        super().__init__(parent)
        self.result = None
        self.fixed_browser_key = fixed_browser_key
        self.title("账号信息")
        self.configure(bg=BG)
        self.transient(parent)
        self.grab_set()
        self.resizable(True, True)
        screen_h = self.winfo_screenheight()
        height = min(680, max(500, screen_h - 120))
        self.geometry(f"580x{height}")
        self.minsize(520, 480)
        self.after_idle(lambda: center_window(self, parent))

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
        if fixed_browser_key is not None:
            current_key = fixed_browser_key
        current = next(
            (value for value in browser_values if value.startswith(f"{current_key} ·")),
            "未指定",
        )
        self.browser_var = tk.StringVar(value=current)
        if fixed_browser_key is None:
            ttk.Combobox(
                form, textvariable=self.browser_var, values=browser_values,
                state="readonly",
            ).pack(fill="x", ipady=4)
        else:
            ttk.Entry(
                form,
                textvariable=self.browser_var,
                state="readonly",
            ).pack(fill="x", ipady=5)

        self.bind("<Escape>", lambda _: self.destroy())
        self.bind("<Control-Return>", lambda _: self.save())

    def save(self):
        site = self.vars["site"].get().strip()
        if not site:
            messagebox.showerror("缺少网站名称", "请输入网站名称。", parent=self)
            return
        url = self.vars["url"].get().strip()
        if url and url != "https://":
            try:
                self.vars["url"].set(normalize_web_url(url))
            except ValueError as error:
                messagebox.showerror("网址格式错误", str(error), parent=self)
                return
        elif url == "https://":
            self.vars["url"].set("")
        browser_value = self.browser_var.get()
        browser_key = (
            self.fixed_browser_key
            if self.fixed_browser_key is not None
            else "" if browser_value == "未指定" else browser_value.split(" · ", 1)[0]
        )
        self.result = {key: value.get().strip() for key, value in self.vars.items()}
        self.result["browser_key"] = browser_key
        self.destroy()


class App:
    def __init__(self, root):
        self.root = root
        self.map = load_json(MAP_FILE, {})
        map_changed = False
        for record in self.map.values():
            current = record.get("environment")
            normalized = normalize_environment(current)
            if current != normalized:
                record["environment"] = normalized
                map_changed = True
            home = normalize_home_value(record.get("home", ""))
            if record.get("home", "") != home:
                record["home"] = home
                map_changed = True
        if map_changed:
            save_json(MAP_FILE, self.map)
        self.map_mtime = MAP_FILE.stat().st_mtime_ns if MAP_FILE.exists() else 0
        self.settings = load_json(
            SETTINGS_FILE,
            {"quick_urls": [], "minimize_to_tray": True, "password_hash": ""},
        )
        if not self.settings.get("password_save_prompt_migrated"):
            migrated = False
            for record in self.map.values():
                environment = record.get("environment", {})
                if environment.get("disable_password_prompt"):
                    environment["disable_password_prompt"] = False
                    migrated = True
            if migrated:
                save_json(MAP_FILE, self.map)
                self.map_mtime = MAP_FILE.stat().st_mtime_ns
            self.settings["password_save_prompt_migrated"] = True
            save_json(SETTINGS_FILE, self.settings)
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
        if FROZEN and self.settings.get("auto_check_updates", True):
            self.root.after(5000, self.auto_check_app_update)

    def configure_style(self):
        self.root.title(f"Chrome 多开管理器 {APP_VERSION}")
        display = system_display_info(self.root)
        width = min(1380, max(1080, display["work_width"] - 40))
        height = min(800, max(640, display["work_height"] - 40))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(1080, width), min(640, height))
        self.root.configure(bg=BG)
        self.apply_window_icon()
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(name).configure(
                    family="Microsoft YaHei UI", size=10
                )
            except tk.TclError:
                pass
        try:
            tkfont.nametofont("TkHeadingFont").configure(
                family="Microsoft YaHei UI", size=10, weight="bold"
            )
            tkfont.nametofont("TkFixedFont").configure(
                family="Consolas", size=10
            )
        except tk.TclError:
            pass
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=CARD,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        font=("Microsoft YaHei UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Title.TLabel", background=PANEL, font=("Microsoft YaHei UI", 17, "bold"),
                        foreground=TEXT)
        style.configure("Section.TLabel", background=BG, font=("Microsoft YaHei UI", 14, "bold"),
                        foreground=TEXT)
        style.configure("DialogTitle.TLabel", background=PANEL, foreground=TEXT,
                        font=("Microsoft YaHei UI", 14, "bold"))
        style.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED,
                        font=("Microsoft YaHei UI", 9))
        style.configure("Muted.TLabel", foreground=MUTED,
                        font=("Microsoft YaHei UI", 9))
        style.configure("TEntry", padding=(10, 7), fieldbackground=PANEL, foreground=TEXT,
                        borderwidth=1, relief="solid")
        style.configure("TCombobox", padding=(10, 6), fieldbackground=PANEL, foreground=TEXT,
                        arrowcolor=MUTED)
        style.configure(
            "Filter.TCombobox",
            padding=(10, 6),
            fieldbackground=PANEL,
            background=PANEL,
            foreground=TEXT,
            arrowcolor=BLUE,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
        )
        style.map(
            "Filter.TCombobox",
            fieldbackground=[("readonly", PANEL), ("focus", PANEL)],
            background=[("readonly", PANEL), ("focus", PANEL)],
            foreground=[("readonly", TEXT), ("focus", TEXT)],
            selectbackground=[("readonly", PANEL), ("focus", PANEL)],
            selectforeground=[("readonly", TEXT), ("focus", TEXT)],
            arrowcolor=[("active", BLUE), ("readonly", BLUE)],
        )
        style.configure("Treeview", background=CARD, foreground=TEXT,
                        fieldbackground=CARD, rowheight=40, borderwidth=0,
                        font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", background="#f4f2ed", foreground="#57534e",
                        bordercolor=BORDER, relief="flat",
                        font=("Microsoft YaHei UI", 10, "bold"), padding=(8, 8))
        style.map("Treeview", background=[("selected", "#f2dfd8")],
                  foreground=[("selected", TEXT)])
        style.configure("TButton", padding=(13, 7), background=BLUE, foreground="white",
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
        style.configure("Icon.TButton", padding=(11, 7), background=BLUE,
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

        sidebar = tk.Frame(shell, bg=SIDEBAR, width=164, highlightthickness=1,
                           highlightbackground=BORDER)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = tk.Frame(sidebar, bg=SIDEBAR)
        brand.pack(fill="x", padx=16, pady=(16, 18))
        tk.Label(brand, text="C", bg=BLUE, fg="white", width=2, height=1,
                 font=("Segoe UI", 14, "bold")).pack(side="left")
        tk.Label(brand, text="  Chrome\n  Manager", bg=SIDEBAR, fg=TEXT,
                 justify="left", font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")

        content = tk.Frame(shell, bg=BG)
        content.pack(side="left", fill="both", expand=True)
        header = ttk.Frame(content, style="Panel.TFrame", padding=(20, 11))
        header.pack(fill="x")
        left = ttk.Frame(header, style="Panel.TFrame")
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text="独立浏览器", style="Title.TLabel").pack(side="left")
        ttk.Label(
            left,
            text="独立配置 · 固定 CDP 端口 · 本地数据",
            style="PanelMuted.TLabel",
        ).pack(side="left", padx=(14, 0), pady=(4, 0))
        self.summary = ttk.Label(header, text="", foreground=GREEN,
                                 background=PANEL, font=("Microsoft YaHei UI", 10, "bold"))
        right = ttk.Frame(header, style="Panel.TFrame")
        right.pack(side="right")
        self.summary.pack(in_=right, side="left", padx=(0, 14))
        self.cloud_account_var = tk.StringVar()
        ttk.Button(
            right,
            textvariable=self.cloud_account_var,
            command=self.open_cloud_account,
        ).pack(side="left")
        self.cloud_dialog = None
        self.cloud_server_var = tk.StringVar(
            value=self.settings.get("cloud_server", "http://127.0.0.1:8787")
        )
        self.cloud_username_var = tk.StringVar(
            value=self.settings.get("cloud_username", "")
        )
        self.cloud_password_var = tk.StringVar()
        self.cloud_status_var = tk.StringVar()
        self.update_cloud_status()

        self.notebook = ttk.Notebook(content, style="Hidden.TNotebook")
        self.notebook.pack(fill="both", expand=True, padx=16, pady=(12, 14))
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
                font=("Microsoft YaHei UI", 10, "bold"), padx=20, pady=10,
                command=lambda target=index: self.select_page(target),
            )
            button.pack(fill="x", padx=9, pady=2)
            self.nav_buttons.append(button)
        tk.Label(sidebar, text=f"版本  {APP_VERSION}", bg=SIDEBAR, fg=MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(side="bottom", pady=14)

        self.build_main()
        self.build_passwords()
        self.build_logs()
        self.build_settings()
        self.update_access_controls()
        self.select_page(0)

    def select_page(self, index):
        self.notebook.select(index)
        for position, button in enumerate(self.nav_buttons):
            active = position == index
            button.configure(
                bg="#e7e2d9" if active else SIDEBAR,
                fg=BLUE if active else TEXT,
                font=("Microsoft YaHei UI", 10, "bold"),
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
        filters.pack(fill="x", pady=(0, 7))
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
        toolbar.pack(fill="x", pady=(0, 7))
        self.create_browser_button = ttk.Button(
            toolbar, text="＋ 新建浏览器", command=self.create,
            style="Primary.TButton",
        )
        self.create_browser_button.pack(side="left", padx=(0, 8))
        ttk.Button(toolbar, text="停止选中", command=self.stop_selected,
                   style="Danger.TButton").pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="更多操作", command=self.more_menu).pack(side="left")
        ttk.Label(
            toolbar,
            text="拖动浏览器名称可调整排序；列表支持 Ctrl / Shift 多选",
            style="Muted.TLabel",
        ).pack(side="left", padx=(12, 0))
        ttk.Button(toolbar, text="全部启动", command=self.start_all).pack(side="right")

        columns = (
            "name", "group", "port", "status", "pid", "tabs", "memory",
            "last_open", "home", "proxy", "action",
        )
        table = ttk.Frame(self.main_tab, style="Panel.TFrame")
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            table, columns=columns, show="headings", selectmode="extended"
        )
        headers = {
            "name": "浏览器名称", "action": "操作", "group": "分组", "port": "CDP 端口",
            "status": "设备状态", "pid": "进程 PID", "tabs": "标签页", "memory": "内存",
            "last_open": "上次打开时间", "home": "启动首页", "proxy": "代理配置",
        }
        widths = {
            "name": 105, "group": 70, "port": 75, "status": 75, "pid": 75,
            "tabs": 58, "memory": 65, "last_open": 120,
            "home": 120, "proxy": 105, "action": 210,
        }
        self.tree_min_widths = widths
        self.tree_width_weights = {
            "name": 1.1, "group": 0.7, "port": 0.7, "status": 0.75,
            "pid": 0.7, "tabs": 0.55, "memory": 0.6, "last_open": 1.05,
            "home": 1.25, "proxy": 1.15, "action": 1.45,
        }
        for column in columns:
            self.tree.heading(column, text=headers[column], anchor="center")
            self.tree.column(
                column, width=widths[column], minwidth=widths[column],
                anchor="center", stretch=False,
            )
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.on_tree_scroll)
        horizontal = ttk.Scrollbar(
            table, orient="horizontal", command=self.on_tree_xscroll
        )
        self.tree_scrollbar = vertical
        self.tree_xscrollbar = horizontal
        self.tree.configure(
            yscrollcommand=self.on_tree_yview,
            xscrollcommand=self.on_tree_xview,
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self.on_tree_double_click)
        self.tree.bind("<ButtonPress-1>", self.on_tree_drag_start)
        self.tree.bind("<B1-Motion>", self.on_tree_drag_motion)
        self.tree.bind("<ButtonRelease-1>", self.on_tree_drag_release)
        self.tree.bind("<Configure>", self.on_tree_configure)
        self.tree.bind("<MouseWheel>", lambda _: self.root.after_idle(self.position_action_buttons))
        self.tree.tag_configure("running", foreground=GREEN)
        self.tree.tag_configure("occupied", foreground=RED)
        self.action_buttons = {}
        self.drag_indicator = tk.Frame(table, bg=BLUE, height=3)
        self._drag_browser_key = ""
        self._drag_start_y = 0
        self._drag_active = False
        self._drag_target_key = ""
        self._drag_after = False

    def can_drag_browser_rows(self):
        return (
            not self.search_var.get().strip()
            and self.group_filter_var.get() == "全部分组"
            and self.status_filter_var.get() == "全部状态"
        )

    def on_tree_drag_start(self, event):
        self._drag_browser_key = ""
        self._drag_active = False
        self._drag_target_key = ""
        self.hide_drag_indicator()
        if event.state & 0x0005 or not self.can_drag_browser_rows():
            return
        if (
            self.tree.identify_region(event.x, event.y) != "cell"
            or self.tree.identify_column(event.x) != "#1"
        ):
            return
        row = self.tree.identify_row(event.y)
        if row in self.map:
            self._drag_browser_key = row
            self._drag_start_y = event.y

    def on_tree_drag_motion(self, event):
        if not self._drag_browser_key:
            return
        if abs(event.y - self._drag_start_y) >= 6:
            self._drag_active = True
            self.tree.configure(cursor="fleur")
        if not self._drag_active:
            return
        target_key = self.tree.identify_row(event.y)
        if target_key not in self.map or target_key == self._drag_browser_key:
            self._drag_target_key = ""
            self.hide_drag_indicator()
            return
        box = self.tree.bbox(target_key)
        if not box:
            self._drag_target_key = ""
            self.hide_drag_indicator()
            return
        self._drag_target_key = target_key
        self._drag_after = event.y > box[1] + box[3] // 2
        line_y = box[1] + (box[3] if self._drag_after else 0) - 1
        self.drag_indicator.place(
            x=self.tree.winfo_x() + 4,
            y=self.tree.winfo_y() + line_y,
            width=max(1, self.tree.winfo_width() - 8),
            height=3,
        )

    def hide_drag_indicator(self):
        if hasattr(self, "drag_indicator"):
            self.drag_indicator.place_forget()

    def on_tree_drag_release(self, event):
        source_key = self._drag_browser_key
        was_dragging = self._drag_active
        target_key = self._drag_target_key
        after = self._drag_after
        self._drag_browser_key = ""
        self._drag_active = False
        self._drag_target_key = ""
        self._drag_after = False
        self.tree.configure(cursor="")
        self.hide_drag_indicator()
        if not source_key or not was_dragging or not self.can_drag_browser_rows():
            return
        if target_key not in self.map or target_key == source_key:
            return
        if reorder_mapping_key(self.map, source_key, target_key, after):
            self.save_map()
            self.refresh()
            self.tree.selection_set(source_key)

    def on_tree_configure(self, _event=None):
        pending = getattr(self, "tree_resize_job", None)
        if pending:
            self.root.after_cancel(pending)
        self.tree_resize_job = self.root.after(60, self.resize_tree_columns)

    def resize_tree_columns(self):
        self.tree_resize_job = None
        if not hasattr(self, "tree") or not self.tree.winfo_exists():
            return
        available = max(0, self.tree.winfo_width() - 4)
        minimum_total = sum(self.tree_min_widths.values())
        extra = max(0, available - minimum_total)
        weight_total = sum(self.tree_width_weights.values())
        for column in self.tree["columns"]:
            width = self.tree_min_widths[column]
            if extra:
                width += round(
                    extra * self.tree_width_weights[column] / weight_total
                )
            self.tree.column(column, width=width)
        self.position_action_buttons()

    def on_tree_yview(self, first, last):
        self.tree_scrollbar.set(first, last)
        self.root.after_idle(self.position_action_buttons)

    def on_tree_scroll(self, *args):
        self.tree.yview(*args)
        self.root.after_idle(self.position_action_buttons)

    def on_tree_xview(self, first, last):
        self.tree_xscrollbar.set(first, last)
        if float(first) <= 0 and float(last) >= 0.999:
            self.tree_xscrollbar.grid_remove()
        else:
            self.tree_xscrollbar.grid()
        self.root.after_idle(self.position_action_buttons)

    def on_tree_xscroll(self, *args):
        self.tree.xview(*args)
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
                x=x + 4, y=y + 5, width=max(204, width - 8), height=max(30, height - 10)
            )

    def start_row(self, key):
        record = self.map.get(key)
        if not record or port_open(record["port"]):
            return
        self.tree.selection_set(key)
        self.start_browser(record)
        self.root.after(1200, self.refresh)

    def stop_row(self, key):
        record = self.map.get(key)
        if not record or not cdp_alive(record["port"]):
            return
        self.tree.selection_set(key)
        self.stop_selected()

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
        actions = ttk.Frame(self.password_tab)
        actions.pack(fill="x", pady=(0, 10))
        self.vault_write_buttons = []
        write_actions = {
            "新增账号", "导入 Chrome CSV", "导出完整 CSV",
            "导出 Google CSV", "打开 Google 导入页",
        }
        for text, command in (
            ("新增账号", self.add_vault_entry),
            ("导入 Chrome CSV", self.import_chrome_csv),
            ("导出完整 CSV", self.export_vault_csv),
            ("导出 Google CSV", self.export_google_csv),
            ("打开 Google 导入页", self.open_chrome_passwords),
        ):
            button = ttk.Button(
                actions, text=text, command=command, style="Primary.TButton"
            )
            button.pack(side="left", padx=(0, 7))
            if text in write_actions:
                self.vault_write_buttons.append(button)

        filters = ttk.Frame(self.password_tab)
        filters.pack(fill="x", pady=(0, 10))
        self.vault_search_var = tk.StringVar()
        self.vault_search_var.trace_add("write", lambda *_: self.refresh_vault())
        self.vault_browser_filter_var = tk.StringVar(value="全部浏览器")
        self.vault_count_var = tk.StringVar(value="")

        ttk.Label(filters, text="搜索", style="Muted.TLabel").pack(
            side="left", padx=(0, 6)
        )
        ttk.Entry(filters, textvariable=self.vault_search_var, width=28).pack(
            side="left", padx=(0, 10), ipady=1
        )
        ttk.Label(filters, text="浏览器", style="Muted.TLabel").pack(
            side="left", padx=(0, 6)
        )
        self.vault_browser_filter = ttk.Combobox(
            filters,
            textvariable=self.vault_browser_filter_var,
            style="Filter.TCombobox",
            state="readonly",
            width=22,
        )
        self.vault_browser_filter.pack(side="left", padx=(0, 10), ipady=1)
        self.vault_browser_filter.bind(
            "<<ComboboxSelected>>", lambda _: self.refresh_vault()
        )
        ttk.Button(
            filters,
            text="清空筛选",
            command=self.reset_vault_filters,
            style="Primary.TButton",
        ).pack(side="left", padx=(0, 10))
        ttk.Label(filters, textvariable=self.vault_count_var, style="Muted.TLabel").pack(
            side="right"
        )

        columns = ("site", "url", "username", "password", "browser", "note", "action")
        vault_table = ttk.Frame(self.password_tab, style="Panel.TFrame")
        vault_table.pack(fill="both", expand=True)
        self.vault_tree = ttk.Treeview(
            vault_table, columns=columns, show="headings", selectmode="browse"
        )
        headers = {
            "site": "网站", "url": "网址", "username": "账号", "password": "密码",
            "browser": "所属浏览器", "note": "备注", "action": "操作",
        }
        widths = {
            "site": 110, "url": 210, "username": 130, "password": 110,
            "browser": 120, "note": 130, "action": 360,
        }
        self.vault_tree_min_widths = widths
        self.vault_tree_width_weights = {
            "site": 0.85, "url": 1.6, "username": 1.0, "password": 0.65,
            "browser": 0.9, "note": 1.1, "action": 1.75,
        }
        for column in columns:
            self.vault_tree.heading(column, text=headers[column], anchor="center")
            self.vault_tree.column(
                column, width=widths[column], anchor="center",
                stretch=False, minwidth=widths[column],
            )
        vault_vertical = ttk.Scrollbar(
            vault_table, orient="vertical", command=self.on_vault_tree_scroll
        )
        vault_horizontal = ttk.Scrollbar(
            vault_table, orient="horizontal", command=self.on_vault_tree_xscroll
        )
        self.vault_tree_scrollbar = vault_vertical
        self.vault_tree_xscrollbar = vault_horizontal
        self.vault_tree.configure(
            yscrollcommand=self.on_vault_tree_yview,
            xscrollcommand=self.on_vault_tree_xview,
        )
        self.vault_tree.grid(row=0, column=0, sticky="nsew")
        vault_vertical.grid(row=0, column=1, sticky="ns")
        vault_horizontal.grid(row=1, column=0, sticky="ew")
        vault_table.rowconfigure(0, weight=1)
        vault_table.columnconfigure(0, weight=1)
        self.vault_tree.bind("<Double-1>", lambda _: self.edit_vault_entry())
        self.vault_tree.bind("<Configure>", self.on_vault_tree_configure)
        self.vault_tree.bind(
            "<MouseWheel>", lambda _: self.root.after_idle(self.position_vault_buttons)
        )
        self.vault_action_buttons = {}
        self.refresh_vault()

    def on_vault_tree_configure(self, _event=None):
        pending = getattr(self, "vault_tree_resize_job", None)
        if pending:
            self.root.after_cancel(pending)
        self.vault_tree_resize_job = self.root.after(60, self.resize_vault_tree_columns)

    def resize_vault_tree_columns(self):
        self.vault_tree_resize_job = None
        if not hasattr(self, "vault_tree") or not self.vault_tree.winfo_exists():
            return
        available = max(0, self.vault_tree.winfo_width() - 4)
        minimum_total = sum(self.vault_tree_min_widths.values())
        extra = max(0, available - minimum_total)
        weight_total = sum(self.vault_tree_width_weights.values())
        for column in self.vault_tree["columns"]:
            width = self.vault_tree_min_widths[column]
            if extra:
                width += round(
                    extra * self.vault_tree_width_weights[column] / weight_total
                )
            self.vault_tree.column(column, width=width)
        self.position_vault_buttons()

    def on_vault_tree_yview(self, first, last):
        self.vault_tree_scrollbar.set(first, last)
        self.root.after_idle(self.position_vault_buttons)

    def on_vault_tree_scroll(self, *args):
        self.vault_tree.yview(*args)
        self.root.after_idle(self.position_vault_buttons)

    def on_vault_tree_xview(self, first, last):
        self.vault_tree_xscrollbar.set(first, last)
        if float(first) <= 0 and float(last) >= 0.999:
            self.vault_tree_xscrollbar.grid_remove()
        else:
            self.vault_tree_xscrollbar.grid()
        self.root.after_idle(self.position_vault_buttons)

    def on_vault_tree_xscroll(self, *args):
        self.vault_tree.xview(*args)
        self.root.after_idle(self.position_vault_buttons)

    def reset_vault_filters(self):
        self.vault_search_var.set("")
        self.vault_browser_filter_var.set("全部浏览器")
        self.refresh_vault()

    def refresh_vault_browser_filter_values(self):
        if not hasattr(self, "vault_browser_filter"):
            return
        current = self.vault_browser_filter_var.get() or "全部浏览器"
        values = ["全部浏览器", "未指定"] + [
            f"{key} · {record.get('name', key)}"
            for key, record in self.map.items()
        ]
        self.vault_browser_filter.configure(values=values)
        if current not in values:
            self.vault_browser_filter_var.set("全部浏览器")

    def vault_record_matches_filters(self, record, browser_name):
        browser_filter = self.vault_browser_filter_var.get()
        if browser_filter == "未指定" and record.get("browser_key"):
            return False
        if (
            browser_filter
            and browser_filter not in ("全部浏览器", "未指定")
            and record.get("browser_key") != browser_filter.split(" · ", 1)[0]
        ):
            return False
        query = self.vault_search_var.get().strip().lower()
        if not query:
            return True
        haystack = " ".join(
            str(value).lower()
            for value in (
                record.get("site", ""),
                record.get("url", ""),
                record.get("username", ""),
                browser_name,
                record.get("note", ""),
            )
        )
        return query in haystack

    def selected_vault_index(self):
        selected = self.vault_tree.selection()
        if not selected:
            messagebox.showinfo("请选择账号", "请先选择一条账号记录。")
            return None
        return int(selected[0])

    def refresh_vault(self):
        if not hasattr(self, "vault_tree"):
            return
        self.refresh_vault_browser_filter_values()
        for button_group in getattr(self, "vault_action_buttons", {}).values():
            button_group.destroy()
        self.vault_action_buttons = {}
        self.vault_tree.delete(*self.vault_tree.get_children())
        visible_count = 0
        for index, record in enumerate(self.vault):
            browser = self.map.get(record.get("browser_key", ""), {}).get("name", "未指定")
            if not self.vault_record_matches_filters(record, browser):
                continue
            visible_count += 1
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
            button_specs = [
                ("复制账号", lambda row=key: self.copy_vault_row_value(row, "username", "账号")),
                ("复制密码", lambda row=key: self.copy_vault_row_value(row, "password", "密码")),
                ("打开并填充", lambda row=key: self.open_vault_row(row)),
            ]
            if not self.is_cloud_read_only():
                button_specs.extend(
                    [
                        ("编辑", lambda row=key: self.edit_vault_row(row)),
                        ("删除", lambda row=key: self.delete_vault_row(row)),
                    ]
                )
            for text, command in button_specs:
                tk.Button(
                    button_group, text=text, command=command, relief="flat",
                    borderwidth=0, bg=BLUE, fg="white",
                    activebackground="#b9684f", activeforeground="white",
                    font=("Microsoft YaHei UI", 9, "bold"), cursor="hand2",
                ).pack(side="left", fill="both", expand=True, padx=2)
            self.vault_action_buttons[key] = button_group
        if hasattr(self, "vault_count_var"):
            self.vault_count_var.set(f"显示 {visible_count} / {len(self.vault)} 条")
        self.resize_vault_tree_columns()
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
                x=x + 4, y=y + 5, width=max(340, width - 8), height=max(30, height - 10)
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

    def copy_vault_row_value(self, key, field, label):
        if not self.vault_tree.exists(key):
            return
        self.vault_tree.selection_set(key)
        index = int(key)
        value = self.vault[index].get(field, "")
        if not value:
            messagebox.showinfo("没有内容", f"这条记录没有保存{label}。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        messagebox.showinfo("已复制", f"{label}已复制到剪贴板。")

    def add_vault_entry(self):
        if not self.require_cloud_write():
            return
        dialog = VaultDialog(self.root, self.map)
        self.root.wait_window(dialog)
        if dialog.result:
            self.vault.append(dialog.result)
            save_vault(self.vault)
            self.refresh_vault()

    def edit_vault_entry(self):
        if not self.require_cloud_write():
            return
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
        if not self.require_cloud_write():
            return
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
        try:
            url = normalize_web_url(url)
        except ValueError as error:
            messagebox.showerror("网址格式错误", str(error))
            return
        browser = self.map.get(record.get("browser_key", ""))
        username = record.get("username", "")
        password = record.get("password", "")
        if browser:
            was_running = cdp_alive(browser["port"])
            if was_running:
                try:
                    cdp_open_tab(browser["port"], url)
                except Exception:
                    self.start_browser(browser, url)
            else:
                self.start_browser(browser, url)
            if username or password:
                def fill_later():
                    try:
                        apply_vault_autofill(
                            browser["port"], url, username, password
                        )
                    except Exception as error:
                        log(f"账号自动填充失败：{record.get('site', '')}，{error}")
                threading.Thread(target=fill_later, daemon=True).start()
        else:
            os.startfile(url)

    def export_vault_csv(self):
        if not self.require_cloud_write():
            return
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
        if not self.require_cloud_write():
            return
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
        if not self.require_cloud_write():
            return
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

    def open_cloud_account(self):
        if self.cloud_dialog and self.cloud_dialog.winfo_exists():
            self.cloud_dialog.lift()
            self.cloud_dialog.focus_force()
            return
        dialog = tk.Toplevel(self.root)
        self.cloud_dialog = dialog
        dialog.title("云端账号")
        dialog.geometry("820x520")
        dialog.minsize(740, 460)
        dialog.configure(bg=BG)
        dialog.transient(self.root)
        dialog.grab_set()
        if APP_ICON_FILE.exists():
            try:
                dialog.iconbitmap(default=str(APP_ICON_FILE))
            except tk.TclError:
                pass
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self.cloud_dialog_box = ttk.Frame(dialog, style="Panel.TFrame", padding=24)
        self.cloud_dialog_box.pack(fill="both", expand=True, padx=18, pady=18)
        self.render_cloud_account()
        dialog.wait_visibility()
        center_window(dialog, self.root)
        dialog.focus_force()

    def render_cloud_account(self):
        if not self.cloud_dialog or not self.cloud_dialog.winfo_exists():
            return
        box = self.cloud_dialog_box
        for child in box.winfo_children():
            child.destroy()
        box.pack(fill="both", expand=True, padx=18, pady=18)
        ttk.Label(
            box,
            text="云端账号与同步",
            style="Section.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            box,
            text="同步浏览器配置、程序设置和网站账号。Chrome Profile、Cookie 与扩展不会自动上传。",
            style="PanelMuted.TLabel",
        ).pack(anchor="w", pady=(4, 18))

        logged_in = bool(
            self.settings.get("cloud_token")
            and self.settings.get("cloud_username")
        )
        server_row = ttk.Frame(box, style="Panel.TFrame")
        server_row.pack(fill="x", pady=(0, 10))
        ttk.Label(
            server_row, text="服务器地址", width=12, background=PANEL
        ).pack(side="left")
        ttk.Entry(
            server_row,
            textvariable=self.cloud_server_var,
            state="readonly" if logged_in else "normal",
            width=58,
        ).pack(side="left", fill="x", expand=True)
        if logged_in:
            ttk.Button(
                server_row,
                text="更换服务器",
                command=self.cloud_switch_server,
            ).pack(side="left", padx=(8, 0))
        else:
            ttk.Label(
                box,
                text="本机可填写 http://127.0.0.1:8787；远程服务器请使用 HTTPS 地址。",
                style="PanelMuted.TLabel",
            ).pack(anchor="w", pady=(0, 8))
        if logged_in:
            role = self.settings.get("cloud_role", "owner")
            role_text = "只读子账号" if role == "member" else "主账号"
            owner = self.settings.get("cloud_owner_username", "")
            browser_count = self.settings.get("cloud_browser_count", "")
            vault_count = self.settings.get("cloud_vault_count", "")
            updated_at = self.settings.get("cloud_updated_at", "")
            account = ttk.Frame(box, style="Panel.TFrame")
            account.pack(fill="x", pady=(4, 16))
            ttk.Label(
                account,
                text="当前账号",
                width=12,
                background=PANEL,
            ).pack(side="left")
            ttk.Label(
                account,
                text=f"{self.settings['cloud_username']}  ·  {role_text}",
                background=PANEL,
                font=("Microsoft YaHei UI", 11, "bold"),
            ).pack(side="left")
            detail_parts = []
            if owner and owner != self.settings["cloud_username"]:
                detail_parts.append(f"主账号：{owner}")
            if browser_count != "":
                detail_parts.append(f"浏览器：{browser_count}")
            if vault_count != "":
                detail_parts.append(f"网站账号：{vault_count}")
            if updated_at:
                detail_parts.append(f"最近同步：{updated_at}")
            if detail_parts:
                ttk.Label(
                    box,
                    text=" · ".join(str(part) for part in detail_parts),
                    style="PanelMuted.TLabel",
                ).pack(anchor="w", pady=(0, 8))
            actions = ttk.Frame(box, style="Panel.TFrame")
            actions.pack(fill="x", pady=(10, 8))
            if role != "member":
                ttk.Button(
                    actions, text="上传同步", command=self.cloud_upload
                ).pack(side="left")
            ttk.Button(actions, text="拉取同步", command=self.cloud_download).pack(
                side="left", padx=7
            )
            ttk.Button(
                actions, text="刷新状态", command=self.cloud_refresh_status
            ).pack(side="left", padx=7)
            ttk.Button(
                actions, text="打开网页控制台", command=self.open_cloud_console
            ).pack(side="left", padx=7)
            ttk.Button(actions, text="退出登录", command=self.cloud_logout).pack(
                side="left", padx=7
            )
        else:
            fields = (
                ("用户名", self.cloud_username_var, False),
                ("密码", self.cloud_password_var, True),
            )
            for label, variable, secret in fields:
                row = ttk.Frame(box, style="Panel.TFrame")
                row.pack(fill="x", pady=6)
                ttk.Label(
                    row, text=label, width=12, background=PANEL
                ).pack(side="left")
                ttk.Entry(
                    row,
                    textvariable=variable,
                    show="*" if secret else "",
                    width=62,
                ).pack(side="left", fill="x", expand=True)
            actions = ttk.Frame(box, style="Panel.TFrame")
            actions.pack(fill="x", pady=(16, 8))
            ttk.Button(actions, text="注册账号", command=self.cloud_register).pack(
                side="left"
            )
            ttk.Button(actions, text="登录", command=self.cloud_login).pack(
                side="left", padx=7
            )
            ttk.Button(
                actions, text="打开网页控制台", command=self.open_cloud_console
            ).pack(side="left", padx=7)
        ttk.Label(
            box,
            textvariable=self.cloud_status_var,
            style="PanelMuted.TLabel",
        ).pack(anchor="w", pady=(10, 0))
        self.update_cloud_status()

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
        self.auto_update_var = tk.BooleanVar(
            value=self.settings.get("auto_check_updates", True)
        )
        ttk.Checkbutton(
            box,
            text="启动后自动检查软件新版本",
            variable=self.auto_update_var,
            command=self.save_settings,
        ).pack(anchor="w", pady=5)
        row = ttk.Frame(box, style="Panel.TFrame")
        row.pack(fill="x", pady=12)
        ttk.Button(row, text="设置/修改密码", command=self.set_password).pack(side="left")
        ttk.Button(row, text="清除密码", command=self.clear_password).pack(side="left", padx=6)
        ttk.Button(row, text="导出全部配置", command=self.export_config).pack(side="left", padx=6)
        ttk.Button(row, text="导入配置", command=self.import_config).pack(side="left", padx=6)
        ttk.Button(row, text="检查 Chrome 版本", command=self.chrome_version).pack(side="left", padx=6)
        ttk.Button(
            row, text="检查软件更新", command=self.check_app_update
        ).pack(side="left", padx=6)
        self.update_status_var = tk.StringVar(value="")
        ttk.Label(
            box,
            textvariable=self.update_status_var,
            style="PanelMuted.TLabel",
        ).pack(anchor="w", pady=(0, 6))
        ttk.Label(box, text=f"版本：{APP_VERSION}\n数据目录：{ROOT}",
                  style="PanelMuted.TLabel").pack(anchor="w", pady=12)

    def update_cloud_status(self, message=""):
        username = self.settings.get("cloud_username", "")
        token = self.settings.get("cloud_token", "")
        if message:
            text = message
        elif token and username:
            text = f"已登录：{username}"
        else:
            text = "尚未登录云端账号。"
        if hasattr(self, "cloud_status_var"):
            self.cloud_status_var.set(text)
        if hasattr(self, "cloud_account_var"):
            self.cloud_account_var.set(username if token and username else "云端账号")

    def open_cloud_console(self):
        try:
            server = normalize_server_url(
                self.cloud_server_var.get()
                or self.settings.get("cloud_server", "")
            )
        except CloudError as error:
            messagebox.showerror("无法打开网页控制台", str(error))
            return
        webbrowser.open(f"{server}/dashboard")

    def cloud_refresh_status(self):
        try:
            server, token = self.cloud_connection()
        except CloudError as error:
            messagebox.showerror("无法刷新", str(error))
            return

        def finished(result):
            self.settings["cloud_role"] = result.get(
                "role", self.settings.get("cloud_role", "owner")
            )
            self.settings["cloud_owner_username"] = result.get(
                "owner_username", self.settings.get("cloud_owner_username", "")
            )
            self.settings["cloud_browser_count"] = result.get("browser_count", "")
            self.settings["cloud_vault_count"] = result.get("vault_count", "")
            self.settings["cloud_updated_at"] = result.get("updated_at", "")
            self.save_settings()
            self.update_access_controls()
            self.render_cloud_account()
            self.update_cloud_status(
                f"状态已刷新：{result.get('browser_count', 0)} 个浏览器，"
                f"{result.get('vault_count', 0)} 条网站账号"
            )

        self.run_cloud_task(
            "正在刷新云端账号状态...",
            lambda: cloud_account_status(server, token),
            finished,
        )

    def is_cloud_read_only(self):
        return bool(
            self.settings.get("cloud_token")
            and self.settings.get("cloud_role") == "member"
        )

    def require_cloud_write(self):
        if not self.is_cloud_read_only():
            return True
        messagebox.showwarning(
            "只读子账号",
            "该账号只能查看、启动和关闭已分配的浏览器，不能修改浏览器或网站账号数据。",
        )
        return False

    def update_access_controls(self):
        state = "disabled" if self.is_cloud_read_only() else "normal"
        if hasattr(self, "create_browser_button"):
            self.create_browser_button.configure(state=state)
        for button in getattr(self, "vault_write_buttons", []):
            button.configure(state=state)

    def cloud_auth_values(self):
        server = normalize_server_url(self.cloud_server_var.get())
        username = self.cloud_username_var.get().strip()
        password = self.cloud_password_var.get()
        if len(username) < 3:
            raise CloudError("用户名至少需要 3 个字符。")
        return server, username, password

    def run_cloud_task(self, status, worker, success):
        self.update_cloud_status(status)

        def run():
            try:
                result = worker()
            except Exception as error:
                error_message = str(error)
                self.root.after(
                    0,
                    lambda message=error_message: (
                        self.update_cloud_status(f"失败：{message}"),
                        messagebox.showerror("云端操作失败", message),
                    ),
                )
                return
            self.root.after(0, lambda: success(result))

        threading.Thread(target=run, daemon=True).start()

    def finish_cloud_auth(self, result, server):
        self.settings["cloud_server"] = server
        self.settings["cloud_username"] = result["username"]
        self.settings["cloud_token"] = result["token"]
        self.settings["cloud_role"] = result.get("role", "owner")
        self.settings["cloud_owner_username"] = result.get(
            "owner_username", result["username"]
        )
        self.cloud_username_var.set(result["username"])
        self.cloud_password_var.set("")
        self.save_settings()
        self.update_cloud_status(f"已登录：{result['username']}")
        self.render_cloud_account()
        self.update_access_controls()
        self.refresh()
        self.refresh_vault()
        messagebox.showinfo("登录成功", "云端账号已连接。")

    def cloud_register(self):
        try:
            server, username, password = self.cloud_auth_values()
        except CloudError as error:
            messagebox.showerror("注册信息错误", str(error))
            return
        self.run_cloud_task(
            "正在注册...",
            lambda: cloud_register_request(server, username, password),
            lambda result: self.finish_cloud_auth(result, server),
        )

    def cloud_login(self):
        try:
            server, username, password = self.cloud_auth_values()
        except CloudError as error:
            messagebox.showerror("登录信息错误", str(error))
            return
        self.run_cloud_task(
            "正在登录...",
            lambda: cloud_login_request(server, username, password),
            lambda result: self.finish_cloud_auth(result, server),
        )

    def cloud_connection(self):
        server = normalize_server_url(
            self.cloud_server_var.get()
            or self.settings.get("cloud_server", "")
        )
        token = self.settings.get("cloud_token", "")
        if not token:
            raise CloudError("请先注册或登录云端账号。")
        return server, token

    def syncable_settings(self):
        excluded = {
            "password_hash", "cloud_server", "cloud_username", "cloud_token",
            "cloud_role", "cloud_owner_username", "cloud_browser_count",
            "cloud_vault_count", "cloud_updated_at",
        }
        return {
            key: value for key, value in self.settings.items() if key not in excluded
        }

    def cloud_upload(self):
        if not self.require_cloud_write():
            return
        try:
            server, token = self.cloud_connection()
        except CloudError as error:
            messagebox.showerror("无法同步", str(error))
            return
        browsers = json.loads(json.dumps(self.map, ensure_ascii=False))
        settings = json.loads(json.dumps(self.syncable_settings(), ensure_ascii=False))
        vault = json.loads(json.dumps(self.vault, ensure_ascii=False))

        def finished(result):
            self.settings["cloud_server"] = server
            self.settings["cloud_browser_count"] = result.get("browser_count", "")
            self.settings["cloud_vault_count"] = result.get("vault_count", "")
            self.settings["cloud_updated_at"] = result.get("updated_at", "")
            self.save_settings()
            self.update_cloud_status(
                f"上传完成：{result['browser_count']} 个浏览器，"
                f"{result['vault_count']} 条网站账号；{result['updated_at']}"
            )
            self.render_cloud_account()
            messagebox.showinfo("同步完成", "本地数据已加密上传到服务器。")

        self.run_cloud_task(
            "正在上传同步数据...",
            lambda: upload_snapshot(server, token, browsers, settings, vault),
            finished,
        )

    def validate_cloud_snapshot(self, result):
        browsers = result.get("browsers", {})
        settings = result.get("settings", {})
        vault = result.get("vault", [])
        if not isinstance(browsers, dict) or not isinstance(settings, dict) or not isinstance(vault, list):
            raise CloudError("云端数据格式无效。")
        used_ports = set()
        for record in browsers.values():
            if not isinstance(record, dict):
                raise CloudError("云端浏览器记录格式无效。")
            try:
                port = int(record["port"])
            except (KeyError, TypeError, ValueError) as error:
                raise CloudError("云端浏览器包含无效端口。") from error
            if not 1024 <= port <= MAX_CDP_PORT or port in used_ports:
                raise CloudError(f"云端浏览器端口无效或重复：{port}")
            used_ports.add(port)
            record["port"] = port
            record["home"] = normalize_home_value(record.get("home", ""))
            record["environment"] = normalize_environment(record.get("environment"))
            safe_profile_path(record)
        return browsers, settings, vault

    def cloud_download(self):
        if any(port_open(record["port"]) for record in self.map.values()):
            messagebox.showwarning("请先关闭", "拉取云端数据前，请关闭所有独立浏览器。")
            return
        if not messagebox.askyesno(
            "确认拉取",
            "拉取会用云端浏览器列表、设置和账号库覆盖本地对应数据。\n"
            "Chrome Profile 不会被删除或下载，是否继续？",
        ):
            return
        try:
            server, token = self.cloud_connection()
        except CloudError as error:
            messagebox.showerror("无法同步", str(error))
            return

        def finished(result):
            try:
                browsers, settings, vault = self.validate_cloud_snapshot(result)
            except CloudError as error:
                self.update_cloud_status(f"失败：{error}")
                messagebox.showerror("云端数据错误", str(error))
                return
            preserved = {
                key: self.settings.get(key, "")
                for key in (
                    "password_hash", "cloud_server", "cloud_username",
                    "cloud_token", "cloud_role", "cloud_owner_username",
                )
            }
            self.map = browsers
            self.settings.update(settings)
            self.settings.update(preserved)
            access = result.get("access", {})
            if access:
                self.settings["cloud_role"] = access.get(
                    "role", self.settings.get("cloud_role", "owner")
                )
                self.settings["cloud_owner_username"] = access.get(
                    "owner_username", ""
                )
            self.vault = vault
            self.settings["cloud_browser_count"] = len(self.map)
            self.settings["cloud_vault_count"] = len(self.vault)
            self.settings["cloud_updated_at"] = result.get("updated_at", "")
            for record in self.map.values():
                self.profile_path(record).mkdir(parents=True, exist_ok=True)
            self.save_map()
            self.save_settings()
            save_vault(self.vault)
            self.update_access_controls()
            self.refresh()
            self.refresh_vault()
            self.update_cloud_status(f"拉取完成：{result.get('updated_at') or '云端暂无时间'}")
            self.render_cloud_account()
            messagebox.showinfo("同步完成", "云端数据已保存到本机。")

        self.run_cloud_task(
            "正在拉取云端数据...",
            lambda: download_snapshot(server, token),
            finished,
        )

    def cloud_logout(self):
        server = self.settings.get("cloud_server", "")
        token = self.settings.get("cloud_token", "")
        self.settings["cloud_token"] = ""
        self.settings["cloud_role"] = ""
        self.settings["cloud_owner_username"] = ""
        self.settings["cloud_browser_count"] = ""
        self.settings["cloud_vault_count"] = ""
        self.settings["cloud_updated_at"] = ""
        self.cloud_password_var.set("")
        self.save_settings()
        self.update_cloud_status()
        self.render_cloud_account()
        self.update_access_controls()
        self.refresh()
        self.refresh_vault()
        if server and token:
            threading.Thread(
                target=lambda: self._revoke_cloud_session(server, token),
                daemon=True,
            ).start()

    def cloud_switch_server(self):
        if not messagebox.askyesno(
            "更换服务器",
            "更换服务器需要退出当前云端账号。是否继续？",
        ):
            return
        self.cloud_logout()
        self.cloud_server_var.set("")
        self.render_cloud_account()

    def _revoke_cloud_session(self, server, token):
        try:
            cloud_logout_request(server, token)
        except CloudError:
            pass

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
            if port > MAX_CDP_PORT:
                raise RuntimeError("没有可用的 CDP 端口。")
        return port

    def profile_path(self, record):
        return safe_profile_path(record)

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
        if not isinstance(incoming, dict) or not all(
            isinstance(record, dict) for record in incoming.values()
        ):
            log("检测到无效的 browser-map.json，已保留当前配置")
            self.map_mtime = current_mtime
            return
        changed = False
        for record in incoming.values():
            current = record.get("environment")
            normalized = normalize_environment(current)
            if current != normalized:
                record["environment"] = normalized
                changed = True
            home = normalize_home_value(record.get("home", ""))
            if record.get("home", "") != home:
                record["home"] = home
                changed = True
        self.map = incoming
        if changed:
            self.save_map()
        else:
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
        if FROZEN:
            cli = Path(sys.executable).parent / "cli" / "ChromeManagerCLI.exe"
            content = f'@echo off\r\n"{cli}" start {key}\r\n'
        else:
            content = (
                '@echo off\r\n'
                'cd /d "%~dp0.."\r\n'
                f'python browser_cli.py start {key}\r\n'
            )
        launcher.write_text(content, encoding="utf-8-sig")

    def replace_browser_vault(self, browser_key, accounts):
        if not browser_key:
            return
        kept = [
            record
            for record in self.vault
            if record.get("browser_key", "") != browser_key
        ]
        linked = []
        for account in accounts or []:
            item = account.copy()
            item["browser_key"] = browser_key
            linked.append(item)
        self.vault = kept + linked
        save_vault(self.vault)
        if hasattr(self, "vault_tree"):
            self.refresh_vault()

    def create(self):
        if not self.require_cloud_write():
            return
        index = self.next_index()
        key = f"browser{index}"
        dialog = BrowserDialog(
            self.root,
            "新建独立浏览器",
            default_port=self.next_port(),
            browser_key=key,
            vault=self.vault,
        )
        self.root.wait_window(dialog)
        if not dialog.result or self.port_conflict(dialog.result["port"]):
            return
        record = dialog.result
        record["profile"] = f"profiles/browser-{index}"
        record["created_at"] = datetime.now().isoformat(timespec="seconds")
        self.profile_path(record).mkdir(parents=True, exist_ok=True)
        self.map[key] = record
        self.replace_browser_vault(key, dialog.vault_result or [])
        self.save_map()
        self.write_launcher(key, record)
        log(f"创建浏览器：{record['name']}，端口 {record['port']}")
        self.refresh()

    def edit(self):
        if not self.require_cloud_write():
            return
        keys = self.selected(single=True)
        if not keys:
            return
        key = keys[0]
        old = self.map[key].copy()
        dialog = BrowserDialog(
            self.root,
            "编辑浏览器",
            old,
            old["port"],
            browser_key=key,
            vault=self.vault,
        )
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        if dialog.result["port"] != old["port"] and self.port_conflict(dialog.result["port"], key):
            return
        dialog.result["profile"] = old["profile"]
        dialog.result["created_at"] = old.get("created_at", "")
        dialog.result["last_open_at"] = old.get("last_open_at", "")
        self.map[key] = dialog.result
        self.replace_browser_vault(key, dialog.vault_result or [])
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
        if not self.require_cloud_write():
            return
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
                      last_open_at="",
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
        if not self.require_cloud_write():
            return
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
        browser_key = next(
            (key for key, value in self.map.items() if value is record),
            "",
        )
        autofill = prepare_autofill_extension(
            profile, browser_key, self.vault
        )
        if autofill:
            extensions.append(autofill)
        if extensions:
            extension_value = ",".join(str(path) for path in extensions)
            arguments.append(f"--load-extension={extension_value}")
        if record.get("proxy"):
            try:
                proxy = parse_proxy(record["proxy"])
            except ValueError as error:
                messagebox.showerror("代理配置错误", str(error))
                return False
            shutil.rmtree(profile / "ChromeManagerProxyAuth", ignore_errors=True)
            if proxy["username"]:
                bridge = ensure_proxy_bridge(profile, record["port"], proxy)
                arguments.append(f"--proxy-server={bridge}")
            else:
                arguments.append(f"--proxy-server={proxy['server']}")
        arguments.append(resolve_start_url(record, url))
        subprocess.Popen(arguments, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        deadline = time.time() + 12
        while time.time() < deadline and not cdp_alive(record["port"]):
            self.root.update()
            time.sleep(0.08)
        if cdp_alive(record["port"]):
            ensure_environment_controller(
                ROOT, profile, record["port"], environment
            )
            record["last_open_at"] = datetime.now().isoformat(timespec="seconds")
            self.save_map()
        else:
            log(f"浏览器启动超时：{record['name']}，端口 {record['port']}")
            return False
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
            proxy_config_path = proxy_bridge_config_path(self.profile_path(record))
            stop_port_listener(
                proxy_bridge_port(record["port"], proxy_config_path),
                proxy_bridge_markers(proxy_config_path),
            )
            environment_config_path = (
                self.profile_path(record) / "environment-controller.json"
            )
            stop_port_listener(
                environment_controller_port(record["port"]),
                environment_controller_markers(environment_config_path),
            )
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
        if not self.require_cloud_write():
            return
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
        if not self.require_cloud_write():
            return
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
        if not self.require_cloud_write():
            return
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
                last_open_at="",
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
        if not self.require_cloud_write():
            return
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
        if not self.require_cloud_write():
            return
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
        if not self.is_cloud_read_only():
            menu.add_command(label="修改环境配置", command=self.edit_selected_environment)
        menu.add_command(label="查看当前环境信息", command=self.view_environment)
        menu.add_command(label="环境一致性检查", command=self.check_environment)
        if not self.is_cloud_read_only():
            menu.add_separator()
            menu.add_command(label="导出选中浏览器", command=self.backup)
            menu.add_command(label="导入为新浏览器", command=self.import_browser)
            menu.add_command(label="恢复到选中浏览器", command=self.restore)
            menu.add_command(label="清除缓存、Cookie 和历史记录", command=self.clear_browser_data)
        menu.add_separator()
        menu.add_command(
            label="复制操作指令",
            command=self.copy_operation_instructions,
        )
        if not self.is_cloud_read_only():
            menu.add_command(label="打开数据目录", command=self.open_profiles)
            menu.add_command(label="打开启动器目录", command=lambda: os.startfile(LAUNCHERS))
        menu.add_command(label="检查软件更新", command=self.check_app_update)
        menu.add_command(label="检查 Chrome 更新", command=self.open_chrome_update)
        menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def copy_operation_instructions(self):
        keys = self.selected()
        if not keys:
            return
        prompts = []
        for key in keys:
            record = self.map[key]
            port = record["port"]
            root_path = str(ROOT)
            if FROZEN:
                cli_path = Path(sys.executable).parent / "cli" / "ChromeManagerCLI.exe"
                start_command = f"& '{cli_path}' start {key}"
            else:
                start_command = (
                    f"Set-Location -LiteralPath '{root_path}'\n"
                    f"python .\\browser_cli.py start {key}"
                )
            prompts.append(
                f"请控制本地独立浏览器 [{record['name']}]。\n"
                f"浏览器配置键：{key}\n"
                f"CDP 调试端口：{port}\n"
                f"管理器目录：{root_path}\n\n"
                f"先检测 http://127.0.0.1:{port}/json/version 是否可访问。\n"
                "如果无法访问，说明浏览器尚未启动。请直接使用终端执行以下命令启动，"
                "不要要求我手动点击管理器：\n"
                f"{start_command}\n\n"
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
        messagebox.showinfo("已复制", "操作指令已复制到剪贴板。")

    def open_profiles(self):
        keys = self.selected(single=True)
        if keys:
            path = self.profile_path(self.map[keys[0]])
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)

    def open_chrome_update(self):
        keys = self.selected(single=True)
        if keys:
            self.start_browser(self.map[keys[0]], "chrome://settings/help")

    def auto_check_app_update(self):
        last_value = self.settings.get("last_update_check", "")
        if last_value:
            try:
                elapsed = datetime.now() - datetime.fromisoformat(last_value)
                if elapsed.total_seconds() < 12 * 60 * 60:
                    return
            except ValueError:
                pass
        self.check_app_update(manual=False)

    def check_app_update(self, manual=True):
        if getattr(self, "update_checking", False):
            if manual:
                messagebox.showinfo("正在检查", "软件正在检查新版本，请稍候。")
            return
        self.update_checking = True
        if hasattr(self, "update_status_var"):
            self.update_status_var.set("正在检查软件新版本...")

        def worker():
            try:
                release = latest_release_info()
            except Exception as error:
                message = str(error)
                self.root.after(
                    0,
                    lambda: self.finish_update_check(
                        manual=manual, error_message=message
                    ),
                )
                return
            self.root.after(
                0,
                lambda: self.finish_update_check(
                    manual=manual, release=release
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def finish_update_check(self, manual, release=None, error_message=""):
        self.update_checking = False
        if error_message:
            if hasattr(self, "update_status_var"):
                self.update_status_var.set(error_message)
            if manual:
                messagebox.showerror("检查更新失败", error_message)
            return
        self.settings["last_update_check"] = datetime.now().isoformat(
            timespec="seconds"
        )
        self.save_settings()
        if version_tuple(release["version"]) <= version_tuple(APP_VERSION):
            text = f"当前已是最新版本：{APP_VERSION}"
            if hasattr(self, "update_status_var"):
                self.update_status_var.set(text)
            if manual:
                messagebox.showinfo("没有新版本", text)
            return
        if hasattr(self, "update_status_var"):
            self.update_status_var.set(
                f"发现新版本：{release['version']}"
            )
        self.show_update_dialog(release)

    def show_update_dialog(self, release):
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("发现软件新版本")
        display = system_display_info(self.root)
        dialog_width = min(760, max(640, display["work_width"] - 80))
        dialog_height = min(580, max(500, display["work_height"] - 80))
        dialog.geometry(f"{dialog_width}x{dialog_height}")
        dialog.minsize(min(640, dialog_width), min(500, dialog_height))
        dialog.configure(bg=BG)
        dialog.transient(self.root)
        dialog.grab_set()
        if APP_ICON_FILE.exists():
            try:
                dialog.iconbitmap(default=str(APP_ICON_FILE))
            except tk.TclError:
                pass
        panel = ttk.Frame(dialog, style="Panel.TFrame", padding=24)
        panel.pack(fill="both", expand=True, padx=18, pady=18)
        ttk.Label(
            panel,
            text=f"发现新版本 {release['version']}",
            style="DialogTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            panel,
            text=f"当前版本：{APP_VERSION}    安装包：{release['asset_name']}",
            style="PanelMuted.TLabel",
        ).pack(anchor="w", pady=(5, 14))
        notes = tk.Text(
            panel,
            height=14,
            wrap="word",
            bg=CARD,
            fg=TEXT,
            relief="solid",
            borderwidth=1,
            font=("Microsoft YaHei UI", 10),
            padx=12,
            pady=10,
        )
        notes.pack(fill="both", expand=True)
        notes.insert("1.0", release["notes"] or "该版本没有提供更新说明。")
        notes.configure(state="disabled")
        status_var = tk.StringVar(value="点击下方按钮即可在软件内下载并安装。")
        ttk.Label(
            panel,
            textvariable=status_var,
            style="PanelMuted.TLabel",
        ).pack(anchor="w", pady=(12, 6))
        progress = ttk.Progressbar(panel, mode="determinate", maximum=100)
        progress.pack(fill="x", pady=(0, 12))
        actions = tk.Frame(panel, bg=PANEL)
        actions.pack(fill="x")
        button_options = {
            "bg": BLUE,
            "fg": "white",
            "activebackground": "#b9684f",
            "activeforeground": "white",
            "relief": "flat",
            "borderwidth": 0,
            "highlightthickness": 0,
            "font": ("Microsoft YaHei UI", 10, "bold"),
            "padx": 18,
            "pady": 9,
            "cursor": "hand2",
        }
        download_button = tk.Button(
            actions,
            text="下载并安装",
            command=lambda: self.download_app_update(
                dialog, release, status_var, progress, download_button
            ),
            **button_options,
        )
        download_button.pack(side="left")
        tk.Button(
            actions,
            text="稍后再说",
            command=dialog.destroy,
            **button_options,
        ).pack(side="left", padx=8)
        if release.get("page_url"):
            tk.Button(
                actions,
                text="查看发布说明",
                command=lambda: os.startfile(release["page_url"]),
                **button_options,
            ).pack(side="right")

        def show_dialog():
            center_window(dialog, self.root)
            dialog.deiconify()
            dialog.lift()
            dialog.focus_force()

        dialog.after_idle(show_dialog)

    def download_app_update(
        self, dialog, release, status_var, progress, download_button
    ):
        download_button.configure(state="disabled")
        status_var.set("正在下载安装包...")

        def update_progress(downloaded, total):
            percent = downloaded * 100 / total if total else 0
            def apply_progress():
                if not dialog.winfo_exists():
                    return
                progress.configure(value=percent)
                status_var.set(
                    f"正在下载：{downloaded / 1024 / 1024:.1f} MB"
                    + (
                        f" / {total / 1024 / 1024:.1f} MB"
                        if total else ""
                    )
                )

            self.root.after(0, apply_progress)

        def worker():
            try:
                installer, digest = download_release_installer(
                    release, update_progress
                )
            except Exception as error:
                message = str(error)
                self.root.after(
                    0,
                    lambda: self.finish_update_download(
                        dialog, status_var, progress, download_button,
                        error_message=message,
                    ),
                )
                return
            self.root.after(
                0,
                lambda: self.finish_update_download(
                    dialog, status_var, progress, download_button,
                    installer=installer, digest=digest,
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def finish_update_download(
        self, dialog, status_var, progress, download_button,
        installer=None, digest="", error_message="",
    ):
        if not dialog.winfo_exists():
            return
        if error_message:
            progress.configure(value=0)
            status_var.set(error_message)
            download_button.configure(state="normal")
            messagebox.showerror("更新下载失败", error_message, parent=dialog)
            return
        progress.configure(value=100)
        status_var.set(f"下载和 SHA256 校验完成：{digest[:16]}...")
        if not messagebox.askyesno(
            "开始安装更新",
            "安装包已验证完成。程序将退出并启动覆盖安装，是否现在更新？",
            parent=dialog,
        ):
            download_button.configure(state="normal", text="重新打开安装包")
            download_button.configure(
                command=lambda: os.startfile(installer)
            )
            return
        subprocess.Popen(
            [
                str(installer),
                "/SILENT",
                "/CLOSEAPPLICATIONS",
                "/RESTARTAPPLICATIONS",
            ]
        )
        self.root.after(300, self.quit_app)

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
        if not self.require_cloud_write():
            return
        target = filedialog.asksaveasfilename(
            defaultextension=".json",
            initialfile="Chrome多开配置.json",
            filetypes=[("JSON", "*.json")],
        )
        if target:
            save_json(Path(target), {"browsers": self.map, "settings": self.settings})
            log(f"导出配置：{target}")

    def import_config(self):
        if not self.require_cloud_write():
            return
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
            try:
                port = int(record.get("port", self.next_port(used_ports)))
            except (TypeError, ValueError):
                port = self.next_port(used_ports)
            if not 1024 <= port <= MAX_CDP_PORT or port in used_ports or port_open(port):
                port = self.next_port(used_ports)
            used_ports.add(port)
            profile = str(record.get("profile", f"profiles/browser-{index}")).replace("\\", "/")
            profile_parts = PurePosixPath(profile).parts
            if (
                Path(profile).is_absolute()
                or ".." in profile_parts
                or any(":" in part or "\x00" in part for part in profile_parts)
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
            record["home"] = normalize_home_value(record.get("home", ""))
            record.setdefault("proxy", "")
            record.setdefault("note", "")
            record.setdefault("schedule", "")
            record.setdefault("auto_start", False)
            record["environment"] = normalize_environment(record.get("environment"))
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
        if hasattr(self, "auto_update_var"):
            self.settings["auto_check_updates"] = self.auto_update_var.get()
        save_json(SETTINGS_FILE, self.settings)

    def startup_command(self):
        if FROZEN:
            return f'"{Path(sys.executable).resolve()}"'
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
        try:
            version = ".".join(str(part) for part in win_file_version(chrome))
        except OSError:
            version = "无法读取版本"
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
        self.hide_drag_indicator()
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
        listeners = listener_pid_map()
        for button_group in getattr(self, "action_buttons", {}).values():
            button_group.destroy()
        self.action_buttons = {}
        self.tree.delete(*self.tree.get_children())
        running = 0
        visible = 0
        for key, record in self.map.items():
            pid = listeners.get(int(record["port"]))
            is_open = bool(pid) or port_open(record["port"])
            is_cdp = cdp_alive(record["port"]) if is_open else False
            pid, tabs, memory = (
                browser_stats(record["port"], pid)
                if is_cdp
                else (pid, 0, 0)
            )
            status = "运行中" if is_cdp else ("端口占用" if is_open else "未启动")
            running += bool(is_cdp)
            searchable = " ".join((
                record.get("name", ""), record.get("group", ""), str(record.get("port", "")),
                display_home_value(record.get("home", "")), record.get("proxy", ""),
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
                    format_open_time(record.get("last_open_at")),
                    display_home_value(record.get("home", "")), record.get("proxy", ""), "",
                ),
                tags=("running" if is_cdp else "occupied" if is_open else "stopped",),
            )
            button_group = tk.Frame(self.tree, bg=CARD)
            if is_cdp:
                button_specs = [
                    (
                        "停止",
                        lambda browser_key=key: self.stop_row(browser_key),
                        "normal",
                    ),
                ]
            else:
                button_specs = [
                    (
                        "不可用" if is_open else "启动",
                        lambda browser_key=key: self.start_row(browser_key),
                        "disabled" if is_open else "normal",
                    ),
                ]
            if not self.is_cloud_read_only():
                button_specs.extend(
                    [
                        ("编辑", lambda browser_key=key: self.edit_row(browser_key), "normal"),
                        ("删除", lambda browser_key=key: self.delete_row(browser_key), "normal"),
                    ]
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
            pystray.MenuItem(
                "显示管理器",
                lambda: self.root.after(0, self.show_window),
                default=True,
            ),
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
    assert format_open_time("") == "从未打开"
    assert format_open_time("2026-06-12T09:08:07") == "2026-06-12 09:08"
    assert normalize_web_url("example.com") == "https://example.com"
    failed = False
    try:
        normalize_web_url("C:/Windows/notepad.exe")
    except ValueError:
        failed = True
    assert failed
    legacy_mobile = normalize_environment({
        "mobile_mode": True,
        "touch_mode": True,
        "user_agent": "Mozilla/5.0 Android Mobile",
        "window_width": 412,
        "window_height": 915,
        "device_scale_factor": 3,
    })
    assert not legacy_mobile["mobile_mode"]
    assert not legacy_mobile["touch_mode"]
    assert legacy_mobile["user_agent"] == ""
    assert legacy_mobile["window_width"] == 1280
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
    follow_system = normalize_environment({
        "device_scale_factor": 0.0,
    })
    follow_arguments = build_chrome_arguments(follow_system)
    assert not any(
        argument.startswith("--force-device-scale-factor=")
        for argument in follow_arguments
    )
    assert consistency_report(environment)["warnings"]
    order = {"browser1": {"name": "A"}, "browser2": {"name": "B"}, "browser3": {"name": "C"}}
    assert reorder_mapping_key(order, "browser1", "browser3", after=True)
    assert list(order) == ["browser2", "browser3", "browser1"]
    assert reorder_mapping_key(order, "browser1", "browser2")
    assert list(order) == ["browser1", "browser2", "browser3"]
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
    bad_windows_path = test_root / "bad-windows.zip"
    with zipfile.ZipFile(bad_windows_path, "w") as archive:
        archive.writestr("profile/C:/outside.txt", "bad")
    failed = False
    try:
        with zipfile.ZipFile(bad_windows_path) as archive:
            safe_extract_profile(archive, destination)
    except ValueError:
        failed = True
    assert failed
    shutil.rmtree(test_root)
    print("SELFTEST_OK")


if __name__ == "__main__":
    if "--environment-controller" in sys.argv:
        from environment_controller import main as environment_controller_main

        sys.argv.remove("--environment-controller")
        environment_controller_main()
        raise SystemExit(0)
    if "--proxy-forwarder" in sys.argv:
        from proxy_forwarder import main as proxy_forwarder_main

        sys.argv.remove("--proxy-forwarder")
        proxy_forwarder_main()
        raise SystemExit(0)
    if "--self-test" in sys.argv:
        self_test()
        raise SystemExit(0)
    instance = SingleInstance()
    if not instance.acquire():
        instance.notify_existing()
        raise SystemExit(0)
    set_windows_app_id()
    ensure_app_icon()
    enable_windows_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    configure_dialog_parent(root)
    scaling = system_tk_scaling()
    if scaling:
        try:
            root.tk.call("tk", "scaling", scaling)
        except Exception:
            pass
    app = App(root)
    if root.winfo_exists():
        root.update_idletasks()
        center_window(root)
        root.deiconify()
        root.lift()
        instance.start_listener(lambda: root.after(0, app.show_window))
        root.mainloop()
