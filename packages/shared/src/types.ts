// Shared types that cross the Worker / DO / GPU-worker / dashboard boundary.

export type ClipStatus =
  | "raw"
  | "downloaded"
  | "analyzing"
  | "editing"
  | "pending_approval"
  | "approved"
  | "rejected"
  | "expired"
  | "ready_to_post"
  | "posted"
  | "dispatched"
  | "posted_partial"
  | "post_failed"
  | "failed_capture"
  | "failed_edit"
  | "failed_post"
  | "ignored"
  | "missed";

export type ClipVibe =
  | "hype"
  | "funny"
  | "emotional"
  | "skill"
  | "fail"
  | "banter"
  | "unknown";

export interface ClipIngestJob {
  clip_id: string;
  broadcaster_id: string;
  broadcaster_login: string;
  triggered_by: string;
  triggered_at: string; // ISO8601
  label?: string;
  stream_session_id?: string;
  content_tag?: string; // 'gameplay' | 'vlog' | ... — defaults to 'gameplay'
}

export interface ClipEditJob {
  clip_id: string;
  raw_clip_r2_key: string;
  stream_session_id?: string;
  content_tag?: string; // forwarded from ClipIngestJob
}

export interface ApprovalSendJob {
  clip_id: string;
}

export interface PostDispatchJob {
  clip_id: string;
  approved_by: string; // telegram user id
}

export interface VisionAnalysis {
  peak_timestamp_sec: number;
  vibe: ClipVibe;
  key_elements: string[];
  quotes: Array<{ text: string; start: number; end: number }>;
  recommended_trim: { start_sec: number; end_sec: number };
  degraded?: boolean;
}

// v1.1 Substance Scorer output (Step 1, Gemini). Stored as JSON in
// substance_score_json. The shape is intentionally permissive — the schema
// is enforced server-side by the prompt + Python validator.
export interface SubstanceScore {
  rule_scores: Record<string, { score: number; reasoning: string; [k: string]: unknown }>;
  coherence_bonus: { score: number; reasoning: string };
  weighted_total: number;
  interpretation: "viral_candidate" | "solid" | "mid_tier" | "weak" | "very_weak";
  primary_strength: string;
  primary_weakness: string;
  context_summary: string;
  recommended_trim_window: {
    start_seconds: number;
    end_seconds: number;
    rationale: string;
  };
  rulebook_version: string;
  _extracted?: {
    peak_timestamp_seconds: number;
    peak_emotion: string;
    extractable_element: string;
    trigger_type: string;
  };
  degraded?: boolean;
}

export interface HookScore {
  verdict: "PASS" | "FAIL";
  weighted_total: number;
  rule_scores: Record<string, { score: number; reasoning: string; [k: string]: unknown }>;
  coherence_bonus: { score: number; reasoning: string };
  interpretation: string;
  primary_strength: string;
  primary_weakness: string;
  improvement_feedback: Array<{
    rule_number: number;
    rule_name: string;
    current_score: number;
    what_is_wrong: string;
    why_it_fails: string;
    what_to_change: string;
  }>;
  minor_concerns: string[];
  iteration_number: number;
  addressed_previous_feedback: "true" | "false" | "partial" | null;
  rulebook_version: string;
}

export interface GpuTimingsMs {
  download?: number;
  vision?: number;
  transcribe?: number;
  substance_score?: number;
  hook_loop?: number;
  ffmpeg?: number;
  upload?: number;
  copy?: number;
  total?: number;
}

export interface ClipRow {
  id: string;
  twitch_clip_id: string | null;
  twitch_edit_url: string | null;
  stream_session_id: string | null;
  triggered_by: string;
  triggered_at: string;
  label: string | null;
  status: ClipStatus;
  vision_analysis: string | null;
  transcript_srt: string | null;
  raw_clip_r2_key: string | null;
  final_clip_r2_key: string | null;
  duration_sec: number | null;
  instagram_post_text: string | null;
  youtube_post_text: string | null;
  tiktok_post_text: string | null;
  // v1.1 substance + hook scoring (added in migration 0006)
  substance_score: number | null;
  substance_score_json: string | null;
  low_potential_flag: number;
  peak_timestamp_sec: number | null;
  trim_start_sec: number | null;
  trim_end_sec: number | null;
  hook_overlay_text: string | null;
  hook_score: number | null;
  hook_score_json: string | null;
  hook_iterations: number;
  caption_scores_json: string | null;
  // dispatch columns (added in migration 0009)
  dispatched_brand_id: number | null;
  dispatched_brand_name: string | null;
  dispatched_blog_id: number | null;
  dispatched_at: string | null;
  metricool_post_ids: string | null;
  post_errors: string | null;
  alert_sent_at: string | null;
  approver_decision: string | null;
  approver_reason: string | null;
  approver_edits: string | null;
  approved_at: string | null;
  posted_at: string | null;
  post_urls: string | null;
  error_log: string | null;
  gpu_timings_ms: string | null;
  telegram_message_id: number | null;
  sent_at: string | null;
  reminder_sent: number;
  created_at: string;
  updated_at: string;
}

export interface PostUrls {
  instagram?: string;
  youtube?: string;
  tiktok?: string;
}

export interface CaptionsTriple {
  instagram: string;
  youtube: string;
  tiktok: string;
}

export interface N8nPostPayload {
  clip_id: string;
  video_url: string;
  brand: { id: number; brand_name: string; blog_id: number; user_id: string; "x-mc-token": string };
  titles_per_platform: { youtube: string; tiktok: string; instagram: string };
  publish_now: true;
  // backwards-compat for one release: mirror of titles_per_platform
  captions: CaptionsTriple;
  sponsor_session_id?: string | null;
  approved_by: string;
}
