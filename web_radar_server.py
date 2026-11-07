"""
ReaderRadar Web Server
Run:  uvicorn web_radar_server:app --host 0.0.0.0 --port 8080
      (or: python web_radar_server.py)

Data flow:
  Reader laptop  →  POST /push (X-Push-Key header)  →  this server
  Browsers       →  WebSocket /ws (cookie auth)      →  live entity feed

On first run a config file is written with a generated push key and
default admin credentials.  Change the admin password immediately via
the /admin panel.
"""

import asyncio
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import (Cookie, Depends, FastAPI, Form, HTTPException,
                     Request, WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

BASE       = Path(__file__).parent
WEB_STATIC = BASE / "web_static"
DB_PATH    = BASE / "radar_users.db"
CFG_PATH   = BASE / "web_radar_config.json"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if CFG_PATH.exists():
        return json.loads(CFG_PATH.read_text(encoding="utf-8"))
    cfg = {
        "push_key":           secrets.token_hex(32),
        "admin_username":     "admin",
        "admin_password_hash": _hash_pw("changeme"),
        "world_size":         3500,
        "session_hours":      72,
        "server_name":        "My Rust Server",
    }
    CFG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"\n[CONFIG] Created {CFG_PATH}")
    print(f"[CONFIG] Push key (put in web_radar_push.py): {cfg['push_key']}\n")
    return cfg

CONFIG: dict = {}

# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def _hash_pw(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}:{h.hex()}"

