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
from pathlib import Path

import psutil
from environment_config import (
    apply_profile_preferences,
    build_chrome_arguments,
    ensure_environment_controller,
    extension_paths,
    normalize_environment,
)


ROOT = Path(__file__).resolve().parent
MAP_FILE = ROOT / "browser-map.json"
PROFILES = ROOT / "profiles"


def load_map():
    if not MAP_FILE.exists():
        return {}
    return json.loads(MAP_FILE.read_text(encoding="utf-8-sig"))


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


def find_chrome():
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    return next((path for path in candidates if path.exists()), None)


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


def prepare_proxy_extension(profile, proxy):
    extension = profile / "ChromeManagerProxyAuth"
    shutil.rmtree(extension, ignore_errors=True)
    if not proxy["username"]:
        return None
    if proxy["scheme"] == "socks5":
        raise SystemExit("Chrome 不支持 SOCKS5 用户名密码认证")
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
        "  {urls: ['<all_urls>']}, ['asyncBlocking']);\n"
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
    config_path.write_text(
        json.dumps({
            "host": proxy["host"], "port": proxy["port"],
            "username": proxy["username"], "password": proxy["password"],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
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
        "home": args.home,
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
    _, record = find_record(data, args.browser)
    chrome = find_chrome()
    if not chrome:
        raise SystemExit("未找到 Google Chrome")
    profile = ROOT / record["profile"]
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
    if extensions:
        extension_value = ",".join(str(path) for path in extensions)
        command.append(f"--load-extension={extension_value}")
    if record.get("proxy"):
        proxy = parse_proxy(record["proxy"])
        extension = prepare_proxy_extension(profile, proxy)
        if extension:
            command.append(
                f"--proxy-server={ensure_proxy_bridge(profile, record['port'], proxy)}"
            )
        else:
            command.append(f"--proxy-server={proxy['server']}")
    command.append(args.url or record.get("home") or "about:blank")
    subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    deadline = time.time() + 12
    while time.time() < deadline and not cdp_alive(record["port"]):
        time.sleep(0.08)
    if cdp_alive(record["port"]):
        ensure_environment_controller(
            ROOT, profile, record["port"], environment
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
    stop_port_listener(30000 + port)
    stop_port_listener(40000 + port)
    print(f"已关闭：{record['name']}，端口 {port}")


def main():
    parser = argparse.ArgumentParser(description="Chrome 多开管理器 Codex/CLI 工具")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="列出全部浏览器")

    add = subparsers.add_parser("add", help="新增浏览器")
    add.add_argument("--name", required=True)
    add.add_argument("--port", type=int)
    add.add_argument("--group", default="默认分组")
    add.add_argument("--home", default="about:blank")
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
