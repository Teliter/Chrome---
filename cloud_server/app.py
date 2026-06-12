import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("CHROME_MANAGER_DB", ROOT / "cloud.db"))
KEY_PATH = Path(os.environ.get("CHROME_MANAGER_KEY_FILE", ROOT / "cloud-data.key"))
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{3,32}$")
SESSION_DAYS = 30

app = FastAPI(title="Chrome Manager Cloud", version="1.0.0")


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)


class Snapshot(BaseModel):
    browsers: dict = Field(default_factory=dict)
    settings: dict = Field(default_factory=dict)
    vault: list = Field(default_factory=list)


def utc_now():
    return datetime.now(timezone.utc)


def timestamp(value=None):
    return (value or utc_now()).isoformat(timespec="seconds")


@contextmanager
def database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def initialize_database():
    with database() as connection:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                user_id INTEGER PRIMARY KEY,
                encrypted_payload BLOB NOT NULL,
                browser_count INTEGER NOT NULL DEFAULT 0,
                vault_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )


def data_cipher():
    configured = os.environ.get("CHROME_MANAGER_DATA_KEY", "").strip()
    if configured:
        key = hashlib.sha256(configured.encode("utf-8")).digest()
        return Fernet(base64.urlsafe_b64encode(key))
    if not KEY_PATH.exists():
        KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        KEY_PATH.write_bytes(Fernet.generate_key())
    return Fernet(KEY_PATH.read_bytes().strip())


def password_hash(password):
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
    )
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(derived).decode()}"


def password_matches(password, encoded):
    try:
        algorithm, salt_text, hash_text = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        salt = base64.b64decode(salt_text)
        expected = base64.b64decode(hash_text)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def token_hash(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def issue_session(connection, user_id):
    token = secrets.token_urlsafe(32)
    expires = utc_now() + timedelta(days=SESSION_DAYS)
    connection.execute(
        "INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (token_hash(token), user_id, timestamp(expires), timestamp()),
    )
    return token


def current_user(authorization: str = Header(default="")):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="请先登录。")
    token = authorization[7:].strip()
    with database() as connection:
        row = connection.execute(
            """
            SELECT users.id, users.username, sessions.expires_at
            FROM sessions JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ?
            """,
            (token_hash(token),),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="登录状态无效。")
        expires = datetime.fromisoformat(row["expires_at"])
        if expires <= utc_now():
            connection.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),)
            )
            raise HTTPException(status_code=401, detail="登录已过期，请重新登录。")
        return {
            "id": row["id"],
            "username": row["username"],
            "token_hash": token_hash(token),
        }


@app.on_event("startup")
def startup():
    initialize_database()
    data_cipher()


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return (ROOT / "dashboard.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    return {"ok": True, "time": timestamp()}


@app.post("/api/register")
def register(credentials: Credentials):
    username = credentials.username.strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise HTTPException(
            status_code=400,
            detail="用户名需为 3-32 位中文、字母、数字、下划线或短横线。",
        )
    with database() as connection:
        try:
            cursor = connection.execute(
                "INSERT INTO users(username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, password_hash(credentials.password), timestamp()),
            )
        except sqlite3.IntegrityError as error:
            raise HTTPException(status_code=409, detail="用户名已存在。") from error
        token = issue_session(connection, cursor.lastrowid)
    return {"token": token, "username": username}


@app.post("/api/login")
def login(credentials: Credentials):
    with database() as connection:
        row = connection.execute(
            "SELECT id, username, password_hash FROM users WHERE username = ? COLLATE NOCASE",
            (credentials.username.strip(),),
        ).fetchone()
        if not row or not password_matches(credentials.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="用户名或密码错误。")
        token = issue_session(connection, row["id"])
    return {"token": token, "username": row["username"]}


@app.get("/api/me")
def me(user=Depends(current_user)):
    with database() as connection:
        snapshot = connection.execute(
            "SELECT browser_count, vault_count, updated_at FROM snapshots WHERE user_id = ?",
            (user["id"],),
        ).fetchone()
    return {
        "username": user["username"],
        "browser_count": snapshot["browser_count"] if snapshot else 0,
        "vault_count": snapshot["vault_count"] if snapshot else 0,
        "updated_at": snapshot["updated_at"] if snapshot else "",
    }


@app.post("/api/logout")
def logout(user=Depends(current_user)):
    with database() as connection:
        connection.execute(
            "DELETE FROM sessions WHERE token_hash = ?", (user["token_hash"],)
        )
    return {"ok": True}


@app.put("/api/sync")
def save_snapshot(snapshot: Snapshot, user=Depends(current_user)):
    if len(snapshot.browsers) > 1000 or len(snapshot.vault) > 100000:
        raise HTTPException(status_code=400, detail="同步数据数量超过限制。")
    payload = json.dumps(snapshot.model_dump(), ensure_ascii=False).encode("utf-8")
    if len(payload) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="同步数据超过 20 MB。")
    encrypted = data_cipher().encrypt(payload)
    updated_at = timestamp()
    with database() as connection:
        connection.execute(
            """
            INSERT INTO snapshots(user_id, encrypted_payload, browser_count, vault_count, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                encrypted_payload=excluded.encrypted_payload,
                browser_count=excluded.browser_count,
                vault_count=excluded.vault_count,
                updated_at=excluded.updated_at
            """,
            (
                user["id"],
                encrypted,
                len(snapshot.browsers),
                len(snapshot.vault),
                updated_at,
            ),
        )
    return {
        "ok": True,
        "browser_count": len(snapshot.browsers),
        "vault_count": len(snapshot.vault),
        "updated_at": updated_at,
    }


@app.get("/api/sync")
def get_snapshot(user=Depends(current_user)):
    with database() as connection:
        row = connection.execute(
            "SELECT encrypted_payload, updated_at FROM snapshots WHERE user_id = ?",
            (user["id"],),
        ).fetchone()
    if not row:
        return {"browsers": {}, "settings": {}, "vault": [], "updated_at": ""}
    try:
        payload = json.loads(data_cipher().decrypt(row["encrypted_payload"]))
    except (InvalidToken, json.JSONDecodeError) as error:
        raise HTTPException(status_code=500, detail="云端数据无法解密。") from error
    payload["updated_at"] = row["updated_at"]
    return payload
