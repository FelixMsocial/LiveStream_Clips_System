-- Add content_tag to clips so every clip knows which prompt set was used.
-- Defaults to 'gameplay' so existing clips are unaffected.

ALTER TABLE clips ADD COLUMN content_tag TEXT NOT NULL DEFAULT 'gameplay';
