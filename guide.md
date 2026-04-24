# ClipFactory Operator Guide

## System Overview

ClipFactory automates the capture, editing, approval, and posting of Twitch livestream clips. The system consists of:

- **Listener Worker** — Durable Object that connects to Twitch IRC, watches for `!clip` commands from whitelisted mods
- **Capture Worker** — Creates Twitch clips via Helix API, downloads MP4 to R2
- **GPU Worker** — Python daemon that runs the editing pipeline (Gemini vision analysis, Deepgram transcription, FFmpeg editing, AI copy generation)
- **Approval Worker** — Central hub: sends Telegram approval messages, handles webhook callbacks, cron sweeps, dashboard API, internal APIs for GPU worker
- **Dashboard** — Static Cloudflare Pages site showing clip status and stats

### Queue Flow

```
!clip → listener-worker → CLIP_INGEST queue → capture-worker
  → CLIP_EDIT queue → GPU worker (pull-based)
  → /api/internal/approval-send → Telegram approval message
  → approve callback → POST_DISPATCH queue → n8n posting
```

---

## Common Operations

### Add/Remove Mods from Whitelist

```sql
-- Add a mod
INSERT INTO mod_whitelist (twitch_username, added_by, active) VALUES ('modname', 'admin', 1);

-- Remove a mod (soft delete)
UPDATE mod_whitelist SET active = 0 WHERE twitch_username = 'modname';

-- List active mods
SELECT * FROM mod_whitelist WHERE active = 1;
```

### Upload a Sponsor Asset

1. Upload the animation file to R2 (supported formats: `mp4`, `png`, `webp`):
   ```bash
   wrangler r2 object put clip-bucket/sponsors/session-2026-04-26.mp4 --file=./sponsor.mp4
   ```

2. Insert the sponsor config in D1:
   ```sql
   INSERT INTO sponsor_config (stream_session_id, sponsor_animation_r2_key, position, opacity, scale_pct, active_from, active_to)
   VALUES ('session-2026-04-26', 'sponsors/session-2026-04-26.mp4', 'bottom-right', 0.85, 0.15, '2026-04-26T00:00:00Z', '2026-04-27T00:00:00Z');
   ```

### Update Prompts

Prompts are versioned in D1. Insert a new version to update (GPU worker fetches latest every 5 minutes):

```sql
-- Check current prompt versions
SELECT key, version, length(body) as body_len FROM prompts ORDER BY key, version;

-- Update a prompt (version must be higher than existing)
INSERT INTO prompts (key, body, version) VALUES ('gemini_analysis', 'Your new prompt text...', 2);
```

Valid prompt keys: `gemini_analysis`, `ig_copy`, `yt_copy`, `tt_copy`.

### Rotate Twitch OAuth Tokens

1. Generate new tokens via Twitch Developer Console
2. Update Workers:
   ```bash
   wrangler secret put TWITCH_BROADCASTER_OAUTH_TOKEN --name clip-capture
   wrangler secret put TWITCH_BOT_OAUTH_TOKEN --name clip-listener
   ```
3. Restart the listener DO by hitting `/start` on its fetch endpoint
4. Update GPU worker `.env` if applicable

### Check System Health

- **Dashboard URL:** `https://clipfactory.pages.dev?t=<dashboard_jwt>`
- **Get a dashboard token:**
  ```bash
  curl -X POST https://clip-approval.<subdomain>.workers.dev/api/dashboard/token \
    -H "Content-Type: application/json" \
    -d '{"secret":"<GPU_INTERNAL_SECRET>"}'
  ```
- **KV heartbeat keys** (check via Wrangler):
  - `gpu:heartbeat` — GPU worker, refreshed every 30s, TTL 300s
  - `gpu:heartbeat:listener` — Listener DO, refreshed per heartbeat call, TTL 300s
  - If either key is absent, the cron job sends a Telegram alert to Jordy (with 30-min suppression)

---

## Environment Variables Reference

### All Workers (set via `wrangler secret put`)

