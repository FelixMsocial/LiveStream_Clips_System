export interface Env {
  CLIP_DB: D1Database;
  CLIP_KV: KVNamespace;
  CLIP_BUCKET: R2Bucket;
  POST_DISPATCH: Queue<unknown>;

  TELEGRAM_BOT_TOKEN: string;
  TELEGRAM_APPROVER_CHAT_ID: string;
  TELEGRAM_JORDY_CHAT_ID: string;
  TELEGRAM_WEBHOOK_SECRET: string; // Telegram secret_token header validator
  TELEGRAM_OPS_CHAT_ID?: string; // optional separate ops alert chat

  DASHBOARD_URL: string;
  DASHBOARD_JWT_SECRET: string;

  N8N_WEBHOOK_SECRET: string; // validates inbound /webhook/clip-posted callbacks from n8n
  GPU_INTERNAL_SECRET: string; // shared bearer for GPU worker -> /api/internal/*
  R2_PUBLIC_BASE?: string; // optional public base for R2 if using R2.dev or custom domain
  R2_ACCOUNT_ID?: string;
  R2_ACCESS_KEY_ID?: string;
  R2_SECRET_ACCESS_KEY?: string;
  R2_BUCKET_NAME?: string;

  RESEND_API_KEY?: string;
  ALERT_EMAIL_TO?: string;
  DASHBOARD_ADMIN_SECRET?: string; // dedicated secret for dashboard token issuance; falls back to GPU_INTERNAL_SECRET

  READY_TO_POST_RETRY_INTERVAL_MINUTES?: string; // default 30
  READY_TO_POST_MAX_RETRIES?: string;             // default 3

  SKIP_APPROVAL?: string; // "true" to bypass Telegram and auto-approve all clips
}
