ALTER TABLE clips ADD COLUMN dispatched_brand_id INTEGER;
ALTER TABLE clips ADD COLUMN dispatched_brand_name TEXT;
ALTER TABLE clips ADD COLUMN dispatched_blog_id INTEGER;
ALTER TABLE clips ADD COLUMN dispatched_at TEXT;
ALTER TABLE clips ADD COLUMN metricool_post_ids TEXT;   -- JSON: {tiktok, youtube, instagram}
ALTER TABLE clips ADD COLUMN post_errors TEXT;          -- JSON: {<platform>: <error_msg>}
ALTER TABLE clips ADD COLUMN alert_sent_at TEXT;        -- guards against duplicate alerts
