# ClipFactory — Livestream Clip System

`!clip` in a whitelisted mod's Twitch chat → inside 30 minutes the moment is live
on Instagram Reels, YouTube Shorts, and TikTok with platform-tuned captions,
after a Telegram-based human approval.

See the architecture plan at `plans/shiny-moon.md` for the full design; this
README is the operator cheat sheet.

## Layers

| Layer           | Runtime                          | Folder                              |
|-----------------|----------------------------------|-------------------------------------|
| 1. Listener     | Cloudflare Worker + Durable Obj. | `packages/listener-worker/`         |
| 2. Capture      | Cloudflare Worker (queue consumer)| `packages/capture-worker/`         |
| 3. Analyze+Edit | **Local NVIDIA GPU box** (Python)| `gpu-worker/`                       |
| 4. Approval     | Cloudflare Worker + Pages dash.  | `packages/approval-worker/`, `apps/dashboard/` |
| 5. Post         | Cloudflare Worker (queue consumer)| `packages/post-worker/`            |

All cross-layer messages flow through Cloudflare Queues (`clip-ingest`,
`clip-edit`, `approval-send`, `post-dispatch`). No Worker→Worker HTTP.

## One-time infra setup

```bash
# From repo root, authenticate wrangler first:  wrangler login
wrangler d1 create CLIP_DB
wrangler kv namespace create CLIP_KV
wrangler r2 bucket create clip-bucket
wrangler queues create clip-ingest
wrangler queues create clip-ingest-dlq
wrangler queues create clip-edit
wrangler queues create clip-edit-dlq
wrangler queues create approval-send
wrangler queues create approval-send-dlq
wrangler queues create post-dispatch
wrangler queues create post-dispatch-dlq
```

Paste the resulting `database_id`/`id` values into each package's
`wrangler.toml` (replacing `REPLACE_WITH_*` placeholders).

Apply migrations:

```bash
npm run d1:migrate:local    # for `wrangler dev`
npm run d1:migrate:remote   # once you're happy with it
```

Seed initial approvers and mod whitelist (edit values first):

```bash
wrangler d1 execute CLIP_DB --remote --command "INSERT INTO approvers (telegram_user_id, display_name, role) VALUES ('<id>', 'Felix', 'primary');"
wrangler d1 execute CLIP_DB --remote --command "INSERT INTO mod_whitelist (twitch_username, added_by, added_at) VALUES ('<mod_login>', 'felix', datetime('now'));"
```

## Install dev deps

```bash
npm install
```

## Secrets

Each Worker needs its own `wrangler secret put <NAME>` values. See
[`.env.example`](./.env.example) for the full list. For the GPU worker,
drop those same values into `gpu-worker/.env`.

Pull consumer token for the GPU worker:

```bash
wrangler queues consumer http add clip-edit
# Copy the returned token → CF_QUEUES_PULL_TOKEN
```

## Deploy the Workers

```bash
npm run deploy:all
```

or individually:

```bash
npm run deploy --workspace packages/listener-worker
npm run deploy --workspace packages/capture-worker
npm run deploy --workspace packages/approval-worker
npm run deploy --workspace packages/post-worker
npm run deploy --workspace apps/dashboard
```

## Wire Telegram

1. Create a bot with `@BotFather` → copy token → `wrangler secret put TELEGRAM_BOT_TOKEN`.
2. Get your approver Telegram user ID (e.g. via `@userinfobot`) → `INSERT INTO approvers`.
3. Set the bot's webhook:

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -H "content-type: application/json" \
  -d '{"url":"https://clip-approval.<your-subdomain>.workers.dev/telegram/webhook","secret_token":"<TELEGRAM_WEBHOOK_SECRET>"}'
```

## Run the GPU worker

See [`gpu-worker/README.md`](./gpu-worker/README.md). Short version:

```bash
cd gpu-worker
python -m venv .venv
# activate, then:
pip install -r requirements.txt
python -m clipfactory_gpu.main
```

## Verifying the end-to-end flow

1. Trigger `!clip` from a whitelisted mod in the test channel.
2. Row appears in D1 `clips` with `status='raw'`, within ~5s.
3. `clip-capture` → `status='downloaded'`, raw MP4 in R2.
4. GPU worker picks it up → Gemini → Deepgram → FFmpeg → Claude → `status='pending_approval'`.
5. Telegram bot sends the approval card.
6. ✅ Approve → `post-dispatch` fires → n8n webhook invoked → `status='ready_to_post'`.
7. n8n posts back via `/webhook/clip-posted` → `status='posted'`, `post_urls` populated.

## Where things hide

- Worker logs: `wrangler tail <worker>`.
- DLQ depth: `wrangler queues consumer list`; the approval-worker cron should
  alert when any DLQ > 0 for > 5 min (TODO: implement alert).
- GPU heartbeat: KV key `gpu:heartbeat` (5m TTL).
- Per-clip timings: `clips.gpu_timings_ms` JSON.
