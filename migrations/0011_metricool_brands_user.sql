-- Add user_id and mc_token columns; seed 15 placeholder brands for the second user.
-- Replace PLACEHOLDER_TOKEN_USER_1 / PLACEHOLDER_TOKEN_USER_2 with real tokens via UPDATE.
-- Replace placeholder_brand_* names/blog_ids with real values via UPDATE.

ALTER TABLE metricool_brands ADD COLUMN user_id  TEXT NOT NULL DEFAULT 'user_1';
ALTER TABLE metricool_brands ADD COLUMN mc_token TEXT NOT NULL DEFAULT 'PLACEHOLDER_TOKEN_USER_1';

-- The existing 15 brands already got DEFAULT values above; no extra UPDATE needed.

INSERT INTO metricool_brands (id, brand_name, blog_id, user_id, mc_token) VALUES
  (16, 'placeholder_brand_16', 9000001, 'user_2', 'PLACEHOLDER_TOKEN_USER_2'),
  (17, 'placeholder_brand_17', 9000002, 'user_2', 'PLACEHOLDER_TOKEN_USER_2'),
  (18, 'placeholder_brand_18', 9000003, 'user_2', 'PLACEHOLDER_TOKEN_USER_2'),
  (19, 'placeholder_brand_19', 9000004, 'user_2', 'PLACEHOLDER_TOKEN_USER_2'),
  (20, 'placeholder_brand_20', 9000005, 'user_2', 'PLACEHOLDER_TOKEN_USER_2'),
  (21, 'placeholder_brand_21', 9000006, 'user_2', 'PLACEHOLDER_TOKEN_USER_2'),
  (22, 'placeholder_brand_22', 9000007, 'user_2', 'PLACEHOLDER_TOKEN_USER_2'),
  (23, 'placeholder_brand_23', 9000008, 'user_2', 'PLACEHOLDER_TOKEN_USER_2'),
  (24, 'placeholder_brand_24', 9000009, 'user_2', 'PLACEHOLDER_TOKEN_USER_2'),
  (25, 'placeholder_brand_25', 9000010, 'user_2', 'PLACEHOLDER_TOKEN_USER_2'),
  (26, 'placeholder_brand_26', 9000011, 'user_2', 'PLACEHOLDER_TOKEN_USER_2'),
  (27, 'placeholder_brand_27', 9000012, 'user_2', 'PLACEHOLDER_TOKEN_USER_2'),
  (28, 'placeholder_brand_28', 9000013, 'user_2', 'PLACEHOLDER_TOKEN_USER_2'),
  (29, 'placeholder_brand_29', 9000014, 'user_2', 'PLACEHOLDER_TOKEN_USER_2'),
  (30, 'placeholder_brand_30', 9000015, 'user_2', 'PLACEHOLDER_TOKEN_USER_2');
