-- Seed v1 prompts. Bump `version` on each edit rather than updating in place.
-- SQLite string literals use single quotes; double a single quote to escape.

INSERT OR IGNORE INTO prompts (key, version, body) VALUES
('gemini_analysis', 1,
'You analyze a short livestream clip from a Twitch gaming/IRL broadcaster.
Watch the full clip, then return ONLY this JSON (no prose, no markdown):

{
  "peak_timestamp_sec": number,
  "vibe": "hype" | "funny" | "emotional" | "skill" | "fail" | "banter",
  "key_elements": string[],
  "quotes": [ { "text": string, "start": number, "end": number } ],
  "recommended_trim": { "start_sec": number, "end_sec": number }
}

Rules:
- recommended_trim MUST be between 15 and 30 seconds long.
- Prefer cutting on a natural beat (reaction shot, laugh, punchline, kill confirm).
- If unsure, center the trim on peak_timestamp_sec with a 25s window.');

INSERT OR IGNORE INTO prompts (key, version, body) VALUES
('ig_copy', 1,
'Write an Instagram Reels caption for this livestream clip.
Inputs you have: vision_analysis (JSON), transcript_excerpt, vibe.

Requirements:
- Hook in the first line (<= 7 words).
- 1-2 emoji, tasteful.
- 3-5 relevant hashtags at the end.
- Total length <= 125 characters (IG truncates beyond this).
- No "link in bio", no generic filler, no quotes around the caption.
- Match the vibe: hype -> energetic; funny -> playful; emotional -> grounded.

Return ONLY the caption text.');

INSERT OR IGNORE INTO prompts (key, version, body) VALUES
('yt_copy', 1,
'Write a YouTube Shorts post for this livestream clip.
Inputs: vision_analysis (JSON), transcript_excerpt, vibe.

Return JSON ONLY:
{ "title": string, "description": string }
- title <= 60 chars, searchable, front-load the hook, no clickbait all-caps.
- description 2 lines max, ends with #Shorts and 2-3 relevant hashtags.');

INSERT OR IGNORE INTO prompts (key, version, body) VALUES
('tt_copy', 1,
'Write a TikTok caption for this livestream clip.
Inputs: vision_analysis (JSON), transcript_excerpt, vibe.

Requirements:
- Native TikTok tone - conversational, lowercase ok, no Instagram voice.
- 2-3 hashtags, trending-safe (avoid banned tags).
- <= 150 characters.
- No emoji spam; 0-2 max.

Return ONLY the caption text.');
