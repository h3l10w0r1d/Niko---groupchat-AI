import os, hmac, hashlib, json
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from fastapi import FastAPI, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from telegram import Bot

DATABASE_URL = os.getenv("DATABASE_URL", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "")  # e.g. niko_memory_bot
SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "change-me")

if not DATABASE_URL or not BOT_TOKEN or not BOT_USERNAME:
    raise RuntimeError("Missing DATABASE_URL / TELEGRAM_BOT_TOKEN / TELEGRAM_BOT_USERNAME")

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", https_only=True)

templates = Jinja2Templates(directory="templates")
bot = Bot(token=BOT_TOKEN)

pool: asyncpg.Pool | None = None

@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

def _verify_telegram_login(payload: dict, bot_token: str, max_age_sec: int = 86400) -> dict:
    """
    Verify Telegram Login Widget payload per official docs:
    - build data_check_string from sorted key=value pairs (excluding hash)
    - secret_key = SHA256(bot_token)
    - HMAC_SHA256(data_check_string, secret_key) hex must equal payload['hash']
     [oai_citation:3‡core.telegram.org](https://core.telegram.org/widgets/login?utm_source=chatgpt.com)
    """
    if "hash" not in payload or "auth_date" not in payload:
        raise HTTPException(400, "Invalid Telegram auth payload")

    auth_date = int(payload["auth_date"])
    now = int(datetime.now(timezone.utc).timestamp())
    if now - auth_date > max_age_sec:
        raise HTTPException(401, "Telegram auth expired")

    their_hash = payload["hash"]
    data = {k: str(v) for k, v in payload.items() if k != "hash"}
    data_check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data.keys()))

    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calc_hash, their_hash):
        raise HTTPException(401, "Telegram auth failed")

    return payload

async def require_admin(request: Request) -> int:
    user_id = request.session.get("admin_user_id")
    if not user_id:
        raise HTTPException(401, "Not logged in")

    assert pool is not None
    row = await pool.fetchrow("SELECT enabled FROM admins WHERE user_id=$1", int(user_id))
    if not row or not row["enabled"]:
        raise HTTPException(403, "Not an admin")
    return int(user_id)

@app.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "bot_username": BOT_USERNAME},
    )

@app.get("/auth/telegram")
async def auth_telegram(request: Request):
    payload = dict(request.query_params)
    verified = _verify_telegram_login(payload, BOT_TOKEN)

    user_id = int(verified["id"])
    assert pool is not None
    ok = await pool.fetchval("SELECT 1 FROM admins WHERE user_id=$1 AND enabled=true", user_id)
    if not ok:
        raise HTTPException(403, "You are not allowed")

    request.session["admin_user_id"] = user_id
    return RedirectResponse("/", status_code=302)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, admin_user_id: int = Depends(require_admin)):
    assert pool is not None
    chats = await pool.fetch(
        """
        SELECT c.chat_id, c.chat_type, c.title, c.username, c.last_seen_utc,
               COALESCE(s.memory_enabled, true) AS memory_enabled,
               COALESCE(s.max_requests_per_minute, 10) AS rpm
        FROM chats c
        LEFT JOIN chat_settings s ON s.chat_id=c.chat_id
        ORDER BY c.last_seen_utc DESC
        LIMIT 200
        """
    )
    return templates.TemplateResponse("dashboard.html", {"request": request, "chats": chats})

@app.post("/api/chats/{chat_id}/settings")
async def update_chat_settings(
    request: Request,
    chat_id: int,
    memory_enabled: bool = Form(...),
    max_requests_per_minute: int = Form(...),
    max_requests_per_day: int = Form(...),
    admin_user_id: int = Depends(require_admin),
):
    assert pool is not None
    await pool.execute(
        """
        INSERT INTO chat_settings(chat_id, memory_enabled, max_requests_per_minute, max_requests_per_day)
        VALUES ($1,$2,$3,$4)
        ON CONFLICT (chat_id) DO UPDATE SET
          memory_enabled=EXCLUDED.memory_enabled,
          max_requests_per_minute=EXCLUDED.max_requests_per_minute,
          max_requests_per_day=EXCLUDED.max_requests_per_day
        """,
        chat_id, memory_enabled, max_requests_per_minute, max_requests_per_day
    )
    await pool.execute(
        "INSERT INTO audit_log(admin_user_id, action, details) VALUES ($1,$2,$3)",
        admin_user_id, "update_chat_settings", json.dumps({"chat_id": chat_id})
    )
    return RedirectResponse("/", status_code=303)

@app.post("/api/admin/notify")
async def notify_admins(
    request: Request,
    message: str = Form(...),
    admin_user_id: int = Depends(require_admin),
):
    """
    Send a DM to every admin that has dm_chat_id recorded.
    (Admins must /start the bot in DM at least once.)
    """
    assert pool is not None
    rows = await pool.fetch("SELECT dm_chat_id FROM admins WHERE enabled=true AND dm_chat_id IS NOT NULL")
    for r in rows:
        try:
            await bot.send_message(chat_id=int(r["dm_chat_id"]), text=message)
        except Exception:
            pass
    await pool.execute(
        "INSERT INTO audit_log(admin_user_id, action, details) VALUES ($1,$2,$3)",
        admin_user_id, "notify_admins", json.dumps({"len": len(rows)})
    )
    return {"ok": True, "sent": len(rows)}
