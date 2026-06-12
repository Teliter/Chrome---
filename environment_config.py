import json
import socket
import shutil
import subprocess
import sys
import time
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
    "disable_password_prompt": True,
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
    if env["disable_password_prompt"]:
        preferences["credentials_enable_service"] = False
        preferences.setdefault("profile", {})["password_manager_enabled"] = False
    _write_json(preferences_path, preferences)


def build_chrome_arguments(environment):
    env = normalize_environment(environment)
    arguments = [
        f"--window-size={int(env['window_width'])},{int(env['window_height'])}",
        f"--window-position={int(env['window_x'])},{int(env['window_y'])}",
        f"--lang={env['language']}",
        f"--force-device-scale-factor={float(env['device_scale_factor'])}",
    ]
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
        subprocess.Popen(
            [
                sys.executable,
                str(Path(root) / "environment_controller.py"),
                "--cdp-port",
                str(cdp_port),
                "--lock-port",
                str(lock_port),
                "--config",
                str(config_path),
            ],
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