| Variable | Used By | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | approval-worker | Telegram Bot API token |
| `TELEGRAM_APPROVER_CHAT_ID` | approval-worker | Chat ID for approval messages |
| `TELEGRAM_JORDY_CHAT_ID` | approval-worker | Chat ID for alerts to Jordy |
| `TELEGRAM_WEBHOOK_SECRET` | approval-worker | Validates inbound Telegram webhooks |
| `DASHBOARD_JWT_SECRET` | approval-worker | Signs/verifies dashboard JWTs |
| `GPU_INTERNAL_SECRET` | approval-worker, capture-worker | Shared bearer token for internal APIs |
| `DASHBOARD_ADMIN_SECRET` | approval-worker | (Optional) Dedicated secret for dashboard token issuance; falls back to `GPU_INTERNAL_SECRET` |
| `APPROVAL_WORKER_URL` | capture-worker | Base URL for approval-worker |
| `RESEND_API_KEY` | approval-worker | (Optional) Resend API key for email fallback |
| `ALERT_EMAIL_TO` | approval-worker | (Optional) Email address for fallback alerts |
| `TWITCH_CLIENT_ID` | capture-worker | Twitch app client ID |
| `TWITCH_CLIENT_SECRET` | capture-worker | Twitch app client secret |
| `TWITCH_BROADCASTER_OAUTH_TOKEN` | capture-worker | OAuth token with clips:edit scope |
| `TWITCH_BROADCASTER_ID` | capture-worker, listener | Twitch broadcaster user ID |
| `TWITCH_BROADCASTER_LOGIN` | listener | Twitch channel login name |
| `TWITCH_BOT_OAUTH_TOKEN` | listener | IRC bot OAuth token (oauth:xxx) |
| `TWITCH_BOT_NICK` | listener | IRC bot username |

### GPU Worker (set in `.env`)

| Variable | Description |
|----------|-------------|
| `GPU_WORKER_ID` | Identifier for this GPU worker instance |
| `GPU_HEARTBEAT_URL` | Full URL to `/api/gpu/heartbeat` |
| `GPU_D1_API_URL` | Full URL to `/api/internal` (base for clips, prompts) |
| `GPU_INTERNAL_SECRET` | Shared bearer token |
| `GEMINI_API_KEY` | Google Gemini API key |
| `DEEPGRAM_API_KEY` | Deepgram transcription API key |
| `R2_ENDPOINT` | R2 S3-compatible endpoint |
| `R2_ACCESS_KEY_ID` | R2 access key |
| `R2_SECRET_ACCESS_KEY` | R2 secret key |
| `R2_BUCKET` | R2 bucket name (default: clip-bucket) |
| `CF_QUEUES_PULL_TOKEN` | Cloudflare API token for queue pull |
| `CF_CLIP_EDIT_QUEUE_ID` | Queue ID for clip-edit queue |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID |

---

## Alerting & Fallback

- **Primary channel:** Telegram (bot messages to Jordy's chat)
- **Fallback channel:** Email via Resend API (when `RESEND_API_KEY` + `ALERT_EMAIL_TO` are configured)
- **Alert types:**
  - Capture failure — sent when Twitch clip creation fails
  - Clip expiry — sent when a clip times out without approval (20 min)
  - Heartbeat staleness — sent when GPU worker or listener goes unresponsive for 5+ min (30-min suppression)

---

## Spec Deviations (Build Log)

The following intentional deviations from the original `Livestream_Clip_System_Handoff.md` spec were made:

### Messaging: WhatsApp/Twilio -> Telegram Bot API
**Reason:** Telegram Bot API is free, instant, supports inline buttons for approve/reject, and doesn't require Twilio account setup. WhatsApp Business API has per-message costs and complex approval flows for message templates. Telegram provides a better UX for the approval workflow.

### Vision Analysis: Claude Vision -> Gemini 2.5 Pro
**Reason:** Gemini 2.5 Pro offers native video understanding (can process the full clip as video input), while Claude Vision requires frame extraction. This simplifies the pipeline and provides better temporal analysis for peak moment detection.

### Transcription: OpenAI Whisper -> Deepgram Nova-3
**Reason:** Deepgram Nova-3 provides faster-than-realtime transcription via API with better accuracy for gaming/streaming content. Avoids self-hosting Whisper or paying OpenAI API costs.

### Worker Communication: KV-based messaging -> Cloudflare Queues
**Reason:** Queues provide built-in retry logic, dead-letter queues, visibility timeouts, and at-least-once delivery guarantees. KV polling would require custom retry logic and has higher latency.

### Notification Fallback: WhatsApp -> SMS -> Email -> Telegram -> Email
**Reason:** With Telegram as the primary channel, the fallback chain is simpler. Email via Resend provides a reliable secondary channel. SMS was dropped as unnecessary given Telegram's reliability.

### Dashboard Auth: Telegram Login Widget -> JWT query param
**Reason:** Simpler to implement for V1. Dashboard URLs with embedded JWT tokens can be shared directly via Telegram. Full Telegram Login Widget auth is planned for V2.

---

## Known Limitations

- **Single GPU worker:** Concurrency is 1; scaling requires running multiple daemon instances with separate worker IDs
- **OAuth token refresh:** Twitch tokens must be manually rotated (auto-refresh is V2)
- **Font delivery:** Fonts for FFmpeg overlays must be manually installed on the GPU worker machine
- **No admin UI:** Mod whitelist, sponsor config, and prompt management are SQL-only operations
- **Session IDs are date-based:** `session-YYYY-MM-DD` format; multiple streams on the same day share a session ID
