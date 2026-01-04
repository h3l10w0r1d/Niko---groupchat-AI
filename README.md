# Niko---groupchat-AI
A powerful quiet telegram listener with lots of interesting features inside. It is designed to help small teams to easily organise and summarise all messages. Works on the core of OpenAI API and can be used by erveryone. 


# Niko Telegram Memory Bot (Render + Postgres + pgvector)

## What it does
- Stores all group messages in Postgres
- Optionally stores embeddings in pgvector for memory retrieval
- Answers `/Niko {question}` using relevant chat history
- Admin panel to:
  - view groups
  - toggle memory per group
  - set rate limits per group
  - broadcast message to admins

## Setup steps

### 1) Telegram bot
- Create bot via @BotFather
- IMPORTANT: in @BotFather run `/setprivacy` and set to **Disable** (or make bot admin) so it can read group messages.
- Get:
  - TELEGRAM_BOT_TOKEN
  - TELEGRAM_BOT_USERNAME (without @)

### 2) Postgres
- Create Render Postgres
- Run `db/schema.sql` once (enable pgvector + tables)

### 3) Admin allowlist
Insert your admin user ids:
```sql
INSERT INTO admins(user_id) VALUES (123456789), (987654321)
ON CONFLICT (user_id) DO NOTHING;
