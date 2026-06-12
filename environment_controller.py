import argparse
import json
import socket
import threading
import time
import urllib.request
from pathlib import Path

import websocket

from environment_config import environment_script, normalize_environment


def load_environment(path):
    try:
        return normalize_environment(
            json.loads(Path(path).read_text(encoding="utf-8-sig"))
        )
    except Exception:
        return normalize_environment()


def send_command(connection, command_id, method, params=None):
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


def apply_to_page(websocket_url, environment):
    connection = websocket.create_connection(
        websocket_url,
        timeout=3,
        origin="http://127.0.0.1",
        suppress_origin=True,
    )
    try:
        command_id = 1

        def invoke(method, params=None):
            nonlocal command_id
            result = send_command(connection, command_id, method, params)
            command_id += 1
            return result

        language = environment["language"]
        if environment["user_agent"]:
            invoke(
                "Emulation.setUserAgentOverride",
                {
                    "userAgent": environment["user_agent"],
                    "acceptLanguage": environment["accept_languages"],
                    "platform": "Win32",
                },
            )
        if language:
            invoke("Emulation.setLocaleOverride", {"locale": language})
        if environment["timezone"]:
            invoke(
                "Emulation.setTimezoneOverride",
                {"timezoneId": environment["timezone"]},
            )
        if environment["geo_enabled"]:
            invoke(
                "Emulation.setGeolocationOverride",
                {
                    "latitude": float(environment["latitude"]),
                    "longitude": float(environment["longitude"]),
                    "accuracy": float(environment["accuracy"]),
                },
            )
        if environment["mobile_mode"] or environment["touch_mode"]:
            invoke(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": int(environment["window_width"]),
                    "height": int(environment["window_height"]),
                    "deviceScaleFactor": float(environment["device_scale_factor"]),
                    "mobile": bool(environment["mobile_mode"]),
                },
            )
            invoke(
                "Emulation.setTouchEmulationEnabled",
                {
                    "enabled": bool(environment["touch_mode"]),
                    "maxTouchPoints": 5 if environment["touch_mode"] else 1,
                },
            )
        script = environment_script(environment)
        invoke("Page.addScriptToEvaluateOnNewDocument", {"source": script})
        invoke("Runtime.evaluate", {"expression": script})
    finally:
        connection.close()


def list_pages(cdp_port):
    with urllib.request.urlopen(
        f"http://127.0.0.1:{cdp_port}/json/list", timeout=1
    ) as response:
        return [
            item
            for item in json.load(response)
            if item.get("type") == "page" and item.get("webSocketDebuggerUrl")
        ]


def run(cdp_port, lock_port, config_path):
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lock.bind(("127.0.0.1", lock_port))
    lock.listen()

    def accept_probes():
        while True:
            try:
                connection, _ = lock.accept()
                connection.close()
            except OSError:
                return

    threading.Thread(target=accept_probes, daemon=True).start()
    applied = {}
    while True:
        try:
            pages = list_pages(cdp_port)
        except Exception:
            time.sleep(1)
            try:
                pages = list_pages(cdp_port)
            except Exception:
                return
        environment = load_environment(config_path)
        try:
            config_mtime = Path(config_path).stat().st_mtime_ns
        except OSError:
            time.sleep(0.8)
            continue
        current = set()
        for page in pages:
            target_id = page.get("id") or page["webSocketDebuggerUrl"]
            current.add(target_id)
            marker = (
                page["webSocketDebuggerUrl"],
                config_mtime,
            )
            if applied.get(target_id) == marker:
                continue
            try:
                apply_to_page(page["webSocketDebuggerUrl"], environment)
                applied[target_id] = marker
            except Exception:
                continue
        applied = {key: value for key, value in applied.items() if key in current}
        time.sleep(0.8)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-port", type=int, required=True)
    parser.add_argument("--lock-port", type=int, required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    run(args.cdp_port, args.lock_port, args.config)


if __name__ == "__main__":
    main()
