import json
import socket
import shutil
import subprocess
import sys
import time
import urllib.parse
from copy import deepcopy
from pathlib import Path


DEFAULT_ENVIRONMENT = {
    "user_agent": "",
    "window_width": 1280,
    "window_height": 800,
    "window_x": 80,
    "window_y": 80,
    "language": "zh-CN",
    "accept_languages": "zh-CN,zh,en-US,en",
    "timezone": "Asia/Shanghai",
    "geo_enabled": False,
    "latitude": 31.2304,
    "longitude": 121.4737,
    "accuracy": 100,
    "webrtc_policy": "disable_non_proxied_udp",
    "dns_mode": "system",
    "dns_template": "https://cloudflare-dns.com/dns-query",
    "images": True,
    "notifications": False,
    "camera": False,
    "microphone": False,
    "geolocation_permission": False,
    "download_dir": "",
    "color_mode": "system",
    "mobile_mode": False,
    "touch_mode": False,
    "device_scale_factor": 1.0,
    "disable_password_prompt": False,
    "disable_translate": True,
    "incognito": False,
    "extension_paths": [],
    "privacy_canvas": False,
    "privacy_webgl": False,
    "privacy_hardware": False,
    "privacy_fonts": False,
    "hardware_concurrency": 4,
    "device_memory": 8,
    "webgl_vendor": "Privacy Protected",
    "webgl_renderer": "Privacy Protected Renderer",
}


def normalize_environment(value=None):
    result = deepcopy(DEFAULT_ENVIRONMENT)
    if isinstance(value, dict):
        for key in result:
            if key in value:
                result[key] = value[key]
    legacy_mobile = bool(result["mobile_mode"] or result["touch_mode"])
    user_agent = str(result.get("user_agent", ""))
    if any(marker in user_agent.lower() for marker in ("android", "iphone", "mobile")):
        legacy_mobile = True
    if legacy_mobile:
        result.update(
            user_agent="",
            window_width=1280,
            window_height=800,
            device_scale_factor=1.0,
        )
    result["mobile_mode"] = False
    result["touch_mode"] = False
    return result


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return deepcopy(default)


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def apply_profile_preferences(profile, environment):
    env = normalize_environment(environment)
    default_dir = profile / "Default"
    preferences_path = default_dir / "Preferences"
    preferences = _read_json(preferences_path, {})
    profile_values = preferences.setdefault("profile", {}).setdefault(
        "default_content_setting_values", {}
    )
    profile_values["images"] = 1 if env["images"] else 2
    profile_values["notifications"] = 1 if env["notifications"] else 2
    profile_values["media_stream_camera"] = 1 if env["camera"] else 2
    profile_values["media_stream_mic"] = 1 if env["microphone"] else 2
    profile_values["geolocation"] = 1 if env["geolocation_permission"] else 2
    preferences.setdefault("intl", {})["accept_languages"] = env["accept_languages"]
    preferences.setdefault("webrtc", {})[
        "ip_handling_policy"
    ] = env["webrtc_policy"]
    if env["download_dir"]:
        preferences.setdefault("download", {})[
            "default_directory"
        ] = str(Path(env["download_dir"]).expanduser())
        preferences["download"]["prompt_for_download"] = False
    preferences["credentials_enable_service"] = not env["disable_password_prompt"]
    preferences.setdefault("profile", {})["password_manager_enabled"] = (
        not env["disable_password_prompt"]
    )
    _write_json(preferences_path, preferences)


def build_chrome_arguments(environment):
    env = normalize_environment(environment)
    arguments = [
        f"--window-size={int(env['window_width'])},{int(env['window_height'])}",
        f"--window-position={int(env['window_x'])},{int(env['window_y'])}",
        f"--lang={env['language']}",
    ]
    if float(env["device_scale_factor"]) > 0:
        arguments.append(
            f"--force-device-scale-factor={float(env['device_scale_factor'])}"
        )
    if env["user_agent"]:
        arguments.append(f"--user-agent={env['user_agent']}")
    if env["touch_mode"] or env["mobile_mode"]:
        arguments.append("--touch-events=enabled")
    if env["mobile_mode"]:
        arguments.extend(["--enable-viewport", "--enable-features=OverlayScrollbar"])
    if env["color_mode"] == "dark":
        arguments.append("--force-dark-mode")
    elif env["color_mode"] == "light":
        arguments.append("--disable-features=WebContentsForceDark")
    if env["disable_translate"]:
        arguments.append("--disable-translate")
    if env["disable_password_prompt"]:
        arguments.append("--disable-save-password-bubble")
    if env["incognito"]:
        arguments.append("--incognito")
    if env["dns_mode"] == "disable_async":
        arguments.append("--disable-features=AsyncDns")
    elif env["dns_mode"] in ("secure", "custom"):
        template = (
            env["dns_template"]
            if env["dns_mode"] == "custom"
            else "https://cloudflare-dns.com/dns-query"
        )
        arguments.extend([
            "--dns-over-https-mode=secure",
            f"--dns-over-https-templates={template}",
        ])
    return arguments


