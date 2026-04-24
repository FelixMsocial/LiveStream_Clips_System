# LIVESTREAM CLIP SYSTEM — HANDOFF DOC

**Assignee:** Felix (AI Systems Integrator) **Priority:** CRITICAL **Build window:** April 21 – April 25, 2026 (5 working days) **Live deployment target:** April 26, 2026 (Jordy's livestream) **Parent ticket:** `CLIP-SYSTEM-V1`

---

## OUTCOME (one sentence)

A trusted mod types `!clip` in Twitch chat during a live stream → within 30 minutes, that moment is live on Instagram Reels, YouTube Shorts, and TikTok with platform-optimized captions and post text, after a human approval step via WhatsApp.

---

## BUSINESS CONTEXT (why this matters)

During Jordy's livestreams, moments happen that would perform well on short-form social. Right now, those moments stay trapped inside the stream. This system makes the livestream **promote itself while it's still live** — clip goes to socials within 30 minutes, drives views back to the live stream, creates a self-reinforcing growth loop.

The 30-minute SLA is the whole system. Miss the window, the clip is dead — the stream is over or the moment is stale.

---

## START HERE — TOP 3 FIRST ACTIONS

Felix: you should be able to start work within 10 minutes of reading this section.

1. **Register a Twitch application at [https://dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps).** Request `clips:edit` and `chat:read` scopes. This gives you the Client ID \+ Client Secret you'll need for everything else. OAuth user access token must be generated against Jordy's Twitch account (broadcaster) — he'll do the auth flow with you on a call after the system has been tested on a different account. *Est: 30 min including Jordy handoff.*  
     
2. **Stand up the Cloudflare Worker skeleton.** Name: `clip-system`. Bindings: `CLIP_KV` (namespace), `CLIP_DB` (D1), `CLIP_BUCKET` (R2). Use the pattern from `hr-manager` — KV-based decoupling, not Worker-to-Worker HTTP (error 1042 learning applies). Deploy a health-check endpoint to confirm the Worker is live. *Est: 45 min.*  
     
3. **Build the Twitch chat listener.** Connect to Twitch IRC via WebSocket, listen for `!clip` commands from the mod whitelist (whitelist is in D1 — see config section). When a valid `!clip` fires, log to D1 with status `raw` and trigger the clip creation pipeline. Test by having Jordy run a test stream and firing `!clip` from his own account. *Est: 3 hours.*

Ship these three on Day 1\. Everything else builds on them.

---

## SYSTEM DESIGN

### Architecture overview

```
┌──────────────────────────────────────────────────────────────┐
│  LIVE LAYER (always-on during stream)                        │
├──────────────────────────────────────────────────────────────┤
│  Twitch Chat IRC → Worker listens for !clip from whitelist   │
└──────────────────────────────────────────────────────────────┘
                              │
                              │ !clip fires
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  CLIP CREATION LAYER (Twitch Clips API)                      │
├──────────────────────────────────────────────────────────────┤
│  1. POST /helix/clips → returns clip ID                      │
│  2. Poll GET /helix/clips until clip is ready (15s timeout)  │
│  3. Download MP4 from clip URL → R2                          │
│  4. D1 row: status = "downloaded"                            │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  ANALYSIS + EDIT LAYER                                       │
├──────────────────────────────────────────────────────────────┤
│  1. Claude Vision: analyze clip, identify peak moment        │
│     Returns {peak_timestamp, vibe, key_elements, quotes}     │
│  2. Whisper API: transcribe audio → SRT                      │
│  3. FFmpeg pipeline:                                         │
│     a. Trim to 15-30s around peak_timestamp                  │
│     b. Reframe 16:9 → 9:16 (center-crop + blur fill)         │
│     c. Burn in captions from SRT (styled)                    │
│     d. Overlay sponsor animation (static position)           │
│     e. Export final MP4 → R2                                 │
│  4. Generate 3 platform-specific post texts (one Claude call │
│     per platform, parallel is fine for text-only)            │
│  5. D1 row: status = "pending_approval"                      │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  APPROVAL LAYER (WhatsApp via Twilio)                        │
├──────────────────────────────────────────────────────────────┤
│  1. Send WhatsApp to approver: video preview link + 3 post   │
│     texts + approval options (✅ approve / ❌ reject / ✏️    │
│     edit)                                                    │
│  2. Webhook receives approver response                       │
│  3. T+10min: reminder if no response                         │
│  4. T+20min: auto-abort, status = "expired"                  │
│  5. ALL approval decisions + reasons logged to D1            │
│     (training data for future AI approver — V2)              │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  POSTING LAYER (existing scheduler integration)              │
├──────────────────────────────────────────────────────────────┤
│  Hand off approved clip + per-platform text to Jordy's       │
│  existing posting automation. Status = "posted", log post    │
│  URLs back to D1.                                            │
└──────────────────────────────────────────────────────────────┘
```

### Data flow (KV vs D1 vs R2)

- **D1** (`CLIP_DB`) — Clip records, status tracking, approver decisions, mod whitelist, sponsor config, approval training data.  
- **KV** (`CLIP_KV`) — Short-lived state (active stream session, current sponsor for the session, OAuth tokens with TTL).  
- **R2** (`CLIP_BUCKET`) — Binary assets (raw clip MP4, edited final MP4, sponsor animation file, subtitle files).

**Why this split:** Mirrors the HR system architecture. KV for fast ephemeral reads, D1 for relational queries and audit trails, R2 for binaries. Worker-to-Worker HTTP calls are forbidden (Cloudflare error 1042\) — use KV for any cross-worker signaling.

### D1 schema (core tables)

```sql
CREATE TABLE clips (
  id TEXT PRIMARY KEY,              -- UUID
  twitch_clip_id TEXT,               -- from Twitch API response
  triggered_by TEXT NOT NULL,        -- Twitch username of mod
  triggered_at DATETIME NOT NULL,
  status TEXT NOT NULL,              -- raw | downloaded | analyzed | edited | pending_approval | approved | rejected | expired | posted | failed
  vision_analysis JSON,              -- {peak_timestamp, vibe, key_elements, quotes}
  raw_clip_r2_key TEXT,
  final_clip_r2_key TEXT,
  instagram_post_text TEXT,
  youtube_post_text TEXT,
  tiktok_post_text TEXT,
  approver_decision TEXT,            -- approve | reject | edit
  approver_reason TEXT,              -- free text, critical for AI training
  approver_edits JSON,               -- if edited, what changed
  approved_at DATETIME,
  posted_at DATETIME,
  post_urls JSON,                    -- {instagram: url, youtube: url, tiktok: url}
  error_log JSON
);

CREATE TABLE mod_whitelist (
  twitch_username TEXT PRIMARY KEY,
  added_by TEXT NOT NULL,
  added_at DATETIME NOT NULL,
  active BOOLEAN DEFAULT TRUE
);

CREATE TABLE sponsor_config (
  stream_session_id TEXT PRIMARY KEY,
  sponsor_animation_r2_key TEXT NOT NULL,
  position TEXT DEFAULT 'bottom-right',  -- bottom-right | top-right | bottom-left | top-left
  opacity REAL DEFAULT 0.85,
  active_from DATETIME NOT NULL,
  active_to DATETIME
);

CREATE TABLE approval_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  clip_id TEXT NOT NULL,
  event_type TEXT NOT NULL,          -- sent | reminder | approved | rejected | edited | expired
  event_at DATETIME NOT NULL,
  details JSON,
  FOREIGN KEY (clip_id) REFERENCES clips(id)
);
```

---

## TECHNICAL SPECS

### Platform stack

- **Cloudflare Workers** (Wrangler, TypeScript preferred)  
- **Cloudflare D1** (SQLite) — data persistence  
- **Cloudflare KV** — ephemeral state  
- **Cloudflare R2** — video file storage  
- **Cloudflare Browser Rendering** — NOT needed for this build (no scraping)  
- **FFmpeg** — for video editing. Run via a dedicated Worker or use Cloudflare Containers. If Workers can't run FFmpeg reliably, spin up a small VPS (DigitalOcean droplet or Fly.io) for the editing layer and use KV to coordinate. Felix, evaluate on Day 1 and flag which path you're taking.

### External APIs

| Service | Purpose | Auth | Rate Limits | Failure Mode |
| :---- | :---- | :---- | :---- | :---- |
| Twitch Helix API (`/helix/clips`) | Clip creation \+ retrieval | OAuth user access token with `clips:edit` scope | 800 pts/min per client ID, 1 pt per Create Clip call | 15s polling window. If clip not returned after 15s, retry once. If retry fails, WhatsApp Jordy \+ mod: "Clip failed at \[timestamp\], moment lost." |
| Twitch IRC (chat) | Listen for `!clip` | OAuth bot token, `chat:read` scope | 20 msgs/30s per channel (we're read-only, not a constraint) | Auto-reconnect on drop. Log missed `!clip` commands to D1 with a "missed" flag. |
| Claude API (Vision) | Analyze clip, identify peak | API key (in project: `HR Manager - API Keys.txt`) | Sequential calls only (parallel causes rate-limit errors per HR system learnings) | Retry with 10s backoff, max 3 attempts. On final failure, use clip midpoint as peak and continue (degraded mode). |
| Claude API (text gen) | Per-platform post text | Same API key | Standard tier | Retry once, then fall back to generic template `"🔴 Live now on Twitch → {stream_url}"` |
| OpenAI Whisper (transcription) | Generate captions | OpenAI API key (Jordy to provide — if not available, use Claude-based transcription as fallback) | Standard | On failure, skip captions for that clip but continue the pipeline. Log for manual review. |
| Twilio WhatsApp API | Approval messaging | Twilio Account SID \+ auth token | 1 msg/sec per number | On send failure, retry 3x with 30s backoff. If still failing, fall back to Twilio SMS. If SMS also fails, email approver \+ Jordy. |
| Jordy's existing scheduler | Posting to IG/YT/TikTok | Existing integration | Unknown — confirm on Day 1 | If scheduler down, hold approved clip in D1 with status `ready_to_post`, post when scheduler recovers. |

### Key environment variables / secrets

Store in Worker secrets, not in code:

- `TWITCH_CLIENT_ID`  
- `TWITCH_CLIENT_SECRET`  
- `TWITCH_BROADCASTER_OAUTH_TOKEN` (Jordy's user token with `clips:edit`)  
- `TWITCH_BROADCASTER_ID` (Jordy's numeric Twitch ID)  
- `TWITCH_BOT_OAUTH_TOKEN` (for chat IRC read access)  
- `ANTHROPIC_API_KEY` (see project file)  
- `OPENAI_API_KEY` (for Whisper, if used)  
- `TWILIO_ACCOUNT_SID`  
- `TWILIO_AUTH_TOKEN`  
- `TWILIO_WHATSAPP_FROM` (Twilio WhatsApp-enabled number)  
- `APPROVER_WHATSAPP_NUMBER` — **TBD, Jordy to fill Day 4**  
- `JORDY_WHATSAPP_NUMBER` (escalation fallback)

### Video processing specs

- **Input:** Twitch clip MP4, typically 720p60 or 1080p60, 16:9, up to 90s.  
- **Trim length:** 15-30s, targeting peak moment identified by Claude Vision. Default center on peak — Vision returns a timestamp within the clip, we trim \~10s before and \~15s after (adjust based on clip length).  
- **Output aspect ratio:** 9:16 vertical, 1080x1920.  
- **Reframing method (V1):** Center-crop the 16:9 source \+ blurred stretched copy as background fill for the empty top/bottom bars. This is the "letterbox with blur" style. Fast, cheap, universally OK. **V2 candidate:** subject-tracking smart crop — parked.  
- **Captions:** Whisper-generated SRT. Burn-in style: large bold sans-serif (Montserrat Black or Inter Black), white with black outline, positioned \~70% down the frame, max 2 lines, word-by-word timing if Whisper provides it.  
- **Sponsor animation overlay:** Jordy provides a single sponsor animation file. Position: **bottom-right**, scaled to \~15% of frame width, opacity 85%, persistent throughout the clip. Store in R2 as `sponsors/current.mp4` (or .mov/.gif depending on format).  
- **Final export:** H.264, AAC audio, 1080x1920, 30fps, \~8 Mbps bitrate. MP4 container.

### Per-platform post text generation

One Claude Vision call produces the shared analysis JSON. That JSON feeds three separate text-generation prompts:

- **Instagram Reels** — Based on first principles rulebook –  
- **YouTube Shorts** — Based on first principles rulebook –  
- **TikTok** — Based on first principles rulebook –

Each platform gets its own prompt template stored in D1 or as constants. During calibration (Day 3), tune these prompts based on Jordy's feedback.

### WhatsApp approval message format **(Can also just be link to the QA dashboard)**

```
🎬 New clip ready for review
Triggered by: @mod_username at 14:32 UTC
Duration: 22s

▶️ Preview: [signed R2 URL, 1-hour TTL]

📱 Suggested posts:

Instagram: [text]
YouTube: [text]
TikTok: [text]

Reply:
✅ APPROVE — post to all platforms
❌ REJECT [reason] — skip this clip
✏️ EDIT — I'll reply with updated text

⏱ Auto-expires in 20 min.
```

Webhook parses approver's reply. Keywords `APPROVE`, `REJECT`, `EDIT` trigger the respective paths. Log reason for every reject/edit — this is training data for the V2 AI approver.

---

## ACCEPTANCE CRITERIA (yes/no testable)

**Functional:**

- [ ] When a whitelisted mod types `!clip` in Jordy's Twitch chat during a live stream, the system creates a Twitch clip via API within 30 seconds.  
- [ ] The raw clip MP4 is downloaded and stored in R2.  
- [ ] Claude Vision analyzes the clip and returns a structured JSON with peak\_timestamp, vibe, and key elements.  
- [ ] The final edited clip is 9:16 aspect ratio, 15-30s long, with burned-in captions and sponsor animation overlay.  
- [ ] Per-platform post text is generated for Instagram, YouTube Shorts, and TikTok.  
- [ ] A WhatsApp message is sent to the approver within 10 minutes of the `!clip` trigger.  
- [ ] When the approver responds `APPROVE`, the clip is handed off to the existing posting scheduler.  
- [ ] When the approver does not respond within 10 minutes, a reminder WhatsApp is sent.  
- [ ] When the approver does not respond within 20 minutes, the clip status is set to `expired` and no posting occurs.  
- [ ] Every approval decision (approve/reject/edit) including reason text is logged to the `approval_log` table in D1.  
- [ ] From `!clip` trigger to post going live on all three platforms: ≤ 30 minutes end-to-end.

**Reliability:**

- [ ] If Twitch Clips API fails, a WhatsApp alert is sent to Jordy within 60 seconds.  
- [ ] If Claude Vision rate-limits, the system retries sequentially with 10s backoff (mirrors HR system pattern).  
- [ ] If WhatsApp send fails, the system falls back to SMS, then email.  
- [ ] If the posting scheduler is down, approved clips are held with status `ready_to_post` and post when scheduler recovers.  
- [ ] Non-whitelisted users typing `!clip` are ignored (logged, but no action).

**Observability:**

- [ ] A dashboard (simple HTML Worker endpoint) shows: active clip count, today's clip count, success/failure breakdown, average time-to-post, last 10 clips with status.  
- [ ] A heartbeat signal fires daily confirming the system is alive (per factory protocol — defined during build).  
- [ ] Every clip record has a complete audit trail in D1 from trigger to final status.

---

## API DEPENDENCY CHECK (summary)

See Technical Specs table above for full details. Key points:

- **Twitch Clips API** is the primary footage source. 15s timeout with retry-once, then fail gracefully. No RTMP buffer infrastructure needed.  
- **Claude Vision** must run sequentially, never in parallel (per HR system learning).  
- **WhatsApp** has a three-tier degradation: WhatsApp → SMS → email.  
- **Existing scheduler** integration details are a Day-1 discovery task — Felix confirms how to hand off.

---

## REFERENCE EXAMPLES (taste-sensitive outputs)

Because captioning, post text, and clip trimming are taste-dependent, **Day 3 includes a calibration session** with Jordy:

1. Felix runs the pipeline on 5-8 test clips from past Jordy streams (use existing VODs or Twitch clips).  
2. Jordy rates each output: trim quality, caption style, per-platform post text, overall vibe.  
3. Felix tunes prompts and config based on feedback.  
4. Repeat until Jordy approves the overall pattern.  
5. Only then does the system go live.

This replaces binary QA for the taste-dependent elements. Binary QA still applies to the structural acceptance criteria above.

---

## BUILD SCHEDULE (Day 1-5)

**Day 1 (April 21\) — Architecture \+ Foundation**

- Twitch app registration \+ OAuth flow with Jordy  
- Worker skeleton \+ D1 schema \+ R2 bucket setup  
- Twitch chat IRC listener with whitelist  
- Decide: FFmpeg on Worker vs external VPS  
- End-of-day Slack update: architecture decisions \+ any blockers

**Day 2 (April 22\) — MVP End-to-End**

- Twitch Clips API integration (create \+ poll \+ download)  
- Claude Vision analysis call  
- Basic FFmpeg trim \+ 9:16 reframe (no captions, no sponsor yet)  
- WhatsApp approval flow with Twilio (send \+ webhook response)  
- Posting scheduler handoff (confirm integration, even if stubbed)  
- End-of-day: full loop works for one test clip (rough quality OK)

**Day 3 (April 23\) — Polish \+ Calibration**

- Whisper captions with styled burn-in  
- Sponsor animation overlay  
- Per-platform post text generation (3 prompts)  
- Calibration session with Jordy (5-8 test clips, rate \+ tune)  
- End-of-day: clip quality passes Jordy's taste check

**Day 4 (April 24\) — Stabilization \+ Dashboard**

- Error handling \+ degradation paths (every API failure mode)  
- Reminder \+ auto-expire logic  
- Dashboard endpoint (simple HTML)  
- Heartbeat config defined  
- **GATE:** Jordy must provide (a) approver WhatsApp \+ timezone, (b) mod whitelist usernames. System cannot go live without these.  
- End-of-day: go/no-go for deployment

**Day 5 (April 25\) — Dry-Run \+ Deployment**

- Deploy to production  
- Dry-run on a private Twitch test stream with Jordy \+ a test mod  
- Run 3-5 real `!clip` triggers end-to-end  
- Monitor dashboard, fix anything that breaks  
- Final go/no-go for April 26 livestream  
- End-of-day: system is live and approved for use

**April 26 — LIVE DEPLOYMENT** (Jordy's livestream)

---

## RISKS & KNOWN FAILURE POINTS

**Weakest link:** The 30-minute SLA itself. Every step has to be fast enough that the compounded latency fits. Specific watchpoints:

1. **Claude Vision latency on video analysis** — unknown until tested. If single-frame extraction \+ analysis takes too long, fall back to analyzing keyframes only (every 2s) rather than dense sampling. Test on Day 2\.  
2. **FFmpeg execution time on Workers** — Workers have CPU time limits. If FFmpeg pipeline exceeds them, we move editing to a dedicated VPS with KV-based job queue. Decide Day 1\.  
3. **Human approver response time** — biggest variable. Auto-expire at 20min is the safety valve. If the approver consistently misses the window, that's a people problem, not a system problem — flag it to Jordy.  
4. **Twitch Clips API silent failure** — 15s timeout \+ retry \+ WhatsApp alert. Don't build a secondary RTMP path for V1; the cost of occasional missed clips is low.

**Pre-known constraints** (from Jordy's context):

- Worker-to-Worker HTTP calls fail on Cloudflare (error 1042). Use KV for any cross-worker signaling.  
- Claude Vision requires sequential processing with 10s delays between calls.  
- Slack formatting is `*bold*` (mrkdwn), not `**bold**` — though Slack isn't in this build's scope.

---

## V2 PARKING LOT (do NOT build in V1)

Per Jordy's system factory protocol, ideas that emerge during build go here, not into the live build:

- AI agent that watches the stream and auto-triggers `!clip` without human mod  
- AI approver trained on the approval\_log data (target: 2-3 months of logged decisions before training)  
- Subject-tracking smart crop (replaces center-crop \+ blur fill)  
- X/Twitter posting (not in V1 scope)  
- Multi-mod workflow with permission levels

---

## DELIVERABLES AT HANDOFF-BACK (for QA Layer 1\)

When Felix moves this ticket to QA, the build log must include:

- What was built (vs spec)  
- Any deviations from spec \+ reasoning  
- Known limitations  
- Test results (against each acceptance criterion, pass/fail)  
- Dashboard URL  
- Dry-run recording from Day 5  
- Calibration session notes from Day 3  
- Heartbeat config

---

## OPEN TBDs FOR JORDY (Day-4 blockers)

These must be filled before April 25 go-live:

1. **Approver WhatsApp number \+ timezone** — identity of trusted human on standby  
2. **Mod whitelist** — Twitch usernames allowed to fire `!clip` (start with 2-3)

Felix: build everything else. These two slots are config values, swap-in-able at deploy time.

---

## FELIX KNOWLEDGE BASE — RELEVANT PATTERNS FROM PRIOR BUILDS

Reference these from the HR Monitoring System build:

- **KV-based decoupling pattern** — Never call Worker → Worker via HTTP (Cloudflare 1042). Use KV as the message bus.  
- **Claude Vision sequential processing** — Never parallel. 10s delay between calls.  
- **Slack `mrkdwn` formatting** — Single asterisks for bold (not applicable here but good to remember).  
- **Worker URL pattern** — `{worker-name}.missioncontrol5mof.workers.dev`  
- **Cron deployment timing gap** — Recently surfaced issue with KV data not accumulating. Be mindful of cron schedules vs deployment windows.

---

**End of handoff doc.**  
