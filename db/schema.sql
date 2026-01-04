-- Enable pgvector
CREATE EXTENSION IF NOT EXISTS vector;

-- Raw messages
CREATE TABLE IF NOT EXISTS messages (
  chat_id      BIGINT NOT NULL,
  message_id   BIGINT NOT NULL,
  user_id      BIGINT,
  username     TEXT,
  full_name    TEXT,
  text         TEXT,
  date_utc     TIMESTAMPTZ,
  PRIMARY KEY (chat_id, message_id)
);

-- Chats/groups the bot has seen
CREATE TABLE IF NOT EXISTS chats (
  chat_id       BIGINT PRIMARY KEY,
  chat_type     TEXT NOT NULL,
  title         TEXT,
  username      TEXT,
  last_seen_utc TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-chat settings
CREATE TABLE IF NOT EXISTS chat_settings (
  chat_id BIGINT PRIMARY KEY REFERENCES chats(chat_id) ON DELETE CASCADE,
  memory_enabled BOOLEAN NOT NULL DEFAULT TRUE,
  max_requests_per_minute INT NOT NULL DEFAULT 10,
  max_requests_per_day INT NOT NULL DEFAULT 500
);

-- Memory embeddings (pgvector)
-- Default is 1536 dims for text-embedding-3-small.
-- If you change embedding model, update EMBED_DIMS env and adjust this schema accordingly.
CREATE TABLE IF NOT EXISTS message_embeddings (
  chat_id      BIGINT NOT NULL,
  message_id   BIGINT NOT NULL,
  embedding    vector(1536) NOT NULL,
  date_utc     TIMESTAMPTZ,
  username     TEXT,
  full_name    TEXT,
  text         TEXT,
  PRIMARY KEY (chat_id, message_id),
  CONSTRAINT fk_msg FOREIGN KEY (chat_id, message_id)
    REFERENCES messages(chat_id, message_id)
    ON DELETE CASCADE
);

-- Index for similarity search (good default once you have volume)
CREATE INDEX IF NOT EXISTS message_embeddings_ivfflat
ON message_embeddings
USING ivfflat (embedding vector_cosine_ops)
WITH (lists = 100);

-- /Niko usage for rate limiting
CREATE TABLE IF NOT EXISTS niko_usage (
  id BIGSERIAL PRIMARY KEY,
  chat_id BIGINT NOT NULL,
  user_id BIGINT NOT NULL,
  ts TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS niko_usage_chat_ts ON niko_usage(chat_id, ts);

-- Admin allowlist (array of user IDs lives here)
CREATE TABLE IF NOT EXISTS admins (
  user_id BIGINT PRIMARY KEY,
  dm_chat_id BIGINT,
  enabled BOOLEAN NOT NULL DEFAULT TRUE
);

-- Audit log for admin changes
CREATE TABLE IF NOT EXISTS audit_log (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  admin_user_id BIGINT NOT NULL,
  action TEXT NOT NULL,
  details JSONB NOT NULL DEFAULT '{}'::jsonb
);
