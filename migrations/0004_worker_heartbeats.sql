-- worker_heartbeats — single row per worker, last_seen_ts is unix epoch ms.
-- Replaces gpu:heartbeat / gpu:heartbeat:listener KV keys (KV Free 1k writes/day).
CREATE TABLE IF NOT EXISTS worker_heartbeats (
  worker_id     TEXT PRIMARY KEY,
  last_seen_ts  INTEGER NOT NULL,
  meta          TEXT,
  updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