def prepare_environment_extension(profile, environment):
    env = normalize_environment(environment)
    enabled = any(
        (
            env["timezone"],
            env["geo_enabled"],
            env["privacy_canvas"],
            env["privacy_webgl"],
            env["privacy_hardware"],
            env["privacy_fonts"],
        )
    )
    extension = profile / "ChromeManagerEnvironment"
    shutil.rmtree(extension, ignore_errors=True)
    if not enabled:
        return None
    extension.mkdir(parents=True, exist_ok=True)
    manifest = {
        "manifest_version": 3,
        "name": "Chrome Manager Test Environment",
        "version": "1.0.0",
        "description": "Development-test environment overrides. Not an anti-detection guarantee.",
        "content_scripts": [{
            "matches": ["<all_urls>"],
            "run_at": "document_start",
            "world": "MAIN",
            "js": ["environment.js"],
        }],
    }
    script = environment_script(env)
    (extension / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (extension / "environment.js").write_text(script, encoding="utf-8")
    return extension


def prepare_autofill_extension(profile, browser_key, records):
    extension = profile / "ChromeManagerAutofill"
    shutil.rmtree(extension, ignore_errors=True)
    entries = []
    matches = set()
    for record in records:
        if record.get("browser_key", "") != browser_key:
            continue
        raw_url = str(record.get("url", "")).strip()
        if not raw_url:
            continue
        if "://" not in raw_url:
            raw_url = "https://" + raw_url
        parsed = urllib.parse.urlsplit(raw_url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or not hostname:
            continue
        password = str(record.get("password", ""))
        if not password:
            continue
        entries.append({
            "hostname": hostname,
            "username": str(record.get("username", "")),
            "password": password,
        })
        matches.add(f"*://{hostname}/*")
    if not entries:
        return None
    extension.mkdir(parents=True, exist_ok=True)
    manifest = {
        "manifest_version": 3,
        "name": "Chrome Manager Autofill",
        "version": "1.0.0",
        "description": "Fills assigned credentials for exact website domains.",
        "content_scripts": [{
            "matches": sorted(matches),
            "run_at": "document_idle",
            "all_frames": False,
            "js": ["autofill.js"],
        }],
    }
    script = f"""
(() => {{
  const entries = {json.dumps(entries, ensure_ascii=False)};
  const entry = entries.find(item => item.hostname === location.hostname.toLowerCase());
  if (!entry) return;
  const setValue = (element, value) => {{
    if (!element || element.value || !value) return;
    const prototype = element instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(prototype, 'value');
    if (descriptor && descriptor.set) descriptor.set.call(element, value);
    else element.value = value;
    element.dispatchEvent(new Event('input', {{bubbles: true}}));
    element.dispatchEvent(new Event('change', {{bubbles: true}}));
  }};
  const fill = () => {{
    const password = document.querySelector('input[type="password"]:not([disabled])');
    if (!password) return;
    const form = password.form || password.closest('form') || document;
    const username = form.querySelector(
      'input[autocomplete="username"],input[type="email"],'
      + 'input[name*="user" i],input[name*="email" i],input[type="text"]'
    );
    setValue(username, entry.username);
    setValue(password, entry.password);
  }};
  fill();
  const observer = new MutationObserver(fill);
  observer.observe(document.documentElement, {{childList: true, subtree: true}});
  setTimeout(() => observer.disconnect(), 15000);
}})();
""".strip()
    (extension / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (extension / "autofill.js").write_text(script, encoding="utf-8")
    return extension


def environment_script(environment):
    env = normalize_environment(environment)
    return f"""
(() => {{
  const env = {json.dumps(env, ensure_ascii=False)};
  const define = (obj, key, value) => {{
    try {{ Object.defineProperty(obj, key, {{get: () => value, configurable: true}}); }} catch (_) {{}}
  }};
  if (env.user_agent) {{
    define(Navigator.prototype, 'userAgent', String(env.user_agent));
    define(Navigator.prototype, 'appVersion', String(env.user_agent).replace(/^Mozilla\\//, ''));
  }}
  if (env.language) {{
    define(Navigator.prototype, 'language', env.language);
    define(Navigator.prototype, 'languages',
      String(env.accept_languages || env.language).split(',').map(v => v.trim()).filter(Boolean));
  }}
  if (env.timezone) {{
    const original = Intl.DateTimeFormat.prototype.resolvedOptions;
    Intl.DateTimeFormat.prototype.resolvedOptions = function() {{
      const value = original.call(this);
      return Object.assign(value, {{timeZone: env.timezone}});
    }};
  }}
  if (env.geo_enabled && navigator.geolocation) {{
    const position = {{
      coords: {{
        latitude: Number(env.latitude), longitude: Number(env.longitude),
        accuracy: Number(env.accuracy), altitude: null, altitudeAccuracy: null,
        heading: null, speed: null
      }},
      timestamp: Date.now()
    }};
    navigator.geolocation.getCurrentPosition = success => success(position);
    navigator.geolocation.watchPosition = success => {{
      success(position); return 1;
    }};
    navigator.geolocation.clearWatch = () => {{}};
  }}
  if (env.privacy_hardware) {{
    define(Navigator.prototype, 'hardwareConcurrency', Number(env.hardware_concurrency));
    define(Navigator.prototype, 'deviceMemory', Number(env.device_memory));
  }}
  if (env.privacy_fonts && document.fonts && document.fonts.check) {{
    document.fonts.check = () => false;
  }}
  if (env.privacy_webgl) {{
    for (const name of ['WebGLRenderingContext', 'WebGL2RenderingContext']) {{
      const Ctor = globalThis[name];
      if (!Ctor) continue;
      const original = Ctor.prototype.getParameter;
      Ctor.prototype.getParameter = function(parameter) {{
        if (parameter === 37445) return String(env.webgl_vendor);
        if (parameter === 37446) return String(env.webgl_renderer);
        return original.call(this, parameter);
      }};
    }}
  }}
  if (env.privacy_canvas) {{
    const original = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(...args) {{
      const context = this.getContext('2d');
      if (context && this.width && this.height) {{
        const image = context.getImageData(0, 0, this.width, this.height);
        for (let i = 0; i < image.data.length; i += 400) image.data[i] ^= 1;
        context.putImageData(image, 0, 0);
      }}
      return original.apply(this, args);
    }};
  }}
}})();
""".strip()


def extension_paths(profile, environment):
    env = normalize_environment(environment)
    paths = []
    generated = prepare_environment_extension(profile, env)
    if generated:
        paths.append(generated)
    for value in env["extension_paths"]:
        path = Path(value).expanduser()
        if path.is_dir() and (path / "manifest.json").exists():
            paths.append(path.resolve())
    return paths


def environment_controller_port(cdp_port):
    return 40000 + int(cdp_port)


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.2):
            return True
    except OSError:
        return False


def ensure_environment_controller(root, profile, cdp_port, environment):
    env = normalize_environment(environment)
    config_path = profile / "environment-controller.json"
    _write_json(config_path, env)
    lock_port = environment_controller_port(cdp_port)
    if not _port_open(lock_port):
        if getattr(sys, "frozen", False):
            manager = Path(sys.executable).with_name("ChromeManager.exe")
            command = [
                str(manager),
                "--environment-controller",
                "--cdp-port",
                str(cdp_port),
                "--lock-port",
                str(lock_port),
                "--config",
                str(config_path),
            ]
        else:
            command = [
                sys.executable,
                str(Path(root) / "environment_controller.py"),
                "--cdp-port",
                str(cdp_port),
                "--lock-port",
                str(lock_port),
                "--config",
                str(config_path),
            ]
        subprocess.Popen(
            command,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.time() + 5
        while time.time() < deadline and not _port_open(lock_port):
            time.sleep(0.05)
    return _port_open(lock_port)


def consistency_report(environment, proxy_info=None):
    env = normalize_environment(environment)
    issues = []
    warnings = []
    if env["mobile_mode"] and not env["touch_mode"]:
        issues.append("移动设备模式已开启，但触摸模式未开启。")
    if env["mobile_mode"] and env["window_width"] > 900:
        warnings.append("移动模式窗口宽度超过 900，页面可能判断为桌面设备。")
    if env["dns_mode"] in ("secure", "custom") and not env["dns_template"]:
        issues.append("安全 DNS 已开启，但 DNS 模板为空。")
    if env["language"].lower().startswith("zh") and env["timezone"] not in (
        "Asia/Shanghai", "Asia/Hong_Kong", "Asia/Taipei", "Asia/Singapore"
    ):
        warnings.append("中文语言与当前时区可能不一致。")
    if proxy_info and proxy_info.get("country"):
        country = proxy_info["country"]
        timezone = env["timezone"]
        rough_match = {
            "CN": "Asia/", "HK": "Asia/", "JP": "Asia/Tokyo",
            "SG": "Asia/Singapore", "US": "America/", "GB": "Europe/",
        }.get(country)
        if rough_match and rough_match not in timezone:
            warnings.append(f"代理国家 {country} 与时区 {timezone} 可能不一致。")
    if any(
        env[key] for key in (
            "privacy_canvas", "privacy_webgl", "privacy_hardware", "privacy_fonts"
        )
    ):
        warnings.append(
            "隐私测试覆盖会改变 JavaScript 可见值，但不能改变 TLS、浏览器内核或全部指纹面。"
        )
        warnings.append(
            "固定或虚构 CPU、内存、WebGL、Canvas、字体值可能比真实值更异常，反而增加识别风险。"
        )
    warnings.append("TLS 指纹由 Chrome/操作系统网络栈决定，本程序不能可靠伪装。")
    return {"issues": issues, "warnings": warnings}
