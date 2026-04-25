CREATE TABLE IF NOT EXISTS dashboard_users (
  username      TEXT PRIMARY KEY,
  salt          TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
