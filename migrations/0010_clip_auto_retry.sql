ALTER TABLE clips ADD COLUMN auto_retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE clips ADD COLUMN last_auto_retry_at TEXT;
