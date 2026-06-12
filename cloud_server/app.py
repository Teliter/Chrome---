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
ADMIN_USERNAME = os.environ.get("CHROME_MANAGER_ADMIN_USERNAME", "admin").strip()
ADMIN_PASSWORD = os.environ.get("CHROME_MANAGER_ADMIN_PASSWORD", "").strip()
ADMIN_PASSWORD_FILE = Path(
    os.environ.get(
        "CHROME_MANAGER_ADMIN_PASSWORD_FILE",
        ROOT / "cloud-admin-password.txt",
    )
)

app = FastAPI(title="Chrome Manager Cloud", version="2.0.0")


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)


class Snapshot(BaseModel):
    browsers: dict = Field(default_factory=dict)
    settings: dict = Field(default_factory=dict)
    vault: list = Field(default_factory=list)


class PasswordReset(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class MemberCreate(Credentials):
    display_name: str = Field(default="", max_length=64)


class MemberUpdate(BaseModel):
    display_name: str = Field(default="", max_length=64)
    disabled: bool = False


class ResourceAssignments(BaseModel):
    browser_keys: list[str] = Field(default_factory=list)
    vault_keys: list[str] = Field(default_factory=list)


def utc_now():
    return datetime.now(timezone.utc)


def timestamp(value=None):
    return (value or utc_now()).isoformat(timespec="seconds")


@contextmanager
def database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
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
                created_at TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                disabled INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL DEFAULT 'owner',
                parent_user_id INTEGER,
                display_name TEXT NOT NULL DEFAULT ''
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
            CREATE TABLE IF NOT EXISTS resource_assignments (
                member_id INTEGER NOT NULL,
                resource_type TEXT NOT NULL,
                resource_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(member_id, resource_type, resource_key),
                FOREIGN KEY(member_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_assignments_member
                ON resource_assignments(member_id);
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        migrations = {
            "is_admin": "INTEGER NOT NULL DEFAULT 0",
            "disabled": "INTEGER NOT NULL DEFAULT 0",
            "role": "TEXT NOT NULL DEFAULT 'owner'",
            "parent_user_id": "INTEGER",
            "display_name": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in migrations.items():
            if name not in columns:
                connection.execute(
                    f'ALTER TABLE users ADD COLUMN "{name}" {definition}'
                )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_parent ON users(parent_user_id)"
        )
        connection.execute(
            "UPDATE users SET role='admin' WHERE is_admin=1"
        )
        connection.execute(
            """
            UPDATE users SET role='owner'
            WHERE is_admin=0 AND (role IS NULL OR role NOT IN ('owner', 'member'))
            """
        )
        admin_password = load_admin_password()
        admin = connection.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
            (ADMIN_USERNAME,),
        ).fetchone()
        if admin:
            connection.execute(
                """
                UPDATE users
                SET password_hash=?, is_admin=1, disabled=0, role='admin',
                    parent_user_id=NULL
                WHERE id=?
                """,
                (password_hash(admin_password), admin["id"]),
            )
        else:
            connection.execute(
                """
                INSERT INTO users(
                    username, password_hash, created_at, is_admin, disabled,
                    role, display_name
                ) VALUES (?, ?, ?, 1, 0, 'admin', '系统管理员')
                """,
                (ADMIN_USERNAME, password_hash(admin_password), timestamp()),
            )


def load_admin_password():
    if ADMIN_PASSWORD:
        return ADMIN_PASSWORD
    if ADMIN_PASSWORD_FILE.exists():
        return ADMIN_PASSWORD_FILE.read_text(encoding="utf-8").strip()
    password = secrets.token_urlsafe(18)
    ADMIN_PASSWORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    ADMIN_PASSWORD_FILE.write_text(password, encoding="utf-8")
    return password


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
    return (
        f"scrypt${base64.b64encode(salt).decode()}"
        f"${base64.b64encode(derived).decode()}"
    )


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
        """
        INSERT INTO sessions(token_hash, user_id, expires_at, created_at)
        VALUES (?, ?, ?, ?)
        """,
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
            SELECT users.id, users.username, users.is_admin, users.disabled,
                   users.role, users.parent_user_id, users.display_name,
                   sessions.expires_at, parents.disabled AS parent_disabled
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            LEFT JOIN users parents ON parents.id = users.parent_user_id
            WHERE sessions.token_hash = ?
            """,
            (token_hash(token),),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="登录状态无效。")
        if row["disabled"]:
            raise HTTPException(status_code=403, detail="账号已被禁用。")
        if row["role"] == "member" and row["parent_disabled"]:
            raise HTTPException(status_code=403, detail="所属主账号已被禁用。")
        expires = datetime.fromisoformat(row["expires_at"])
        if expires <= utc_now():
            connection.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),)
            )
            raise HTTPException(status_code=401, detail="登录已过期，请重新登录。")
        return {
            "id": row["id"],
            "username": row["username"],
            "is_admin": bool(row["is_admin"]),
            "role": row["role"],
            "parent_user_id": row["parent_user_id"],
            "display_name": row["display_name"],
            "token_hash": token_hash(token),
        }


def current_admin(user=Depends(current_user)):
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="仅系统管理员可以访问。")
    return user


def current_owner(user=Depends(current_user)):
    if user["role"] != "owner" or user["is_admin"]:
        raise HTTPException(status_code=403, detail="仅主账号可以管理子账号。")
    return user


def owner_id_for(user):
    if user["role"] == "member":
        return user["parent_user_id"]
    return user["id"]


def decrypt_snapshot(row):
    if not row or not row["encrypted_payload"]:
        return {"browsers": {}, "settings": {}, "vault": []}
    try:
        return json.loads(data_cipher().decrypt(row["encrypted_payload"]))
    except (InvalidToken, json.JSONDecodeError) as error:
        raise HTTPException(status_code=500, detail="云端数据无法解密。") from error


def vault_resource_key(record):
    identity = {
        "site": str(record.get("site", "")).strip().lower(),
        "url": str(record.get("url", "")).strip().lower(),
        "username": str(record.get("username", "")).strip().lower(),
        "browser_key": str(record.get("browser_key", "")).strip(),
    }
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def load_owner_snapshot(connection, owner_id):
    row = connection.execute(
        "SELECT encrypted_payload, updated_at FROM snapshots WHERE user_id=?",
        (owner_id,),
    ).fetchone()
    payload = decrypt_snapshot(row)
    payload["updated_at"] = row["updated_at"] if row else ""
    return payload


def member_assignment_sets(connection, member_id):
    rows = connection.execute(
        """
        SELECT resource_type, resource_key
        FROM resource_assignments WHERE member_id=?
        """,
        (member_id,),
    ).fetchall()
    return {
        "browser": {
            row["resource_key"] for row in rows if row["resource_type"] == "browser"
        },
        "vault": {
            row["resource_key"] for row in rows if row["resource_type"] == "vault"
        },
    }


def authorized_snapshot(connection, user):
    owner_id = owner_id_for(user)
    payload = load_owner_snapshot(connection, owner_id)
    if user["role"] != "member":
        return payload
    assignments = member_assignment_sets(connection, user["id"])
    payload["browsers"] = {
        key: value
        for key, value in payload.get("browsers", {}).items()
        if key in assignments["browser"]
    }
    payload["vault"] = [
        record
        for record in payload.get("vault", [])
        if vault_resource_key(record) in assignments["vault"]
    ]
    return payload


def member_for_owner(connection, owner_id, member_id):
    member = connection.execute(
        """
        SELECT id, username, display_name, created_at, disabled, role
        FROM users
        WHERE id=? AND parent_user_id=? AND role='member' AND is_admin=0
        """,
        (member_id, owner_id),
    ).fetchone()
    if not member:
        raise HTTPException(status_code=404, detail="子账号不存在。")
    return member


def login_payload(connection, row):
    token = issue_session(connection, row["id"])
    owner_username = row["username"]
    if row["role"] == "member":
        owner = connection.execute(
            "SELECT username FROM users WHERE id=?", (row["parent_user_id"],)
        ).fetchone()
        owner_username = owner["username"] if owner else ""
    return {
        "token": token,
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "owner_username": owner_username,
        "read_only": row["role"] == "member",
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
                """
                INSERT INTO users(
                    username, password_hash, created_at, is_admin, disabled,
                    role, display_name
                ) VALUES (?, ?, ?, 0, 0, 'owner', '')
                """,
                (username, password_hash(credentials.password), timestamp()),
            )
        except sqlite3.IntegrityError as error:
            raise HTTPException(status_code=409, detail="用户名已存在。") from error
        row = connection.execute(
            "SELECT * FROM users WHERE id=?", (cursor.lastrowid,)
        ).fetchone()
        result = login_payload(connection, row)
    return result


def authenticate(connection, credentials):
    row = connection.execute(
        """
        SELECT id, username, password_hash, is_admin, disabled, role,
               parent_user_id, display_name
        FROM users WHERE username=? COLLATE NOCASE
        """,
        (credentials.username.strip(),),
    ).fetchone()
    if not row or not password_matches(credentials.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误。")
    if row["disabled"]:
        raise HTTPException(status_code=403, detail="账号已被禁用。")
    return row


@app.post("/api/login")
def login(credentials: Credentials):
    with database() as connection:
        row = authenticate(connection, credentials)
        if row["is_admin"]:
            raise HTTPException(status_code=403, detail="管理员请使用网页后台登录。")
        result = login_payload(connection, row)
    return result


@app.post("/api/portal/login")
def portal_login(credentials: Credentials):
    with database() as connection:
        row = authenticate(connection, credentials)
        result = login_payload(connection, row)
    return result


@app.post("/api/admin/login")
def admin_login(credentials: Credentials):
    with database() as connection:
        row = authenticate(connection, credentials)
        if not row["is_admin"]:
            raise HTTPException(status_code=403, detail="该账号不是系统管理员。")
        result = login_payload(connection, row)
    return result


@app.get("/api/me")
def me(user=Depends(current_user)):
    with database() as connection:
        payload = authorized_snapshot(connection, user)
        owner_username = user["username"]
        if user["role"] == "member":
            owner = connection.execute(
                "SELECT username FROM users WHERE id=?", (user["parent_user_id"],)
            ).fetchone()
            owner_username = owner["username"] if owner else ""
    return {
        "username": user["username"],
        "display_name": user["display_name"],
        "role": user["role"],
        "owner_username": owner_username,
        "read_only": user["role"] == "member",
        "browser_count": len(payload.get("browsers", {})),
        "vault_count": len(payload.get("vault", [])),
        "updated_at": payload.get("updated_at", ""),
    }


@app.get("/api/organization/resources")
def organization_resources(owner=Depends(current_owner)):
    with database() as connection:
        payload = load_owner_snapshot(connection, owner["id"])
    browsers = [
        {
            "key": key,
            "name": record.get("name", key),
            "group": record.get("group", ""),
            "port": record.get("port", ""),
            "home": record.get("home", ""),
        }
        for key, record in payload.get("browsers", {}).items()
    ]
    vault = [
        {
            "key": vault_resource_key(record),
            "site": record.get("site", ""),
            "url": record.get("url", ""),
            "username": record.get("username", ""),
            "password": record.get("password", ""),
            "browser_key": record.get("browser_key", ""),
            "note": record.get("note", ""),
        }
        for record in payload.get("vault", [])
    ]
    return {
        "browsers": browsers,
        "vault": vault,
        "updated_at": payload.get("updated_at", ""),
    }


@app.get("/api/organization/members")
def organization_members(owner=Depends(current_owner)):
    with database() as connection:
        rows = connection.execute(
            """
            SELECT users.id, users.username, users.display_name, users.created_at,
                   users.disabled,
                   SUM(CASE WHEN resource_assignments.resource_type='browser'
                       THEN 1 ELSE 0 END) AS browser_count,
                   SUM(CASE WHEN resource_assignments.resource_type='vault'
                       THEN 1 ELSE 0 END) AS vault_count
            FROM users
            LEFT JOIN resource_assignments
                ON resource_assignments.member_id=users.id
            WHERE users.parent_user_id=? AND users.role='member'
            GROUP BY users.id
            ORDER BY users.id DESC
            """,
            (owner["id"],),
        ).fetchall()
    return {"members": [dict(row) for row in rows]}


@app.post("/api/organization/members")
def create_member(member: MemberCreate, owner=Depends(current_owner)):
    username = member.username.strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise HTTPException(
            status_code=400,
            detail="用户名需为 3-32 位中文、字母、数字、下划线或短横线。",
        )
    with database() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS value FROM users WHERE parent_user_id=?",
            (owner["id"],),
        ).fetchone()["value"]
        if count >= 100:
            raise HTTPException(status_code=400, detail="每个主账号最多创建 100 个子账号。")
        try:
            cursor = connection.execute(
                """
                INSERT INTO users(
                    username, password_hash, created_at, is_admin, disabled,
                    role, parent_user_id, display_name
                ) VALUES (?, ?, ?, 0, 0, 'member', ?, ?)
                """,
                (
                    username,
                    password_hash(member.password),
                    timestamp(),
                    owner["id"],
                    member.display_name.strip(),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise HTTPException(status_code=409, detail="用户名已存在。") from error
    return {"ok": True, "id": cursor.lastrowid}


@app.get("/api/organization/members/{member_id}")
def get_member(member_id: int, owner=Depends(current_owner)):
    with database() as connection:
        member = member_for_owner(connection, owner["id"], member_id)
        assignments = member_assignment_sets(connection, member_id)
    return {
        "member": dict(member),
        "browser_keys": sorted(assignments["browser"]),
        "vault_keys": sorted(assignments["vault"]),
    }


@app.put("/api/organization/members/{member_id}")
def update_member(
    member_id: int,
    update: MemberUpdate,
    owner=Depends(current_owner),
):
    with database() as connection:
        member_for_owner(connection, owner["id"], member_id)
        connection.execute(
            "UPDATE users SET display_name=?, disabled=? WHERE id=?",
            (update.display_name.strip(), int(update.disabled), member_id),
        )
        if update.disabled:
            connection.execute("DELETE FROM sessions WHERE user_id=?", (member_id,))
    return {"ok": True}


@app.put("/api/organization/members/{member_id}/password")
def reset_member_password(
    member_id: int,
    reset: PasswordReset,
    owner=Depends(current_owner),
):
    with database() as connection:
        member_for_owner(connection, owner["id"], member_id)
        connection.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (password_hash(reset.password), member_id),
        )
        connection.execute("DELETE FROM sessions WHERE user_id=?", (member_id,))
    return {"ok": True}


@app.put("/api/organization/members/{member_id}/assignments")
def assign_member_resources(
    member_id: int,
    assignments: ResourceAssignments,
    owner=Depends(current_owner),
):
    browser_keys = {str(value) for value in assignments.browser_keys}
    vault_keys = {str(value) for value in assignments.vault_keys}
    with database() as connection:
        member_for_owner(connection, owner["id"], member_id)
        resources = load_owner_snapshot(connection, owner["id"])
        valid_browsers = set(resources.get("browsers", {}))
        valid_vault = {
            vault_resource_key(record) for record in resources.get("vault", [])
        }
        if not browser_keys <= valid_browsers or not vault_keys <= valid_vault:
            raise HTTPException(status_code=400, detail="授权资源不存在或已经失效。")
        connection.execute(
            "DELETE FROM resource_assignments WHERE member_id=?", (member_id,)
        )
        rows = [
            (member_id, "browser", key, timestamp()) for key in sorted(browser_keys)
        ]
        rows += [
            (member_id, "vault", key, timestamp()) for key in sorted(vault_keys)
        ]
        connection.executemany(
            """
            INSERT INTO resource_assignments(
                member_id, resource_type, resource_key, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            rows,
        )
    return {
        "ok": True,
        "browser_count": len(browser_keys),
        "vault_count": len(vault_keys),
    }


@app.delete("/api/organization/members/{member_id}")
def delete_member(member_id: int, owner=Depends(current_owner)):
    with database() as connection:
        member_for_owner(connection, owner["id"], member_id)
        connection.execute("DELETE FROM users WHERE id=?", (member_id,))
    return {"ok": True}


@app.get("/api/admin/summary")
def admin_summary(_admin=Depends(current_admin)):
    with database() as connection:
        totals = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM users
               WHERE role='owner' AND is_admin=0) AS owner_count,
              (SELECT COUNT(*) FROM users
               WHERE role='member' AND is_admin=0) AS member_count,
              (SELECT COUNT(*) FROM users
               WHERE is_admin=0 AND disabled=1) AS disabled_count,
              COALESCE((SELECT SUM(browser_count) FROM snapshots), 0)
                  AS browser_count,
              COALESCE((SELECT SUM(vault_count) FROM snapshots), 0)
                  AS vault_count
            """
        ).fetchone()
    return dict(totals)


@app.get("/api/admin/users")
def admin_users(_admin=Depends(current_admin)):
    with database() as connection:
        rows = connection.execute(
            """
            SELECT users.id, users.username, users.display_name, users.role,
                   users.parent_user_id, owners.username AS owner_username,
                   users.created_at, users.disabled,
                   CASE WHEN users.role='owner'
                     THEN COALESCE(snapshots.browser_count, 0)
                     ELSE 0 END AS browser_count,
                   CASE WHEN users.role='owner'
                     THEN COALESCE(snapshots.vault_count, 0)
                     ELSE 0 END AS vault_count,
                   COALESCE(snapshots.updated_at, '') AS updated_at
            FROM users
            LEFT JOIN users owners ON owners.id=users.parent_user_id
            LEFT JOIN snapshots ON snapshots.user_id=users.id
            WHERE users.is_admin=0
            ORDER BY users.id DESC
            """
        ).fetchall()
    return {"users": [dict(row) for row in rows]}


@app.get("/api/admin/users/{user_id}")
def admin_user_detail(user_id: int, _admin=Depends(current_admin)):
    with database() as connection:
        row = connection.execute(
            """
            SELECT id, username, display_name, role, parent_user_id,
                   created_at, disabled
            FROM users WHERE id=? AND is_admin=0
            """,
            (user_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在。")
        user = dict(row)
        payload = authorized_snapshot(connection, user)
    payload["user"] = user
    return payload


@app.post("/api/admin/users/{user_id}/toggle-disabled")
def admin_toggle_user(user_id: int, _admin=Depends(current_admin)):
    with database() as connection:
        user = connection.execute(
            "SELECT disabled FROM users WHERE id=? AND is_admin=0", (user_id,)
        ).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在。")
        disabled = 0 if user["disabled"] else 1
        connection.execute(
            "UPDATE users SET disabled=? WHERE id=?", (disabled, user_id)
        )
        if disabled:
            connection.execute(
                """
                DELETE FROM sessions
                WHERE user_id=? OR user_id IN (
                    SELECT id FROM users WHERE parent_user_id=?
                )
                """,
                (user_id, user_id),
            )
    return {"ok": True, "disabled": bool(disabled)}


@app.put("/api/admin/users/{user_id}/password")
def admin_reset_password(
    user_id: int,
    reset: PasswordReset,
    _admin=Depends(current_admin),
):
    with database() as connection:
        user = connection.execute(
            "SELECT id FROM users WHERE id=? AND is_admin=0", (user_id,)
        ).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在。")
        connection.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (password_hash(reset.password), user_id),
        )
        connection.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    return {"ok": True}


@app.delete("/api/admin/users/{user_id}/snapshot")
def admin_clear_snapshot(user_id: int, _admin=Depends(current_admin)):
    with database() as connection:
        user = connection.execute(
            "SELECT id, role FROM users WHERE id=? AND is_admin=0", (user_id,)
        ).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在。")
        if user["role"] != "owner":
            raise HTTPException(status_code=400, detail="子账号没有独立同步快照。")
        connection.execute("DELETE FROM snapshots WHERE user_id=?", (user_id,))
    return {"ok": True}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, _admin=Depends(current_admin)):
    with database() as connection:
        user = connection.execute(
            "SELECT id FROM users WHERE id=? AND is_admin=0", (user_id,)
        ).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在。")
        connection.execute(
            "DELETE FROM users WHERE id=? OR parent_user_id=?", (user_id, user_id)
        )
    return {"ok": True}


@app.post("/api/logout")
def logout(user=Depends(current_user)):
    with database() as connection:
        connection.execute(
            "DELETE FROM sessions WHERE token_hash=?", (user["token_hash"],)
        )
    return {"ok": True}


@app.put("/api/sync")
def save_snapshot(snapshot: Snapshot, user=Depends(current_user)):
    if user["role"] != "owner" or user["is_admin"]:
        raise HTTPException(status_code=403, detail="子账号只有查看权限，不能上传或修改数据。")
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
            INSERT INTO snapshots(
                user_id, encrypted_payload, browser_count, vault_count, updated_at
            ) VALUES (?, ?, ?, ?, ?)
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
    if user["is_admin"]:
        raise HTTPException(status_code=403, detail="系统管理员不使用客户端同步。")
    with database() as connection:
        payload = authorized_snapshot(connection, user)
        owner_username = user["username"]
        if user["role"] == "member":
            owner = connection.execute(
                "SELECT username FROM users WHERE id=?", (user["parent_user_id"],)
            ).fetchone()
            owner_username = owner["username"] if owner else ""
    payload["access"] = {
        "role": user["role"],
        "read_only": user["role"] == "member",
        "owner_username": owner_username,
    }
    return payload
