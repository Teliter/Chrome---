import json
import urllib.error
import urllib.parse
import urllib.request


class CloudError(RuntimeError):
    pass


def normalize_server_url(value):
    url = str(value or "").strip().rstrip("/")
    if not url:
        raise CloudError("请填写服务器地址。")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    parsed = urllib.parse.urlsplit(url)
    if not parsed.hostname:
        raise CloudError("服务器地址格式不正确。")
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and parsed.hostname.lower() not in local_hosts:
        raise CloudError("远程服务器必须使用 HTTPS，避免账号和同步数据被窃取。")
    return url


def request_json(server, path, method="GET", payload=None, token="", timeout=20):
    server = normalize_server_url(server)
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        server + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8")).get("detail")
        except Exception:
            detail = None
        raise CloudError(detail or f"服务器返回错误：HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise CloudError(f"无法连接服务器：{error.reason}") from error
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError as error:
        raise CloudError("服务器返回了无法识别的数据。") from error


def register(server, username, password):
    return request_json(
        server,
        "/api/register",
        method="POST",
        payload={"username": username, "password": password},
    )


def login(server, username, password):
    return request_json(
        server,
        "/api/login",
        method="POST",
        payload={"username": username, "password": password},
    )


def upload_snapshot(server, token, browsers, settings, vault):
    return request_json(
        server,
        "/api/sync",
        method="PUT",
        token=token,
        payload={"browsers": browsers, "settings": settings, "vault": vault},
        timeout=60,
    )


def download_snapshot(server, token):
    return request_json(server, "/api/sync", token=token, timeout=60)


def account_status(server, token):
    return request_json(server, "/api/me", token=token, timeout=20)


def logout(server, token):
    return request_json(server, "/api/logout", method="POST", token=token)
