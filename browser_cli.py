import argparse
import json
import os
import socket
import subprocess
import sys
import shutil
import urllib.parse
import time
from datetime import datetime
from pathlib import Path, PurePosixPath

import psutil
from environment_config import (
    apply_profile_preferences,
    build_chrome_arguments,
    ensure_environment_controller,
    environment_controller_port,
    extension_paths,
    normalize_environment,
    prepare_autofill_extension,
)


FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ROOT = (
    Path(os.environ.get("LOCALAPPDATA", Path.home()))
    / "ChromeMultiManager"
    if FROZEN
    else Path(__file__).resolve().parent
)
MAP_FILE = ROOT / "browser-map.json"
VAULT_FILE = ROOT / "password-vault.json"
PROFILES = ROOT / "profiles"
WELCOME_FILE = ROOT / "ChromeManager-welcome.html"
WELCOME_HOME_LABEL = "软件欢迎页"
LOCK_PORT = 39231
MAX_CDP_PORT = 25535
PROXY_BRIDGE_FALLBACK_START = 56000
PROXY_BRIDGE_FALLBACK_END = 60999
PROFILES.mkdir(parents=True, exist_ok=True)


def normalize_home_value(value):
    value = str(value or "").strip()
    if value == WELCOME_HOME_LABEL:
        return ""
    return "" if value.lower() == "about:blank" else value


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


def load_map():
    if not MAP_FILE.exists():
        return {}
    data = json.loads(MAP_FILE.read_text(encoding="utf-8-sig"))
    changed = False
    for record in data.values():
        current = record.get("environment")
        normalized = normalize_environment(current)
        if current != normalized:
            record["environment"] = normalized
            changed = True
    if changed:
        save_map(data)
    return data


def save_map(data):
    temporary = MAP_FILE.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, MAP_FILE)


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def cdp_alive(port):
    try:
        import urllib.request
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=0.35
        ) as response:
            data = json.load(response)
            return bool(data.get("webSocketDebuggerUrl") or data.get("Browser"))
    except Exception:
        return False


def port_pid(port):
    return next(
        (
            connection.pid
            for connection in psutil.net_connections(kind="tcp")
            if connection.status == psutil.CONN_LISTEN
            and connection.laddr
            and connection.laddr.port == int(port)
        ),
        None,
    )


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


def proxy_bridge_config_path(profile):
    return Path(profile) / "proxy-bridge.json"


def proxy_bridge_markers(config_path):
    return ["proxy", str(config_path)]


def environment_controller_markers(config_path):
    return ["environment", str(config_path)]


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


def find_chrome():
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    return next((path for path in candidates if path.exists()), None)


def safe_profile_path(record):
    raw = str(record.get("profile", "")).replace("\\", "/")
    parts = PurePosixPath(raw).parts
    if (
        len(parts) < 2
        or parts[0].lower() != "profiles"
        or any(part in (".", "..") or ":" in part or "\x00" in part for part in parts)
    ):
        raise SystemExit(f"不安全的浏览器数据目录：{raw or '空路径'}")
    root = PROFILES.resolve()
    target = (ROOT / Path(*parts)).resolve()
    if target != root and root not in target.parents:
        raise SystemExit(f"浏览器数据目录超出 profiles：{raw}")
    return target


def parse_proxy(value):
    value = value.strip()
    if "://" not in value:
        value = "http://" + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https", "socks5"):
        raise SystemExit("仅支持 http、https 和 socks5 代理")
    if not parsed.hostname or not parsed.port:
        raise SystemExit("代理格式错误，应为 协议://主机:端口")
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": parsed.port,
        "username": urllib.parse.unquote(parsed.username or ""),
        "password": urllib.parse.unquote(parsed.password or ""),
        "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
    }


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
    if config_path and Path(config_path).exists():
        try:
            saved_port = json.loads(
                Path(config_path).read_text(encoding="utf-8-sig")
            ).get("listen_port")
            saved_port = int(saved_port)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
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
    raise SystemExit("No available proxy bridge port")


