export interface Env {
  CLIP_DB: D1Database;
  CLIP_BUCKET: R2Bucket;
  CLIP_EDIT: Queue<unknown>;

  TWITCH_CLIENT_ID: string;
  TWITCH_CLIENT_SECRET: string;
  TWITCH_BROADCASTER_ID: string;
  TWITCH_BROADCASTER_OAUTH_TOKEN: string; // clips:edit

  APPROVAL_WORKER_URL: string; // base URL for approval-worker internal API
  GPU_INTERNAL_SECRET: string; // shared bearer token for internal API auth
}
