"""Fallback prompt bodies (kept in sync with migrations/0002_seed_prompts.sql)."""

GEMINI_ANALYSIS = """You analyze a short livestream clip from a Twitch gaming/IRL broadcaster.
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
- If unsure, center the trim on peak_timestamp_sec with a 25s window.
"""

IG_COPY = """Write an Instagram Reels caption for this livestream clip.
Inputs you have: vision_analysis (JSON), transcript_excerpt, vibe.

Requirements:
- Hook in the first line (<= 7 words).
- 1-2 emoji, tasteful.
- 3-5 relevant hashtags at the end.
- Total length <= 125 characters.
- No "link in bio", no generic filler, no quotes around the caption.
- Match the vibe: hype -> energetic; funny -> playful; emotional -> grounded.

Return ONLY the caption text.
"""

YT_COPY = """Write a YouTube Shorts post for this livestream clip.
Inputs: vision_analysis (JSON), transcript_excerpt, vibe.

Return JSON ONLY:
{ "title": string, "description": string }
- title <= 60 chars, searchable, front-load the hook.
- description 2 lines max, ends with #Shorts and 2-3 relevant hashtags.
"""

TT_COPY = """Write a TikTok caption for this livestream clip.
Inputs: vision_analysis (JSON), transcript_excerpt, vibe.

Requirements:
- Native TikTok tone - conversational, lowercase ok, no Instagram voice.
- 2-3 hashtags, trending-safe.
- <= 150 characters.
- 0-2 emoji max.

Return ONLY the caption text.
"""

PROMPTS = {
    "gemini_analysis": GEMINI_ANALYSIS,
    "ig_copy": IG_COPY,
    "yt_copy": YT_COPY,
    "tt_copy": TT_COPY,
}
