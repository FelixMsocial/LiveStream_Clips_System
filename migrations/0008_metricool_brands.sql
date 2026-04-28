CREATE TABLE metricool_brands (
  id INTEGER PRIMARY KEY,
  brand_name TEXT NOT NULL UNIQUE,
  blog_id INTEGER NOT NULL UNIQUE,
  last_scheduled_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE metricool_rr_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  last_brand_id INTEGER NOT NULL DEFAULT 0
);
INSERT INTO metricool_rr_state (id, last_brand_id) VALUES (1, 0);

INSERT INTO metricool_brands (id, brand_name, blog_id) VALUES
  (1,  'CS2HighlightsMojo', 5942252),
  (2,  'CS2MojoClips',      5945662),
  (3,  'MojoCS2daily',      3808534),
  (4,  'MojoCS2elite',      3808539),
  (5,  'MojoCS2insane',     5942304),
  (6,  'mojoCS2Rage',       5942450),
  (7,  'mojocs2raw',        5942421),
  (8,  'Mojocs2tv',         5942148),
  (9,  'mojocs2vault',      5942634),
  (10, 'mojocs2zone',       3845257),
  (11, 'mojoonace',         5942385),
  (12, 'mojooncombo',       5942533),
  (13, 'mojoonfire',        5942324),
  (14, 'MojoOnFragz',       5942011),
  (15, 'mojoplayscs2',      5942309);
