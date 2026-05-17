-- Persist per-stream-session metadata.
-- Currently stores the content_tag (gameplay|vlog|event…) so every clip
-- from a session inherits the correct prompt set automatically.
-- Set via POST /api/internal/sessions/:id/tag before going live.

CREATE TABLE IF NOT EXISTS stream_sessions (
  id          TEXT PRIMARY KEY,                    -- e.g. 'session-2026-05-17'
  content_tag TEXT NOT NULL DEFAULT 'gameplay',
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
