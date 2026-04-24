-- ClipFactory D1 schema v1 — matches §4 of the architecture plan.
-- D1 is SQLite; use TEXT for DATETIME/JSON.

CREATE TABLE IF NOT EXISTS clips (
  id TEXT PRIMARY KEY,                     -- uuidv7
  twitch_clip_id TEXT,
  stream_session_id TEXT,
  triggered_by TEXT NOT NULL,
  triggered_at TEXT NOT NULL,
  label TEXT,
  status TEXT NOT NULL,                    -- raw|downloaded|analyzing|editing|pending_approval|
                                           -- approved|rejected|expired|ready_to_post|posted|
                                           -- failed_capture|failed_edit|failed_post|ignored|missed
  vision_analysis TEXT,                    -- JSON
  transcript_srt TEXT,
  raw_clip_r2_key TEXT,
  final_clip_r2_key TEXT,
  duration_sec REAL,
  instagram_post_text TEXT,
  youtube_post_text TEXT,
  tiktok_post_text TEXT,
  approver_decision TEXT,
  approver_reason TEXT,
  approver_edits TEXT,                     -- JSON
  approved_at TEXT,
  posted_at TEXT,
  post_urls TEXT,                          -- JSON
  error_log TEXT,                          -- JSON
  gpu_timings_ms TEXT,                     -- JSON {download, vision, transcribe, ffmpeg, upload, copy}
  telegram_message_id INTEGER,
  sent_at TEXT,
  reminder_sent INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mod_whitelist (
  twitch_username TEXT PRIMARY KEY,        -- lowercase
  added_by TEXT NOT NULL,
  added_at TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sponsor_config (
  stream_session_id TEXT PRIMARY KEY,
  sponsor_animation_r2_key TEXT NOT NULL,
  position TEXT NOT NULL DEFAULT 'bottom-right',
  opacity REAL NOT NULL DEFAULT 0.85,
  scale_pct REAL NOT NULL DEFAULT 0.15,
  active_from TEXT NOT NULL,
  active_to TEXT
);

CREATE TABLE IF NOT EXISTS approval_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  clip_id TEXT NOT NULL,
  event_type TEXT NOT NULL,                -- sent|reminder|approved|rejected|edited|expired|posted|failed
  event_at TEXT NOT NULL DEFAULT (datetime('now')),
  actor TEXT,                              -- telegram user id / worker name
  details TEXT,                            -- JSON
  FOREIGN KEY (clip_id) REFERENCES clips(id)
);

CREATE TABLE IF NOT EXISTS approvers (
  telegram_user_id TEXT PRIMARY KEY,
  display_name TEXT,
  role TEXT NOT NULL DEFAULT 'primary',    -- primary|escalation
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS prompts (
  key TEXT NOT NULL,                       -- ig_copy, yt_copy, tt_copy, gemini_analysis
  version INTEGER NOT NULL,
  body TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (key, version)
);

CREATE INDEX IF NOT EXISTS idx_clips_status ON clips(status);
CREATE INDEX IF NOT EXISTS idx_clips_triggered_at ON clips(triggered_at);
CREATE INDEX IF NOT EXISTS idx_clips_pending_sent ON clips(status, sent_at);
CREATE INDEX IF NOT EXISTS idx_approval_log_clip ON approval_log(clip_id);
CREATE INDEX IF NOT EXISTS idx_mod_whitelist_active ON mod_whitelist(active);
