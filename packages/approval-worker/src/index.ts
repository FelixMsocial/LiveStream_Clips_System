// Approval Worker:
//   - Queue consumer on APPROVAL_SEND → builds Telegram message with buttons.
//   - Telegram webhook → handles callback_query (approve/reject) + text replies.
//   - Cron → 10m reminder + 20m auto-expire.
//   - HTTP API (`/api/...`) → used by GPU worker and dashboard.
//
// We use one Worker for these because (a) they all share D1/KV/Telegram state and
// (b) Telegram webhooks are one endpoint anyway.

import {
  clipsDb,
  TelegramClient,
  sendEmail,
  type ApprovalSendJob,
  type CaptionsTriple,
  type ClipRow,
  type PostDispatchJob,
} from "@clipfactory/shared";
import type { Env } from "./env.js";
import { signJwt, verifyJwt } from "./jwt.js";

const TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token";
const PAGES_PROD_ORIGIN = "https://clipfactory.pages.dev";
const APPROVAL_WINDOW_SECONDS = 20 * 60;
const HEARTBEAT_DEAD_MS = 180_000; // 3 missed 60s beats — tolerates one transient failure

export default {
  async queue(batch: MessageBatch<ApprovalSendJob>, env: Env): Promise<void> {
    for (const msg of batch.messages) {
      try {
        await sendApprovalRequest(msg.body.clip_id, env);
        msg.ack();
      } catch (err) {
        console.error("approval send error", msg.body.clip_id, err);
        await clipsDb.appendErrorLog(
          env.CLIP_DB,
          msg.body.clip_id,
          "approval_send",
          String(err),
        );
        msg.retry({ delaySeconds: 15 });
      }
    }
  },

  async scheduled(_ev: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(sweepPending(env));
    ctx.waitUntil(checkHeartbeats(env));
  },

  async fetch(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(req.url);

    if (req.method === "OPTIONS" && url.pathname.startsWith("/api/")) {
      return new Response(null, {
        status: 204,
        headers: buildCorsHeaders(req.headers.get("origin"), env),
      });
    }

    if (url.pathname === "/healthz") return Response.json({ ok: true });

    // Telegram webhook.
    if (url.pathname === "/telegram/webhook" && req.method === "POST") {
      const supplied = req.headers.get(TELEGRAM_SECRET_HEADER);
      if (env.TELEGRAM_WEBHOOK_SECRET && supplied !== env.TELEGRAM_WEBHOOK_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
      const update = await req.json<TelegramUpdate>();
      ctx.waitUntil(handleTelegramUpdate(update, env));
      return new Response("ok");
    }

    // n8n callback: { clip_id, post_urls: {...} }
    if (url.pathname === "/webhook/clip-posted" && req.method === "POST") {
      // Simple shared-secret gate.
      const auth = req.headers.get("authorization") ?? "";
      if (!auth || auth !== `Bearer ${env.GPU_INTERNAL_SECRET}`) {
        return new Response("forbidden", { status: 403 });
      }
      const body = await req.json<{ clip_id: string; post_urls: Record<string, string> }>();
      await clipsDb.setStatus(env.CLIP_DB, body.clip_id, "posted", {
        post_urls: JSON.stringify(body.post_urls ?? {}),
        posted_at: new Date().toISOString(),
      });
      await clipsDb.appendApprovalLog(env.CLIP_DB, body.clip_id, "posted", "n8n", {
        post_urls: body.post_urls,
      });
      return Response.json({ ok: true });
    }

    // Internal APIs (GPU worker, dashboard).
    if (url.pathname.startsWith("/api/")) {
      const res = await handleApi(req, url, env);
      return withCors(req, res, env);
    }

    return new Response("clip-approval", { status: 200 });
  },
};

// -------------------- Telegram + email fallback -------------------- //

/**
 * Send a Telegram message; on failure, fall back to email via Resend.
 * Returns `{ ok, channel }` where channel is "telegram" | "email" | "none".
 */
async function sendTelegramWithFallback(
  tg: TelegramClient,
  tgCall: () => Promise<unknown>,
  env: Env,
  emailSubject: string,
  emailHtml: string,
): Promise<{ ok: boolean; channel: "telegram" | "email" | "none" }> {
  try {
    await tgCall();
    return { ok: true, channel: "telegram" };
  } catch (tgErr) {
    console.error("telegram send failed, attempting email fallback:", tgErr);
    if (env.RESEND_API_KEY && env.ALERT_EMAIL_TO) {
      const result = await sendEmail(env.RESEND_API_KEY, env.ALERT_EMAIL_TO, emailSubject, emailHtml);
      if (result.ok) {
        return { ok: true, channel: "email" };
      }
      console.error("email fallback also failed:", result.error);
    }
    return { ok: false, channel: "none" };
  }
}

// -------------------- APPROVAL_SEND → Telegram -------------------- //

async function sendApprovalRequest(clipId: string, env: Env): Promise<void> {
  const clip = await clipsDb.getClip(env.CLIP_DB, clipId);
  if (!clip) throw new Error(`clip ${clipId} not found`);
  if (!clip.final_clip_r2_key) throw new Error(`clip ${clipId} has no final_clip_r2_key`);

  const tg = new TelegramClient(env.TELEGRAM_BOT_TOKEN);
  const token = await signJwt(
    env.DASHBOARD_JWT_SECRET,
    { clip_id: clipId, purpose: "review" },
    APPROVAL_WINDOW_SECONDS,
  );
  const reviewUrl = `${env.DASHBOARD_URL.replace(/\/$/, "")}/review?id=${clipId}&t=${token}`;

  const keyboard = {
    inline_keyboard: [
      [
        { text: "✅ Approve", callback_data: `approve:${clipId}` },
        { text: "❌ Reject", callback_data: `reject:${clipId}` },
      ],
      [{ text: "✏️ Edit on dashboard", url: reviewUrl }],
    ],
  };

  const caption = buildTelegramCaption(clip);
  let messageId: number | undefined;
  const videoUrl = await buildFinalClipUrl(env, clip.final_clip_r2_key);

  const emailSubject = `ClipFactory — clip ${clipId} pending approval`;
  const emailHtml = `<p>${caption.replace(/\n/g, "<br>")}</p><p><a href="${videoUrl}">Watch clip</a></p><p><a href="${reviewUrl}">Review on dashboard</a></p>`;

  const result = await sendTelegramWithFallback(
    tg,
    async () => {
      const res = await tg.sendMessage({
        chat_id: env.TELEGRAM_APPROVER_CHAT_ID,
        text: `${caption}\n\n<a href="${videoUrl}">Watch clip</a>`,
        parse_mode: "HTML",
        reply_markup: keyboard,
        disable_web_page_preview: true,
      });
      messageId = res.message_id;
    },
    env,
    emailSubject,
    emailHtml,
  );
  if (!result.ok) throw new Error("approval send failed via all channels");

  await clipsDb.setStatus(env.CLIP_DB, clipId, "pending_approval", {
    telegram_message_id: messageId ?? null,
    sent_at: new Date().toISOString(),
  });
  await clipsDb.appendApprovalLog(env.CLIP_DB, clipId, "sent", "approval-worker", {
    message_id: messageId ?? null,
  });
}

function buildTelegramCaption(clip: ClipRow): string {
  const triggeredAt = clip.triggered_at
    ? new Date(clip.triggered_at).toISOString().slice(11, 16) + " UTC"
    : null;
  const duration =
    clip.trim_start_sec != null && clip.trim_end_sec != null
      ? `${Math.round(clip.trim_end_sec - clip.trim_start_sec)}s`
      : clip.duration_sec
        ? `${Math.round(clip.duration_sec)}s`
        : null;

  const lowFlag = clip.low_potential_flag ? `⚠️ <b>LOW CLIP POTENTIAL</b> — review carefully\n\n` : "";

  const header = [
    `🎬 <b>ClipFactory</b> — new clip ready for review`,
    `• triggered by: @${clip.triggered_by}${triggeredAt ? ` at ${triggeredAt}` : ""}`,
    duration ? `• duration: ${duration}` : "",
    clip.label ? `• label: ${clip.label}` : "",
  ].filter(Boolean).join("\n");

  const scoreParts: string[] = [];
  if (clip.substance_score != null) {
    scoreParts.push(`Substance: <b>${clip.substance_score}</b>/100`);
  }
  if (clip.hook_score != null) {
    const iter = clip.hook_iterations ? ` (iter ${clip.hook_iterations})` : "";
    scoreParts.push(`Hook: <b>${clip.hook_score}</b>/100${iter}`);
  }
  const scoreLine = scoreParts.length ? `📊 ${scoreParts.join("  •  ")}` : "";

  const hookLine = clip.hook_overlay_text
    ? `🎯 <b>On-video hook:</b> ${escapeHtml(trunc(clip.hook_overlay_text, 120))}`
    : "";

  const posts = [
    `📱 <b>Suggested posts:</b>`,
    ``,
    `<b>Instagram:</b> ${trunc(clip.instagram_post_text, 200)}`,
    `<b>YouTube:</b> ${trunc(clip.youtube_post_text, 200)}`,
    `<b>TikTok:</b> ${trunc(clip.tiktok_post_text, 200)}`,
  ].join("\n");

  const middle = [scoreLine, hookLine].filter(Boolean).join("\n");
  const middleBlock = middle ? `\n\n${middle}` : "";

  return `${lowFlag}${header}${middleBlock}\n\n${posts}\n\n⏱ Auto-expires in 20 min.`;
}


function trunc(s: string | null, n: number): string {
  if (!s) return "—";
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function clipLabelOrIgHook(clip: {
  id: string;
  label?: string | null;
  instagram_post_text?: string | null;
}): string {
  const normalizedLabel = (clip.label ?? "").replace(/\s+/g, " ").trim();
  if (normalizedLabel) return trunc(normalizedLabel, 80);

  const igFirstLine = (clip.instagram_post_text ?? "").split(/\r?\n/, 1)[0] ?? "";
  const normalizedHook = igFirstLine.replace(/\s+/g, " ").trim();
  if (normalizedHook) return trunc(normalizedHook, 80);

  return `clip ${clip.id}`;
}

async function buildFinalClipUrl(env: Env, key: string): Promise<string> {
  // Prefer a public R2 base if configured. Otherwise use a presigned URL.
  if (env.R2_PUBLIC_BASE) {
    return `${env.R2_PUBLIC_BASE.replace(/\/$/, "")}/${key}`;
  }
  if (
    env.R2_ACCOUNT_ID &&
    env.R2_ACCESS_KEY_ID &&
    env.R2_SECRET_ACCESS_KEY &&
    env.R2_BUCKET_NAME
  ) {
    const { signR2GetUrl } = await import("@clipfactory/shared/r2");
    return signR2GetUrl(
      {
        accountId: env.R2_ACCOUNT_ID,
        accessKeyId: env.R2_ACCESS_KEY_ID,
        secretAccessKey: env.R2_SECRET_ACCESS_KEY,
        bucket: env.R2_BUCKET_NAME,
      },
      key,
      3600,
    );
  }
  throw new Error("no R2 public base or S3-compat credentials configured");
}

// -------------------- Telegram inbound -------------------- //

interface TelegramUpdate {
  callback_query?: {
    id: string;
    from: { id: number; username?: string };
    message: { message_id: number; chat: { id: number } };
    data: string;
  };
  message?: {
    message_id: number;
    chat: { id: number };
    from: { id: number; username?: string };
    reply_to_message?: { message_id: number };
    text?: string;
  };
}

async function handleTelegramUpdate(update: TelegramUpdate, env: Env): Promise<void> {
  const tg = new TelegramClient(env.TELEGRAM_BOT_TOKEN);

  if (update.callback_query) {
    const cq = update.callback_query;
    // Always answer the callback query — Telegram times out after 10s if we don't.
    try {
      const userId = String(cq.from.id);
      // Authorize: approvers table, personal DM match, or member of the approver chat.
      if (!(await isApproverAuthorized(env, userId, cq.message.chat.id))) {
        await tg.answerCallbackQuery({
          callback_query_id: cq.id,
          text: "Not authorized.",
          show_alert: true,
        });
        return;
      }
      // callback_data is "action:clipId" — clipId may contain dashes but no colons.
      const colonIdx = cq.data.indexOf(":");
      const action = colonIdx === -1 ? cq.data : cq.data.slice(0, colonIdx);
      const clipId = colonIdx === -1 ? "" : cq.data.slice(colonIdx + 1);
      if (!action || !clipId) {
        await tg.answerCallbackQuery({ callback_query_id: cq.id, text: "Bad payload" });
        return;
      }

      if (action === "approve") {
        const ok = await approveClip(clipId, userId, env, tg, cq.message.chat.id, cq.message.message_id);
        await tg.answerCallbackQuery({
          callback_query_id: cq.id,
          text: ok ? "Approved." : "Clip already resolved.",
        });
      } else if (action === "reject") {
        const ok = await rejectClip(clipId, userId, null, env, tg, cq.message.chat.id, cq.message.message_id);
        await tg.answerCallbackQuery({
          callback_query_id: cq.id,
          text: ok ? "Rejected." : "Clip already resolved.",
        });
      } else {
        await tg.answerCallbackQuery({
          callback_query_id: cq.id,
          text: `Unknown action: ${action}`,
        });
      }
    } catch (err) {
      console.error("callback_query handling error", err);
      try {
        await tg.answerCallbackQuery({
          callback_query_id: cq.id,
          text: "Error processing request. Please try again.",
          show_alert: true,
        });
      } catch {
        // best-effort; Telegram may have already timed out the query
      }
    }
  }
}

// -------------------- approve / reject -------------------- //

async function approveClip(
  clipId: string,
  actor: string,
  env: Env,
  tg: TelegramClient,
  chatId: number,
  messageId: number,
): Promise<boolean> {
  const clip = await clipsDb.getClip(env.CLIP_DB, clipId);
  if (!clip || clip.status !== "pending_approval") return false;
  await clipsDb.setStatus(env.CLIP_DB, clipId, "approved", {
    approver_decision: "approved",
    approved_at: new Date().toISOString(),
  });
  await clipsDb.appendApprovalLog(env.CLIP_DB, clipId, "approved", actor, null);
  const job: PostDispatchJob = { clip_id: clipId, approved_by: actor };
  await env.POST_DISPATCH.send(job);
  await tg
    .editMessageReplyMarkup({ chat_id: chatId, message_id: messageId, reply_markup: { inline_keyboard: [] } })
    .catch(() => {});
  return true;
}

async function rejectClip(
  clipId: string,
  actor: string,
  reason: string | null,
  env: Env,
  tg: TelegramClient,
  chatId: number,
  messageId: number,
): Promise<boolean> {
  const clip = await clipsDb.getClip(env.CLIP_DB, clipId);
  if (!clip || clip.status !== "pending_approval") return false;
  await clipsDb.setStatus(env.CLIP_DB, clipId, "rejected", {
    approver_decision: "rejected",
    approver_reason: reason,
  });
  await clipsDb.appendApprovalLog(env.CLIP_DB, clipId, "rejected", actor, { reason });
  await tg
    .editMessageReplyMarkup({ chat_id: chatId, message_id: messageId, reply_markup: { inline_keyboard: [] } })
    .catch(() => {});
  return true;
}

// -------------------- Cron: reminders + expiry -------------------- //

async function sweepPending(env: Env): Promise<void> {
  const tg = new TelegramClient(env.TELEGRAM_BOT_TOKEN);

  // Reminders: pending > 10m, reminder not yet sent.
  // Plain sent_at comparison (no datetime() wrapper) allows idx_clips_pending_sent index to be used.
  const reminders = await env.CLIP_DB.prepare(
    `SELECT id, sent_at, label, instagram_post_text FROM clips
     WHERE status = 'pending_approval'
       AND reminder_sent = 0
       AND sent_at IS NOT NULL
       AND sent_at < strftime('%Y-%m-%dT%H:%M:%S', 'now', '-10 minutes')
       AND sent_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-20 minutes')`,
  ).all<{ id: string; sent_at: string; label: string | null; instagram_post_text: string | null }>();
  // Send Telegram notifications first, then batch all D1 writes together
  const reminderSuccessIds: Array<{ id: string; channel: string }> = [];
  for (const r of reminders.results ?? []) {
    try {
      const token = await signJwt(
        env.DASHBOARD_JWT_SECRET,
        { clip_id: r.id, purpose: "review" },
        APPROVAL_WINDOW_SECONDS,
      );
      const reviewUrl = `${env.DASHBOARD_URL.replace(/\/$/, "")}/review?id=${r.id}&t=${token}`;
      const title = escapeHtml(clipLabelOrIgHook(r));
      const reminderText = `⏰ Reminder: Clip "<b>${title}</b>" awaiting approval. Auto-expires in 10 min.\n<a href="${escapeHtml(reviewUrl)}">Open dashboard review</a>`;
      const result = await sendTelegramWithFallback(
        tg,
        () => tg.sendMessage({
          chat_id: env.TELEGRAM_APPROVER_CHAT_ID,
          text: reminderText,
          parse_mode: "HTML",
          disable_web_page_preview: true,
        }),
        env,
        `ClipFactory reminder — clip ${r.id}`,
        `<p>⏰ Reminder: Clip "<strong>${title}</strong>" awaiting approval. Auto-expires in 10 min.</p><p><a href="${escapeHtml(reviewUrl)}">Open dashboard review</a></p>`,
      );
      if (result.ok) {
        reminderSuccessIds.push({ id: r.id, channel: result.channel });
      } else {
        console.error("reminder delivery failed via all channels for clip", r.id);
      }
    } catch (e) {
      console.error("reminder send failed", r.id, e);
    }
  }
  // Batch all reminder D1 writes in a single round-trip
  if (reminderSuccessIds.length > 0) {
    const stmts: D1PreparedStatement[] = [];
    for (const { id, channel } of reminderSuccessIds) {
      stmts.push(
        env.CLIP_DB.prepare(
          `UPDATE clips SET reminder_sent = 1, updated_at = datetime('now') WHERE id = ?1`,
        ).bind(id),
      );
      stmts.push(
        env.CLIP_DB.prepare(
          `INSERT INTO approval_log (clip_id, event_type, actor, details, event_at) VALUES (?1, 'reminder', 'cron', ?2, datetime('now'))`,
        ).bind(id, JSON.stringify({ channel })),
      );
    }
    await env.CLIP_DB.batch(stmts);
  }

  // Expiries: pending > 20m.
  // Plain sent_at comparison allows idx_clips_pending_sent index to be used.
  const expired = await env.CLIP_DB.prepare(
    `SELECT id, label, instagram_post_text FROM clips
     WHERE status = 'pending_approval'
       AND sent_at IS NOT NULL
       AND sent_at < strftime('%Y-%m-%dT%H:%M:%S', 'now', '-20 minutes')`,
  ).all<{ id: string; label: string | null; instagram_post_text: string | null }>();
  // Batch all expiry D1 writes, then send notifications
  if ((expired.results ?? []).length > 0) {
    const expiryStmts: D1PreparedStatement[] = [];
    for (const r of expired.results ?? []) {
      expiryStmts.push(
        env.CLIP_DB.prepare(
          `UPDATE clips
             SET status = 'expired',
                 approver_decision = 'expired',
                 updated_at = datetime('now')
           WHERE id = ?1`,
        ).bind(r.id),
      );
      expiryStmts.push(
        env.CLIP_DB.prepare(
          `INSERT INTO approval_log (clip_id, event_type, actor, details, event_at) VALUES (?1, 'expired', 'cron', NULL, datetime('now'))`,
        ).bind(r.id),
      );
    }
    await env.CLIP_DB.batch(expiryStmts);
  }
  // Send expiry notifications after DB writes (non-critical, best-effort)
  for (const r of expired.results ?? []) {
    try {
      const title = escapeHtml(clipLabelOrIgHook(r));
      const expireText = `⚠️ Clip "<b>${title}</b>" expired with no approver decision.`;
      await sendTelegramWithFallback(
        tg,
        () => tg.sendMessage({
          chat_id: env.TELEGRAM_JORDY_CHAT_ID,
          text: expireText,
          parse_mode: "HTML",
          disable_web_page_preview: true,
        }),
        env,
        `ClipFactory — clip ${r.id} expired`,
        `<p>⚠️ Clip "<strong>${title}</strong>" expired with no approver decision.</p>`,
      );
    } catch (e) {
      console.error("expire escalate failed", r.id, e);
    }
  }
}

// -------------------- Heartbeat dead-man checks -------------------- //

async function checkHeartbeats(env: Env): Promise<void> {
  const tg = new TelegramClient(env.TELEGRAM_BOT_TOKEN);
  const checks = [
    { workerId: "gpu",      alertKey: "alert:gpu:sent",      label: "GPU worker" },
    { workerId: "listener", alertKey: "alert:listener:sent", label: "Twitch listener" },
  ];

  const rows = await env.CLIP_DB.prepare(
    `SELECT worker_id, last_seen_ts FROM worker_heartbeats
     WHERE worker_id IN ('gpu', 'listener')`,
  ).all<{ worker_id: string; last_seen_ts: number }>();
  const lastSeen = new Map<string, number>();
  for (const r of rows.results ?? []) lastSeen.set(r.worker_id, r.last_seen_ts);

  const now = Date.now();
  for (const { workerId, alertKey, label } of checks) {
    const ts = lastSeen.get(workerId);
    const alive = ts !== undefined && (now - ts) <= HEARTBEAT_DEAD_MS;

    if (alive) {
      const alertWasSent = await env.CLIP_KV.get(alertKey);
      if (alertWasSent) await env.CLIP_KV.delete(alertKey);
      continue;
    }
    const alreadySent = await env.CLIP_KV.get(alertKey);
    if (alreadySent) continue; // Suppress duplicate alerts for 30 minutes.
    const alertText = `🔴 ${label} has been unresponsive for 3+ minutes.`;
    try {
      const result = await sendTelegramWithFallback(
        tg,
        () => tg.sendMessage({ chat_id: env.TELEGRAM_JORDY_CHAT_ID, text: alertText }),
        env,
        `ClipFactory — ${label} unresponsive`,
        `<p>${alertText}</p>`,
      );
      if (result.ok) {
        await env.CLIP_KV.put(alertKey, String(now), { expirationTtl: 1800 }); // 30 min suppression
      } else {
        console.error(`heartbeat alert delivery failed for ${label} via all channels`);
      }
    } catch (e) {
      console.error(`heartbeat alert failed for ${label}`, e);
    }
  }
}

// -------------------- HTTP API -------------------- //

async function handleApi(req: Request, url: URL, env: Env): Promise<Response> {
  // GPU worker heartbeat.
  if (url.pathname === "/api/gpu/heartbeat" && req.method === "POST") {
    if (!checkInternal(req, env)) return forbidden();
    const body = await req
      .json<{ worker_id?: string }>()
      .catch(() => ({}) as { worker_id?: string });
    const meta = JSON.stringify({ worker_id: body.worker_id ?? "unknown" });
    await env.CLIP_DB.prepare(
      `INSERT INTO worker_heartbeats (worker_id, last_seen_ts, meta, updated_at)
       VALUES ('gpu', ?1, ?2, datetime('now'))
       ON CONFLICT(worker_id) DO UPDATE SET
         last_seen_ts = excluded.last_seen_ts,
         meta         = excluded.meta,
         updated_at   = datetime('now')`,
    ).bind(Date.now(), meta).run();
    return Response.json({ ok: true });
  }

  // Internal alert endpoint — called by capture-worker (and others) to send alerts.
  if (url.pathname === "/api/internal/alert" && req.method === "POST") {
    if (!checkInternal(req, env)) return forbidden();
    const body = await req.json<{ clip_id?: string; alert_type: string; message: string }>();

    // Dedup: if clip_id is provided, suppress duplicate alerts for the same clip+type (5 min).
    if (body.clip_id) {
      const dedupKey = `alert:dedup:${body.clip_id}:${body.alert_type}`;
      const existing = await env.CLIP_KV.get(dedupKey);
      if (existing) {
        return Response.json({ ok: true, channel: "dedup", deduplicated: true });
      }
      await env.CLIP_KV.put(dedupKey, String(Date.now()), { expirationTtl: 300 }); // 5 min
    }

    const tg = new TelegramClient(env.TELEGRAM_BOT_TOKEN);
    const alertText = `🚨 ${body.alert_type}: ${body.message}`;
    const result = await sendTelegramWithFallback(
      tg,
      () => tg.sendMessage({ chat_id: env.TELEGRAM_JORDY_CHAT_ID, text: alertText }),
      env,
      `ClipFactory alert — ${body.alert_type}`,
      `<p><strong>${body.alert_type}</strong></p><p>${body.message.replace(/\n/g, "<br>")}</p>`,
    );
    if (body.clip_id) {
      await clipsDb.appendApprovalLog(env.CLIP_DB, body.clip_id, "alert", "system", {
        alert_type: body.alert_type,
        channel: result.channel,
      });
    }
    return Response.json({ ok: result.ok, channel: result.channel });
  }

  // GPU worker → fetch latest prompts from D1.
  if (url.pathname === "/api/internal/prompts" && req.method === "GET") {
    if (!checkInternal(req, env)) return forbidden();
    const rows = await env.CLIP_DB.prepare(
      `SELECT p.key, p.body FROM prompts p
       INNER JOIN (SELECT key, MAX(version) AS max_v FROM prompts GROUP BY key) latest
       ON p.key = latest.key AND p.version = latest.max_v`,
    ).all<{ key: string; body: string }>();
    const prompts: Record<string, string> = {};
    for (const r of rows.results ?? []) prompts[r.key] = r.body;
    return Response.json({ prompts });
  }

  // GPU worker → patch clip row.
  const patchMatch = /^\/api\/internal\/clips\/([^/]+)$/.exec(url.pathname);
  if (patchMatch && req.method === "PATCH") {
    if (!checkInternal(req, env)) return forbidden();
    const clipId = patchMatch[1]!;
    const patch = await req.json<Partial<Record<string, string | number | null>>>();
    const allowed = new Set([
      "status",
      "vision_analysis",
      "transcript_srt",
      "final_clip_r2_key",
      "instagram_post_text",
      "youtube_post_text",
      "tiktok_post_text",
      "gpu_timings_ms",
      "duration_sec",
      // v1.1 substance + hook scoring
      "substance_score",
      "substance_score_json",
      "low_potential_flag",
      "peak_timestamp_sec",
      "trim_start_sec",
      "trim_end_sec",
      "hook_overlay_text",
      "hook_score",
      "hook_score_json",
      "hook_iterations",
      "caption_scores_json",
    ]);
    const entries = Object.entries(patch).filter(([k]) => allowed.has(k));
    if (entries.length === 0) return Response.json({ ok: true });
    const fields = entries.map(([k], i) => `${k} = ?${i + 1}`);
    fields.push(`updated_at = datetime('now')`);
    const bindings = entries.map(([, v]) => v as string | number | null);
    bindings.push(clipId);
    await env.CLIP_DB.prepare(
      `UPDATE clips SET ${fields.join(", ")} WHERE id = ?${bindings.length}`,
    )
      .bind(...bindings)
      .run();
    return Response.json({ ok: true });
  }

  // GPU worker → enqueue APPROVAL_SEND (triggered when pipeline finishes).
  if (url.pathname === "/api/internal/approval-send" && req.method === "POST") {
    if (!checkInternal(req, env)) return forbidden();
    const body = await req.json<{ clip_id: string }>();
    // We'd normally enqueue through a Queue binding, but approval-worker is the
    // consumer and cannot enqueue to itself. Instead, send the Telegram message directly.
    await sendApprovalRequest(body.clip_id, env);
    return Response.json({ ok: true });
  }

  // Dashboard: GET /api/clips/:id (token-validated)
  const clipMatch = /^\/api\/clips\/([^/]+)$/.exec(url.pathname);
  if (clipMatch && req.method === "GET") {
    const clipId = clipMatch[1]!;
    const token = url.searchParams.get("t") ?? "";
    const ok = await verifyJwt<{ clip_id: string; purpose?: string }>(env.DASHBOARD_JWT_SECRET, token);
    if (!ok || ok.clip_id !== clipId) return forbidden();
    const row = await getClipForActiveReview(env, clipId);
    if (row === "expired") return new Response("review window expired", { status: 410 });
    if (!row) return new Response("not found", { status: 404 });
    const videoUrl = row.final_clip_r2_key
      ? `/api/clips/${clipId}/video?t=${encodeURIComponent(token)}`
      : null;
    return Response.json({ clip: row, video_url: videoUrl });
  }

  // Dashboard: GET /api/clips/:id/video?t=... (proxied video for reliable playback)
  const clipVideoMatch = /^\/api\/clips\/([^/]+)\/video$/.exec(url.pathname);
  if (clipVideoMatch && req.method === "GET") {
    const clipId = clipVideoMatch[1]!;
    const token = url.searchParams.get("t") ?? "";
    const ok = await verifyJwt<{ clip_id: string; purpose?: string }>(env.DASHBOARD_JWT_SECRET, token);
    if (!ok || ok.clip_id !== clipId) return forbidden();
    const row = await getClipForActiveReview(env, clipId);
    if (row === "expired") return new Response("review window expired", { status: 410 });
    if (!row) return new Response("not found", { status: 404 });
    if (!row.final_clip_r2_key) return new Response("clip video unavailable", { status: 404 });
    const obj = await env.CLIP_BUCKET.get(row.final_clip_r2_key);
    if (!obj) return new Response("clip video unavailable", { status: 404 });

    const headers = new Headers();
    obj.writeHttpMetadata(headers);
    headers.set("etag", obj.httpEtag);
    if (!headers.has("content-type")) headers.set("content-type", "video/mp4");
    headers.set("cache-control", "private, max-age=60");
    return new Response(obj.body, { headers });
  }

  // Dashboard: POST /api/clips/:id/decision  { action, reason?, edits? }
  const decisionMatch = /^\/api\/clips\/([^/]+)\/decision$/.exec(url.pathname);
  if (decisionMatch && req.method === "POST") {
    const clipId = decisionMatch[1]!;
    const token = url.searchParams.get("t") ?? "";
    const ok = await verifyJwt<{ clip_id: string; purpose?: string }>(env.DASHBOARD_JWT_SECRET, token);
    if (!ok || ok.clip_id !== clipId) return forbidden();
    const body = await req.json<{
      action: "approve" | "reject" | "save";
      reason?: string;
      edits?: Partial<CaptionsTriple>;
    }>();
    const actor = "dashboard";
    const tg = new TelegramClient(env.TELEGRAM_BOT_TOKEN);
    const clip = await getClipForActiveReview(env, clipId);
    if (clip === "expired") return new Response("review window expired", { status: 410 });
    if (!clip) return new Response("not found", { status: 404 });

    if (body.edits) {
      const patch: Record<string, string | null> = {};
      if (typeof body.edits.instagram === "string") patch.instagram_post_text = body.edits.instagram;
      if (typeof body.edits.youtube === "string") patch.youtube_post_text = body.edits.youtube;
      if (typeof body.edits.tiktok === "string") patch.tiktok_post_text = body.edits.tiktok;
      patch.approver_edits = JSON.stringify(body.edits);
      await clipsDb.updateClip(env.CLIP_DB, clipId, patch);
      await clipsDb.appendApprovalLog(env.CLIP_DB, clipId, "edited", actor, body.edits);
    }

    const chatId = Number(env.TELEGRAM_APPROVER_CHAT_ID);
    const msgId = clip.telegram_message_id ?? 0;

    if (body.action === "approve") {
      const changed = await approveClip(clipId, actor, env, tg, chatId, msgId);
      if (!changed) return new Response("clip is no longer pending approval", { status: 409 });
    } else if (body.action === "reject") {
      const changed = await rejectClip(clipId, actor, body.reason ?? null, env, tg, chatId, msgId);
      if (!changed) return new Response("clip is no longer pending approval", { status: 409 });
    } // else "save" — edits were already applied above.
    return Response.json({ ok: true });
  }

  // Dashboard login: POST /api/auth/login { username, password } → { token }
  if (url.pathname === "/api/auth/login" && req.method === "POST") {
    const body = await req.json<{ username?: string; password?: string }>().catch(() => ({ username: undefined, password: undefined }));
    const username = (body.username ?? "").trim().toLowerCase();
    const password = body.password ?? "";
    if (!username || !password) return new Response("missing credentials", { status: 401 });
    const row = await env.CLIP_DB.prepare(
      `SELECT salt, password_hash FROM dashboard_users WHERE username = ?1`,
    ).bind(username).first<{ salt: string; password_hash: string }>();
    // Always run hash derivation to avoid timing oracle even when user not found.
    const salt = row?.salt ?? "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa=";
    const derived = await hashPbkdf2(password, salt);
    if (!row || !timingSafeEqual(derived, row.password_hash)) {
      return new Response("invalid credentials", { status: 401 });
    }
    const token = await signJwt(env.DASHBOARD_JWT_SECRET, { purpose: "dashboard" }, 86400); // 24h
    return Response.json({ token });
  }

  // Internal: POST /api/internal/setup-dashboard-user { username, password } — create/reset a user
  if (url.pathname === "/api/internal/setup-dashboard-user" && req.method === "POST") {
    if (!checkInternal(req, env)) return forbidden();
    const body = await req.json<{ username?: string; password?: string }>().catch(() => ({ username: undefined, password: undefined }));
    const username = (body.username ?? "").trim().toLowerCase();
    const password = body.password ?? "";
    if (!username || password.length < 8) {
      return new Response("username required and password must be at least 8 characters", { status: 400 });
    }
    const salt = randomSalt();
    const hash = await hashPbkdf2(password, salt);
    await env.CLIP_DB.prepare(
      `INSERT INTO dashboard_users (username, salt, password_hash)
       VALUES (?1, ?2, ?3)
       ON CONFLICT(username) DO UPDATE SET salt = excluded.salt, password_hash = excluded.password_hash`,
    ).bind(username, salt, hash).run();
    return Response.json({ ok: true, username });
  }

  // Dashboard: GET /api/dashboard (JWT-protected via ?t= query param)
  if (url.pathname === "/api/dashboard" && req.method === "GET") {
    const token = url.searchParams.get("t") ?? "";
    const payload = await verifyJwt<{ purpose: string }>(env.DASHBOARD_JWT_SECRET, token);
    if (!payload || payload.purpose !== "dashboard") return forbidden();
    // Use db.batch() for a single D1 round-trip instead of Promise.all (avoids timeout)
    const batchResults = await env.CLIP_DB.batch([
      env.CLIP_DB.prepare(
        `SELECT id, status, triggered_by, triggered_at, posted_at, post_urls
         FROM clips ORDER BY triggered_at DESC LIMIT 50`,
      ),
      // Range comparison allows idx_clips_triggered_at to be used (no function wrapping)
      env.CLIP_DB.prepare(
        `SELECT COUNT(*) as today_count FROM clips
         WHERE triggered_at >= strftime('%Y-%m-%dT00:00:00', 'now')
           AND triggered_at < strftime('%Y-%m-%dT00:00:00', 'now', '+1 day')`,
      ),
      env.CLIP_DB.prepare(
        `SELECT COUNT(*) as active_count FROM clips WHERE status IN ('raw','downloaded','analyzing','editing','pending_approval','approved','ready_to_post')`,
      ),
      // Merged: status breakdown + outcome counts in a single full-table pass
      env.CLIP_DB.prepare(
        `SELECT status, COUNT(*) as count FROM clips GROUP BY status`,
      ),
      env.CLIP_DB.prepare(
        `SELECT AVG((julianday(posted_at) - julianday(triggered_at)) * 24 * 60) as avg_minutes_to_post FROM clips WHERE status = 'posted' AND posted_at IS NOT NULL`,
      ),
    ]);
    const rows = batchResults[0] as D1Result;
    const todayCount = (batchResults[1] as D1Result).results?.[0] as { today_count: number } | undefined;
    const activeCount = (batchResults[2] as D1Result).results?.[0] as { active_count: number } | undefined;
    const statusRows = (batchResults[3] as D1Result<{ status: string; count: number }>).results ?? [];
    const avgTime = (batchResults[4] as D1Result).results?.[0] as { avg_minutes_to_post: number | null } | undefined;
    // Derive outcome counts from status breakdown instead of a separate full-table scan
    const breakdown: Record<string, number> = {};
    for (const r of statusRows) breakdown[r.status] = r.count;
    const failedStatuses = ["failed_capture", "failed_edit", "failed_post"];
    const outcomes = {
      success: breakdown["posted"] ?? 0,
      failed: failedStatuses.reduce((sum, s) => sum + (breakdown[s] ?? 0), 0),
      rejected: breakdown["rejected"] ?? 0,
      expired: breakdown["expired"] ?? 0,
    };
    return Response.json({
      clips: rows.results ?? [],
      stats: {
        today_count: todayCount?.today_count ?? 0,
        active_count: activeCount?.active_count ?? 0,
        avg_minutes_to_post: avgTime?.avg_minutes_to_post ?? null,
        status_breakdown: breakdown,
        success: outcomes.success,
        failed: outcomes.failed,
        rejected: outcomes.rejected,
        expired: outcomes.expired,
      },
    });
  }

  return new Response("not found", { status: 404 });
}

function checkInternal(req: Request, env: Env): boolean {
  const auth = req.headers.get("authorization") ?? "";
  return !!env.GPU_INTERNAL_SECRET && auth === `Bearer ${env.GPU_INTERNAL_SECRET}`;
}

async function isApproverAuthorized(
  env: Env,
  telegramUserId: string,
  messageChatId?: number,
): Promise<boolean> {
  // Explicit approvers table (any individual).
  if (await clipsDb.isApprover(env.CLIP_DB, telegramUserId)) return true;
  // Personal DM: user ID equals the approver chat ID (1-on-1 bot chat).
  if (telegramUserId === String(env.TELEGRAM_APPROVER_CHAT_ID)) return true;
  // Group/channel: callback came from within the configured approver chat.
  // Anyone who can see and click the button inside that chat is authorized.
  if (messageChatId !== undefined && String(messageChatId) === String(env.TELEGRAM_APPROVER_CHAT_ID)) return true;
  return false;
}

function isReviewWindowOpen(sentAt: string | null, nowMs = Date.now()): boolean {
  if (!sentAt) return false;
  const sentMs = Date.parse(sentAt);
  if (!Number.isFinite(sentMs)) return false;
  return nowMs - sentMs <= APPROVAL_WINDOW_SECONDS * 1000;
}

async function getClipForActiveReview(
  env: Env,
  clipId: string,
): Promise<ClipRow | "expired" | null> {
  const clip = await clipsDb.getClip(env.CLIP_DB, clipId);
  if (!clip) return null;
  if (clip.status !== "pending_approval") return "expired";
  if (!isReviewWindowOpen(clip.sent_at)) return "expired";
  return clip;
}

function forbidden(): Response {
  return new Response("forbidden", { status: 403 });
}

function withCors(req: Request, res: Response, env: Env): Response {
  const cors = buildCorsHeaders(req.headers.get("origin"), env);
  let hasCors = false;
  for (const _ of cors.entries()) {
    hasCors = true;
    break;
  }
  if (!hasCors) return res;

  const headers = new Headers(res.headers);
  for (const [k, v] of cors.entries()) headers.set(k, v);
  return new Response(res.body, {
    status: res.status,
    statusText: res.statusText,
    headers,
  });
}

function buildCorsHeaders(origin: string | null, env: Env): Headers {
  const h = new Headers();
  if (!origin) return h;

  const dashboardOrigin = safeOrigin(env.DASHBOARD_URL);
  if (
    origin === dashboardOrigin ||
    origin === PAGES_PROD_ORIGIN ||
    origin.endsWith(".clipfactory.pages.dev") ||
    /^https:\/\/[a-z0-9-]+\.pages\.dev$/i.test(origin) ||
    /^http:\/\/localhost(:\d+)?$/i.test(origin) ||
    /^http:\/\/127\.0\.0\.1(:\d+)?$/i.test(origin)
  ) {
    h.set("access-control-allow-origin", origin);
    h.set("access-control-allow-methods", "GET,POST,PATCH,OPTIONS");
    h.set("access-control-allow-headers", "authorization,content-type");
    h.set("access-control-max-age", "86400");
    h.set("vary", "origin");
  }
  return h;
}

function safeOrigin(url: string): string | null {
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

// -------------------- Password helpers (PBKDF2 via Web Crypto) -------------------- //

function randomSalt(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...bytes));
}

async function hashPbkdf2(password: string, salt: string): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", enc.encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt: enc.encode(salt), iterations: 100_000 },
    key,
    256,
  );
  return btoa(String.fromCharCode(...new Uint8Array(bits)));
}

function timingSafeEqual(a: string, b: string): boolean {
  const enc = new TextEncoder();
  const ab = enc.encode(a);
  const bb = enc.encode(b);
  if (ab.length !== bb.length) return false;
  let diff = 0;
  for (let i = 0; i < ab.length; i++) diff |= ab[i]! ^ bb[i]!;
  return diff === 0;
}
