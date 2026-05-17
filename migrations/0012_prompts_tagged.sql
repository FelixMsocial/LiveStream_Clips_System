-- Add `tag` column to prompts, enabling per-content-type prompt variants.
-- Existing rows are preserved under tag='gameplay' (the default).
-- New composite PK: (key, tag, version).

CREATE TABLE prompts_new (
  key        TEXT NOT NULL,
  tag        TEXT NOT NULL DEFAULT 'gameplay',
  version    INTEGER NOT NULL,
  body       TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (key, tag, version)
);

INSERT INTO prompts_new (key, tag, version, body, updated_at)
  SELECT key, 'gameplay', version, body, updated_at FROM prompts;

DROP TABLE prompts;
ALTER TABLE prompts_new RENAME TO prompts;

CREATE INDEX IF NOT EXISTS idx_prompts_key_tag ON prompts(key, tag);
