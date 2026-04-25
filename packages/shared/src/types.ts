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
}

export interface ClipEditJob {
  clip_id: string;
  raw_clip_r2_key: string;
  stream_session_id?: string;
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

export interface GpuTimingsMs {
  download?: number;
  vision?: number;
  transcribe?: number;
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
  captions: CaptionsTriple;
  sponsor_session_id?: string | null;
  approved_by: string;
}