def _verify_pw(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
        expected = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
        return hmac.compare_digest(expected.hex(), h)
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def _init_db():
    with _db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY,
                username      TEXT    UNIQUE NOT NULL,
                password_hash TEXT    NOT NULL,
                is_admin      INTEGER DEFAULT 0,
                is_revoked    INTEGER DEFAULT 0,
                created_at    REAL    DEFAULT (unixepoch()),
                last_seen     REAL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT    PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                created_at REAL    DEFAULT (unixepoch()),
                expires_at REAL    NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
        """)
        db.execute(
            "INSERT OR IGNORE INTO users (username,password_hash,is_admin) VALUES (?,?,1)",
            (CONFIG["admin_username"], CONFIG["admin_password_hash"]),
        )

def _create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    exp   = time.time() + CONFIG.get("session_hours", 72) * 3600
    with _db() as db:
        db.execute("INSERT INTO sessions VALUES (?,?,?,?)",
                   (token, user_id, time.time(), exp))
    return token

def _user_from_token(token: str) -> Optional[dict]:
    if not token:
        return None
    with _db() as db:
        row = db.execute("""
            SELECT u.id, u.username, u.is_admin, u.is_revoked
            FROM   sessions s JOIN users u ON s.user_id = u.id
            WHERE  s.token = ? AND s.expires_at > ?
        """, (token, time.time())).fetchone()
    if not row or row["is_revoked"]:
        return None
    with _db() as db:
        db.execute("UPDATE users SET last_seen=? WHERE id=?", (time.time(), row["id"]))
    return dict(row)

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

latest_snapshot: dict = {}
ws_clients: set[WebSocket] = set()

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    CONFIG.update(_load_config())
    _init_db()
    yield

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/web_static", StaticFiles(directory=str(WEB_STATIC)), name="static")

# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------

def _require_user(radar_session: Optional[str] = Cookie(default=None)):
    u = _user_from_token(radar_session or "")
    if not u:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return u

def _require_admin(u=Depends(_require_user)):
    if not u["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin only")
    return u

# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return RedirectResponse("/radar")

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return FileResponse(WEB_STATIC / "login.html")

@app.post("/login")
async def do_login(username: str = Form(), password: str = Form()):
    with _db() as db:
        row = db.execute(
            "SELECT id,password_hash,is_revoked FROM users WHERE username=?",
            (username,)).fetchone()
    if not row or not _verify_pw(password, row["password_hash"]) or row["is_revoked"]:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = _create_session(row["id"])
    resp  = RedirectResponse("/radar", status_code=303)
    resp.set_cookie("radar_session", token, httponly=True,
                    samesite="lax", max_age=259200)
    return resp

@app.post("/logout")
async def logout(radar_session: Optional[str] = Cookie(default=None)):
    if radar_session:
        with _db() as db:
            db.execute("DELETE FROM sessions WHERE token=?", (radar_session,))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("radar_session")
    return resp

# ---------------------------------------------------------------------------
# Main pages
# ---------------------------------------------------------------------------

@app.get("/radar", response_class=HTMLResponse)
async def radar_page(u=Depends(_require_user)):
    return FileResponse(WEB_STATIC / "radar.html")

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(u=Depends(_require_admin)):
    return FileResponse(WEB_STATIC / "admin.html")

@app.get("/overlay", response_class=HTMLResponse)
async def overlay_page(u=Depends(_require_user)):
    return FileResponse(WEB_STATIC / "overlay.html")

# ---------------------------------------------------------------------------
# Push endpoint  (reader laptop → server)
# ---------------------------------------------------------------------------

@app.post("/push")
async def push_snapshot(request: Request):
    key = request.headers.get("X-Push-Key", "")
    if not hmac.compare_digest(key, CONFIG.get("push_key", "")):
        raise HTTPException(status_code=403)
    global latest_snapshot
    latest_snapshot = await request.json()
    if ws_clients:
        msg  = json.dumps(latest_snapshot)
        dead: set[WebSocket] = set()
        for ws in list(ws_clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        ws_clients -= dead
    return {"ok": True, "clients": len(ws_clients)}

# ---------------------------------------------------------------------------
# WebSocket  (server → browsers)
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_feed(ws: WebSocket):
    token = ws.cookies.get("radar_session", "")
    user  = _user_from_token(token)
    if not user:
        await ws.close(code=4001)
        return
    await ws.accept()
    ws_clients.add(ws)
    if latest_snapshot:
        await ws.send_text(json.dumps(latest_snapshot))
    try:
        while True:
            await ws.receive_text()       # keepalive pings from client
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(ws)

# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------

@app.get("/api/users")
async def list_users(admin=Depends(_require_admin)):
    with _db() as db:
        rows = db.execute(
            "SELECT id,username,is_admin,is_revoked,created_at,last_seen "
            "FROM users ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]

@app.post("/api/users")
async def add_user(data: dict, admin=Depends(_require_admin)):
    username = str(data.get("username","")).strip()
    password = str(data.get("password","")).strip()
    if not username or not password:
        raise HTTPException(400, "username and password required")
    try:
        with _db() as db:
            db.execute("INSERT INTO users (username,password_hash) VALUES (?,?)",
                       (username, _hash_pw(password)))
        return {"ok": True}
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Username already exists")

@app.post("/api/users/{uid}/revoke")
async def revoke_user(uid: int, admin=Depends(_require_admin)):
    with _db() as db:
        db.execute("UPDATE users SET is_revoked=1 WHERE id=? AND is_admin=0", (uid,))
        db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    return {"ok": True}

@app.post("/api/users/{uid}/restore")
async def restore_user(uid: int, admin=Depends(_require_admin)):
    with _db() as db:
        db.execute("UPDATE users SET is_revoked=0 WHERE id=?", (uid,))
    return {"ok": True}

@app.post("/api/users/{uid}/reset-password")
async def reset_password(uid: int, data: dict, admin=Depends(_require_admin)):
    pw = str(data.get("password","")).strip()
    if not pw:
        raise HTTPException(400, "password required")
    with _db() as db:
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (_hash_pw(pw), uid))
        db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    return {"ok": True}

@app.get("/api/config")
async def client_config(u=Depends(_require_user)):
    return {
        "world_size":   CONFIG.get("world_size", 3500),
        "server_name":  CONFIG.get("server_name", ""),
        "has_map":      (WEB_STATIC / "rust_map.png").exists(),
    }

# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
