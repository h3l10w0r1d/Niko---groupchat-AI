import os
import re
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import asyncpg
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("niko-bot-worker")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.2")
OPENAI_EMBED_MODEL = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")
DATABASE_URL = os.getenv("DATABASE_URL", "")
EMBED_DIMS = int(os.getenv("EMBED_DIMS", "1536"))

# Retrieval knobs
TOP_K = int(os.getenv("TOP_K", "10"))

# Embedding batching
BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "32"))
BATCH_FLUSH_SECONDS = float(os.getenv("EMBED_BATCH_FLUSH_SECONDS", "1.0"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY")
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL")

openai_client = OpenAI(api_key=OPENAI_API_KEY)

pool: Optional[asyncpg.Pool] = None
embed_queue: asyncio.Queue[dict] = asyncio.Queue()

# ----------------------------
# Utilities
# ----------------------------
def vector_literal(vec: list[float]) -> str:
    # pgvector input format: '[0.1,0.2,...]'
    return "[" + ",".join(f"{v:.8f}" for v in vec) + "]"

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def safe_text(s: Optional[str]) -> str:
    return (s or "").strip()

# ----------------------------
# OpenAI helpers
# ----------------------------
def get_embeddings(texts: list[str]) -> list[list[float]]:
    resp = openai_client.embeddings.create(
        model=OPENAI_EMBED_MODEL,
        input=texts,
        encoding_format="float",
    )
    return [x.embedding for x in resp.data]

def generate_answer(question: str, memory_snippets: str) -> str:
    instructions = (
        "You are Niko, a helpful assistant.\n"
        "Answer using ONLY the provided chat excerpts as evidence.\n"
        "If excerpts are insufficient, say you don't know from this chat.\n"
        "Be concise and practical."
    )
    user_input = (
        f"Question:\n{question}\n\n"
        f"Relevant chat excerpts:\n{memory_snippets}\n\n"
        "Answer:"
    )
    resp = openai_client.responses.create(
        model=OPENAI_MODEL,
        instructions=instructions,
        input=user_input,
    )
    return (resp.output_text or "").strip()

# ----------------------------
# DB init
# ----------------------------
async def init_pool_and_schema():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    # Minimal safety: ensure extension/tables exist.
    # In production, you can remove this and run db/schema.sql once.
    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
          chat_id BIGINT NOT NULL,
          message_id BIGINT NOT NULL,
          user_id BIGINT,
          username TEXT,
          full_name TEXT,
          text TEXT,
          date_utc TIMESTAMPTZ,
          PRIMARY KEY (chat_id, message_id)
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS chats (
          chat_id BIGINT PRIMARY KEY,
          chat_type TEXT NOT NULL,
          title TEXT,
          username TEXT,
          last_seen_utc TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_settings (
          chat_id BIGINT PRIMARY KEY REFERENCES chats(chat_id) ON DELETE CASCADE,
          memory_enabled BOOLEAN NOT NULL DEFAULT TRUE,
          max_requests_per_minute INT NOT NULL DEFAULT 10,
          max_requests_per_day INT NOT NULL DEFAULT 500
        );
        """)

        await conn.execute(f"""
        CREATE TABLE IF NOT EXISTS message_embeddings (
          chat_id BIGINT NOT NULL,
          message_id BIGINT NOT NULL,
          embedding vector({EMBED_DIMS}) NOT NULL,
          date_utc TIMESTAMPTZ,
          username TEXT,
          full_name TEXT,
          text TEXT,
          PRIMARY KEY (chat_id, message_id),
          CONSTRAINT fk_msg FOREIGN KEY (chat_id, message_id)
            REFERENCES messages(chat_id, message_id)
            ON DELETE CASCADE
        );
        """)

        await conn.execute("""
        CREATE INDEX IF NOT EXISTS message_embeddings_ivfflat
        ON message_embeddings
        USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100);
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS niko_usage (
          id BIGSERIAL PRIMARY KEY,
          chat_id BIGINT NOT NULL,
          user_id BIGINT NOT NULL,
          ts TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """)

        await conn.execute("CREATE INDEX IF NOT EXISTS niko_usage_chat_ts ON niko_usage(chat_id, ts);")

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS admins (
          user_id BIGINT PRIMARY KEY,
          dm_chat_id BIGINT,
          enabled BOOLEAN NOT NULL DEFAULT TRUE
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
          id BIGSERIAL PRIMARY KEY,
          ts TIMESTAMPTZ NOT NULL DEFAULT now(),
          admin_user_id BIGINT NOT NULL,
          action TEXT NOT NULL,
          details JSONB NOT NULL DEFAULT '{}'::jsonb
        );
        """)

# ----------------------------
# DB operations
# ----------------------------
async def upsert_chat(conn: asyncpg.Connection, chat) -> None:
    title = safe_text(getattr(chat, "title", None))
    username = safe_text(getattr(chat, "username", None))
    chat_type = str(chat.type) if chat.type else "unknown"

    await conn.execute(
        """
        INSERT INTO chats(chat_id, chat_type, title, username, last_seen_utc)
        VALUES ($1,$2,$3,$4,now())
        ON CONFLICT (chat_id) DO UPDATE SET
          chat_type=EXCLUDED.chat_type,
          title=EXCLUDED.title,
          username=EXCLUDED.username,
          last_seen_utc=now()
        """,
        chat.id, chat_type, title, username
    )

    # Ensure settings row exists
    await conn.execute(
        """
        INSERT INTO chat_settings(chat_id)
        VALUES ($1)
        ON CONFLICT (chat_id) DO NOTHING
        """,
        chat.id
    )

async def get_chat_settings(conn: asyncpg.Connection, chat_id: int) -> dict:
    row = await conn.fetchrow(
        """
        SELECT memory_enabled, max_requests_per_minute, max_requests_per_day
        FROM chat_settings
        WHERE chat_id=$1
        """,
        chat_id
    )
    if not row:
        # Shouldn't happen due to upsert_chat, but safe default
        return {"memory_enabled": True, "max_requests_per_minute": 10, "max_requests_per_day": 500}
    return dict(row)

async def check_and_record_rate_limit(conn: asyncpg.Connection, chat_id: int, user_id: int) -> tuple[bool, dict]:
    """
    Returns (allowed, info)
    info includes: minute_count, day_count, rpm_limit, rpd_limit
    """
    settings = await get_chat_settings(conn, chat_id)
    rpm = int(settings["max_requests_per_minute"])
    rpd = int(settings["max_requests_per_day"])

    t_now = now_utc()
    t_minute = t_now - timedelta(seconds=60)
    t_day = t_now - timedelta(hours=24)

    async with conn.transaction():
        minute_count = await conn.fetchval(
            "SELECT COUNT(*) FROM niko_usage WHERE chat_id=$1 AND ts >= $2",
            chat_id, t_minute
        )
        day_count = await conn.fetchval(
            "SELECT COUNT(*) FROM niko_usage WHERE chat_id=$1 AND ts >= $2",
            chat_id, t_day
        )

        allowed = (minute_count < rpm) and (day_count < rpd)
        if allowed:
            await conn.execute(
                "INSERT INTO niko_usage(chat_id, user_id, ts) VALUES ($1,$2,now())",
                chat_id, user_id
            )

    return allowed, {
        "minute_count": int(minute_count),
        "day_count": int(day_count),
        "rpm_limit": rpm,
        "rpd_limit": rpd,
    }

# ----------------------------
# Embedding worker
# ----------------------------
async def embed_worker():
    assert pool is not None
    log.info("Embedding worker started")

    while True:
        batch = [await embed_queue.get()]
        start = asyncio.get_running_loop().time()

        while len(batch) < BATCH_SIZE:
            remaining = BATCH_FLUSH_SECONDS - (asyncio.get_running_loop().time() - start)
            if remaining <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(embed_queue.get(), timeout=remaining))
            except asyncio.TimeoutError:
                break

        texts = [b["text"] for b in batch]

        try:
            vectors = await asyncio.to_thread(get_embeddings, texts)

            async with pool.acquire() as conn:
                async with conn.transaction():
                    for item, vec in zip(batch, vectors):
                        if len(vec) != EMBED_DIMS:
                            raise RuntimeError(f"Embedding dims {len(vec)} != EMBED_DIMS {EMBED_DIMS}")

                        await conn.execute(
                            """
                            INSERT INTO message_embeddings
                              (chat_id, message_id, embedding, date_utc, username, full_name, text)
                            VALUES ($1,$2,$3::vector,$4,$5,$6,$7)
                            ON CONFLICT (chat_id, message_id) DO UPDATE SET
                              embedding=EXCLUDED.embedding,
                              date_utc=EXCLUDED.date_utc,
                              username=EXCLUDED.username,
                              full_name=EXCLUDED.full_name,
                              text=EXCLUDED.text
                            """,
                            item["chat_id"],
                            item["message_id"],
                            vector_literal(vec),
                            item["date_utc"],
                            item["username"],
                            item["full_name"],
                            item["text"],
                        )
        except Exception:
            log.exception("Embedding batch failed")
        finally:
            for _ in batch:
                embed_queue.task_done()

# ----------------------------
# Telegram handlers
# ----------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    In private chat, admins can /start so we store their dm_chat_id for panel broadcasts.
    """
    assert pool is not None
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return

    if chat.type != ChatType.PRIVATE:
        await msg.reply_text("Hi! I’m Niko. Use /Niko {question} in a group I’m in.")
        return

    async with pool.acquire() as conn:
        is_admin = await conn.fetchval(
            "SELECT 1 FROM admins WHERE user_id=$1 AND enabled=true",
            user.id
        )
        if is_admin:
            await conn.execute(
                "UPDATE admins SET dm_chat_id=$1 WHERE user_id=$2",
                chat.id, user.id
            )
            await msg.reply_text("Admin DM linked ✅")
        else:
            await msg.reply_text("Hi! I’m Niko. You’re not on the admin list.")

async def on_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Store all text messages in Postgres. Optionally embed them based on chat_settings.memory_enabled.
    """
    assert pool is not None
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return

    text = safe_text(msg.text)
    if not text:
        return

    # Avoid duplicating /Niko itself (optional)
    if text.lower().startswith("/niko"):
        return

    date_utc = msg.date.astimezone(timezone.utc) if msg.date else now_utc()

    async with pool.acquire() as conn:
        await upsert_chat(conn, chat)

        await conn.execute(
            """
            INSERT INTO messages(chat_id, message_id, user_id, username, full_name, text, date_utc)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (chat_id, message_id) DO UPDATE SET
              text=EXCLUDED.text,
              date_utc=EXCLUDED.date_utc
            """,
            chat.id, msg.message_id, user.id,
            safe_text(user.username), safe_text(user.full_name),
            text, date_utc
        )

        settings = await get_chat_settings(conn, chat.id)
        memory_enabled = bool(settings["memory_enabled"])

    # Only embed if memory is enabled
    if memory_enabled:
        await embed_queue.put({
            "chat_id": chat.id,
            "message_id": msg.message_id,
            "username": safe_text(user.username),
            "full_name": safe_text(user.full_name),
            "text": text,
            "date_utc": date_utc,
        })

async def niko_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /Niko {question}
    - enforce per-group rate limits
    - retrieve from pgvector (this group only)
    - answer using OpenAI
    """
    assert pool is not None
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return

    raw = msg.text or ""
    m = re.match(r"^/niko(?:@\w+)?\s+(.+)$", raw, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        await msg.reply_text("Usage: /Niko your question here")
        return

    question = m.group(1).strip()
    if not question:
        await msg.reply_text("Usage: /Niko your question here")
        return

    async with pool.acquire() as conn:
        await upsert_chat(conn, chat)

        settings = await get_chat_settings(conn, chat.id)
        if not settings["memory_enabled"]:
            await msg.reply_text("Memory is disabled for this group by the admins.")
            return

        allowed, info = await check_and_record_rate_limit(conn, chat.id, user.id)
        if not allowed:
            await msg.reply_text(
                f"Rate limit hit.\n"
                f"Last 60s: {info['minute_count']}/{info['rpm_limit']}\n"
                f"Last 24h: {info['day_count']}/{info['rpd_limit']}"
            )
            return

    # Embed the question
    try:
        qvec = (await asyncio.to_thread(get_embeddings, [question]))[0]
        if len(qvec) != EMBED_DIMS:
            raise RuntimeError(f"Question embed dims {len(qvec)} != {EMBED_DIMS}")
        qlit = vector_literal(qvec)
    except Exception:
        log.exception("Embedding question failed")
        await msg.reply_text("Embedding failed (OpenAI error).")
        return

    # Retrieve top-k relevant messages from THIS chat_id
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT date_utc, username, full_name, text
                FROM message_embeddings
                WHERE chat_id = $1
                ORDER BY embedding <-> $2::vector
                LIMIT $3
                """,
                chat.id, qlit, TOP_K
            )
    except Exception:
        log.exception("Vector search failed")
        await msg.reply_text("Memory search failed (database error).")
        return

    if not rows:
        await msg.reply_text("I don't have any saved memories for this group yet.")
        return

    lines = []
    for r in rows:
        dt = r["date_utc"].isoformat() if r["date_utc"] else ""
        who = r["username"] or r["full_name"] or "someone"
        if r["username"]:
            who = "@" + r["username"]
        lines.append(f"- [{dt}] {who}: {r['text']}")

    memory_snippets = "\n".join(lines)

    # Generate answer
    try:
        answer = await asyncio.to_thread(generate_answer, question, memory_snippets)
    except Exception:
        log.exception("Answer generation failed")
        await msg.reply_text("OpenAI request failed.")
        return

    if not answer:
        answer = "I couldn't generate an answer from the saved excerpts."

    # Telegram length safety
    for i in range(0, len(answer), 3500):
        await msg.reply_text(answer[i:i+3500])

# ----------------------------
# Main
# ----------------------------
async def main():
    await init_pool_and_schema()
    asyncio.create_task(embed_worker())

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("Niko", niko_command))

    # Save all text (including other commands if you want)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_any_message))
    app.add_handler(MessageHandler(filters.COMMAND & ~filters.Regex(r"^/niko"), on_any_message))

    log.info("Niko worker running (polling)...")
    await app.run_polling(close_loop=False)

if __name__ == "__main__":
    asyncio.run(main())