def ensure_proxy_bridge(profile, cdp_port, proxy):
    config_path = proxy_bridge_config_path(profile)
    listen_port = proxy_bridge_port(cdp_port, config_path)
    config_path.write_text(
        json.dumps({
            "host": proxy["host"], "port": proxy["port"],
            "username": proxy["username"], "password": proxy["password"],
            "listen_port": listen_port,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markers = proxy_bridge_markers(config_path)
    if not port_open(listen_port):
        if FROZEN:
            manager = Path(sys.executable).parent.parent / "ChromeManager.exe"
            command = [
                str(manager),
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
        raise SystemExit("本地代理认证桥启动失败")
    return f"http://127.0.0.1:{listen_port}"


def next_index(data):
    numbers = [
        int(key[7:])
        for key in data
        if key.startswith("browser") and key[7:].isdigit()
    ]
    for path in PROFILES.glob("browser-*"):
        suffix = path.name.removeprefix("browser-")
        if suffix.isdigit():
            numbers.append(int(suffix))
    return max(numbers, default=0) + 1


def next_port(data):
    used = {int(record["port"]) for record in data.values()}
    port = 9231
    while port in used or port_open(port):
        port += 1
        if port > MAX_CDP_PORT:
            raise SystemExit("没有可用的 CDP 端口")
    return port


def find_record(data, value):
    if value in data:
        return value, data[value]
    for key, record in data.items():
        if str(record.get("port")) == value or record.get("name") == value:
            return key, record
    raise SystemExit(f"未找到浏览器：{value}")


def command_list(data, _):
    rows = []
    for key, record in sorted(data.items(), key=lambda item: int(item[1]["port"])):
        rows.append(
            {
                "key": key,
                "name": record["name"],
                "port": record["port"],
                "running": port_open(int(record["port"])),
                "profile": record["profile"],
                "last_open_at": record.get("last_open_at", ""),
            }
        )
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def command_add(data, args):
    port = args.port or next_port(data)
    if not 1024 <= port <= MAX_CDP_PORT:
        raise SystemExit(f"端口必须在 1024 到 {MAX_CDP_PORT} 之间")
    if any(int(record["port"]) == port for record in data.values()) or port_open(port):
        raise SystemExit(f"端口已被占用：{port}")
    index = next_index(data)
    key = f"browser{index}"
    profile = f"profiles/browser-{index}"
    (ROOT / profile).mkdir(parents=True, exist_ok=True)
    data[key] = {
        "name": args.name,
        "group": args.group,
        "port": port,
        "home": normalize_home_value(args.home),
        "proxy": args.proxy,
        "note": args.note,
        "schedule": "",
        "auto_start": False,
        "profile": profile,
        "environment": normalize_environment(),
        "last_open_at": "",
    }
    save_map(data)
    print(json.dumps({"key": key, **data[key]}, ensure_ascii=False, indent=2))


def command_start(data, args):
    key, record = find_record(data, args.browser)
    chrome = find_chrome()
    if not chrome:
        raise SystemExit("未找到 Google Chrome")
    profile = safe_profile_path(record)
    profile.mkdir(parents=True, exist_ok=True)
    environment = normalize_environment(record.get("environment"))
    apply_profile_preferences(profile, environment)
    command = [
        str(chrome),
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={record['port']}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=http://127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    command.extend(build_chrome_arguments(environment))
    extensions = extension_paths(profile, environment)
    vault = (
        json.loads(VAULT_FILE.read_text(encoding="utf-8-sig"))
        if VAULT_FILE.exists()
        else []
    )
    autofill = prepare_autofill_extension(profile, key, vault)
    if autofill:
        extensions.append(autofill)
    if extensions:
        extension_value = ",".join(str(path) for path in extensions)
        command.append(f"--load-extension={extension_value}")
    if record.get("proxy"):
        proxy = parse_proxy(record["proxy"])
        shutil.rmtree(profile / "ChromeManagerProxyAuth", ignore_errors=True)
        if proxy["username"]:
            command.append(
                f"--proxy-server={ensure_proxy_bridge(profile, record['port'], proxy)}"
            )
        else:
            command.append(f"--proxy-server={proxy['server']}")
    command.append(resolve_start_url(record, args.url))
    subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    deadline = time.time() + 12
    while time.time() < deadline and not cdp_alive(record["port"]):
        time.sleep(0.08)
    if cdp_alive(record["port"]):
        ensure_environment_controller(
            RESOURCE_ROOT, profile, record["port"], environment
        )
        record["last_open_at"] = datetime.now().isoformat(timespec="seconds")
        save_map(data)
    else:
        raise SystemExit(
            f"浏览器启动超时：{record['name']}，端口 {record['port']}"
        )
    print(f"已启动：{record['name']}，端口 {record['port']}")


def command_stop(data, args):
    _, record = find_record(data, args.browser)
    port = int(record["port"])
    profile = safe_profile_path(record)
    pid = port_pid(port)
    if not pid:
        print("浏览器未运行")
        return
    if not cdp_alive(port):
        raise SystemExit(f"端口 {port} 不是 Chrome CDP，拒绝结束该进程")
    process = psutil.Process(pid)
    for child in process.children(recursive=True):
        child.terminate()
    process.terminate()
    try:
        process.wait(timeout=5)
    except psutil.TimeoutExpired:
        process.kill()
    proxy_config_path = proxy_bridge_config_path(profile)
    stop_port_listener(
        proxy_bridge_port(port, proxy_config_path),
        proxy_bridge_markers(proxy_config_path),
    )
    environment_config_path = profile / "environment-controller.json"
    stop_port_listener(
        environment_controller_port(port),
        environment_controller_markers(environment_config_path),
    )
    print(f"已关闭：{record['name']}，端口 {port}")


def main():
    parser = argparse.ArgumentParser(description="Chrome 多开管理器命令行工具")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="列出全部浏览器")

    add = subparsers.add_parser("add", help="新增浏览器")
    add.add_argument("--name", required=True)
    add.add_argument("--port", type=int)
    add.add_argument("--group", default="默认分组")
    add.add_argument("--home", default="")
    add.add_argument("--proxy", default="")
    add.add_argument("--note", default="")

    start = subparsers.add_parser("start", help="启动浏览器")
    start.add_argument("browser", help="配置键、名称或端口")
    start.add_argument("--url")

    stop = subparsers.add_parser("stop", help="关闭浏览器")
    stop.add_argument("browser", help="配置键、名称或端口")

    args = parser.parse_args()
    data = load_map()
    commands = {
        "list": command_list,
        "add": command_add,
        "start": command_start,
        "stop": command_stop,
    }
    commands[args.command](data, args)


if __name__ == "__main__":
    main()
