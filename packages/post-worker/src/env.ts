export interface Env {
  CLIP_DB: D1Database;
  CLIP_BUCKET: R2Bucket;

  N8N_WEBHOOK_URL: string;
  N8N_WEBHOOK_SECRET: string;

  GPU_INTERNAL_SECRET: string;

  // Service binding to approval-worker for ops alerts
  OPS: Fetcher;

  DASHBOARD_URL?: string;

  R2_ACCOUNT_ID: string;
  R2_ACCESS_KEY_ID: string;
  R2_SECRET_ACCESS_KEY: string;
  R2_BUCKET_NAME: string;
  R2_PUBLIC_BASE?: string;
}
