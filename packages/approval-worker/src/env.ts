export interface Env {
  CLIP_DB: D1Database;
  CLIP_KV: KVNamespace;
  CLIP_BUCKET: R2Bucket;
  POST_DISPATCH: Queue<unknown>;

  TELEGRAM_BOT_TOKEN: string;
  TELEGRAM_APPROVER_CHAT_ID: string;
  TELEGRAM_JORDY_CHAT_ID: string;
  TELEGRAM_WEBHOOK_SECRET: string; // Telegram secret_token header validator

  DASHBOARD_URL: string;
  DASHBOARD_JWT_SECRET: string;

  GPU_INTERNAL_SECRET: string; // shared bearer for GPU worker -> /api/internal/*
  R2_PUBLIC_BASE?: string; // optional public base for R2 if using R2.dev or custom domain
  R2_ACCOUNT_ID?: string;
  R2_ACCESS_KEY_ID?: string;
  R2_SECRET_ACCESS_KEY?: string;
  R2_BUCKET_NAME?: string;

  RESEND_API_KEY?: string;
  ALERT_EMAIL_TO?: string;
  DASHBOARD_ADMIN_SECRET?: string; // dedicated secret for dashboard token issuance; falls back to GPU_INTERNAL_SECRET
}
