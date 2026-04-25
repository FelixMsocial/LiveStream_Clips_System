-- v1.1 pipeline columns: substance score (Step 1), hook overlay + hook score (Steps 2/3),
-- recommended trim window, and per-platform caption scoring.

ALTER TABLE clips ADD COLUMN substance_score INTEGER;
ALTER TABLE clips ADD COLUMN substance_score_json TEXT;
ALTER TABLE clips ADD COLUMN low_potential_flag INTEGER NOT NULL DEFAULT 0;
ALTER TABLE clips ADD COLUMN peak_timestamp_sec REAL;
ALTER TABLE clips ADD COLUMN trim_start_sec REAL;
ALTER TABLE clips ADD COLUMN trim_end_sec REAL;
ALTER TABLE clips ADD COLUMN hook_overlay_text TEXT;
ALTER TABLE clips ADD COLUMN hook_score INTEGER;
ALTER TABLE clips ADD COLUMN hook_score_json TEXT;
ALTER TABLE clips ADD COLUMN hook_iterations INTEGER NOT NULL DEFAULT 0;
ALTER TABLE clips ADD COLUMN caption_scores_json TEXT;

CREATE INDEX IF NOT EXISTS idx_clips_substance_score ON clips(substance_score);
CREATE INDEX IF NOT EXISTS idx_clips_low_potential ON clips(low_potential_flag);
