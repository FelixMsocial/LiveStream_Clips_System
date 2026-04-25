// Queue consumer. For each ClipIngestJob:
//   1. POST /helix/clips
//   2. Poll /helix/clips?id=... up to 15s + 1 retry
//   3. Stream MP4 to R2 at raw/{clip_id}.mp4
//   4. Update D1 + enqueue CLIP_EDIT

import { clipsDb, type ClipIngestJob, type ClipEditJob } from "@clipfactory/shared";
import type { Env } from "./env.js";
import {
  createClip,
  extendClipDuration,
  getClipById,
  getClipPlaybackUrl,
  mp4UrlFromThumbnail,
  PermanentTwitchError,
} from "./twitch.js";

export default {
  async queue(batch: MessageBatch<ClipIngestJob>, env: Env): Promise<void> {
    for (const msg of batch.messages) {
      try {
        await handleOne(msg.body, env);
        msg.ack();
      } catch (err) {
        console.error("capture handleOne error", msg.body.clip_id, err);
        await clipsDb.appendErrorLog(env.CLIP_DB, msg.body.clip_id, "capture", String(err));
        // Fire-and-forget alert to Jordy via approval-worker.
        fireAlert(env, {
          clip_id: msg.body.clip_id,
          alert_type: "capture_failure",
          message: `Clip capture failed for ${msg.body.clip_id}.\nTriggered by: @${msg.body.triggered_by}\nError: ${String(err)}`,
        }).catch((e) => console.error("alert fire failed", e));

        if (err instanceof PermanentTwitchError) {
          // 4xx errors won't succeed on retry — ack and mark failed.
          console.error(`permanent failure (${err.status}), not retrying`, msg.body.clip_id);
          await clipsDb.setStatus(env.CLIP_DB, msg.body.clip_id, "failed_capture");
          msg.ack();
        } else {
          // Transient error — let Queues retry up to wrangler.toml max_retries; last attempt → DLQ.
          msg.retry({ delaySeconds: 10 });
        }
      }
    }
  },
};

async function handleOne(job: ClipIngestJob, env: Env): Promise<void> {
  const { clip_id, broadcaster_id } = job;
  await clipsDb.setStatus(env.CLIP_DB, clip_id, "raw");

  const created = await createClip(
    env.TWITCH_CLIENT_ID,
    env.TWITCH_BROADCASTER_OAUTH_TOKEN,
    broadcaster_id,
  );

  // Best-effort: extend raw clip to 60 s before download. Falls back to 30 s default on failure.
  await extendClipDuration(
    env.TWITCH_CLIENT_ID,
    env.TWITCH_BROADCASTER_OAUTH_TOKEN,
    created.id,
  ).catch((err) => console.warn("extendClipDuration failed, keeping 30s default:", err));

  const helixClip = await pollForClip(env, created.id);
  if (!helixClip) {
    await clipsDb.setStatus(env.CLIP_DB, clip_id, "failed_capture", {
      twitch_clip_id: created.id,
    });
    throw new Error(`clip ${created.id} never became available`);
  }

  let mp4Url: string | null = null;
  try {
    mp4Url = await getClipPlaybackUrl(
      env.TWITCH_CLIENT_ID,
      helixClip.id,
    );
  } catch (err) {
    console.warn("clip playback URL lookup failed, falling back to thumbnail derivation", err);
  }
  if (!mp4Url) {
    mp4Url = mp4UrlFromThumbnail(helixClip.thumbnail_url);
  }

  const rawKey = `raw/${clip_id}.mp4`;
  const dl = await fetch(mp4Url);
  if (!dl.ok || !dl.body) {
    throw new Error(`raw mp4 fetch ${dl.status} ${mp4Url}`);
  }
  const contentType = dl.headers.get("content-type") ?? "";
  if (contentType.toLowerCase().startsWith("image/")) {
    throw new Error(`raw clip URL resolved to image (${contentType}): ${mp4Url}`);
  }

  await env.CLIP_BUCKET.put(rawKey, dl.body, {
    httpMetadata: { contentType: "video/mp4" },
  });

  await clipsDb.setStatus(env.CLIP_DB, clip_id, "downloaded", {
    twitch_clip_id: helixClip.id,
    raw_clip_r2_key: rawKey,
    duration_sec: helixClip.duration,
    twitch_edit_url: created.edit_url,
  });

  const editJob: ClipEditJob = {
    clip_id,
    raw_clip_r2_key: rawKey,
    stream_session_id: job.stream_session_id,
  };
  await env.CLIP_EDIT.send(editJob);
}

async function pollForClip(env: Env, twitchClipId: string) {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    const clip = await getClipById(
      env.TWITCH_CLIENT_ID,
      env.TWITCH_BROADCASTER_OAUTH_TOKEN,
      twitchClipId,
    );
    if (clip && clip.thumbnail_url) return clip;
    await sleep(1000);
  }
  // One last retry after 5s (per plan §3.2).
  await sleep(5000);
  return getClipById(
    env.TWITCH_CLIENT_ID,
    env.TWITCH_BROADCASTER_OAUTH_TOKEN,
    twitchClipId,
  );
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

async function fireAlert(
  env: Env,
  body: { clip_id: string; alert_type: string; message: string },
): Promise<void> {
  const url = `${env.APPROVAL_WORKER_URL.replace(/\/$/, "")}/api/internal/alert`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GPU_INTERNAL_SECRET}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    console.error(`alert endpoint returned ${res.status}: ${await res.text().catch(() => "")}`);
  }
}
